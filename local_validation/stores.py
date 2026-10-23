"""Layer 3: the validation store, one CSV per series in `input_output/output_repository/`.

The store is the asset: human review decisions, merged into, never replaced. Its schema
and merge rules are frozen (`validation_store.py`). The store file has the same stem as
the source file (`<filename>.csv`); that 1:1 name match is how source and review join.

New source data reaches a store only through `validation_store.merge_new_raw`, which
keeps every reviewed row as it is. Autoflag rule B (and rule A, if enabled) then marks
matching **unreviewed** rows `afgekeurd = 1` (`checks.apply_autoflags`).

Stores are created lazily: when a reviewer saves a series in the app, or explicitly with
`sync_stores(create_missing=True)`. That keeps git free of 600+ untouched copies.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from . import checks, data_io, validation_store


@dataclass
class ReviewSeries:
    filename: str
    store: pd.DataFrame            # merged with the latest source, autoflags applied
    on_disk: pd.DataFrame | None   # the saved store as read, None if none exists yet
    flags: pd.DataFrame            # validate_series result (v1..v4, rule_a, rule_b) per hour
    n_new_rows: int                # source timestamps the saved store did not have yet
    n_autoflagged: int             # rows rule A/B newly marked afgekeurd

    @property
    def exists(self) -> bool:
        return self.on_disk is not None

    @property
    def changed(self) -> bool:
        """True when the merged store differs from what is saved (or nothing is saved)."""
        return self.on_disk is None or not self.store.equals(self.on_disk)


def store_path(settings: dict, filename: str) -> Path:
    return data_io.store_dir(settings) / f"{filename}.csv"


def store_exists(settings: dict, filename: str) -> bool:
    return store_path(settings, filename).exists()


def _autoflag_switches(settings: dict) -> dict:
    a = settings.get("autoflag", {})
    return {"apply_rule_a": bool(a.get("rule_a_apply", False)),
            "apply_rule_b": bool(a.get("rule_b_apply", True))}


def open_series(settings: dict, row, th: checks.Thresholds | None = None) -> ReviewSeries:
    """Load (or start) the store for one object_data row, merge in the current source,
    and apply the autoflag rules. Nothing is written; call `save()` to persist."""
    th = th or data_io.thresholds(settings)
    freq = data_io.round_freq(settings)
    path = store_path(settings, row.filename)
    on_disk = validation_store.load(path) if path.exists() else None

    base = on_disk if on_disk is not None else validation_store.empty_store()
    raw = data_io.load_source_frame(settings, row.filename)
    merged = validation_store.merge_new_raw(base, raw, round_freq=freq)
    n_new = len(merged) - len(base)

    flags = data_io.validate_row(settings, row, th)
    store, n_auto = checks.apply_autoflags(merged, flags.reset_index(), **_autoflag_switches(settings))
    store = validation_store.ensure_columns(store)
    return ReviewSeries(row.filename, store, on_disk, flags, n_new, n_auto)


def save(settings: dict, filename: str, store: pd.DataFrame) -> Path:
    """Atomically write a store into layer 3 (never into the source copy)."""
    path = store_path(settings, filename)
    data_io.assert_writable(settings, path)
    validation_store.save(path, store)
    return path


def load_saved(settings: dict, filename: str) -> pd.DataFrame | None:
    path = store_path(settings, filename)
    return validation_store.load(path) if path.exists() else None


def sync_stores(settings: dict, th: checks.Thresholds | None = None, *,
                create_missing: bool = False, dry_run: bool = False) -> pd.DataFrame:
    """Fold the current source copy into the stores after a new export.

    Existing stores get new timestamps (as unreviewed) and the autoflag rules on their
    unreviewed rows; reviewed rows are untouched. With `create_missing`, a store is also
    created for every series that has none. Only stores whose content changes are written.
    Returns one row per series: what changed (or would, with `dry_run`).
    """
    th = th or data_io.thresholds(settings)
    rows = []
    for row in data_io.load_object_data(settings).itertuples(index=False):
        if not (create_missing or store_exists(settings, row.filename)):
            continue
        rs = open_series(settings, row, th)
        if rs.changed and not dry_run:
            save(settings, row.filename, rs.store)
        rows.append({"filename": row.filename, "created": not rs.exists,
                     "new_rows": rs.n_new_rows, "autoflagged": rs.n_autoflagged,
                     "written": rs.changed and not dry_run})
    return pd.DataFrame(rows, columns=["filename", "created", "new_rows", "autoflagged", "written"])


def store_status(store: pd.DataFrame) -> pd.Series:
    """Per row: `rejected` (human), `auto-rejected` (rule, not yet confirmed),
    `reviewed`, or `unreviewed`."""
    g = store["gecontroleerd"].astype(int) == 1
    a = store["afgekeurd"].astype(int) == 1
    status = pd.Series("unreviewed", index=store.index)
    status[g] = "reviewed"
    status[a & g] = "rejected"
    status[a & ~g] = "auto-rejected"
    return status


def overview(settings: dict) -> tuple[pd.DataFrame, int]:
    """Review progress for every series: from its store when one exists, else from the
    source row count with 0 % reviewed. Returns the table and how many series failed to
    load (surfaced, not silently skipped)."""
    rows, failed = [], 0
    for row in data_io.load_object_data(settings).itertuples(index=False):
        try:
            saved = load_saved(settings, row.filename)
            if saved is not None:
                st = store_status(saved).value_counts()
                total = len(saved)
            else:
                st = pd.Series(dtype=int)
                total = int(row.n_rows) if pd.notna(getattr(row, "n_rows", None)) else len(
                    pd.read_csv(data_io.series_csv(settings, row.filename), usecols=[0]))
            reviewed = int(st.get("reviewed", 0) + st.get("rejected", 0))
            rows.append({
                "filename": row.filename, "vendor": row.vendor, "has_store": saved is not None,
                "rows": total, "reviewed": reviewed,
                "rejected": int(st.get("rejected", 0) + st.get("auto-rejected", 0)),
                "auto_rejected_unconfirmed": int(st.get("auto-rejected", 0)),
                "pct_reviewed": round(100 * reviewed / total, 1) if total else 0.0,
            })
        except Exception:  # noqa: BLE001  (counted and shown in the app)
            failed += 1
    return pd.DataFrame(rows), failed
