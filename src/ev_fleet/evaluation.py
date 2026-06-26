from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd

from .config import ProjectConfig
from .optimization import PlanResult, solve_fleet_plan


def allocation_map(plan: PlanResult) -> dict[tuple[str, str], int]:
    return {
        (str(row.station_id), str(row.vehicle_type)): int(row.planned_count)
        for row in plan.allocation.itertuples(index=False)
    }


def evaluate_allocation_on_scenarios(
    cfg: ProjectConfig,
    plan: PlanResult,
    scenarios: pd.DataFrame,
) -> pd.DataFrame:
    """Fast approximate policy evaluator used only for forecast replay and SAA screening.

    Formal VSS/EVPI calculations below use exact fixed-allocation recourse MILPs.
    """
    specs = pd.read_csv(cfg.generated_dir / "vehicle_specs.csv").set_index("vehicle_type")
    stations = pd.read_csv(cfg.generated_dir / "stations.csv").set_index("station_id")
    counts = allocation_map(plan)
    optimization = cfg.section("optimization")
    move_cost = (
        float(plan.moves["count"].sum()) * float(optimization.get("redeployment_cost", 90.0))
        if not plan.moves.empty
        else 0.0
    )
    temporary_additions = (
        int(plan.allocation["temporary_addition"].sum()) if "temporary_addition" in plan.allocation else 0
    )
    temporary_cost = temporary_additions * float(optimization.get("emergency_lease_cost", 450.0))
    emergency_cap = int(optimization.get("max_emergency_leases_per_station", 3))
    rows: list[dict] = []
    for scenario_id, group in scenarios.groupby("scenario_id", sort=True):
        scenario_cost = move_cost + temporary_cost
        unserved = 0
        critical_unserved = 0
        emergency = 0
        total_routes = len(group)
        for station_id, station_group in group.groupby("station_id", sort=True):
            failure_multiplier = float(station_group["vehicle_failure_multiplier"].iloc[0])
            station = stations.loc[station_id]
            station_fleet = float(
                sum(counts.get((str(station_id), str(vehicle_type)), 0) for vehicle_type in specs.index)
            )
            raw_failure_rate = min(
                0.75,
                float(cfg.section("simulation").get("failure_probability", 0.025)) * failure_multiplier,
            )
            expected_failures = station_fleet * raw_failure_rate
            repair_probability = float(cfg.section("simulation").get("same_day_repair_success_probability", 0.55))
            expected_repairs = min(expected_failures, float(station.repair_capacity)) * repair_probability
            availability_factor = max(
                0.5,
                1.0 - max(expected_failures - expected_repairs, 0.0) / max(station_fleet, 1.0),
            )
            available = {
                vehicle_type: int(np.floor(counts.get((str(station_id), str(vehicle_type)), 0) * availability_factor))
                for vehicle_type in specs.index
            }
            energy = 0.0
            station_emergency = 0
            ordered = station_group.assign(
                critical_order=station_group["criticality"].ne("critical").astype(int)
            ).sort_values(["critical_order", "required_cube"], ascending=[True, False])
            for route in ordered.itertuples(index=False):
                compatible = []
                dwell = max(
                    float(station.mean_dwell_hours) - float(station_group["return_delay_minutes"].iloc[0]) / 60.0,
                    2.0,
                )
                charge_taper = float(cfg.section("simulation").get("individual_charge_taper_factor", 0.90))
                for vehicle_type, spec in specs.iterrows():
                    usable = float(spec.battery_kwh) * float(spec.usable_fraction) - float(spec.reserve_kwh)
                    replenishable = (
                        min(float(spec.charge_power_kw), float(station.charger_power_kw)) * dwell * charge_taper
                    )
                    required_grid_energy = float(route.energy_kwh) / float(optimization.get("charge_efficiency", 0.92))
                    if (
                        float(spec.cube_capacity) >= float(route.required_cube)
                        and usable >= float(route.energy_kwh)
                        and replenishable + 1e-9 >= required_grid_energy
                    ):
                        compatible.append((float(spec.variable_cost_per_mile), str(vehicle_type)))
                compatible.sort()
                chosen = next((vehicle_type for _, vehicle_type in compatible if available[vehicle_type] > 0), None)
                if chosen is not None:
                    available[chosen] -= 1
                    scenario_cost += float(route.distance_miles) * float(specs.loc[chosen, "variable_cost_per_mile"])
                    energy += float(route.energy_kwh) / float(optimization.get("charge_efficiency", 0.92))
                elif compatible and station_emergency < emergency_cap:
                    station_emergency += 1
                    emergency += 1
                    leased_type = compatible[0][1]
                    scenario_cost += float(optimization.get("emergency_lease_cost", 450.0))
                    scenario_cost += float(route.distance_miles) * float(
                        specs.loc[leased_type, "variable_cost_per_mile"]
                    )
                    energy += float(route.energy_kwh) / float(optimization.get("charge_efficiency", 0.92))
                else:
                    unserved += 1
                    is_critical = route.criticality == "critical"
                    critical_unserved += int(is_critical)
                    scenario_cost += float(optimization.get("unserved_cost", 1200.0)) * (
                        float(optimization.get("critical_multiplier", 3.0)) if is_critical else 1.0
                    )
            capacity = min(
                float(station.site_power_kw) * dwell,
                float(station.charger_ports) * float(station.charger_power_kw) * dwell,
            ) * float(station_group["charger_capacity_factor"].iloc[0])
            scenario_cost += max(energy - capacity, 0.0) * float(optimization.get("charging_overage_cost", 12.0))
        rows.append(
            {
                "scenario_id": int(scenario_id),
                "scenario_weight": float(group["scenario_weight"].iloc[0]),
                "realized_cost": scenario_cost,
                "routes": total_routes,
                "service_rate": 1.0 - unserved / max(total_routes, 1),
                "unserved_routes": unserved,
                "critical_unserved": critical_unserved,
                "emergency_leases": emergency,
            }
        )
    return pd.DataFrame(rows)


