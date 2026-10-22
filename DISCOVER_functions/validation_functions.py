import pandas as pd
import numpy as np

def load_groundwater_csv(file_path, dayfirst=False):
    """
    Load groundwater head measurement data from a CSV file.

    - Tries to interpret the *first* column as timestamps (Time).
    - Tries to interpret the *second* column as head values (head).
    - Keeps any additional columns (e.g. Precipitation, Evapotranspiration, recharge).

    Expected structure (minimal):
        col 0: timestamp (any parseable datetime format)
        col 1: head value (numeric or NaN)
        col 2+: optional extra columns (kept as-is)
    """

    # Read CSV as-is
    df = pd.read_csv(file_path)

    if df.shape[1] < 2:
        raise ValueError(
            f"Expected at least 2 columns (time, head) in {file_path}, "
            f"but found {df.shape[1]}."
        )

    # Debug
    print(f" Length of df before cleaning: {len(df)}")

    # Prefer explicit column names if present
    if "Time" in df.columns:
        time_col_original = "Time"
    else:
        time_col_original = df.columns[0]

    if "head" in df.columns:
        head_col_original = "head"
    else:
        head_col_original = df.columns[1]

    # Rename to standard names
    rename_map = {}
    if time_col_original != "Time":
        rename_map[time_col_original] = "Time"
    if head_col_original != "head":
        rename_map[head_col_original] = "head"
    if rename_map:
        df = df.rename(columns=rename_map)

    # --- Parse timestamps ---
    before = len(df)
    df["Time"] = pd.to_datetime(df["Time"], dayfirst=dayfirst, errors="coerce")
    df = df.dropna(subset=["Time"])
    if len(df) < before:
        print(f"⚠️  Removed {before - len(df)} rows with invalid timestamps in '{file_path}'")

    # --- Convert head to numeric, but KEEP NaNs (they are valid gaps) ---
    df["head"] = pd.to_numeric(df["head"], errors="coerce")
    n_nan_head = df["head"].isna().sum()
    if n_nan_head > 0:
        print(f"ℹ️  {n_nan_head} NaN head values in '{file_path}' (kept, not dropped)")

    # Sort by Time and reset index
    df = df.sort_values(by="Time").reset_index(drop=True)

    return df

def remove_duplicate_and_fill_missing(df, freq="1h"):
    """
    Clean groundwater time series based solely on the datetime index.
    ...
    """

    df = df.copy()

    # 1. Ensure index is datetime
    df.index = pd.to_datetime(df.index, errors="coerce")
    df = df[~df.index.isna()]      # drop rows where index could not be parsed
    df = df.sort_index()
    
    # 1a. New line to set index 
    df.index.name = "Time" 
    
    # 2. Round index to nearest frequency
    df.index = df.index.round(freq)

    # 3. Remove duplicate timestamps and create 'head_raw'
    df = df[~df.index.duplicated(keep="first")]
    
    if "head" in df.columns:
        # Create a copy of the 'head' data before any gap filling occurs
        df["head_raw"] = df["head"].copy()
    else:
        print("Warning: 'head' column not found. 'head_raw' column will not be created.")


    before = len(df)

    # 4. Determine expected time step
    step = pd.to_timedelta(freq)

    missing_times = []
    idx = df.index
    
    # 5. Identify missing timestamps (The logic below still works fine)
    for prev, curr in zip(idx[:-1], idx[1:]):
        delta = curr - prev
        if delta > step:
            # number of missing steps between prev and curr
            n_missing = int(delta / step) - 1
            for k in range(1, n_missing + 1):
                missing_times.append(prev + k * step)

    # 6. Insert missing timestamps
    if missing_times:
        df_missing = pd.DataFrame(
            index=pd.DatetimeIndex(missing_times, name="Time") # Name set here too, just in case
        )
        # ensure all existing columns (including head_raw) exist with NaN
        for col in df.columns:
            df_missing[col] = np.nan
        df_missing["v0"] = False

        # add v0=True for existing rows
        df["v0"] = True

        df = pd.concat([df, df_missing]).sort_index()
        # The index name is preserved by pd.concat if it exists on both DataFrames
    else:
        df["v0"] = True   # existing rows only
        # ensure the name is set even if no gaps are found
        df.index.name = "Time" 

    changed = len(df) != before
    return df, changed


