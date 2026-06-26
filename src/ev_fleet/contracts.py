from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .config import ProjectConfig


class DataContractError(ValueError):
    pass


def _require_columns(frame: pd.DataFrame, required: set[str], name: str) -> None:
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise DataContractError(f"{name} missing columns: {missing}")
    if frame.empty:
        raise DataContractError(f"{name} must not be empty")


def _require_nonempty_strings(frame: pd.DataFrame, columns: list[str], name: str) -> None:
    for column in columns:
        values = frame[column]
        if values.isna().any() or values.astype(str).str.strip().eq("").any():
            raise DataContractError(f"{name}.{column} must contain non-empty strings")


def validate_generated_data(cfg: ProjectConfig) -> dict[str, int]:
    specs = pd.read_csv(cfg.generated_dir / "vehicle_specs.csv")
    stations = pd.read_csv(cfg.generated_dir / "stations.csv")
    fleet = pd.read_csv(cfg.generated_dir / "initial_fleet.csv")
    history = pd.read_csv(cfg.generated_dir / "station_day_history.csv")

    _require_columns(
        specs,
        {
            "vehicle_type",
            "cube_capacity",
            "battery_kwh",
            "usable_fraction",
            "reserve_kwh",
            "charge_power_kw",
            "daily_fixed_cost",
            "variable_cost_per_mile",
        },
        "vehicle_specs",
    )
    _require_columns(
        stations,
        {
            "station_id",
            "charger_ports",
            "charger_power_kw",
            "site_power_kw",
            "mean_dwell_hours",
            "repair_capacity",
            "parking_capacity",
        },
        "stations",
    )
    _require_columns(fleet, {"station_id", "vehicle_type", "initial_count"}, "initial_fleet")
    _require_columns(
        history,
        {
            "date",
            "station_id",
            "route_count",
            "avg_cube",
            "avg_distance_miles",
            "avg_route_energy_kwh",
            "cold_regime",
            "capacity_event",
        },
        "station_day_history",
    )

    _require_nonempty_strings(specs, ["vehicle_type"], "vehicle_specs")
    _require_nonempty_strings(stations, ["station_id"], "stations")
    _require_nonempty_strings(fleet, ["station_id", "vehicle_type"], "initial_fleet")
    _require_nonempty_strings(history, ["station_id"], "station_day_history")

    if specs["vehicle_type"].duplicated().any():
        raise DataContractError("vehicle_specs.vehicle_type must be unique")
    if stations["station_id"].duplicated().any():
        raise DataContractError("stations.station_id must be unique")
    if fleet.duplicated(["station_id", "vehicle_type"]).any():
        raise DataContractError("initial_fleet station_id/vehicle_type pairs must be unique")
    if history.duplicated(["date", "station_id"]).any():
        raise DataContractError("station_day_history date/station_id pairs must be unique")

    numeric_nonnegative = [
        (
            specs,
            [
                "cube_capacity",
                "battery_kwh",
                "reserve_kwh",
                "charge_power_kw",
                "daily_fixed_cost",
                "variable_cost_per_mile",
            ],
            "vehicle_specs",
        ),
        (
            stations,
            [
                "charger_ports",
                "charger_power_kw",
                "site_power_kw",
                "mean_dwell_hours",
                "repair_capacity",
                "parking_capacity",
            ],
            "stations",
        ),
        (fleet, ["initial_count"], "initial_fleet"),
        (history, ["route_count", "avg_cube", "avg_distance_miles", "avg_route_energy_kwh"], "station_day_history"),
    ]
    for frame, columns, name in numeric_nonnegative:
        for column in columns:
            values = pd.to_numeric(frame[column], errors="coerce")
            if values.isna().any() or not np.isfinite(values).all() or (values < 0).any():
                raise DataContractError(f"{name}.{column} must be finite, numeric, and nonnegative")

    usable_fraction = pd.to_numeric(specs["usable_fraction"], errors="coerce")
    if usable_fraction.isna().any() or not usable_fraction.between(0.0, 1.0, inclusive="right").all():
        raise DataContractError("vehicle_specs.usable_fraction must be in (0, 1]")
    usable_energy = pd.to_numeric(specs["battery_kwh"], errors="coerce") * usable_fraction
    if (pd.to_numeric(specs["reserve_kwh"], errors="coerce") >= usable_energy).any():
        raise DataContractError("vehicle_specs.reserve_kwh must be smaller than usable battery energy")

    integer_fields = [
        (stations, ["charger_ports", "repair_capacity", "parking_capacity"], "stations"),
        (fleet, ["initial_count"], "initial_fleet"),
        (history, ["route_count", "cold_regime", "capacity_event"], "station_day_history"),
    ]
    for frame, columns, name in integer_fields:
        for column in columns:
            values = pd.to_numeric(frame[column], errors="coerce")
            if values.isna().any() or not np.allclose(values, np.round(values)):
                raise DataContractError(f"{name}.{column} must contain integers")
    for column in ("cold_regime", "capacity_event"):
        if not history[column].isin([0, 1]).all():
            raise DataContractError(f"station_day_history.{column} must be binary")
    parsed_dates = pd.to_datetime(history["date"], errors="coerce", format="mixed")
    if parsed_dates.isna().any():
        raise DataContractError("station_day_history.date must be parseable")
    normalized_keys = pd.DataFrame(
        {"date": parsed_dates.dt.normalize(), "station_id": history["station_id"].astype(str)}
    )
    if normalized_keys.duplicated(["date", "station_id"]).any():
        raise DataContractError("station_day_history contains duplicate parsed date/station_id pairs")

    station_set = set(stations["station_id"].astype(str))
    type_set = set(specs["vehicle_type"].astype(str))
    if not set(fleet["station_id"].astype(str)).issubset(station_set):
        raise DataContractError("initial_fleet contains unknown station_id")
    if not set(fleet["vehicle_type"].astype(str)).issubset(type_set):
        raise DataContractError("initial_fleet contains unknown vehicle_type")
    if not set(history["station_id"].astype(str)).issubset(station_set):
        raise DataContractError("station_day_history contains unknown station_id")

    expected_pairs = {(station, vehicle_type) for station in station_set for vehicle_type in type_set}
    observed_pairs = set(zip(fleet["station_id"].astype(str), fleet["vehicle_type"].astype(str), strict=False))
    if observed_pairs != expected_pairs:
        missing = sorted(expected_pairs - observed_pairs)
        raise DataContractError(f"initial_fleet must contain every station/vehicle pair; missing={missing[:5]}")

    fleet_totals = fleet.groupby("station_id")["initial_count"].sum()
    parking = stations.set_index("station_id")["parking_capacity"]
    overloaded = {
        str(station_id): int(total - parking.loc[station_id])
        for station_id, total in fleet_totals.items()
        if total > parking.loc[station_id]
    }
    if overloaded:
        raise DataContractError(f"initial_fleet exceeds station parking capacity: {overloaded}")

    return {
        "vehicle_types": len(specs),
        "stations": len(stations),
        "fleet_rows": len(fleet),
        "history_rows": len(history),
    }