def cvar(values: np.ndarray, alpha: float = 0.95) -> float:
    numeric = np.asarray(values, dtype=float)
    if len(numeric) == 0:
        return 0.0
    threshold = np.quantile(numeric, alpha)
    tail = numeric[numeric >= threshold]
    return float(tail.mean()) if len(tail) else float(threshold)


def weighted_mean(frame: pd.DataFrame, value_column: str) -> float:
    weights = frame["scenario_weight"].to_numpy(float)
    weights = weights / weights.sum()
    return float(np.average(frame[value_column].to_numpy(float), weights=weights))


def _fixed_plan_cost_exact(
    cfg: ProjectConfig,
    plan: PlanResult,
    scenario: pd.DataFrame,
) -> tuple[float, int, float]:
    recourse = solve_fleet_plan(
        cfg,
        scenario.assign(scenario_weight=1.0),
        name=f"fixed_recourse_{plan.name}",
        stochastic=True,
        initial_fleet_override=plan.allocation,
        allow_redeployment=False,
        risk_weight_override=0.0,
    )
    if recourse.objective is None:
        raise RuntimeError(f"Fixed-allocation recourse failed for {plan.name}")
    optimization = cfg.section("optimization")
    fixed_move_cost = (
        float(plan.moves["count"].sum()) * float(optimization.get("redeployment_cost", 90.0))
        if not plan.moves.empty
        else 0.0
    )
    temporary_additions = (
        int(plan.allocation["temporary_addition"].sum()) if "temporary_addition" in plan.allocation else 0
    )
    fixed_temporary_cost = temporary_additions * float(optimization.get("emergency_lease_cost", 450.0))
    temporary_charging_capacity = float(sum(dict(plan.solver.get("temporary_charging_capacity_kwh", {})).values()))
    fixed_temporary_cost += temporary_charging_capacity * float(
        optimization.get("temporary_charging_capacity_cost_per_kwh", 2.0)
    )
    fixed_temporary_cost += float(plan.solver.get("route_split_program_cost", 0.0))
    unserved = int(recourse.scenario_metrics["unserved_routes"].sum())
    routes = int(recourse.scenario_metrics["routes"].sum())
    return (
        float(recourse.objective + fixed_move_cost + fixed_temporary_cost),
        unserved,
        1.0 - unserved / max(routes, 1),
    )


