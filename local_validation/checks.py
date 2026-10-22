"""Automated validation of one hourly head series: the v1-v4 flag layers and the
autoflag rules A/B, plus the store-side application that protects manual review.

Pure pandas/numpy, no filesystem: `data_io.py` handles reading
`input_output/output_databricks/` and writing results, so these functions can be reused
as-is by the Streamlit review toolkit. Runs locally, not on Databricks. The v2/v4
functions are ported from the on-prem
`functions/validation_functions.py` with the same `(df_in) -> (df_out, changed)`
contract (docs/archive/DISCOVER_functions.md).

Layers run **sequentially** on `head`. Each layer sees `head` as left by the previous
one (NaN where already flagged), stores the original value of what it flags in its own
column (`v1`..`v4`), and sets `head` to NaN:

    v1  out of bounds       head < filter bottom, or head > top of well
    v2  step change         hourly dH > +0.30 m or < -0.05 m
    v3  at filter bottom    |head - eind_filter| <= 0.15 m
    v4  statistical outlier outside Q1 - 1.5*IQR .. Q3 + 1.5*IQR (Tukey)

The autoflag rules look at the **raw** value, as they act on the review store's
`raw_data`:

    rule_a  drop >= 0.10 m in one hourly step (contiguous hours only) - report-only
    rule_b  raw <= filter bottom + 0.10 m                              - applied

Manual review always wins: `apply_autoflags` never touches a row a human reviewed.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

FLAG_COLUMNS = ["v1", "v2", "v3", "v4"]
RULE_COLUMNS = ["rule_a", "rule_b"]
RESULT_COLUMNS = ["raw_head", "head", *FLAG_COLUMNS, *RULE_COLUMNS]


@dataclass(frozen=True)
class Thresholds:
    """Defaults mirror config/settings.yaml `flags:` / `autoflag:` (handover §5)."""
    v2_max_up: float = 0.30        # m per step
    v2_max_down: float = -0.05     # m per step
    v3_band: float = 0.15          # m either side of eind_filter
    v3_min_run: int = 1            # steps; 1 = flag every in-band hour
    v4_iqr_factor: float = 1.5
    rule_a_drop: float = 0.10      # m per step
    rule_b_margin: float = 0.10    # m above the filter bottom
    freq: str = "1h"


def as_float(x) -> float | None:
    """`None`/NaN/unparseable -> None, else float. Metadata bounds are often missing."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(f) else f


def filter_bottom(begin_filter, eind_filter) -> float | None:
    """The lower of the two filter depths [m NAP]; `eind_filter` in practice, but
    taking the min keeps it right if a source row has them swapped."""
    vals = [v for v in (as_float(begin_filter), as_float(eind_filter)) if v is not None]
    return min(vals) if vals else None


# --- v1-v4: sequential flag layers ------------------------------------------
def _move_to(df: pd.DataFrame, mask: pd.Series, vcol: str) -> pd.DataFrame:
    """Store the flagged originals in `vcol` (never overwriting) and blank `head`."""
    if vcol not in df.columns:
        df[vcol] = np.nan
    df.loc[mask & df[vcol].isna(), vcol] = df.loc[mask, "head"]
    df.loc[mask, "head"] = np.nan
    return df


def flag_physical_bounds(df_in, hmin=None, hmax=None, vcol="v1"):
    """v1: head below `hmin` (filter bottom) or above `hmax` (top of well).
    A missing bound disables that side only."""
    df = df_in.copy()
    head = df["head"]
    mask = pd.Series(False, index=df.index)
    if hmin is not None:
        mask |= head.notna() & (head < hmin)
    if hmax is not None:
        mask |= head.notna() & (head > hmax)
    df = _move_to(df, mask, vcol)
    return df, bool(mask.any())


def flag_unrealistic_step_change(df_in, max_up=0.3, max_down=-0.05, vcol="v2"):
    """v2: consecutive-step change above `max_up` or below `max_down`. The row after
    the jump is flagged. The diff runs over the gap-filled grid, so it never spans a
    gap or a value an earlier layer already removed."""
    df = df_in.copy()
    dh = df["head"].diff()
    mask = (dh > max_up) | (dh < max_down)
    df = _move_to(df, mask, vcol)
    return df, bool(mask.any())


def flag_constant_head_runs(df_in, eind_filter=None, band_m=0.15, min_run_steps=1, vcol="v3"):
    """v3: head within `band_m` of the filter bottom (`eind_filter`), i.e. the sensor is
    at its floor. Inclusive: with eind_filter -4.10, -3.95 and -4.05 are flagged, -3.92
    is not. No `eind_filter` -> v3 is skipped for the series.

    `min_run_steps` > 1 only flags in-band stretches of at least that many consecutive
    steps; the default 1 flags every in-band hour. (The on-prem version measured the
    band from the series' own minimum, which flagged whole series that are simply flat.)
    """
    df = df_in.copy()
    if vcol not in df.columns:
        df[vcol] = np.nan
    eind = as_float(eind_filter)
    if eind is None:
        return df, False
    # round so a value exactly `band_m` away (-3.95 vs -4.10) is not lost to float noise
    in_band = df["head"].notna() & ((df["head"] - eind).abs().round(6) <= float(band_m))
    run_lengths = in_band.groupby((~in_band).cumsum()).transform("sum")
    mask = in_band & (run_lengths >= min_run_steps)
    df = _move_to(df, mask, vcol)
    return df, bool(mask.any())


