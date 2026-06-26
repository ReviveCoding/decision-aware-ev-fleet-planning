from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix

from .config import ProjectConfig
from .io_utils import write_json_atomic

_STATUS_CLASS = {
    0: "OPTIMAL",
    1: "LIMIT_REACHED",
    2: "INFEASIBLE",
    3: "UNBOUNDED",
    4: "SOLVER_ERROR",
}


def _finite_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if np.isfinite(numeric) else None


def _accepted_plan_status(status_code: int, success: bool) -> str | None:
    termination_class = _STATUS_CLASS.get(int(status_code), "UNKNOWN")
    if termination_class == "OPTIMAL" and success:
        return "OPTIMAL"
    if termination_class == "LIMIT_REACHED":
        return "FEASIBLE_LIMIT"
    return None


def _maximum_positive_violation(values: np.ndarray) -> float:
    finite = values[np.isfinite(values)]
    return float(np.max(finite)) if finite.size else 0.0


def _solution_diagnostics(
    solution: np.ndarray,
    objective_vector: np.ndarray,
    integrality: np.ndarray,
    lower_bounds: np.ndarray,
    upper_bounds: np.ndarray,
    constraint_matrix,
    constraint_lower: np.ndarray,
    constraint_upper: np.ndarray,
    reported_objective: Any,
) -> dict[str, Any]:
    expected_shape = objective_vector.shape
    finite_solution = solution.shape == expected_shape and bool(np.isfinite(solution).all())
    if not finite_solution:
        return {
            "finite_solution": False,
            "max_bound_violation": float("inf"),
            "max_constraint_violation": float("inf"),
            "max_integrality_violation": float("inf"),
            "objective_reconciliation_error": float("inf"),
            "recomputed_objective": None,
        }

    lower_violation = np.where(np.isfinite(lower_bounds), lower_bounds - solution, -np.inf)
    upper_violation = np.where(np.isfinite(upper_bounds), solution - upper_bounds, -np.inf)
    max_bound_violation = max(
        0.0,
        _maximum_positive_violation(lower_violation),
        _maximum_positive_violation(upper_violation),
    )

    constraint_values = np.asarray(constraint_matrix @ solution, dtype=float)
    lower_constraint_violation = np.where(np.isfinite(constraint_lower), constraint_lower - constraint_values, -np.inf)
    upper_constraint_violation = np.where(np.isfinite(constraint_upper), constraint_values - constraint_upper, -np.inf)
    max_constraint_violation = max(
        0.0,
        _maximum_positive_violation(lower_constraint_violation),
        _maximum_positive_violation(upper_constraint_violation),
    )

    integer_mask = integrality != 0
    max_integrality_violation = (
        float(np.max(np.abs(solution[integer_mask] - np.rint(solution[integer_mask])))) if integer_mask.any() else 0.0
    )
    recomputed_objective = float(objective_vector @ solution)
    reported = _finite_or_none(reported_objective)
    if reported is None:
        objective_error = float("inf")
    else:
        scale = max(1.0, abs(reported), abs(recomputed_objective))
        objective_error = abs(reported - recomputed_objective) / scale
    return {
        "finite_solution": True,
        "max_bound_violation": max_bound_violation,
        "max_constraint_violation": max_constraint_violation,
        "max_integrality_violation": max_integrality_violation,
        "objective_reconciliation_error": float(objective_error),
        "recomputed_objective": recomputed_objective,
    }


@dataclass
class PlanResult:
    name: str
    status: str
    objective: float | None
    allocation: pd.DataFrame
    moves: pd.DataFrame
    scenario_metrics: pd.DataFrame
    solver: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "objective": self.objective,
            "allocation": self.allocation.to_dict(orient="records"),
            "moves": self.moves.to_dict(orient="records"),
            "scenario_metrics": self.scenario_metrics.to_dict(orient="records"),
            "solver": self.solver,
        }


class _Vars:
    def __init__(self) -> None:
        self.names: list[tuple] = []
        self.index: dict[tuple, int] = {}
        self.lb: list[float] = []
        self.ub: list[float] = []
        self.integrality: list[int] = []
        self.cost: list[float] = []

    def add(self, key: tuple, lb: float = 0.0, ub: float = np.inf, integrality: int = 0, cost: float = 0.0) -> int:
        if key in self.index:
            raise KeyError(f"Duplicate decision variable: {key}")
        index = len(self.names)
        self.names.append(key)
        self.index[key] = index
        self.lb.append(float(lb))
        self.ub.append(float(ub))
        self.integrality.append(int(integrality))
        self.cost.append(float(cost))
        return index

    def get(self, key: tuple) -> int:
        return self.index[key]


