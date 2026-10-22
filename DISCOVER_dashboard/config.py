"""App configuration loader.

Reads ``config/settings.yaml`` if present, otherwise falls back to sensible
defaults. The loader is tolerant: a missing file or a missing ``pyyaml`` import
does not break the app.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dataval._utils import find_repo_root

DEFAULTS = {
    "base_data_dir": "output_data",
    "validation_store_dir": "validation_data",
    "stressor_dir": "input_stressors",
    "notes_max_len": 30,
    "round_freq": "1h",
}


@dataclass
class Settings:
    repo_root: Path
    base_data_dir: Path
    validation_store_dir: Path
    stressor_dir: Path
    notes_max_len: int
    round_freq: str


def load_settings() -> Settings:
    repo_root = find_repo_root()
    values = dict(DEFAULTS)

    config_path = repo_root / "config" / "settings.yaml"
    if config_path.exists():
        try:
            import yaml  # type: ignore

            with open(config_path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            if isinstance(loaded, dict):
                values.update({k: v for k, v in loaded.items() if v is not None})
        except Exception:
            # Malformed config or missing pyyaml -> stick with defaults.
            pass

    base_data_dir = Path(values["base_data_dir"])
    if not base_data_dir.is_absolute():
        base_data_dir = repo_root / base_data_dir

    validation_store_dir = Path(values["validation_store_dir"])
    if not validation_store_dir.is_absolute():
        validation_store_dir = repo_root / validation_store_dir

    stressor_dir = Path(values["stressor_dir"])
    if not stressor_dir.is_absolute():
        stressor_dir = repo_root / stressor_dir

    return Settings(
        repo_root=repo_root,
        base_data_dir=base_data_dir,
        validation_store_dir=validation_store_dir,
        stressor_dir=stressor_dir,
        notes_max_len=int(values["notes_max_len"]),
        round_freq=str(values["round_freq"]),
    )