def flag_physical_bounds(df_in, hmin, hmax, vcol="v1"):
    """
    Flag head values that fall outside physically realistic bounds, and
    move flagged values from 'head' into a storage column (default 'v1').

    Logic:
    - If head < hmin or head > hmax → flagged.
    - For flagged rows:
        - store original head in v1 (if not already filled)
        - set head to NaN
    - Optionally keeps a boolean column 'Flag_PhysicalBounds' for diagnostics.

    Args:
        df (pd.DataFrame): DataFrame with 'Time' and 'head' columns.
        hmin (float): Minimum physically realistic head value.
        hmax (float): Maximum physically realistic head value.
        vcol (str): Name of the column to store flagged original values (default 'v1').

    Returns:
        tuple: (pd.DataFrame, bool)
            - pd.DataFrame: DataFrame with updated 'head', added v1 and 'Flag_PhysicalBounds'.
            - bool: True if any rows were flagged, False otherwise.
    """
    df_out = df_in.copy()

    # Create storage column if it doesn't exist yet
    if vcol not in df_out.columns:
        df_out[vcol] = np.nan

    # Mask of values outside physical bounds (ignore existing NaNs)
    mask_flagged = df_out["head"].notna() & ~((df_out["head"] >= hmin) & (df_out["head"] <= hmax))

    # Optional boolean flag column (can be dropped later if you don’t want it)
    df_out["Flag_PhysicalBounds"] = mask_flagged

    # Store original head in v1, but don't overwrite an existing stored value
    df_out.loc[mask_flagged & df_out[vcol].isna(), vcol] = df_out.loc[mask_flagged, "head"]

    # Set flagged head values to NaN (removed from modelling column)
    df_out.loc[mask_flagged, "head"] = np.nan

    # Boolean to see if a row is flagged or not 
    changed = mask_flagged.any()

    # Drop the flag column
    df_out = df_out.drop(columns=["Flag_PhysicalBounds"])

    return df_out, changed


def flag_unrealistic_step_change(df_in, max_up=0.3, max_down=-0.05, vcol="v2"):
    """
    Flags unrealistic step changes in groundwater head between consecutive timestamps.

    Logic:
    - Compute dH = difference in head between current and previous row.
    - If dH > max_up  (too large upward jump)  → flagged
    - If dH < max_down (too large downward jump) → flagged

    For flagged rows:
        - 'head' is set to NaN (filtered)
        - vcol (default 'v2') stores the original head value (the flagged value),
          but only if that cell in vcol is still NaN.

    For unflagged rows:
        - 'head' remains unchanged
        - vcol is unchanged (NaN if nothing else filled it earlier)

    Args:
        df (pd.DataFrame): Must contain 'Time' and 'head'.
        max_up (float): Maximum allowed upward change in meters.
        max_down (float): Maximum allowed downward change in meters (negative).
        vcol (str): Name of the validation column to store flagged values (default 'v2').

    Returns:
        tuple: (df_out, changed)
            df_out  = dataframe with added columns: dH and vcol
            changed = True if any rows were flagged
    """
    df_out = df_in.copy()

    # Difference in head (current - previous)
    df_out["dH"] = df_out["head"].diff()

    # Mask of unrealistic jumps
    mask_flagged = (df_out["dH"] > max_up) | (df_out["dH"] < max_down)

    # Ensure validation column exists
    if vcol not in df_out.columns:
        df_out[vcol] = np.nan

    # Store original head in vcol, but don't overwrite an existing stored value
    df_out.loc[mask_flagged & df_out[vcol].isna(), vcol] = df_out.loc[mask_flagged, "head"]

    # Set flagged head values to NaN (filtered out for modelling)
    df_out.loc[mask_flagged, "head"] = np.nan

    changed = mask_flagged.any()

    return df_out, changed

