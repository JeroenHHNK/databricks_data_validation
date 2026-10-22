from pathlib import Path
from typing import Optional
import pandas as pd

from .load_model import load_model


def simulate_model(model_path: Path,
                   prec: pd.Series,
                   evap: pd.Series,
                   p: Optional[dict] = None,
                   tmin: str = "1906",
                   tmax: str = "2015",
                   freq: str = "D",
                   warmup: int = 3650,
                   return_warmup: bool = False):
    """Load Pastas model and simulate using supplied precipitation and evaporation.

    The function attempts to locate a recharge-like stressmodel and replace its
    input series. If no obvious match is found, it will try to replace the
    first two stress series in the model.

    Returns the simulated Series (pandas.Series).
    """
    ml, _ = load_model(model_path, return_params=False)

    # Try to find a stressmodel named 'recharge' or similar
    sm_name = None
    for name in ml.stressmodels.keys():
        lname = name.lower()
        if "recharge" in lname or "rech" in lname or "tarso" in lname:
            sm_name = name
            break

    if sm_name is not None:
        sm = ml.stressmodels[sm_name]
        # Expect two stress series: precipitation, evaporation
        if len(sm.stress) >= 1:
            sm.stress[0].series_original = prec
        if len(sm.stress) >= 2:
            sm.stress[1].series_original = evap
    else:
        # Fallback: replace first two stressmodels' first two series
        try:
            # iterate stressmodels and assign series where possible
            assigned = 0
            for sm in ml.stressmodels.values():
                for i in range(len(sm.stress)):
                    if assigned == 0:
                        sm.stress[i].series_original = prec
                        assigned += 1
                    elif assigned == 1:
                        sm.stress[i].series_original = evap
                        assigned += 1
                    if assigned >= 2:
                        break
                if assigned >= 2:
                    break
        except Exception:
            raise RuntimeError("Failed to assign forcing series to model stressmodels")

    simulated = ml.simulate(p=p, tmin=tmin, tmax=tmax, freq=freq, warmup=warmup, return_warmup=return_warmup)

    # Ensure a Series (if DataFrame, take first column)
    if isinstance(simulated, pd.DataFrame):
        simulated = simulated.iloc[:, 0]

    simulated.name = simulated.name or "Simulated head"
    return simulated
