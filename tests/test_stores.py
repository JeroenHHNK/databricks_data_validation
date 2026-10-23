"""The three-layer contract: frozen store merge rules, the source write guard, and the
store sync (agent_streamlit_prompt.md §3.3, §6 and the §9 acceptance checklist)."""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_validation import data_io, stores, validation_store as vs  # noqa: E402
from review_app.geo import rd_to_wgs84  # noqa: E402


def store_df(rows):
    return vs.ensure_columns(pd.DataFrame(rows, columns=vs.COLUMNS))


def raw_df(rows):
    return pd.DataFrame(rows, columns=["Time", "head"])


# --- merge_new_raw: the five cases ---------------------------------------------
def test_merge_five_cases():
    store = store_df([
        ("2024-01-01 00:00", 1.0, 1, 0, "ok"),      # reviewed      -> keeps value + verdict
        ("2024-01-01 01:00", 2.0, 0, 0, ""),        # unreviewed    -> takes incoming value
        ("2024-01-01 02:00", 3.0, 1, 1, "spike"),   # rejected      -> keeps value + verdict
        ("2024-01-01 03:00", 4.0, 0, 0, ""),        # store-only    -> kept as is
    ])
    raw = raw_df([
        ("2024-01-01 00:00", 9.0), ("2024-01-01 01:00", 9.1),
        ("2024-01-01 02:00", 9.2), ("2024-01-01 04:00", 5.0),   # new timestamp -> appended
    ])
    m = vs.merge_new_raw(store, raw).set_index("timestamp")
    t = pd.Timestamp
    assert (m.at[t("2024-01-01 00:00"), "raw_data"], m.at[t("2024-01-01 00:00"), "note"]) == (1.0, "ok")
    assert m.at[t("2024-01-01 01:00"), "raw_data"] == 9.1
    assert m.loc[t("2024-01-01 02:00"), ["raw_data", "gecontroleerd", "afgekeurd"]].tolist() == [3.0, 1, 1]
    assert m.at[t("2024-01-01 03:00"), "raw_data"] == 4.0
    assert m.loc[t("2024-01-01 04:00"), ["raw_data", "gecontroleerd", "afgekeurd", "note"]].tolist() == [5.0, 0, 0, ""]


def test_ensure_columns_accepts_legacy_shapes():
    legacy = pd.DataFrame({"Time": ["2024-01-01 01:00", "bad", "2024-01-01 00:00"], "head": [1, 2, 3],
                           "gecontroleerd": [2, 0, 1]})
    out = vs.ensure_columns(legacy)
    assert list(out.columns) == vs.COLUMNS
    assert len(out) == 2 and out["timestamp"].is_monotonic_increasing      # bad row dropped, sorted
    assert out["gecontroleerd"].max() == 1 and out["note"].tolist() == ["", ""]  # clipped, filled


def test_normalize_raw_averages_duplicates():
    out = vs.normalize_raw(raw_df([("2024-01-01 00:10", 1.0), ("2024-01-01 00:20", 3.0)]))
    assert len(out) == 1 and out["raw_data"].iloc[0] == 2.0


def test_save_is_atomic_and_round_trips(tmp_path):
    path = tmp_path / "s.csv"
    df = store_df([("2024-01-01 00:00", 1.0, 1, 0, "ok")])
    vs.save(path, df)
    assert list(tmp_path.iterdir()) == [path]          # no temp file left behind
    assert vs.load(path).equals(df)
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")   # utf-8-sig


# --- layer separation + sync -----------------------------------------------------
@pytest.fixture
def repo(tmp_path):
    """A miniature repo: one source series + metadata, empty store dir."""
    src = tmp_path / "input_output" / "output_databricks"
    (src / "time_series_data").mkdir(parents=True)
    (src / "metadata").mkdir()
    ts = pd.date_range("2024-01-01", periods=6, freq="1h")
    pd.DataFrame({"Time": ts, "head": [-3.0, -3.0, -4.02, -3.0, -3.0, -3.0]}).to_csv(
        src / "time_series_data" / "W1.csv", index=False)
    pd.DataFrame([{"filename": "W1", "meetreeks_id": "abc", "vendor": "fugro", "begin_filter": -3.10,
                   "eind_filter": -4.10, "bovenkant_peilbuis": 1.0, "n_rows": 6}]).to_csv(
        src / "metadata" / "object_data.csv", index=False)
    s = data_io.load_settings()
    for key in ("databricks_output_dir", "time_series_dir", "metadata_file", "validated_dir",
                "validation_summary_file", "repository_output_dir"):
        s[key] = str(tmp_path / data_io.DEFAULTS[key])
    return s, tmp_path


def test_writes_into_source_copy_are_refused(repo):
    s, root = repo
    with pytest.raises(data_io.SourceWriteError):
        data_io.assert_writable(s, root / "input_output" / "output_databricks" / "time_series_data" / "W1.csv")
    with pytest.raises(data_io.SourceWriteError):
        s2 = dict(s, validated_dir=str(root / "input_output" / "output_databricks" / "validated"))
        data_io.validate_all(s2)
    data_io.assert_writable(s, root / "input_output" / "output_repository" / "W1.csv")   # fine


def test_sync_creates_merges_and_protects_review(repo):
    s, root = repo
    src_csv = root / "input_output" / "output_databricks" / "time_series_data" / "W1.csv"
    before = src_csv.read_bytes()

    assert stores.sync_stores(s).empty                     # lazily: no store, nothing written
    rep = stores.sync_stores(s, create_missing=True)
    assert rep.loc[0, "created"] and rep.loc[0, "autoflagged"] == 1   # -4.02 <= -4.10 + 0.10
    store = stores.load_saved(s, "W1")
    assert len(store) == 6
    assert store.loc[2, ["gecontroleerd", "afgekeurd", "note"]].tolist() == [0, 1, "auto: rule B"]

    # a reviewer approves the first two hours
    store.loc[[0, 1], "gecontroleerd"] = 1
    stores.save(s, "W1", store)
    # new export: hour 0 changed upstream, one new hour appended
    ts = pd.date_range("2024-01-01", periods=7, freq="1h")
    pd.DataFrame({"Time": ts, "head": [-9.0, -3.0, -4.02, -3.0, -3.0, -3.0, -3.1]}).to_csv(src_csv, index=False)
    before = src_csv.read_bytes()
    rep = stores.sync_stores(s)
    assert rep.loc[0, "new_rows"] == 1 and bool(rep.loc[0, "written"])
    after = stores.load_saved(s, "W1")
    assert after.loc[0, "raw_data"] == -3.0 and after.loc[0, "gecontroleerd"] == 1   # reviewed row kept
    assert len(after) == 7 and after.loc[6, "gecontroleerd"] == 0                     # new row unreviewed
    assert src_csv.read_bytes() == before                                             # source untouched
    assert not stores.sync_stores(s).loc[0, "written"]                                # idempotent


def test_rd_to_wgs84_origin_and_known_point():
    lon, lat = rd_to_wgs84(155_000, 463_000)
    assert (round(float(lon), 6), round(float(lat), 6)) == (5.387206, 52.155174)
    lon, lat = rd_to_wgs84(120_700.723, 487_525.501)  # Westertoren, RDNAPTRANS benchmark
    assert abs(lon - 4.883526) < 1e-4 and abs(lat - 52.374533) < 1e-4