class _Constraints:
    def __init__(self, nvars_getter) -> None:
        self.rows: list[dict[int, float]] = []
        self.lower: list[float] = []
        self.upper: list[float] = []
        self.nvars_getter = nvars_getter

    def add(self, coeffs: dict[int, float], lower: float = -np.inf, upper: float = np.inf) -> None:
        self.rows.append(coeffs)
        self.lower.append(float(lower))
        self.upper.append(float(upper))

    def matrix(self):
        matrix = lil_matrix((len(self.rows), self.nvars_getter()), dtype=float)
        for row_index, coefficients in enumerate(self.rows):
            for column_index, value in coefficients.items():
                matrix[row_index, column_index] = value
        return matrix.tocsr()


def _compatible(
    route: pd.Series,
    spec: pd.Series,
    station: pd.Series | None = None,
    *,
    charge_taper_factor: float = 0.90,
    charge_efficiency: float = 0.92,
) -> bool:
    """Return whether one vehicle can serve and replenish a route.

    Battery energy and payload are hard physical limits. When station data are
    available, the recurring daily plan also requires the vehicle to replenish
    the route energy during the post-return dwell window. Aggregate site/port
    capacity is enforced separately by the station charging constraint.
    """
    usable = float(spec.battery_kwh) * float(spec.usable_fraction) - float(spec.reserve_kwh)
    if float(spec.cube_capacity) < float(route.required_cube) or usable < float(route.energy_kwh):
        return False
    if station is None:
        return True
    return_delay_hours = float(route.get("return_delay_minutes", 0.0)) / 60.0
    dwell_hours = max(float(station.mean_dwell_hours) - return_delay_hours, 2.0)
    vehicle_power = float(spec.charge_power_kw)
    charger_power = float(station.charger_power_kw)
    replenishable_grid_energy = min(vehicle_power, charger_power) * dwell_hours * float(charge_taper_factor)
    required_grid_energy = float(route.energy_kwh) / float(charge_efficiency)
    return replenishable_grid_energy + 1e-9 >= required_grid_energy


def _weighted_route_profile_quantile(
    routes: pd.DataFrame,
    weights: np.ndarray,
    quantile: float,
) -> pd.Series:
    """Select one observed joint route profile at a weighted severity quantile.

    Selecting cube, distance, and energy independently can synthesize a route that
    never occurred and can break their physical correlation. A percentile-rank
    severity score orders complete observed profiles, after which one original row
    is selected with scenario-balanced route weights.
    """
    columns = ["required_cube", "distance_miles", "energy_kwh"]
    profile = routes[columns].astype(float)
    severity = profile.rank(method="average", pct=True).mean(axis=1).to_numpy(dtype=float)
    order = np.argsort(severity, kind="stable")
    sorted_weights = np.asarray(weights, dtype=float)[order]
    if not np.isfinite(sorted_weights).all() or (sorted_weights < 0).any() or sorted_weights.sum() <= 0:
        raise ValueError("Route-profile weights must be finite, nonnegative, and have positive mass")
    cumulative = np.cumsum(sorted_weights)
    threshold = float(np.clip(quantile, 0.0, 1.0)) * cumulative[-1]
    selected_position = min(int(np.searchsorted(cumulative, threshold, side="left")), len(order) - 1)
    return routes.iloc[int(order[selected_position])]


