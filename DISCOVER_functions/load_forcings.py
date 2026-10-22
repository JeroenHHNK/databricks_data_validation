from pathlib import Path
from typing import Tuple
import pandas as pd


def load_forcings(stressor_dir: Path, prec_name: str = "precipitation_long_clean.csv", evap_name: str = "evaporation_long_clean.csv") -> Tuple[pd.Series, pd.Series]:
    """Load default precipitation and evaporation series from stressor folder.

    Args:
        stressor_dir: Path to directory containing forcing CSVs.
        prec_name: filename for precipitation (defaults to precipitation_long_clean.csv).
        evap_name: filename for evaporation (defaults to evaporation_long_clean.csv).

    Returns:
        (prec_series, evap_series) as pandas Series indexed by DatetimeIndex.

    Raises:
        FileNotFoundError if files are missing, or ValueError if required columns are absent.
    """
    stressor_dir = Path(stressor_dir)
    prec_path = stressor_dir / prec_name
    evap_path = stressor_dir / evap_name

    if not prec_path.exists():
        raise FileNotFoundError(f"Precipitation file not found: {prec_path}")
    if not evap_path.exists():
        raise FileNotFoundError(f"Evaporation file not found: {evap_path}")

    df_prec = pd.read_csv(prec_path, index_col=0, parse_dates=True)
    df_evap = pd.read_csv(evap_path, index_col=0, parse_dates=True)

    # Try to find sensible column names (common names in repo)
    prec_col_candidates = [c for c in df_prec.columns if "prec" in c.lower() or "rain" in c.lower()]
    evap_col_candidates = [c for c in df_evap.columns if "evap" in c.lower() or "et" in c.lower()]

    if not prec_col_candidates and df_prec.shape[1] == 1:
        prec_col = df_prec.columns[0]
    elif prec_col_candidates:
        prec_col = prec_col_candidates[0]
    else:
        raise ValueError(f"No precipitation column found in {prec_path}")

    if not evap_col_candidates and df_evap.shape[1] == 1:
        evap_col = df_evap.columns[0]
    elif evap_col_candidates:
        evap_col = evap_col_candidates[0]
    else:
        raise ValueError(f"No evaporation column found in {evap_path}")

    prec = pd.to_numeric(df_prec[prec_col], errors="coerce").dropna()
    evap = pd.to_numeric(df_evap[evap_col], errors="coerce").dropna()

    prec.name = "precipitation"
    evap.name = "evaporation"

    return prec, evap
