"""Review page: walk one series point by point, mark rows gecontroleerd / afgekeurd.

Behaviour follows the on-prem app (agent_streamlit_prompt.md §5, "Validation" tab):
opening a series merges the current source into its store (reviewed rows protected) and
applies autoflag rule B; edits are written back by timestamp; afgekeurd implies
gecontroleerd for rows a human edits; notes are capped at `notes_max_len`; saving is
atomic and only ever writes to the validation store (layer 3).
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from local_validation import checks, data_io, stores

from . import common, plotting

CHECK_LABELS = {"v1": "v1 out of bounds", "v2": "v2 step change", "v3": "v3 at filter bottom",
                "v4": "v4 outlier", "rule_a": "rule A drop", "rule_b": "rule B ≤ filter + 0.10"}
SHOW_OPTIONS = ["Unreviewed (incl. auto-rejected)", "Auto-rejected, to confirm",
                "Flagged by a check", "Reviewed", "All"]


def _text(v) -> str:
    return "–" if v is None or pd.isna(v) else str(v)


# --- state -------------------------------------------------------------------
def _load(settings: dict, filename: str) -> None:
    row = common.meta_row(filename)
    rs = stores.open_series(settings, row, data_io.thresholds(settings))
    checks_per_ts = pd.Series("", index=rs.flags.index)
    for col in checks.FLAG_COLUMNS:
        checks_per_ts[rs.flags[col].notna()] += f"{col} "
    for col in checks.RULE_COLUMNS:
        checks_per_ts[rs.flags[col].astype(bool)] += f"{col} "
    st.session_state.update(
        rv_filename=filename, rv_row=row, rv_rs=rs, rv_store=rs.store, rv_dirty=False,
        rv_hints=plotting.flag_hints(rs.flags), rv_checks=checks_per_ts.str.strip(),
        rv_chart_n=st.session_state.get("rv_chart_n", 0) + 1)


def _save(settings: dict) -> None:
    filename, store = st.session_state.rv_filename, st.session_state.rv_store
    path = stores.save(settings, filename, store)
    st.session_state.rv_rs.on_disk = store.copy()
    st.session_state.rv_dirty = False
    common.overview.clear()
    st.toast(f"Saved {path.name} to {path.parent.name}/")


def _set_store(store: pd.DataFrame) -> None:
    st.session_state.rv_store = store
    st.session_state.rv_dirty = True


# --- sidebar -----------------------------------------------------------------
def _sidebar(settings: dict, meta: pd.DataFrame) -> str | None:
    sb = st.sidebar
    sb.header("Series")
    target = st.session_state.pop("review_target", None)

    vendors = sorted(meta["vendor"].dropna().unique())
    pick_vendors = sb.multiselect("Vendor", vendors, default=vendors)
    only_loc = sb.checkbox("Only series with a location", value=False)
    query = sb.text_input("Search filename / meetlocatie code / sensor key / code", "")

    view = meta[meta["vendor"].isin(pick_vendors)]
    if only_loc:
        view = view[view["has_location"]]
    if query:
        q = query.lower()
        hay = (view["filename"].astype(str) + " " + view["meetlocatie_code"].astype(str) + " "
               + view["sensor_sleutel"].astype(str) + " "
               + view["code"].astype(str)).str.lower()
        view = view[hay.str.contains(q, regex=False)]
    names = view["filename"].tolist()
    if target and target not in names:
        names = [target, *names]
    if not names:
        sb.warning("No series match the filters.")
        return None
    if target:
        st.session_state["review_pick"] = target
    elif st.session_state.get("review_pick") not in names:
        st.session_state["review_pick"] = names[0]

    saved = common.store_names()
    pick = sb.selectbox(f"Series ({len(names)})", names, key="review_pick",
                        format_func=lambda n: f"{'✓' if n in saved else '·'} {n}",
                        help="✓ = a validation store exists for this series")

    loaded = st.session_state.get("rv_filename")
    if pick != loaded:
        if loaded and st.session_state.get("rv_dirty"):
            sb.warning(f"Unsaved edits on **{loaded}**. Save or discard them to switch.")
            c1, c2 = sb.columns(2)
            if c1.button("Save", type="primary", width="stretch"):
                _save(settings)
                _load(settings, pick)
                st.rerun()
            if c2.button("Discard", width="stretch"):
                _load(settings, pick)
                st.rerun()
        else:
            _load(settings, pick)

    if "rv_store" in st.session_state:
        sb.divider()
        if sb.button("Save to validation store", type="primary", width="stretch",
                     disabled=not (st.session_state.rv_dirty or st.session_state.rv_rs.changed)):
            _save(settings)
        if sb.button("Reload (discard unsaved edits)", width="stretch"):
            _load(settings, st.session_state.rv_filename)
            st.rerun()

    sb.divider()
    sb.caption("Overlay")
    files = common.stressor_files()
    st.session_state.rv_stressor = None
    if files:
        choice = sb.selectbox("KNMI forcing", ["None", *files],
                              format_func=lambda p: p if p == "None" else p.replace("\\", "/").split("/")[-1])
        if choice != "None":
            data = common.load_stressor_csv(choice)
            if "error" in data:
                sb.error(data["error"])
            else:
                st.session_state.rv_stressor = data
    else:
        sb.caption("No KNMI forcing CSV in data/forcing/ (precipitation_mm, makkink_mm).")
    st.session_state.rv_hint_cols = tuple(sb.multiselect(
        "Show check hints", list(plotting.HINT_STYLE), default=list(plotting.HINT_STYLE),
        format_func=lambda c: plotting.HINT_STYLE[c][2]))
    return st.session_state.get("rv_filename")


# --- main --------------------------------------------------------------------
def _plot_range() -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """Time range of the current box/lasso selection on the review plot, if any."""
    ev = st.session_state.get(f"rv_chart_{st.session_state.rv_chart_n}")
    sel = (ev or {}).get("selection", {}) if isinstance(ev, dict) else getattr(ev, "selection", {})
    try:
        boxes = sel.get("box") or []
        if boxes:
            xs = pd.to_datetime(boxes[0]["x"])
            return xs.min(), xs.max()
        xs = pd.to_datetime([p["x"] for p in sel.get("points", []) if "x" in p])
        if len(xs):
            return xs.min(), xs.max()
    except (AttributeError, KeyError, TypeError, ValueError):
        pass
    return None


def _apply_edits(edited: pd.DataFrame, view: pd.DataFrame, max_len: int) -> None:
    cols = ["gecontroleerd", "afgekeurd", "note"]
    ed = edited.set_index("timestamp")[cols].copy()
    before = view.set_index("timestamp")[cols]
    ed["gecontroleerd"] = ed["gecontroleerd"].astype(bool).astype(int)
    ed["afgekeurd"] = ed["afgekeurd"].astype(bool).astype(int)
    ed["note"] = ed["note"].fillna("").astype(str)
    changed = (ed["gecontroleerd"] != before["gecontroleerd"].astype(bool).astype(int)) \
        | (ed["afgekeurd"] != before["afgekeurd"].astype(bool).astype(int)) \
        | (ed["note"] != before["note"].fillna("").astype(str))
    if not changed.any():
        return
    ed = ed[changed]
    if ed["note"].str.len().gt(max_len).any():
        ed["note"] = ed["note"].str.slice(0, max_len)
        st.warning(f"Notes are capped at {max_len} characters and were truncated.")
    ed.loc[ed["afgekeurd"] == 1, "gecontroleerd"] = 1   # afgekeurd implies gecontroleerd
    store = st.session_state.rv_store.set_index("timestamp")
    store.loc[ed.index, cols] = ed[cols]
    _set_store(store.reset_index())


def _bulk(view: pd.DataFrame, g: int, a: int, note: str, max_len: int) -> None:
    store = st.session_state.rv_store.set_index("timestamp")
    ts = view["timestamp"]
    store.loc[ts, "gecontroleerd"] = g
    store.loc[ts, "afgekeurd"] = a
    if note:
        store.loc[ts, "note"] = note[:max_len]
    _set_store(store.reset_index())


def render() -> None:
    settings = common.settings()
    meta = common.object_data()
    filename = _sidebar(settings, meta)
    if filename is None or "rv_store" not in st.session_state:
        st.info("Pick a series in the sidebar, or open one from the map.")
        return

    row, rs = st.session_state.rv_row, st.session_state.rv_rs
    store = st.session_state.rv_store
    max_len = int(settings.get("notes_max_len", 30))
    th = data_io.thresholds(settings)

    st.title(filename)
    st.caption(" · ".join([
        f"**{row.vendor}**", f"code {row.code}",
        f"meetlocatie {_text(getattr(row, 'meetlocatie_code', None))}",
        f"sensor key {_text(getattr(row, 'sensor_sleutel', None))}",
        f"location {getattr(row, 'cs_naam', None) or 'no match in cs_objecten'}",
        f"filter {common.fmt(row.begin_filter)} / {common.fmt(row.eind_filter)} m NAP",
        f"top of well {common.fmt(row.bovenkant_peilbuis)} m NAP"]))
    if st.session_state.rv_dirty:
        st.warning("Unsaved review edits — use **Save to validation store** in the sidebar.")
    elif rs.changed:
        what = "new store, not saved yet" if not rs.exists else (
            f"{rs.n_new_rows} new source rows merged" + (f", {rs.n_autoflagged} auto-rejected" if rs.n_autoflagged else ""))
        st.info(f"Opened with automatic updates ({what}). They are written on the next save.")

    stats_box, plot_box = st.container(), st.container()

    # ---- editor first, so its edits show in the plot in this same run ----
    st.subheader("Review points")
    status = stores.store_status(store)
    checks_col = store["timestamp"].map(st.session_state.rv_checks).fillna("")
    f1, f2, f3 = st.columns([2, 2, 1])
    show = f1.selectbox("Show", SHOW_OPTIONS)
    pick_checks = f2.multiselect("Checks", list(CHECK_LABELS), default=list(CHECK_LABELS),
                                 format_func=CHECK_LABELS.get,
                                 disabled=show != "Flagged by a check")
    max_rows = f3.number_input("Max rows", 50, 20000, 500, 50)

    mask = {
        SHOW_OPTIONS[0]: status.isin(["unreviewed", "auto-rejected"]),
        SHOW_OPTIONS[1]: status == "auto-rejected",
        SHOW_OPTIONS[2]: checks_col.apply(lambda s: any(c in s.split() for c in pick_checks)),
        SHOW_OPTIONS[3]: status.isin(["reviewed", "rejected"]),
        SHOW_OPTIONS[4]: pd.Series(True, index=store.index),
    }[show]

    rng = _plot_range()
    if rng:
        mask &= store["timestamp"].between(*rng)
        r1, r2 = st.columns([4, 1])
        r1.caption(f"Plot selection: {rng[0]:%Y-%m-%d %H:%M} → {rng[1]:%Y-%m-%d %H:%M}")
        if r2.button("Clear selection"):
            st.session_state.rv_chart_n += 1
            st.rerun()
    else:
        st.caption("Tip: drag a box on the plot to limit this table to that period.")

    view = store[mask]
    view_capped = view.head(int(max_rows))
    if len(view) > len(view_capped):
        st.caption(f"Showing the first {len(view_capped)} of {len(view)} matching rows.")

    b1, b2, b3, b4 = st.columns([2, 1, 1, 1])
    bulk_note = b1.text_input("Note for bulk action (optional)", max_chars=max_len)
    if b2.button("Approve visible", width="stretch", disabled=view_capped.empty):
        _bulk(view_capped, 1, 0, bulk_note, max_len)
        st.rerun()
    if b3.button("Reject visible", width="stretch", disabled=view_capped.empty):
        _bulk(view_capped, 1, 1, bulk_note, max_len)
        st.rerun()
    if b4.button("Reset visible", width="stretch", disabled=view_capped.empty,
                 help="Back to unreviewed (0/0). Autoflag may re-reject rule B rows on the next open."):
        _bulk(view_capped, 0, 0, bulk_note, max_len)
        st.rerun()

    table = view_capped.assign(checks=checks_col[view_capped.index],
                               gecontroleerd=view_capped["gecontroleerd"].astype(bool),
                               afgekeurd=view_capped["afgekeurd"].astype(bool))
    table = table[["timestamp", "raw_data", "checks", "gecontroleerd", "afgekeurd", "note"]]
    editor_key = f"rv_editor_{filename}_{int(pd.util.hash_pandas_object(table, index=False).sum())}"
    edited = st.data_editor(
        table, key=editor_key, hide_index=True, width="stretch", height=380,
        disabled=["timestamp", "raw_data", "checks"],
        column_config={
            "timestamp": st.column_config.DatetimeColumn("Timestamp", format="YYYY-MM-DD HH:mm"),
            "raw_data": st.column_config.NumberColumn("Raw data (m NAP)", format="%.4f"),
            "checks": st.column_config.TextColumn("Checks", help="Automated checks that hit this hour"),
            "gecontroleerd": st.column_config.CheckboxColumn("Gecontroleerd"),
            "afgekeurd": st.column_config.CheckboxColumn("Afgekeurd"),
            "note": st.column_config.TextColumn("Note", max_chars=max_len),
        })
    if edited is not None and not table.empty:
        _apply_edits(edited, view_capped, max_len)

    # ---- stats + plot, rendered into the containers above ----
    store = st.session_state.rv_store
    counts = stores.store_status(store).value_counts()
    total = len(store) or 1
    with stats_box:
        m = st.columns(5)
        m[0].metric("Total hours", len(store))
        m[1].metric("Reviewed", f"{int(counts.get('reviewed', 0) + counts.get('rejected', 0))} "
                                f"({100 * (counts.get('reviewed', 0) + counts.get('rejected', 0)) / total:.1f}%)")
        m[2].metric("Rejected", int(counts.get("rejected", 0)))
        m[3].metric("Auto-rejected, to confirm", int(counts.get("auto-rejected", 0)))
        m[4].metric("Unreviewed", int(counts.get("unreviewed", 0)))
    with plot_box:
        fig = plotting.build_figure(
            store, st.session_state.rv_hints, title="",
            eind_filter=checks.as_float(row.eind_filter),
            top_of_well=checks.as_float(row.bovenkant_peilbuis), v3_band=th.v3_band,
            stressor_data=st.session_state.get("rv_stressor"),
            show_hints=st.session_state.get("rv_hint_cols", tuple(plotting.HINT_STYLE)))
        st.plotly_chart(fig, width="stretch", key=f"rv_chart_{st.session_state.rv_chart_n}",
                        on_select="rerun", selection_mode=("box", "lasso"))