def build_expected_value_scenario(routes: pd.DataFrame) -> pd.DataFrame:
    """Construct a deterministic expected-value workload, not an arbitrary sampled scenario."""
    scenario_weights = routes.groupby("scenario_id")["scenario_weight"].first().astype(float)
    scenario_weights = scenario_weights / scenario_weights.sum()
    rows: list[dict] = []
    route_counter = 0
    for station_id, station_routes in routes.groupby("station_id"):
        counts = station_routes.groupby("scenario_id").size().reindex(scenario_weights.index, fill_value=0)
        expected_count = max(1, int(round(float((counts * scenario_weights).sum()))))
        critical_counts = (
            station_routes.assign(is_critical=station_routes["criticality"].eq("critical").astype(int))
            .groupby("scenario_id")["is_critical"]
            .sum()
            .reindex(scenario_weights.index, fill_value=0)
        )
        expected_critical = min(expected_count, int(round(float((critical_counts * scenario_weights).sum()))))

        per_route_weights = station_routes["scenario_id"].map(scenario_weights).to_numpy(dtype=float)
        scenario_route_counts = (
            station_routes.groupby("scenario_id")["route_id"].transform("count").to_numpy(dtype=float)
        )
        per_route_weights = per_route_weights / np.maximum(scenario_route_counts, 1.0)
        capacity_factor = float(
            (
                station_routes.groupby("scenario_id")["charger_capacity_factor"].first().reindex(scenario_weights.index)
                * scenario_weights
            ).sum()
        )
        failure_multiplier = float(
            (
                station_routes.groupby("scenario_id")["vehicle_failure_multiplier"]
                .first()
                .reindex(scenario_weights.index)
                * scenario_weights
            ).sum()
        )
        return_delay = float(
            (
                station_routes.groupby("scenario_id")["return_delay_minutes"].first().reindex(scenario_weights.index)
                * scenario_weights
            ).sum()
        )
        for index in range(expected_count):
            route_counter += 1
            quantile = (index + 0.5) / expected_count
            selected_profile = _weighted_route_profile_quantile(
                station_routes.reset_index(drop=True),
                per_route_weights,
                quantile,
            )
            rows.append(
                {
                    "scenario_id": 0,
                    "scenario_weight": 1.0,
                    "route_id": f"EV_R{route_counter:06d}",
                    "station_id": str(station_id),
                    "required_cube": float(selected_profile.required_cube),
                    "distance_miles": float(selected_profile.distance_miles),
                    "energy_kwh": float(selected_profile.energy_kwh),
                    "criticality": "critical" if index < expected_critical else "standard",
                    "regime": "expected_value",
                    "cold_regime": int(station_routes["cold_regime"].mean() >= 0.5),
                    "charger_outage_regime": int(capacity_factor < 0.95),
                    "charger_capacity_factor": capacity_factor,
                    "vehicle_failure_multiplier": failure_multiplier,
                    "return_delay_minutes": return_delay,
                }
            )
    return pd.DataFrame(rows)


def _initial_fleet_frame(cfg: ProjectConfig, override: pd.DataFrame | None) -> pd.DataFrame:
    if override is None:
        return pd.read_csv(cfg.generated_dir / "initial_fleet.csv")
    required = {"station_id", "vehicle_type"}
    if not required.issubset(override.columns):
        raise ValueError("initial_fleet_override requires station_id and vehicle_type")
    count_column = "planned_count" if "planned_count" in override.columns else "initial_count"
    if count_column not in override.columns:
        raise ValueError("initial_fleet_override requires planned_count or initial_count")
    frame = override[["station_id", "vehicle_type", count_column]].copy()
    return frame.rename(columns={count_column: "initial_count"})


