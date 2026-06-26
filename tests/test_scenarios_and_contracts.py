from __future__ import annotations

import pandas as pd
import pytest

from ev_fleet.contracts import DataContractError, validate_scenarios
from ev_fleet.optimization import build_expected_value_scenario, load_plan


def test_expected_value_workload_is_synthesized_not_first_sample(scenarios):
    expected = build_expected_value_scenario(scenarios)
    first = scenarios[scenarios["scenario_id"] == scenarios["scenario_id"].min()]
    assert expected["route_id"].str.startswith("EV_R").all()
    assert expected["regime"].eq("expected_value").all()
    assert set(expected["scenario_id"]) == {0}
    assert (
        not expected[["required_cube", "energy_kwh"]]
        .reset_index(drop=True)
        .equals(first[["required_cube", "energy_kwh"]].head(len(expected)).reset_index(drop=True))
    )


def test_expected_value_workload_preserves_joint_observed_route_profiles(scenarios):
    expected = build_expected_value_scenario(scenarios)
    profile_columns = ["required_cube", "distance_miles", "energy_kwh"]
    for station_id, group in expected.groupby("station_id"):
        observed_profiles = {
            tuple(round(float(value), 8) for value in row)
            for row in scenarios.loc[scenarios["station_id"].eq(station_id), profile_columns].to_numpy()
        }
        expected_profiles = {
            tuple(round(float(value), 8) for value in row) for row in group[profile_columns].to_numpy()
        }
        assert expected_profiles.issubset(observed_profiles)


def test_scenario_contract_rejects_invalid_weights(scenarios):
    broken = scenarios.copy()
    broken.loc[broken["scenario_id"] == broken["scenario_id"].min(), "scenario_weight"] = 0.0
    with pytest.raises(DataContractError, match="weights"):
        validate_scenarios(broken)


def test_emergency_recourse_is_bounded(cfg, pipeline_summary):
    cap = int(cfg.raw["optimization"]["max_emergency_leases_per_station"])
    stations = int(cfg.raw["history"]["stations"])
    for filename in ("risk_neutral_plan.json", "stochastic_plan.json"):
        plan = load_plan(cfg.artifacts_dir / filename)
        assert (plan.scenario_metrics["emergency_leases"] <= cap * stations).all()


def test_parking_and_fleet_conservation(cfg, pipeline_summary):
    stations = pd.read_csv(cfg.generated_dir / "stations.csv").set_index("station_id")
    plan = load_plan(cfg.artifacts_dir / "stochastic_plan.json")
    assert plan.allocation["planned_count"].sum() == plan.allocation["initial_count"].sum()
    totals = plan.allocation.groupby("station_id")["planned_count"].sum()
    for station_id, count in totals.items():
        assert count <= stations.loc[station_id, "parking_capacity"]


def test_scenario_contract_rejects_invalid_failure_and_delay(scenarios):
    invalid_failure = scenarios.copy()
    invalid_failure.loc[invalid_failure.index[0], "vehicle_failure_multiplier"] = 0.0
    with pytest.raises(DataContractError, match="vehicle_failure_multiplier"):
        validate_scenarios(invalid_failure)

    invalid_delay = scenarios.copy()
    invalid_delay.loc[invalid_delay.index[0], "return_delay_minutes"] = -1.0
    with pytest.raises(DataContractError, match="return_delay_minutes"):
        validate_scenarios(invalid_delay)


def test_scenario_contract_rejects_unknown_station_and_inconsistent_regime(scenarios):
    known = set(scenarios["station_id"].astype(str))
    unknown_station = scenarios.copy()
    unknown_station.loc[unknown_station.index[0], "station_id"] = "UNKNOWN"
    with pytest.raises(DataContractError, match="unknown station_id"):
        validate_scenarios(unknown_station, known_station_ids=known)

    inconsistent = scenarios.copy()
    scenario_id = inconsistent["scenario_id"].iloc[0]
    scenario_rows = inconsistent.index[inconsistent["scenario_id"].eq(scenario_id)]
    inconsistent.loc[scenario_rows[0], "regime"] = "inconsistent"
    with pytest.raises(DataContractError, match="regime must be constant"):
        validate_scenarios(inconsistent)

    non_integer = scenarios.copy()
    non_integer["scenario_id"] = non_integer["scenario_id"].astype(float)
    non_integer.loc[non_integer.index[0], "scenario_id"] = 0.5
    with pytest.raises(DataContractError, match="scenario_id must contain nonnegative integers"):
        validate_scenarios(non_integer)
