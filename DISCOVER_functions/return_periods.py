from typing import Sequence
import numpy as np
import pandas as pd
from scipy.stats import genpareto, genextreme
from scipy.interpolate import interp1d


def _weibull_T(n: int) -> np.ndarray:
    k = np.arange(1, n + 1, dtype=float)
    P = k / (n + 1.0)
    return 1.0 / P


def _fit_min_models(mins: pd.Series):
    y = pd.to_numeric(mins, errors="coerce").dropna().values
    if y.size == 0:
        raise ValueError("No minima values to fit")

    z = -np.sort(y)  # flip; upper tail
    z0 = z.min()

    scale_exp = (z - z0).mean()

    def min_exp(T):
        f = np.minimum(1 - 1e-12, 1.0 / np.asarray(T, float))
        return -genpareto.ppf(1 - f, c=0.0, loc=z0, scale=scale_exp)

    c_gpd, loc_gpd, scale_gpd = genpareto.fit(z, floc=z0)

    def min_gpd(T):
        f = np.minimum(1 - 1e-12, 1.0 / np.asarray(T, float))
        return -genpareto.ppf(1 - f, c=c_gpd, loc=loc_gpd, scale=scale_gpd)

    c_gev, loc_gev, scale_gev = genextreme.fit(z)

    def min_gev(T):
        f = np.minimum(1 - 1e-12, 1.0 / np.asarray(T, float))
        return -genextreme.ppf(1 - f, c=c_gev, loc=loc_gev, scale=scale_gev)

    # empirical interpolant
    y_sort = np.sort(y)
    T_emp = _weibull_T(len(y_sort))
    emp_f = interp1d(np.log(T_emp), y_sort, fill_value="extrapolate")

    return min_exp, min_gpd, min_gev, (0.0, z0, scale_exp), (c_gpd, loc_gpd, scale_gpd), (c_gev, loc_gev, scale_gev), lambda T: emp_f(np.log(np.asarray(T, float)))


def _fit_max_models(maxs: pd.Series):
    y = pd.to_numeric(maxs, errors="coerce").dropna().values
    if y.size == 0:
        raise ValueError("No maxima values to fit")

    z = np.sort(y)[::-1]
    z0 = z.min()

    scale_exp = (z - z0).mean()

    def max_exp(T):
        f = np.minimum(1 - 1e-12, 1.0 / np.asarray(T, float))
        return genpareto.ppf(1 - f, c=0.0, loc=z0, scale=scale_exp)

    c_gpd, loc_gpd, scale_gpd = genpareto.fit(z, floc=z0)

    def max_gpd(T):
        f = np.minimum(1 - 1e-12, 1.0 / np.asarray(T, float))
        return genpareto.ppf(1 - f, c=c_gpd, loc=loc_gpd, scale=scale_gpd)

    c_gev, loc_gev, scale_gev = genextreme.fit(z)

    def max_gev(T):
        f = np.minimum(1 - 1e-12, 1.0 / np.asarray(T, float))
        return genextreme.ppf(1 - f, c=c_gev, loc=loc_gev, scale=scale_gev)

    # empirical interpolant
    y_sort = np.sort(y)[::-1]
    T_emp = _weibull_T(len(y_sort))
    emp_f = interp1d(np.log(T_emp), y_sort, fill_value="extrapolate")

    return max_exp, max_gpd, max_gev, (0.0, z0, scale_exp), (c_gpd, loc_gpd, scale_gpd), (c_gev, loc_gev, scale_gev), lambda T: emp_f(np.log(np.asarray(T, float)))


def heads_at_return_periods(df_annual_drought: pd.DataFrame,
                            df_annual_wet: pd.DataFrame,
                            T_list: Sequence[float] = (1.0, 3.0, 10.0, 30.0, 100.0, 300.0, 1000.0),
                            col_min: str = "head_min",
                            col_max: str = "head_max") -> pd.DataFrame:
    """Compute groundwater head at specified return periods using Exp/GPD/GEV and empirical.

    Returns DataFrame indexed by T with columns in the exact order:
    ['dr_exp','dr_gpd','dr_gev','dr_emp','wet_exp','wet_gpd','wet_gev','wet_emp']
    """
    if col_min not in df_annual_drought.columns:
        raise KeyError(f"'{col_min}' not in df_annual_drought")
    if col_max not in df_annual_wet.columns:
        raise KeyError(f"'{col_max}' not in df_annual_wet")

    mins = df_annual_drought[col_min]
    maxs = df_annual_wet[col_max]

    min_exp, min_gpd, min_gev, _, _, _, min_emp_f = _fit_min_models(mins)
    max_exp, max_gpd, max_gev, _, _, _, max_emp_f = _fit_max_models(maxs)

    T_arr = np.asarray(list(T_list), float)

    out = pd.DataFrame(index=T_arr, columns=[
        "dr_exp","dr_gpd","dr_gev","dr_emp",
        "wet_exp","wet_gpd","wet_gev","wet_emp"
    ], dtype=float)

    out["dr_exp"] = min_exp(T_arr)
    out["dr_gpd"] = min_gpd(T_arr)
    out["dr_gev"] = min_gev(T_arr)
    out["dr_emp"] = min_emp_f(T_arr)

    out["wet_exp"] = max_exp(T_arr)
    out["wet_gpd"] = max_gpd(T_arr)
    out["wet_gev"] = max_gev(T_arr)
    out["wet_emp"] = max_emp_f(T_arr)

    out.index.name = "Return period [years]"
    # enforce exact index order and column order
    out = out.loc[[float(x) for x in T_list], ["dr_exp","dr_gpd","dr_gev","dr_emp","wet_exp","wet_gpd","wet_gev","wet_emp"]]
    return out