def diagnose_structural_feasibility(cfg: ProjectConfig, scenario_routes: pd.DataFrame) -> dict[str, Any]:
    """Return transparent lower-bound diagnostics for an infeasible planning model.

    This is not claimed to be an irreducible infeasible set. It identifies route
    profiles that no configured vehicle can serve, network fleet lower bounds,
    aggregate station charging deficits, and initial parking violations using the
    same physical compatibility assumptions as the optimizer.
    """
    routes = scenario_routes.copy()
    specs = pd.read_csv(cfg.generated_dir / "vehicle_specs.csv")
    stations = pd.read_csv(cfg.generated_dir / "stations.csv")
    initial = pd.read_csv(cfg.generated_dir / "initial_fleet.csv")
    station_map = {str(row.station_id): row for _, row in stations.iterrows()}
    spec_rows = [row for _, row in specs.iterrows()]
    opt = cfg.section("optimization")
    simulation = cfg.section("simulation")
    emergency_cap = int(opt.get("max_emergency_leases_per_station", 3))
    charge_efficiency = float(opt.get("charge_efficiency", 0.92))
    taper = float(simulation.get("individual_charge_taper_factor", 0.90))

    incompatible_rows: list[dict[str, Any]] = []
    charging_rows: list[dict[str, Any]] = []
    for (scenario_id, station_id), group in routes.groupby(["scenario_id", "station_id"], sort=True):
        station = station_map[str(station_id)]
        for _, route in group.iterrows():
            if not any(
                _compatible(
                    route,
                    spec,
                    station,
                    charge_taper_factor=taper,
                    charge_efficiency=charge_efficiency,
                )
                for spec in spec_rows
            ):
                incompatible_rows.append(
                    {
                        "scenario_id": int(scenario_id),
                        "station_id": str(station_id),
                        "route_id": str(route.route_id),
                        "required_cube": float(route.required_cube),
                        "energy_kwh": float(route.energy_kwh),
                    }
                )

        capacity_factor = float(group["charger_capacity_factor"].iloc[0])
        return_delay_hours = float(group["return_delay_minutes"].iloc[0]) / 60.0
        dwell = max(float(station.mean_dwell_hours) - return_delay_hours, 2.0)
        charging_capacity = (
            min(
                float(station.site_power_kw) * dwell,
                float(station.charger_ports) * float(station.charger_power_kw) * dwell,
            )
            * capacity_factor
        )
        required_grid_energy = float(group["energy_kwh"].sum()) / charge_efficiency
        charging_rows.append(
            {
                "scenario_id": int(scenario_id),
                "station_id": str(station_id),
                "required_grid_energy_kwh": required_grid_energy,
                "available_charging_capacity_kwh": charging_capacity,
                "charging_deficit_kwh": max(0.0, required_grid_energy - charging_capacity),
            }
        )

    total_fleet = int(initial["initial_count"].sum())
    station_count = int(stations["station_id"].nunique())
    route_counts = routes.groupby("scenario_id").size()
    network_shortage = {
        str(int(scenario_id)): max(0, int(count) - total_fleet - station_count * emergency_cap)
        for scenario_id, count in route_counts.items()
    }
    initial_by_station = initial.groupby("station_id")["initial_count"].sum().astype(int)
    parking = stations.set_index("station_id")["parking_capacity"].astype(int)
    parking_excess = {
        str(station_id): max(0, int(count) - int(parking.loc[station_id]))
        for station_id, count in initial_by_station.items()
    }
    return {
        "claim_boundary": "structural lower-bound diagnostics; not an exact IIS or globally minimal relaxation",
        "structurally_unserviceable_route_count": len(incompatible_rows),
        "structurally_unserviceable_routes": incompatible_rows,
        "network_vehicle_shortage_lower_bound_by_scenario": network_shortage,
        "station_charging_diagnostics": charging_rows,
        "maximum_station_charging_deficit_kwh": max(
            (float(row["charging_deficit_kwh"]) for row in charging_rows), default=0.0
        ),
        "initial_parking_excess_by_station": parking_excess,
    }


