"""Read + clean + name the groundwater source, bridging the Unity Catalog schema
to the vendor/origin/series layout the on-prem validation toolkit expects.

Two kinds of helper live here:

* **SQL builders** (`series_catalog_sql`, `series_rows_sql`) — run on Spark against
  the read-only `gold_prd.hl_grondwaterpeilmeting` source.
* **Pure pandas / string helpers** (`clean_head`, `sanitize`, `vendor_of`,
  `origin_of`, `relative_csv_path`) — no Spark, so they are unit-testable off-cluster
  and reused by the export and validated-to-delta notebooks.

Everything addresses the source by **explicit three-part names** — a two-part name
resolves against `spark.catalog.currentCatalog()`, which returns the metastore name
`dbw_hhnkda_prd_05`, not a usable catalog (see DATABRICKS_PERMISSIONS.md §7).

The source is **read-only**. Nothing here writes to `gold_prd`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pandas is only needed by the cleaning helpers, imported lazily
    import pandas as pd

# --- Source (read-only) -----------------------------------------------------
SOURCE_SCHEMA = "gold_prd.hl_grondwaterpeilmeting"
METING = f"{SOURCE_SCHEMA}.meting"
MEETREEKS = f"{SOURCE_SCHEMA}.meetreeks"
# Location registry (coordinates, name, compartment, water-level area). Join coverage
# is partial (DATABRICKS_layer_cs_objecten.md §2): matched where possible, never required.
MEETLOCATIE = "gold_prd.hl_cs_objecten.meetlocatie"

# The time column. We index on the UTC twin to avoid the October DST fold, where
# a local timestamp repeats one hour and becomes genuinely ambiguous
# (DATABRICKS_HANDOFF.md §2.0.6 pt 4). Flip to `tijdstempel` if local wall-clock
# alignment with the KNMI overlays ever matters more than monotonicity.
TIME_COLUMN = "tijdstempel_utc"
VALUE_COLUMN = "waarde"
SERIES_KEY = "meetreeks_id"


# --- Naming bridge ----------------------------------------------------------
def sanitize(stem: str) -> str:
    """Filesystem- and Pastas-safe token: keep [A-Za-z0-9_-], collapse the rest.

    Matches the on-prem rename pre-step (DATABRICKS_HANDOFF.md §2.3). Note the
    source `meetreeks.code` carries a trailing space (`'HB28-PB1 '`) — trim first.
    """
    stem = (stem or "").strip()
    stem = re.sub(r"[^A-Za-z0-9_-]", "_", stem)
    stem = re.sub(r"_+", "_", stem).strip("_")
    return stem or "unknown"


def vendor_of(bronsysteem: str | None) -> str:
    """`Fugro`/`Wiertsema` -> `fugro`/`wiertsema`; anything else -> `overig`."""
    v = sanitize((bronsysteem or "").lower())
    return v if v in {"fugro", "wiertsema"} else (v or "overig")


def origin_of(sensor_meetlocatie_sleutel: str | None, code: str | None) -> str:
    """Location grouping token.

    `sensor_meetlocatie_sleutel` looks like `507080_HB28_GMW_PB1`; its leading
    numeric token is the location key and groups the filters of one well together.
    Falls back to the sanitized code when the sleutel is absent.
    """
    key = (sensor_meetlocatie_sleutel or "").strip()
    if key:
        head = key.split("_", 1)[0]
        if head:
            return sanitize(head)
    return sanitize(code) if (code or "").strip() else "onbekend"


def relative_csv_path(vendor: str, origin: str, series: str) -> str:
    """Legacy nested path `<vendor>/<origin>/<series>.csv`, forward-slashed for git.

    The current export (03) is flat under `input_output/output_databricks/time_series_data/`;
    this helper is kept for the nested review layout `04` still accepts.
    """
    return f"{vendor}/{origin}/{series}.csv"


@dataclass(frozen=True)
class SeriesName:
    meetreeks_id: str
    vendor: str
    origin: str
    series: str

    @property
    def rel_csv(self) -> str:
        return relative_csv_path(self.vendor, self.origin, self.series)


def name_series(meetreeks_id: str, code: str | None, bronsysteem: str | None,
                sensor_sleutel: str | None) -> SeriesName:
    # Fall back to a hash-derived name only when the source carries no code at all;
    # sanitize() itself never returns empty, so test the raw code for blankness.
    series = sanitize(code) if (code or "").strip() else f"series_{(meetreeks_id or '')[:24]}"
    return SeriesName(
        meetreeks_id=meetreeks_id,
        vendor=vendor_of(bronsysteem),
        origin=origin_of(sensor_sleutel, code),
        series=series,
    )


# --- SQL builders (run on Spark) --------------------------------------------
# Ways a meetreeks row can point at hl_cs_objecten.meetlocatie, tried in this order
# (DATABRICKS_layer_cs_objecten.md §2). The first key that matches wins per series.
# `object_id` is a guess: it points at the groundwater schema's own `object` table,
# so it may match nothing — kept because it is cheap to test.
CS_MATCH_KEYS: list[tuple[str, str]] = [
    ("meetlocatie_object_id", "l.id = r.meetlocatie_object_id"),
    ("meetlocatie_code",      "l.code = r.meetlocatie_code"),
    ("trim_code",             "l.code = trim(r.code)"),
    ("object_id",             "l.id = r.object_id"),
]
MATCHED = "matched"
NOT_MATCHED = "no match found in cs_objecten"

# The meetlocatie columns carried into the dimension. Staff names (gebiedsbeheerder_*,
# objectbeheerder) are deliberately left out (§1, "Management and ownership").
_CS_COLS = """
    l.id                                AS cs_meetlocatie_id,
    l.code                              AS cs_code,
    l.naam                              AS cs_naam,
    l.compartiment_code                 AS compartiment_code,
    l.compartiment_naam                 AS compartiment_naam,
    l.peilgebiedpraktijk_code           AS peilgebied_code,
    l.meetreeks_actief                  AS meetreeks_actief,
    CAST(l.rd_xcoordinaat AS double)    AS rd_x,
    CAST(l.rd_ycoordinaat AS double)    AS rd_y,
    CAST(l.wgs84_xcoordinaat AS double) AS lon,
    CAST(l.wgs84_ycoordinaat AS double) AS lat"""
_CS_NULLS = """
    CAST(NULL AS string) AS cs_meetlocatie_id,
    CAST(NULL AS string) AS cs_code,
    CAST(NULL AS string) AS cs_naam,
    CAST(NULL AS string) AS compartiment_code,
    CAST(NULL AS string) AS compartiment_naam,
    CAST(NULL AS string) AS peilgebied_code,
    CAST(NULL AS string) AS meetreeks_actief,
    CAST(NULL AS double) AS rd_x,
    CAST(NULL AS double) AS rd_y,
    CAST(NULL AS double) AS lon,
    CAST(NULL AS double) AS lat"""
_CS_OUT = ("cs_meetlocatie_id, cs_code, cs_naam, compartiment_code, compartiment_naam, "
           "peilgebied_code, meetreeks_actief, rd_x, rd_y, lon, lat")

# Series that actually carry measurements (the dimension is restricted to these).
_SERIES_CTE = f"""
    series AS (
        SELECT * FROM {MEETREEKS}
        WHERE id IN (SELECT DISTINCT {SERIES_KEY} FROM {METING})
    )"""


def series_catalog_sql(include_location: bool = True) -> str:
    """One row per series: every `meetreeks` column plus its `hl_cs_objecten.meetlocatie`
    match.

    The `meetreeks` part: the documented columns first (trimmed code, filter depths cast
    to double), then `r.* EXCEPT (...)` so any further source column comes along too.

    The location part is a **LEFT** join, so unmatched series stay in. Each key in
    `CS_MATCH_KEYS` is tried; the first one that hits wins (`match_key`), and if one key
    finds several locations the lowest `cs_meetlocatie_id` is taken (`cs_n_candidates`
    says how many there were). `matched` is `"matched"` or
    `"no match found in cs_objecten"`.

    With `include_location=False` (no `hl_cs_objecten` grant, §6) the column list is
    identical with NULL locations and every series reads as not matched, so downstream
    code never branches on which variant ran.
    """
    meetreeks_cols = """
            r.id                                 AS meetreeks_id,
            trim(r.code)                         AS code,
            r.bronsysteem                        AS bronsysteem,
            r.sensor_meetlocatie_sleutel         AS sensor_sleutel,
            CAST(r.begin_filter AS double)       AS begin_filter,
            CAST(r.eind_filter  AS double)       AS eind_filter,
            CAST(r.bovenkant_peilbuis AS double) AS bovenkant_peilbuis,
            r.* EXCEPT (id, code, bronsysteem, sensor_meetlocatie_sleutel,
                        begin_filter, eind_filter, bovenkant_peilbuis)"""
    if not include_location:
        return f"""
            WITH {_SERIES_CTE}
            SELECT {meetreeks_cols},
                   '{NOT_MATCHED}'      AS matched,
                   CAST(NULL AS string) AS match_key,
                   CAST(NULL AS int)    AS cs_n_candidates,{_CS_NULLS}
            FROM series r
        """
    branches = "\n            UNION ALL\n".join(
        f"""            SELECT r.id AS meetreeks_id, {prio} AS prio, '{name}' AS match_key,{_CS_COLS}
            FROM series r JOIN {MEETLOCATIE} l ON {cond}"""
        for prio, (name, cond) in enumerate(CS_MATCH_KEYS, start=1)
    )
    return f"""
        WITH {_SERIES_CTE},
        candidates AS (
{branches}
        ),
        ranked AS (
            SELECT *,
                   row_number() OVER (PARTITION BY meetreeks_id ORDER BY prio, cs_meetlocatie_id) AS rn,
                   count(*)     OVER (PARTITION BY meetreeks_id, prio)                          AS n_at_prio
            FROM candidates
        )
        SELECT {meetreeks_cols},
               CASE WHEN c.meetreeks_id IS NOT NULL THEN '{MATCHED}' ELSE '{NOT_MATCHED}' END AS matched,
               c.match_key,
               CAST(c.n_at_prio AS int) AS cs_n_candidates,
               {", ".join("c." + col.strip() for col in _CS_OUT.split(","))}
        FROM series r
        LEFT JOIN ranked c ON c.meetreeks_id = r.id AND c.rn = 1
    """


def cs_match_report_sql() -> str:
    """Diagnostic: for each key in `CS_MATCH_KEYS` on its own, how many of the series
    with measurements find at least one `meetlocatie` row — plus how often the link
    columns are filled in at all. One row per key."""
    rows = "\n        UNION ALL\n".join(
        f"""        SELECT {prio} AS prio, '{name}' AS match_key,
               count(DISTINCT r.id) AS matched_series
        FROM series r JOIN {MEETLOCATIE} l ON {cond}"""
        for prio, (name, cond) in enumerate(CS_MATCH_KEYS, start=1)
    )
    return f"""
        WITH {_SERIES_CTE},
        per_key AS (
{rows}
        ),
        fill AS (
            SELECT count(*)                     AS series_total,
                   count(meetlocatie_object_id) AS has_meetlocatie_object_id,
                   count(meetlocatie_code)      AS has_meetlocatie_code,
                   count(object_id)             AS has_object_id
            FROM series
        )
        SELECT k.prio, k.match_key, k.matched_series, f.series_total,
               round(100.0 * k.matched_series / f.series_total, 1) AS pct_matched,
               f.has_meetlocatie_object_id, f.has_meetlocatie_code, f.has_object_id
        FROM per_key k CROSS JOIN fill f
        ORDER BY k.prio
    """


def meetlocatie_id_convention_sql() -> str:
    """Is `meetlocatie.id` = sha256(code)? (§5) If so the code key is as good as the id."""
    return f"""
        SELECT count(*)                         AS meetlocaties,
               count_if(sha2(code, 256) = id)   AS id_is_sha256_of_code
        FROM {MEETLOCATIE}
    """


def series_rows_sql(meetreeks_ids: list[str] | None = None) -> str:
    """Raw measurements for the given series (all series when None).

    Returns the minimal `(meetreeks_id, ts_utc, waarde)` triple; cleaning to
    hourly happens in pandas via `clean_head` so the logic is identical on and
    off cluster and unit-testable.
    """
    where = ""
    if meetreeks_ids:
        quoted = ",".join("'" + m.replace("'", "''") + "'" for m in meetreeks_ids)
        where = f"WHERE {SERIES_KEY} IN ({quoted})"
    return f"""
        SELECT {SERIES_KEY} AS meetreeks_id,
               {TIME_COLUMN} AS ts_utc,
               CAST({VALUE_COLUMN} AS double) AS waarde
        FROM {METING}
        {where}
    """


def dq_failures_sql(schema: str = "hl_grondwaterpeilmeting") -> str:
    """Open data-quality failures for the source schema. Check before trusting a
    run — `meting.id` carries a known open `critical` duplicate-key failure
    (DATABRICKS_HANDOFF.md §2.0.6 pt 2). `validatieresultaat` spans schemas, so
    the WHERE is not optional.
    """
    return f"""
        SELECT tabel, kolom, verwachting_type, ernst, gebruikersbericht, validatie_tijd
        FROM {SOURCE_SCHEMA}.validatieresultaat
        WHERE schema = '{schema}'
        ORDER BY validatie_tijd DESC
    """


# --- Cleaning (pure pandas) -------------------------------------------------
@dataclass(frozen=True)
class CleanedSeries:
    head: pd.Series  # float64, hourly DatetimeIndex (UTC-naive), NaN at gaps
    n_raw_rows: int
    n_missing_value: int
    n_duplicate_timestamps: int
    n_after_dedup: int
    dedup_method: str
    freq: str


def clean_head(rows: pd.DataFrame, *, freq: str = "1h", dedup_method: str = "mean",
               time_col: str = "ts_utc", value_col: str = "waarde") -> CleanedSeries:
    """One series' raw rows -> hourly head series.

    Mirrors the toolkit's `remove_duplicate_and_fill_missing` and the Tarso
    `clean_head`: drop null values, round the index to `freq`, collapse duplicate
    timestamps (the source carries ~55 rows/day and genuine duplicates), and
    reindex onto a continuous hourly grid so gaps are explicit NaNs. The source
    is never mutated — a new frame is returned.
    """
    import pandas as pd

    n_raw_rows = len(rows)
    s = rows[[time_col, value_col]].copy()
    s[time_col] = pd.to_datetime(s[time_col])
    n_missing_value = int(s[value_col].isna().sum())
    s = s.dropna(subset=[value_col])

    s[time_col] = s[time_col].dt.floor(freq)
    dup_mask = s[time_col].duplicated(keep=False)
    n_duplicate_timestamps = int(dup_mask.sum())

    grouped = s.groupby(time_col)[value_col].agg(dedup_method).sort_index()
    n_after_dedup = len(grouped)

    if n_after_dedup:
        full = pd.date_range(grouped.index.min(), grouped.index.max(), freq=freq)
        grouped = grouped.reindex(full)
    grouped.name = "head"
    grouped.index.name = "timestamp"

    return CleanedSeries(
        head=grouped,
        n_raw_rows=n_raw_rows,
        n_missing_value=n_missing_value,
        n_duplicate_timestamps=n_duplicate_timestamps,
        n_after_dedup=n_after_dedup,
        dedup_method=dedup_method,
        freq=freq,
    )


def head_to_only_csv(cleaned: CleanedSeries) -> pd.DataFrame:
    """Shape a cleaned series as the toolkit's raw per-sensor `only_csv` file:
    two columns `Time,head`, gap rows dropped (raw export carries no NaN rows).
    """
    import pandas as pd  # noqa: F401  (kept explicit for parity / future use)

    df = cleaned.head.dropna().reset_index()
    df.columns = ["Time", "head"]
    return df