def validate_scenarios(frame: pd.DataFrame, known_station_ids: set[str] | None = None) -> dict[str, int]:
    _require_columns(
        frame,
        {
            "scenario_id",
            "scenario_weight",
            "route_id",
            "station_id",
            "required_cube",
            "distance_miles",
            "energy_kwh",
            "criticality",
            "charger_capacity_factor",
            "vehicle_failure_multiplier",
            "return_delay_minutes",
            "regime",
            "cold_regime",
            "charger_outage_regime",
        },
        "scenario_routes",
    )
    _require_nonempty_strings(
        frame,
        ["scenario_id", "route_id", "station_id", "criticality", "regime"],
        "scenario_routes",
    )
    scenario_ids = pd.to_numeric(frame["scenario_id"], errors="coerce")
    if scenario_ids.isna().any() or (scenario_ids < 0).any() or not np.allclose(scenario_ids, np.round(scenario_ids)):
        raise DataContractError("scenario_id must contain nonnegative integers")
    if frame["route_id"].duplicated().any():
        raise DataContractError("scenario route_id values must be globally unique")
    if known_station_ids is not None:
        unknown = sorted(set(frame["station_id"].astype(str)) - {str(value) for value in known_station_ids})
        if unknown:
            raise DataContractError(f"scenario_routes contains unknown station_id values: {unknown[:5]}")
    if not frame["criticality"].isin(["critical", "standard"]).all():
        raise DataContractError("criticality must be critical or standard")
    for column in ("required_cube", "distance_miles", "energy_kwh"):
        values = pd.to_numeric(frame[column], errors="coerce")
        if values.isna().any() or not np.isfinite(values).all() or (values <= 0).any():
            raise DataContractError(f"scenario_routes.{column} must be finite and positive")
    factors = pd.to_numeric(frame["charger_capacity_factor"], errors="coerce")
    if factors.isna().any() or not factors.between(0.0, 1.0, inclusive="right").all():
        raise DataContractError("charger_capacity_factor must be in (0, 1]")
    failure_multiplier = pd.to_numeric(frame["vehicle_failure_multiplier"], errors="coerce")
    if failure_multiplier.isna().any() or not np.isfinite(failure_multiplier).all() or (failure_multiplier <= 0).any():
        raise DataContractError("vehicle_failure_multiplier must be finite and positive")
    return_delay = pd.to_numeric(frame["return_delay_minutes"], errors="coerce")
    if return_delay.isna().any() or not np.isfinite(return_delay).all() or (return_delay < 0).any():
        raise DataContractError("return_delay_minutes must be finite and nonnegative")
    for column in ("cold_regime", "charger_outage_regime"):
        if not frame[column].isin([0, 1]).all():
            raise DataContractError(f"{column} must be binary")
    if frame["regime"].isna().any() or frame["regime"].astype(str).str.len().eq(0).any():
        raise DataContractError("regime must be non-empty")

    scenario_weight = pd.to_numeric(frame["scenario_weight"], errors="coerce")
    if scenario_weight.isna().any():
        raise DataContractError("scenario_weight must be numeric")
    per_scenario = frame.assign(scenario_weight=scenario_weight).groupby("scenario_id")["scenario_weight"].first()
    if per_scenario.empty or (per_scenario <= 0).any() or not np.isclose(per_scenario.sum(), 1.0, atol=1e-6):
        raise DataContractError("scenario weights must be positive and sum to 1")
    if frame.groupby("scenario_id")["scenario_weight"].nunique().max() != 1:
        raise DataContractError("scenario_weight must be constant within each scenario")
    for column in ("regime", "cold_regime", "charger_outage_regime", "vehicle_failure_multiplier"):
        if frame.groupby("scenario_id")[column].nunique(dropna=False).max() != 1:
            raise DataContractError(f"{column} must be constant within each scenario")
    for column in ("charger_capacity_factor", "return_delay_minutes"):
        if frame.groupby(["scenario_id", "station_id"])[column].nunique(dropna=False).max() != 1:
            raise DataContractError(f"{column} must be constant within each scenario/station pair")
    return {"scenarios": int(frame["scenario_id"].nunique()), "routes": len(frame)}


def validate_required_artifacts(artifacts_dir: str | Path) -> list[str]:
    root = Path(artifacts_dir)
    required = [
        "run_summary.json",
        "generated_data_manifest.json",
        "forecast_model_comparison.csv",
        "deterministic_plan.json",
        "risk_neutral_plan.json",
        "stochastic_plan.json",
        "base_policy_selection.csv",
        "corrective_frontier.csv",
        "recommended_plan.json",
        "recommended_rcce.json",
        "paired_policy_comparison.csv",
        "saa_runs.csv",
        "reports/executive_decision_memo.md",
    ]
    missing = [name for name in required if not (root / name).exists()]
    if missing:
        raise DataContractError(f"Missing required artifacts: {missing}")
    return required
