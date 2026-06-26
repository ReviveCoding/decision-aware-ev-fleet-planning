from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from ev_fleet.data_generation import generate_public_informed_proxy_data
from ev_fleet.sql_pipeline import build_sql_marts, read_mart


def test_generator_is_reproducible(cfg):
    generate_public_informed_proxy_data(cfg)
    first = pd.read_csv(cfg.generated_dir / "station_day_history.csv")
    generate_public_informed_proxy_data(cfg)
    second = pd.read_csv(cfg.generated_dir / "station_day_history.csv")
    pd.testing.assert_frame_equal(first, second)


def test_sql_marts_and_reconciliation(cfg):
    generate_public_informed_proxy_data(cfg)
    build_sql_marts(cfg)
    mart = read_mart(cfg, "mart_station_day_features")
    assert len(mart) > 100
    con = sqlite3.connect(cfg.database_path)
    try:
        rec = pd.read_sql_query("SELECT * FROM reconciliation_fleet", con)
    finally:
        con.close()
    assert rec.nonnegative_check.eq(1).all()
    assert rec.parking_capacity_check.eq(1).all()

    station = mart["station_id"].iloc[0]
    station_rows = mart[mart["station_id"] == station].sort_values("date").reset_index(drop=True)
    assert pd.isna(station_rows.loc[0, "route_count_lag_1"])
    assert station_rows.loc[1, "route_count_lag_1"] == station_rows.loc[0, "route_count"]
    assert station_rows.loc[1, "route_count_rolling_7"] == station_rows.loc[0, "route_count"]


def test_generated_data_contract_requires_operational_capacity_fields(cfg, pipeline_summary, tmp_path):
    import copy
    import shutil

    import pytest

    from ev_fleet.config import ProjectConfig
    from ev_fleet.contracts import DataContractError, validate_generated_data

    generated = tmp_path / "generated"
    shutil.copytree(cfg.generated_dir, generated)
    stations = pd.read_csv(generated / "stations.csv").drop(columns="parking_capacity")
    stations.to_csv(generated / "stations.csv", index=False)
    raw = copy.deepcopy(cfg.raw)
    raw["paths"] = {
        "generated_data": str(generated),
        "artifacts": str(tmp_path / "artifacts"),
        "database": str(tmp_path / "artifacts" / "test.sqlite"),
    }
    isolated = ProjectConfig(raw=raw, source_path=cfg.source_path, project_root=tmp_path)
    with pytest.raises(DataContractError, match="parking_capacity"):
        validate_generated_data(isolated)


def test_generated_data_contract_rejects_negative_cost_and_parking(cfg, pipeline_summary, tmp_path):
    import copy
    import shutil

    import pytest

    from ev_fleet.config import ProjectConfig
    from ev_fleet.contracts import DataContractError, validate_generated_data

    generated = tmp_path / "generated"
    shutil.copytree(cfg.generated_dir, generated)
    specs = pd.read_csv(generated / "vehicle_specs.csv")
    specs.loc[0, "variable_cost_per_mile"] = -1.0
    specs.to_csv(generated / "vehicle_specs.csv", index=False)
    raw = copy.deepcopy(cfg.raw)
    raw["paths"] = {
        "generated_data": str(generated),
        "artifacts": str(tmp_path / "artifacts"),
        "database": str(tmp_path / "artifacts" / "test.sqlite"),
    }
    isolated = ProjectConfig(raw=raw, source_path=cfg.source_path, project_root=tmp_path)
    with pytest.raises(DataContractError, match="variable_cost_per_mile"):
        validate_generated_data(isolated)

    specs.loc[0, "variable_cost_per_mile"] = 0.5
    specs.to_csv(generated / "vehicle_specs.csv", index=False)
    stations = pd.read_csv(generated / "stations.csv")
    stations.loc[0, "parking_capacity"] = -1
    stations.to_csv(generated / "stations.csv", index=False)
    with pytest.raises(DataContractError, match="parking_capacity"):
        validate_generated_data(isolated)


def test_data_contract_rejects_infinite_numeric_and_duplicate_parsed_dates(cfg):
    from ev_fleet.contracts import DataContractError, validate_generated_data

    specs_path = cfg.generated_dir / "vehicle_specs.csv"
    specs = pd.read_csv(specs_path)
    original_specs = specs.copy()
    specs.loc[0, "daily_fixed_cost"] = float("inf")
    specs.to_csv(specs_path, index=False)
    with pytest.raises(DataContractError, match="finite"):
        validate_generated_data(cfg)
    original_specs.to_csv(specs_path, index=False)

    history_path = cfg.generated_dir / "station_day_history.csv"
    history = pd.read_csv(history_path)
    duplicate = history.iloc[[0]].copy()
    duplicate["date"] = pd.to_datetime(duplicate["date"]).dt.strftime("%m/%d/%Y")
    pd.concat([history, duplicate], ignore_index=True).to_csv(history_path, index=False)
    with pytest.raises(DataContractError, match="duplicate parsed"):
        validate_generated_data(cfg)
    history.to_csv(history_path, index=False)