def flag_statistical_outliers(df_in, factor=1.5, vcol="v4"):
    """v4: Tukey fences, Q1 - factor*IQR .. Q3 + factor*IQR, on what is left of head."""
    df = df_in.copy()
    if vcol not in df.columns:
        df[vcol] = np.nan
    heads = df["head"].dropna()
    if len(heads) < 4:
        return df, False
    q1, q3 = np.percentile(heads, 25), np.percentile(heads, 75)
    iqr = q3 - q1
    lo, hi = q1 - factor * iqr, q3 + factor * iqr
    mask = df["head"].notna() & ((df["head"] < lo) | (df["head"] > hi))
    df = _move_to(df, mask, vcol)
    return df, bool(mask.any())


# --- Autoflag rules (on raw) ------------------------------------------------
def rule_a_sudden_drop(raw: pd.Series, drop: float = 0.10) -> pd.Series:
    """Drop of at least `drop` m between two *consecutive* hours (both present).
    Report-only by default: it disagreed with human reviewers on most hits."""
    # round so a drop of exactly `drop` (e.g. 1.25 -> 1.15) is not lost to float noise
    return (raw.diff().round(6) <= -abs(drop)).fillna(False)


def rule_b_below_filter(raw: pd.Series, bottom: float | None, margin: float = 0.10) -> pd.Series:
    """Raw value at or below the filter bottom + `margin`: physically at/below the
    sensor filter. Applied by default. No filter depth -> never fires."""
    if bottom is None:
        return pd.Series(False, index=raw.index)
    return (raw.notna() & (raw.round(6) <= round(bottom + margin, 6))).fillna(False)


# --- One series end to end --------------------------------------------------
def validate_series(raw: pd.Series, *, begin_filter=None, eind_filter=None,
                    bovenkant_peilbuis=None, th: Thresholds = Thresholds()) -> pd.DataFrame:
    """Run v1 -> v2 -> v3 -> v4 and rules A/B on one series.

    `raw` is the hourly head with a DatetimeIndex (only the hours that have a value).
    The layers run on a continuous `th.freq` grid so step/run logic never bridges a
    gap; only the original hours are returned. Columns: `RESULT_COLUMNS` — `raw_head`
    (untouched), `head` (NaN where any layer flagged), `v1`..`v4` (the flagged
    originals, NaN elsewhere), `rule_a`/`rule_b` (bool).
    """
    raw = pd.to_numeric(raw, errors="coerce")
    raw.index = pd.to_datetime(raw.index)
    raw = raw.groupby(level=0).mean().sort_index()   # defensive: one value per hour
    if raw.empty:
        return pd.DataFrame(columns=RESULT_COLUMNS, index=pd.DatetimeIndex([], name="timestamp"))

    grid = pd.date_range(raw.index.min(), raw.index.max(), freq=th.freq)
    raw_g = raw.reindex(grid)
    df = pd.DataFrame({"raw_head": raw_g, "head": raw_g})

    bottom = filter_bottom(begin_filter, eind_filter)
    df, _ = flag_physical_bounds(df, hmin=bottom, hmax=as_float(bovenkant_peilbuis))
    df, _ = flag_unrealistic_step_change(df, max_up=th.v2_max_up, max_down=th.v2_max_down)
    df, _ = flag_constant_head_runs(df, eind_filter=eind_filter, band_m=th.v3_band,
                                    min_run_steps=th.v3_min_run)
    df, _ = flag_statistical_outliers(df, factor=th.v4_iqr_factor)
    df["rule_a"] = rule_a_sudden_drop(raw_g, th.rule_a_drop)
    df["rule_b"] = rule_b_below_filter(raw_g, bottom, th.rule_b_margin)

    df = df.loc[raw.index, RESULT_COLUMNS]            # back to the hours that exist
    df.index.name = "timestamp"
    return df




# --- Store-side application (local review store) -----------------------------
AUTOFLAG_NOTE = {"rule_a": "autoflag rule A (sudden drop)", "rule_b": "autoflag rule B (at/below filter)"}


def apply_autoflags(store: pd.DataFrame, flags: pd.DataFrame, *, apply_rule_a: bool = False,
                    apply_rule_b: bool = True) -> tuple[pd.DataFrame, int]:
    """Fold rule A/B hits into a review store (`timestamp,raw_data,gecontroleerd,afgekeurd,note`).

    Sets `afgekeurd = 1` and appends a note on matching rows, **only** where
    `gecontroleerd == 0` — a human verdict is never overwritten. `gecontroleerd` is
    left at 0 so the reviewer still sees the row as awaiting confirmation. `flags` has
    `timestamp` + `rule_a`/`rule_b`. Returns the new store and the number of rows changed.
    """
    rules = [r for r, on in (("rule_a", apply_rule_a), ("rule_b", apply_rule_b)) if on]
    out = store.copy()
    if not rules or flags.empty:
        return out, 0
    f = flags.set_index(pd.to_datetime(flags["timestamp"]))[rules].astype(bool)
    hit = f.reindex(pd.to_datetime(out["timestamp"])).fillna(False)
    hit.index = out.index
    any_hit = hit.any(axis=1) & (out["gecontroleerd"].astype(int) == 0) & (out["afgekeurd"].astype(int) == 0)
    for idx in out.index[any_hit]:
        note = "; ".join(AUTOFLAG_NOTE[r] for r in rules if hit.at[idx, r])
        prev = str(out.at[idx, "note"] or "")
        out.at[idx, "note"] = f"{prev}; {note}" if prev else note
    out.loc[any_hit, "afgekeurd"] = 1
    return out, int(any_hit.sum())