def solve_fleet_plan(
    cfg: ProjectConfig,
    scenario_routes: pd.DataFrame,
    name: str,
    stochastic: bool = True,
    initial_fleet_override: pd.DataFrame | None = None,
    allow_redeployment: bool = True,
    risk_weight_override: float | None = None,
) -> PlanResult:
    opt = cfg.section("optimization")
    routes = scenario_routes.copy()
    if not stochastic:
        routes = build_expected_value_scenario(routes)
    scenarios = sorted(int(value) for value in routes["scenario_id"].unique())
    scenario_weights = routes.groupby("scenario_id")["scenario_weight"].first().to_dict()
    stations = pd.read_csv(cfg.generated_dir / "stations.csv")
    initial = _initial_fleet_frame(cfg, initial_fleet_override)
    specs = pd.read_csv(cfg.generated_dir / "vehicle_specs.csv")
    station_ids = sorted(stations.station_id.astype(str).unique())
    vehicle_types = sorted(specs.vehicle_type.astype(str).unique())
    initial_map = {
        (str(row.station_id), str(row.vehicle_type)): int(row.initial_count) for _, row in initial.iterrows()
    }
    spec_map = {str(row.vehicle_type): row for _, row in specs.iterrows()}
    station_map = {str(row.station_id): row for _, row in stations.iterrows()}
    for station_id in station_ids:
        for vehicle_type in vehicle_types:
            initial_map.setdefault((station_id, vehicle_type), 0)

    variables = _Vars()
    move_cost = float(opt.get("redeployment_cost", 90.0))
    emergency_cost = float(opt.get("emergency_lease_cost", 450.0))
    unserved_cost = float(opt.get("unserved_cost", 1200.0))
    critical_multiplier = float(opt.get("critical_multiplier", 3.0))
    overage_cost = float(opt.get("charging_overage_cost", 12.0))
    alpha = float(opt.get("cvar_alpha", 0.9))
    risk_weight = float(opt.get("risk_weight", 0.35)) if risk_weight_override is None else float(risk_weight_override)
    simulation_cfg = cfg.section("simulation")
    base_failure_probability = float(simulation_cfg.get("failure_probability", 0.025))
    charge_taper_factor = float(simulation_cfg.get("individual_charge_taper_factor", 0.90))
    charge_efficiency = float(opt.get("charge_efficiency", 0.92))

    for origin in station_ids:
        for destination in station_ids:
            if origin == destination:
                continue
            for vehicle_type in vehicle_types:
                variables.add(
                    ("move", origin, destination, vehicle_type),
                    ub=np.inf if allow_redeployment else 0.0,
                    integrality=1,
                    cost=move_cost,
                )

    for scenario_id in scenarios:
        weight = float(scenario_weights[scenario_id])
        for station_id in station_ids:
            for vehicle_type in vehicle_types:
                variables.add(
                    ("emergency", scenario_id, station_id, vehicle_type), integrality=1, cost=weight * emergency_cost
                )
            variables.add(("charge_slack", scenario_id, station_id), cost=weight * overage_cost)
        for _, route in routes[routes.scenario_id == scenario_id].iterrows():
            route_key = str(route.route_id)
            penalty = unserved_cost * (critical_multiplier if route.criticality == "critical" else 1.0)
            variables.add(("unserved", scenario_id, route_key), ub=1.0, integrality=1, cost=weight * penalty)
            for vehicle_type in vehicle_types:
                if _compatible(
                    route,
                    spec_map[vehicle_type],
                    station_map[str(route.station_id)],
                    charge_taper_factor=charge_taper_factor,
                    charge_efficiency=charge_efficiency,
                ):
                    operating_cost = float(route.distance_miles) * float(spec_map[vehicle_type].variable_cost_per_mile)
                    variables.add(
                        ("assign", scenario_id, route_key, vehicle_type),
                        ub=1.0,
                        integrality=1,
                        cost=weight * operating_cost,
                    )
        variables.add(("excess", scenario_id), cost=risk_weight * weight / max(1.0 - alpha, 1e-6))
    variables.add(("eta",), cost=risk_weight)

    constraints = _Constraints(lambda: len(variables.names))
    for station_id in station_ids:
        for vehicle_type in vehicle_types:
            coefficients = {
                variables.get(("move", station_id, destination, vehicle_type)): 1.0
                for destination in station_ids
                if destination != station_id
            }
            constraints.add(coefficients, upper=initial_map[(station_id, vehicle_type)])

        parking_coefficients: dict[int, float] = {}
        for vehicle_type in vehicle_types:
            for other in station_ids:
                if other == station_id:
                    continue
                parking_coefficients[variables.get(("move", other, station_id, vehicle_type))] = 1.0
                parking_coefficients[variables.get(("move", station_id, other, vehicle_type))] = -1.0
        parking_limit = float(station_map[station_id].get("parking_capacity", np.inf))
        current_total = sum(initial_map[(station_id, vehicle_type)] for vehicle_type in vehicle_types)
        constraints.add(parking_coefficients, upper=parking_limit - current_total)

    scenario_loss_coefficients: dict[int, dict[int, float]] = {scenario_id: {} for scenario_id in scenarios}
    for scenario_id in scenarios:
        group = routes[routes.scenario_id == scenario_id]
        for _, route in group.iterrows():
            route_key = str(route.route_id)
            coefficients = {variables.get(("unserved", scenario_id, route_key)): 1.0}
            for vehicle_type in vehicle_types:
                key = ("assign", scenario_id, route_key, vehicle_type)
                if key in variables.index:
                    variable_index = variables.get(key)
                    coefficients[variable_index] = 1.0
                    scenario_loss_coefficients[scenario_id][variable_index] = float(route.distance_miles) * float(
                        spec_map[vehicle_type].variable_cost_per_mile
                    )
            constraints.add(coefficients, lower=1.0, upper=1.0)
            penalty = unserved_cost * (critical_multiplier if route.criticality == "critical" else 1.0)
            scenario_loss_coefficients[scenario_id][variables.get(("unserved", scenario_id, route_key))] = penalty

        failure_multiplier = float(group["vehicle_failure_multiplier"].iloc[0])
        repair_success_probability = float(cfg.section("simulation").get("same_day_repair_success_probability", 0.55))
        for station_id in station_ids:
            station_group = group[group.station_id == station_id]
            station_fleet = float(sum(initial_map[(station_id, vehicle_type)] for vehicle_type in vehicle_types))
            raw_failure_rate = min(0.75, base_failure_probability * failure_multiplier)
            expected_failures = station_fleet * raw_failure_rate
            repair_capacity = float(station_map[station_id].get("repair_capacity", 0.0))
            expected_repairs = min(expected_failures, repair_capacity) * repair_success_probability
            residual_failures = max(expected_failures - expected_repairs, 0.0)
            availability_factor = max(0.50, 1.0 - residual_failures / max(station_fleet, 1.0))
            emergency_cap_coefficients = {
                variables.get(("emergency", scenario_id, station_id, vehicle_type)): 1.0
                for vehicle_type in vehicle_types
            }
            constraints.add(
                emergency_cap_coefficients,
                upper=float(opt.get("max_emergency_leases_per_station", 3)),
            )
            for vehicle_type in vehicle_types:
                coefficients: dict[int, float] = {}
                for _, route in station_group.iterrows():
                    key = ("assign", scenario_id, str(route.route_id), vehicle_type)
                    if key in variables.index:
                        coefficients[variables.get(key)] = 1.0
                for other in station_ids:
                    if other == station_id:
                        continue
                    inbound = variables.get(("move", other, station_id, vehicle_type))
                    outbound = variables.get(("move", station_id, other, vehicle_type))
                    coefficients[inbound] = coefficients.get(inbound, 0.0) - availability_factor
                    coefficients[outbound] = coefficients.get(outbound, 0.0) + availability_factor
                coefficients[variables.get(("emergency", scenario_id, station_id, vehicle_type))] = -1.0
                constraints.add(coefficients, upper=availability_factor * initial_map[(station_id, vehicle_type)])
                scenario_loss_coefficients[scenario_id][
                    variables.get(("emergency", scenario_id, station_id, vehicle_type))
                ] = emergency_cost

            energy_coefficients: dict[int, float] = {}
            for _, route in station_group.iterrows():
                for vehicle_type in vehicle_types:
                    key = ("assign", scenario_id, str(route.route_id), vehicle_type)
                    if key in variables.index:
                        energy_coefficients[variables.get(key)] = float(route.energy_kwh) / charge_efficiency
            energy_coefficients[variables.get(("charge_slack", scenario_id, station_id))] = -1.0
            station = station_map[station_id]
            capacity_factor = (
                float(station_group["charger_capacity_factor"].iloc[0]) if not station_group.empty else 1.0
            )
            dwell_reduction = (
                float(station_group["return_delay_minutes"].iloc[0]) / 60.0 if not station_group.empty else 0.0
            )
            dwell = max(float(station.mean_dwell_hours) - dwell_reduction, 2.0)
            capacity = (
                min(
                    float(station.site_power_kw) * dwell,
                    float(station.charger_ports) * float(station.charger_power_kw) * dwell,
                )
                * capacity_factor
            )
            constraints.add(energy_coefficients, upper=capacity)
            scenario_loss_coefficients[scenario_id][variables.get(("charge_slack", scenario_id, station_id))] = (
                overage_cost
            )

        cvar_coefficients = dict(scenario_loss_coefficients[scenario_id])
        cvar_coefficients[variables.get(("eta",))] = -1.0
        cvar_coefficients[variables.get(("excess", scenario_id))] = -1.0
        constraints.add(cvar_coefficients, upper=0.0)

    objective_vector = np.asarray(variables.cost, dtype=float)
    integrality = np.asarray(variables.integrality, dtype=int)
    lower_bounds = np.asarray(variables.lb, dtype=float)
    upper_bounds = np.asarray(variables.ub, dtype=float)
    constraint_matrix = constraints.matrix()
    constraint_lower = np.asarray(constraints.lower, dtype=float)
    constraint_upper = np.asarray(constraints.upper, dtype=float)
    started_at = time.perf_counter()
    try:
        result = milp(
            c=objective_vector,
            integrality=integrality,
            bounds=Bounds(lower_bounds, upper_bounds),
            constraints=LinearConstraint(
                constraint_matrix,
                constraint_lower,
                constraint_upper,
            ),
            options={
                "time_limit": float(opt.get("mip_time_limit_seconds", 30)),
                "mip_rel_gap": float(opt.get("mip_relative_gap", 0.03)),
                "disp": False,
            },
        )
    except Exception as exc:
        return heuristic_fallback(
            cfg,
            routes,
            name=f"{name}_fallback",
            reason=f"MILP exception: {exc}",
            solver_metadata={
                "termination_class": "EXCEPTION",
                "runtime_seconds": round(time.perf_counter() - started_at, 6),
            },
        )
    solver_runtime = time.perf_counter() - started_at
    termination_class = _STATUS_CLASS.get(int(result.status), "UNKNOWN")
    if result.x is None:
        structural_diagnostics = diagnose_structural_feasibility(cfg, routes)
        return heuristic_fallback(
            cfg,
            routes,
            name=f"{name}_fallback",
            reason=str(result.message),
            solver_metadata={
                "termination_class": termination_class,
                "status_code": int(result.status),
                "runtime_seconds": round(solver_runtime, 6),
                "mip_gap": _finite_or_none(getattr(result, "mip_gap", None)),
                "mip_dual_bound": _finite_or_none(getattr(result, "mip_dual_bound", None)),
                "structural_diagnostics": structural_diagnostics,
            },
        )

    solution = np.asarray(result.x, dtype=float)
    diagnostics = _solution_diagnostics(
        solution,
        objective_vector,
        integrality,
        lower_bounds,
        upper_bounds,
        constraint_matrix,
        constraint_lower,
        constraint_upper,
        result.fun,
    )
    thresholds = cfg.section("reliability")
    maximum_constraint_violation = float(thresholds.get("max_solver_constraint_violation", 1e-6))
    maximum_integrality_violation = float(thresholds.get("max_solver_integrality_violation", 1e-6))
    maximum_objective_error = float(thresholds.get("max_solver_objective_error", 1e-6))
    solution_is_valid = (
        diagnostics["finite_solution"]
        and diagnostics["max_bound_violation"] <= maximum_constraint_violation
        and diagnostics["max_constraint_violation"] <= maximum_constraint_violation
        and diagnostics["max_integrality_violation"] <= maximum_integrality_violation
        and diagnostics["objective_reconciliation_error"] <= maximum_objective_error
    )
    if not solution_is_valid:
        return heuristic_fallback(
            cfg,
            routes,
            name=f"{name}_fallback",
            reason="MILP returned a solution that failed numerical validation",
            solver_metadata={
                "termination_class": "INVALID_SOLUTION",
                "status_code": int(result.status),
                "runtime_seconds": round(solver_runtime, 6),
                **diagnostics,
            },
        )

    status = _accepted_plan_status(int(result.status), bool(result.success))
    if status is None:
        return heuristic_fallback(
            cfg,
            routes,
            name=f"{name}_fallback",
            reason=f"Solver termination {termination_class} is not eligible for incumbent release",
            solver_metadata={
                "termination_class": termination_class,
                "status_code": int(result.status),
                "runtime_seconds": round(solver_runtime, 6),
                **diagnostics,
            },
        )
    move_rows = []
    for key, index in variables.index.items():
        if key[0] == "move" and solution[index] > 0.5:
            move_rows.append(
                {
                    "from_station": key[1],
                    "to_station": key[2],
                    "vehicle_type": key[3],
                    "count": int(round(solution[index])),
                }
            )
    moves = pd.DataFrame(move_rows, columns=["from_station", "to_station", "vehicle_type", "count"])

    allocation_rows = []
    for station_id in station_ids:
        for vehicle_type in vehicle_types:
            inbound = sum(
                int(round(solution[variables.get(("move", origin, station_id, vehicle_type))]))
                for origin in station_ids
                if origin != station_id
            )
            outbound = sum(
                int(round(solution[variables.get(("move", station_id, destination, vehicle_type))]))
                for destination in station_ids
                if destination != station_id
            )
            allocation_rows.append(
                {
                    "station_id": station_id,
                    "vehicle_type": vehicle_type,
                    "initial_count": initial_map[(station_id, vehicle_type)],
                    "planned_count": initial_map[(station_id, vehicle_type)] + inbound - outbound,
                    "inbound": inbound,
                    "outbound": outbound,
                }
            )
    allocation = pd.DataFrame(allocation_rows)

    scenario_metric_rows = []
    for scenario_id in scenarios:
        group = routes[routes.scenario_id == scenario_id]
        unserved = 0
        critical_unserved = 0
        emergency = 0
        charge_slack = 0.0
        variable_operating_cost = 0.0
        for _, route in group.iterrows():
            route_key = str(route.route_id)
            value = solution[variables.get(("unserved", scenario_id, route_key))]
            if value > 0.5:
                unserved += 1
                critical_unserved += int(route.criticality == "critical")
            for vehicle_type in vehicle_types:
                key = ("assign", scenario_id, route_key, vehicle_type)
                if key in variables.index and solution[variables.get(key)] > 0.5:
                    variable_operating_cost += float(route.distance_miles) * float(
                        spec_map[vehicle_type].variable_cost_per_mile
                    )
        for station_id in station_ids:
            charge_slack += solution[variables.get(("charge_slack", scenario_id, station_id))]
            for vehicle_type in vehicle_types:
                emergency += int(round(solution[variables.get(("emergency", scenario_id, station_id, vehicle_type))]))
        scenario_metric_rows.append(
            {
                "scenario_id": scenario_id,
                "routes": len(group),
                "unserved_routes": unserved,
                "critical_unserved": critical_unserved,
                "emergency_leases": emergency,
                "charge_energy_slack_kwh": charge_slack,
                "variable_operating_cost": variable_operating_cost,
            }
        )

    objective = _finite_or_none(result.fun)
    solver = {
        "success": bool(result.success),
        "status_code": int(result.status),
        "message": str(result.message),
        "termination_class": termination_class,
        "runtime_seconds": round(solver_runtime, 6),
        "mip_gap": _finite_or_none(getattr(result, "mip_gap", None)),
        "mip_node_count": int(getattr(result, "mip_node_count", 0) or 0),
        "mip_dual_bound": _finite_or_none(getattr(result, "mip_dual_bound", None)),
        "solver_backend": "scipy.optimize.milp (HiGHS)",
        "degraded_mode": False,
        **diagnostics,
        "deterministic_workload": "weighted_expected_value" if not stochastic else None,
        "allow_redeployment": allow_redeployment,
        "risk_weight": risk_weight,
    }
    return PlanResult(
        name=name,
        status=status,
        objective=objective,
        allocation=allocation,
        moves=moves,
        scenario_metrics=pd.DataFrame(scenario_metric_rows),
        solver=solver,
    )


