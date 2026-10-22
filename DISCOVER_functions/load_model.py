from pathlib import Path
from typing import Tuple, Optional, Any
import pastas as ps


def load_model(model_path: Path, return_params: bool = False) -> Tuple[Any, Optional[Any]]:
    """Load a Pastas model from disk.

    Args:
        model_path: Path to a .pas model file (or other Pastas-supported file).
        return_params: If True, also return solved parameters (ml.get_parameters()).

    Returns:
        (ml, params) tuple where params is None if return_params is False.
        Note: parameter return type is annotated as `Any` to remain compatible
        with different Pastas versions (avoid importing internal symbols).

    Raises:
        FileNotFoundError: if model_path does not exist.
        RuntimeError: if Pastas fails to load the model.
    """
    model_path = Path(model_path)
    if not model_path.exists():
        raise FileNotFoundError(f"Model not found: {model_path}")

    try:
        ml = ps.io.load(model_path)
    except Exception as exc:
        raise RuntimeError(f"Failed to load Pastas model: {exc}")

    params = None
    if return_params:
        try:
            params = ml.get_parameters()
        except Exception:
            params = None

    return ml, params
