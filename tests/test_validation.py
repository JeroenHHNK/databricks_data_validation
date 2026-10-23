"""Unit tests for local_validation/checks.py and the settings wiring in data_io.py."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_validation import checks as val  # noqa: E402
from local_validation import data_io  # noqa: E402


def hourly(values, start="2024-01-01"):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="1h"), dtype=float)


def test_v1_bounds_each_side_optional():
    raw = hourly([-5.0, -1.0, 2.0])
    out = val.validate_series(raw, begin_filter=-3.0, eind_filter=-4.0, bovenkant_peilbuis=1.5)
    assert out["v1"].tolist()[0] == -5.0 and out["v1"].tolist()[2] == 2.0
    assert np.isnan(out["v1"].iloc[1])
    # no bounds at all -> v1 never fires
    out = val.validate_series(raw)
    assert out["v1"].isna().all()


def test_v2_step_change_up_and_down():
    raw = hourly([0.0, 0.0, 0.5, 0.5, 0.44, 0.44])
    df, changed = val.flag_unrealistic_step_change(pd.DataFrame({"head": raw}))
    assert changed
    assert df["v2"].notna().tolist() == [False, False, True, False, True, False]
    assert df["head"].isna().tolist() == [False, False, True, False, True, False]


def test_layers_are_sequential_v2_sees_v1_nans():
    # 9.0 is out of bounds (v1); the jumps into/out of it must not also count as v2
    raw = hourly([0.0, 0.0, 9.0, 0.0, 0.0])
    out = val.validate_series(raw, bovenkant_peilbuis=1.0)
    assert out["v1"].notna().sum() == 1
    assert out["v2"].notna().sum() == 0


def test_v2_does_not_bridge_a_gap():
    raw = hourly([0.0, 0.0, 0.0, 1.0])
    raw = raw.drop(raw.index[2])               # 1 h gap before the 1.0
    out = val.validate_series(raw)
    assert out["v2"].isna().all()
    assert len(out) == 3                       # only the original hours come back


def test_v3_within_15cm_of_eind_filter():
    # the agreed example: eind_filter -4.10 -> -3.95 and -4.05 flagged, -3.92 not
    raw = hourly([-3.95, -4.05, -3.92, -4.25, -4.26])
    df, changed = val.flag_constant_head_runs(pd.DataFrame({"head": raw}), eind_filter=-4.10)
    assert changed
    assert df["v3"].notna().tolist() == [True, True, False, True, False]
    # no eind_filter -> skipped
    df, changed = val.flag_constant_head_runs(pd.DataFrame({"head": raw}), eind_filter=None)
    assert not changed and df["v3"].isna().all()


def test_v3_min_run_option():
    raw = hourly([0.0] * 5 + [-4.0] * 3 + [0.0] * 5)
    df, _ = val.flag_constant_head_runs(pd.DataFrame({"head": raw}), eind_filter=-4.10, min_run_steps=3)
    assert df["v3"].notna().sum() == 3
    df, changed = val.flag_constant_head_runs(pd.DataFrame({"head": raw}), eind_filter=-4.10, min_run_steps=4)
    assert not changed


def test_v3_in_full_pipeline_after_v1():
    # -4.20 is below the filter bottom, so v1 takes it before v3 sees it
    # (small steps, so v2 does not take -3.95 first)
    raw = hourly([-3.92, -3.95, -4.20, -3.92])
    out = val.validate_series(raw, begin_filter=-3.10, eind_filter=-4.10)
    assert out["v1"].notna().tolist() == [False, False, True, False]
    assert out["v3"].notna().tolist() == [False, True, False, False]


def test_v4_tukey():
    raw = hourly([1.0, 1.1, 0.9, 1.0, 1.05, 0.95, 10.0])
    df, changed = val.flag_statistical_outliers(pd.DataFrame({"head": raw}))
    assert changed and df["v4"].notna().tolist() == [False] * 6 + [True]


def test_rule_a_contiguous_only_and_exact_threshold():
    raw = hourly([1.25, 1.15, 1.15, 0.0])
    raw = raw.drop(raw.index[2])               # gap before 0.0 -> not contiguous
    out = val.validate_series(raw)
    assert out["rule_a"].tolist() == [False, True, False]


def test_rule_b_margin_and_missing_filter():
    raw = hourly([-3.85, -3.95, -3.5])
    out = val.validate_series(raw, begin_filter=-3.0, eind_filter=-4.0)
    assert out["rule_b"].tolist() == [False, True, False]   # <= -4.0 + 0.10
    assert not val.validate_series(raw)["rule_b"].any()


def test_thresholds_follow_settings_yaml():
    th = data_io.thresholds(data_io.load_settings())
    assert (th.v3_band, th.v3_min_run, th.rule_b_margin, th.freq) == (0.15, 1, 0.10, "1h")
    assert data_io.thresholds({}) == val.Thresholds()      # missing keys keep the defaults


def test_apply_autoflags_never_touches_reviewed_rows():
    ts = pd.date_range("2024-01-01", periods=3, freq="1h")
    store = pd.DataFrame({"timestamp": ts, "raw_data": [1.0, 2.0, 3.0],
                          "gecontroleerd": [1, 0, 0], "afgekeurd": [0, 0, 0], "note": ["", "", "x"]})
    flags = pd.DataFrame({"timestamp": ts, "rule_a": [True, True, False], "rule_b": [True, False, True]})
    out, n = val.apply_autoflags(store, flags)            # default: rule B only
    assert n == 2 - 1                                       # row 0 reviewed, row 1 only rule A
    assert out["afgekeurd"].tolist() == [0, 0, 1]
    assert out["gecontroleerd"].tolist() == [1, 0, 0]
    assert out.at[2, "note"] == "x; auto: rule B"
    again, n_again = val.apply_autoflags(out, flags)      # re-run: nothing new, no duplicate note
    assert n_again == 0 and again.at[2, "note"] == "x; auto: rule B"
    out, n = val.apply_autoflags(store, flags, apply_rule_a=True)
    assert out["afgekeurd"].tolist() == [0, 1, 1] and n == 2


@pytest.mark.parametrize("b,e,expected", [(-3.0, -4.0, -4.0), (-4.0, -3.0, -4.0),
                                          (None, -4.0, -4.0), (None, float("nan"), None)])
def test_filter_bottom(b, e, expected):
    assert val.filter_bottom(b, e) == expected