def heuristic_fallback(
    cfg: ProjectConfig,
    routes: pd.DataFrame,
    name: str = "heuristic_fallback",
    reason: str = "MILP unavailable",
    solver_metadata: dict[str, Any] | None = None,
) -> PlanResult:
    initial = pd.read_csv(cfg.generated_dir / "initial_fleet.csv")
    allocation = initial.rename(columns={"initial_count": "planned_count"}).copy()
    allocation["initial_count"] = allocation["planned_count"]
    allocation["inbound"] = 0
    allocation["outbound"] = 0
    metrics = routes.groupby("scenario_id").size().rename("routes").reset_index()
    for column in ("unserved_routes", "critical_unserved", "emergency_leases", "charge_energy_slack_kwh"):
        metrics[column] = np.nan
    return PlanResult(
        name=name,
        status="HEURISTIC_FALLBACK",
        objective=None,
        allocation=allocation[["station_id", "vehicle_type", "initial_count", "planned_count", "inbound", "outbound"]],
        moves=pd.DataFrame(columns=["from_station", "to_station", "vehicle_type", "count"]),
        scenario_metrics=metrics,
        solver={
            "solver_backend": "carry-forward heuristic",
            "success": False,
            "message": reason,
            "degraded_mode": True,
            "termination_class": "FALLBACK",
            "finite_solution": False,
            "max_bound_violation": None,
            "max_constraint_violation": None,
            "max_integrality_violation": None,
            "objective_reconciliation_error": None,
            **(solver_metadata or {}),
        },
    )


def save_plan(plan: PlanResult, path: str | Path) -> Path:
    return write_json_atomic(path, plan.to_dict())


def load_plan(path: str | Path) -> PlanResult:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    objective = raw.get("objective")
    return PlanResult(
        name=raw["name"],
        status=raw["status"],
        objective=float(objective) if objective is not None else None,
        allocation=pd.DataFrame(raw["allocation"]),
        moves=pd.DataFrame(raw["moves"]),
        scenario_metrics=pd.DataFrame(raw["scenario_metrics"]),
        solver=raw["solver"],
    )
