from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from ev_fleet.config import load_config
from ev_fleet.pipeline import run_pipeline


@pytest.fixture(scope="session")
def cfg():
    root = Path(__file__).resolve().parents[1]
    return load_config(root / "configs" / "smoke.yaml")


@pytest.fixture(scope="session")
def pipeline_summary(cfg):
    return run_pipeline(cfg)


@pytest.fixture(scope="session")
def scenarios(cfg, pipeline_summary):
    return pd.read_csv(cfg.generated_dir / "scenario_routes.csv")