def flag_constant_head_runs(
    df_in,
    min_run_steps=96,
    band_upper_m=0.15,
    hmin_input=None,
    vcol="v3",
):
    """
    v3 flag: constant-head run detector.

    Flags head values that stay within [hmin, hmin + band_upper_m] for
    >= min_run_steps consecutive time steps.
    """
    df = df_in.copy()

    if vcol not in df.columns:
        df[vcol] = np.nan

    valid = df["head"].dropna()
    if valid.empty:
        return df, False

    hmin = float(hmin_input) if hmin_input is not None else float(valid.min())
    threshold = hmin + float(band_upper_m)

    in_band = df["head"].notna() & (df["head"] >= hmin) & (df["head"] <= threshold)

    group_id = (~in_band).cumsum()
    run_lengths = in_band.groupby(group_id).transform("sum")
    mask_flagged = in_band & (run_lengths >= min_run_steps)

    df.loc[mask_flagged & df[vcol].isna(), vcol] = df.loc[mask_flagged, "head"]
    df.loc[mask_flagged, "head"] = np.nan

    return df, mask_flagged.any()


def flag_constant_head_periods(
    df_in,
    tconst_steps=96,
    flat_margin_m=0.02,
    band_upper_m=0.10,
    hmin_input=None,
    vcol="v3",
):
    """Deprecated: use flag_constant_head_runs instead."""
    import warnings
    warnings.warn(
        "flag_constant_head_periods is deprecated; use flag_constant_head_runs",
        DeprecationWarning,
        stacklevel=2,
    )
    return flag_constant_head_runs(
        df_in, min_run_steps=tconst_steps,
        band_upper_m=band_upper_m, hmin_input=hmin_input, vcol=vcol,
    )


def flag_statistical_outliers(df_in, vcol="v4"):
    """
    Detects statistical outliers using the classical boxplot rule:
        Lower bound  = Q1 - 1.5 * IQR
        Upper bound  = Q3 + 1.5 * IQR
    Any 'head' value outside this interval is flagged as an outlier.
    
    For flagged rows:
        - original value is stored in vcol
        - head is set to NaN

    Args:
        df_in (pd.DataFrame): dataframe containing 'head'
        vcol (str): column name used to store outlier original values (default 'v4')

    Returns:
        (df_out, changed)
            df_out: updated dataframe
            changed: True if any outlier was found
    """

    df_out = df_in.copy()

    # Ensure validation column exists
    if vcol not in df_out.columns:
        df_out[vcol] = np.nan

    # Extract non-NaN head values for statistical analysis
    heads = df_out["head"].dropna()

    # Not enough data to compute boxplot stats
    if len(heads) < 4:
        return df_out, False

    # Compute quartiles and IQR
    Q1 = np.percentile(heads, 25)
    Q3 = np.percentile(heads, 75)
    IQR = Q3 - Q1

    # Boxplot thresholds
    lower_bound = Q1 - 1.5 * IQR
    upper_bound = Q3 + 1.5 * IQR

    # Find outliers (ignore NaNs)
    mask_flagged = df_out["head"].notna() & (
        (df_out["head"] < lower_bound) | 
        (df_out["head"] > upper_bound)
    )

    # Store original head in vcol (but don’t overwrite existing content)
    df_out.loc[mask_flagged & df_out[vcol].isna(), vcol] = df_out.loc[mask_flagged, "head"]

    # Remove flagged values from head
    df_out.loc[mask_flagged, "head"] = np.nan

    changed = mask_flagged.any()
    return df_out, changed
