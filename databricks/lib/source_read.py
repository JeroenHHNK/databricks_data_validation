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
# Optional enrichment (coordinates, water-level area). Join coverage is unproven
# (DATABRICKS_layer_cs_objecten.md §2); treated as best-effort, never required.
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

    The current export (02) is flat under `output_databricks/time_series_data/`; this
    helper is kept for the `03`/review layout until that is finalized.
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
def series_catalog_sql(include_location: bool = True) -> str:
    """One row per series: the dimension needed to name files and build
    OBJECT_DATA. With `include_location`, left-joins `meetlocatie` for coordinates
    and the water-level area; set it False to fall back to meetreeks-only when the
    `hl_cs_objecten` grant is absent (DATABRICKS_layer_cs_objecten.md §6). The
    column list is identical either way — the location columns come back NULL — so
    downstream code never has to branch on which variant ran.
    """
    if include_location:
        loc_cols = """
            l.peilgebiedpraktijk_code         AS peilgebied_code,
            CAST(l.rd_xcoordinaat AS double)  AS rd_x,
            CAST(l.rd_ycoordinaat AS double)  AS rd_y,
            CAST(l.wgs84_xcoordinaat AS double) AS lon,
            CAST(l.wgs84_ycoordinaat AS double) AS lat"""
        loc_join = f"LEFT JOIN {MEETLOCATIE} l ON l.id = r.meetlocatie_object_id"
    else:
        loc_cols = """
            CAST(NULL AS string) AS peilgebied_code,
            CAST(NULL AS double) AS rd_x,
            CAST(NULL AS double) AS rd_y,
            CAST(NULL AS double) AS lon,
            CAST(NULL AS double) AS lat"""
        loc_join = ""
    return f"""
        SELECT
            r.id                              AS meetreeks_id,
            trim(r.code)                      AS code,
            r.bronsysteem                     AS bronsysteem,
            r.sensor_meetlocatie_sleutel      AS sensor_sleutel,
            CAST(r.begin_filter AS double)    AS begin_filter,
            CAST(r.eind_filter  AS double)    AS eind_filter,
            CAST(r.bovenkant_peilbuis AS double) AS bovenkant_peilbuis,
            r.eenheid                         AS eenheid,{loc_cols}
        FROM {MEETREEKS} r
        {loc_join}
        WHERE r.id IN (SELECT DISTINCT {SERIES_KEY} FROM {METING})
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