def _value_components(
    cfg: ProjectConfig,
    deterministic_plan: PlanResult,
    risk_neutral_plan: PlanResult,
    scenarios: pd.DataFrame,
    scenario_ids: Iterable[int],
) -> dict[str, float | int]:
    selected_ids = list(int(value) for value in scenario_ids)
    original_fleet = pd.read_csv(cfg.generated_dir / "initial_fleet.csv")
    rows: list[dict[str, float | int]] = []
    for scenario_id in selected_ids:
        single = scenarios[scenarios["scenario_id"] == scenario_id].copy()
        deterministic_cost, deterministic_unserved, deterministic_rate = _fixed_plan_cost_exact(
            cfg, deterministic_plan, single
        )
        stochastic_cost, stochastic_unserved, stochastic_rate = _fixed_plan_cost_exact(cfg, risk_neutral_plan, single)
        wait_and_see = solve_fleet_plan(
            cfg,
            single.assign(scenario_weight=1.0),
            name="wait_and_see",
            stochastic=True,
            initial_fleet_override=original_fleet,
            allow_redeployment=True,
            risk_weight_override=0.0,
        )
        if wait_and_see.objective is None:
            raise RuntimeError("Wait-and-see optimization failed")
        rows.append(
            {
                "scenario_id": scenario_id,
                "scenario_weight": float(single["scenario_weight"].iloc[0]),
                "deterministic_cost": deterministic_cost,
                "risk_neutral_cost": stochastic_cost,
                "wait_and_see_cost": float(wait_and_see.objective),
                "deterministic_unserved": deterministic_unserved,
                "risk_neutral_unserved": stochastic_unserved,
                "deterministic_service": deterministic_rate,
                "risk_neutral_service": stochastic_rate,
            }
        )
    frame = pd.DataFrame(rows)
    weights = frame["scenario_weight"].to_numpy(dtype=float, copy=True)
    weights /= weights.sum()
    expected_value_cost = float(np.average(frame["deterministic_cost"], weights=weights))
    risk_neutral_cost = float(np.average(frame["risk_neutral_cost"], weights=weights))
    wait_and_see_cost = float(np.average(frame["wait_and_see_cost"], weights=weights))
    vss = expected_value_cost - risk_neutral_cost
    evpi = risk_neutral_cost - wait_and_see_cost
    return {
        "scenario_count": len(frame),
        "expected_value_plan_cost": expected_value_cost,
        "risk_neutral_stochastic_cost": risk_neutral_cost,
        "wait_and_see_cost": wait_and_see_cost,
        "vss": vss,
        "evpi": evpi,
        "vss_numerical_tolerance_violation": min(vss, 0.0),
        "evpi_numerical_tolerance_violation": min(evpi, 0.0),
        "deterministic_shortage_cvar95": cvar(frame["deterministic_unserved"].to_numpy(), 0.95),
        "risk_neutral_shortage_cvar95": cvar(frame["risk_neutral_unserved"].to_numpy(), 0.95),
        "deterministic_service_rate": float(np.average(frame["deterministic_service"], weights=weights)),
        "risk_neutral_service_rate": float(np.average(frame["risk_neutral_service"], weights=weights)),
    }


def stochastic_value_analysis(
    cfg: ProjectConfig,
    deterministic_plan: PlanResult,
    risk_neutral_plan: PlanResult,
    optimization_scenarios: pd.DataFrame,
    evaluation_scenarios: pd.DataFrame,
) -> dict[str, object]:
    """Report formal sample VSS/EVPI and separate held-out empirical estimates.

    The formal quantities compare risk-neutral policies on the same optimization
    sample. The held-out VSS can legitimately be negative because it measures
    generalization rather than an optimization theorem.
    """
    in_sample_ids = sorted(int(value) for value in optimization_scenarios["scenario_id"].unique())
    heldout_limit = int(
        cfg.section("optimization").get(
            "value_evaluation_scenarios",
            evaluation_scenarios["scenario_id"].nunique(),
        )
    )
    heldout_ids = sorted(int(value) for value in evaluation_scenarios["scenario_id"].unique())[:heldout_limit]
    in_sample = _value_components(cfg, deterministic_plan, risk_neutral_plan, optimization_scenarios, in_sample_ids)
    heldout = _value_components(cfg, deterministic_plan, risk_neutral_plan, evaluation_scenarios, heldout_ids)
    return {
        "formal_sample": in_sample,
        "heldout_empirical": heldout,
        "value_of_stochastic_solution": in_sample["vss"],
        "expected_value_of_perfect_information": in_sample["evpi"],
        "heldout_empirical_vss": heldout["vss"],
        "heldout_evpi_estimate": heldout["evpi"],
        "evaluation_method": "exact fixed-allocation recourse MILP with common scenario sets",
        "interpretation": (
            "Formal VSS/EVPI use the optimization sample and a risk-neutral stochastic plan. "
            "Held-out empirical VSS is a generalization estimate and may be negative."
        ),
    }
