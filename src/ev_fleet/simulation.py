from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from .config import ProjectConfig
from .optimization import PlanResult


@dataclass
class SimulationOutput:
    runs: pd.DataFrame
    summary: dict[str, Any]


def _wilson_lower(successes: int, total: int, z: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    proportion = successes / total
    denominator = 1 + z * z / total
    centre = proportion + z * z / (2 * total)
    margin = z * np.sqrt((proportion * (1 - proportion) + z * z / (4 * total)) / total)
    return float(max(0.0, (centre - margin) / denominator))


def _mean_lower(values: pd.Series, z: float = 1.96) -> float:
    numeric = values.astype(float)
    if numeric.empty:
        return 0.0
    standard_error = float(numeric.std(ddof=1) / np.sqrt(len(numeric))) if len(numeric) > 1 else 0.0
    return float(max(0.0, numeric.mean() - z * standard_error))


def _stable_seed(base_seed: int, *parts: object) -> int:
    payload = "|".join([str(base_seed), *(str(part) for part in parts)]).encode("utf-8")
    return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "little") % (2**32 - 1)


def _tail_mean(values: pd.Series, alpha: float) -> float:
    if values.empty:
        return 0.0
    threshold = float(values.quantile(alpha))
    tail = values[values >= threshold]
    return float(tail.mean()) if not tail.empty else threshold


def _apply_same_day_repairs(
    failed_by_type: dict[str, int],
    specs: dict[str, dict[str, float]],
    repair_capacity: int,
    repair_draws: list[float],
    success_probability: float,
) -> tuple[dict[str, int], int]:
    """Use finite repair-bay capacity and prioritize the most flexible vehicles."""
    residual = {vehicle_type: max(int(count), 0) for vehicle_type, count in failed_by_type.items()}
    attempts = min(sum(residual.values()), max(int(repair_capacity), 0), len(repair_draws))
    successful_repairs = sum(draw < success_probability for draw in repair_draws[:attempts])
    remaining = int(successful_repairs)
    priority = sorted(
        residual,
        key=lambda vehicle_type: (
            -float(specs[vehicle_type]["cube"]),
            float(specs[vehicle_type]["cost"]),
            vehicle_type,
        ),
    )
    for vehicle_type in priority:
        repaired = min(residual[vehicle_type], remaining)
        residual[vehicle_type] -= repaired
        remaining -= repaired
        if remaining <= 0:
            break
    return residual, int(successful_repairs - remaining)


def _compile_inputs(
    cfg: ProjectConfig,
    plan: PlanResult,
    route_template: pd.DataFrame,
) -> tuple[
    list[str],
    dict[str, dict[str, float]],
    dict[str, dict[str, float]],
    dict[int, dict[str, Any]],
    np.ndarray,
    dict[tuple[str, str], int],
]:
    specs_frame = pd.read_csv(cfg.generated_dir / "vehicle_specs.csv")
    specs: dict[str, dict[str, float]] = {}
    for row in specs_frame.itertuples(index=False):
        vehicle_type = str(row.vehicle_type)
        specs[vehicle_type] = {
            "cube": float(row.cube_capacity),
            "usable": float(row.battery_kwh) * float(row.usable_fraction) - float(row.reserve_kwh),
            "cost": float(row.daily_fixed_cost),
            "variable_cost_per_mile": float(row.variable_cost_per_mile),
            "charge_power_kw": float(row.charge_power_kw),
        }
    vehicle_types = sorted(specs, key=lambda value: (specs[value]["cost"], value))

    stations_frame = pd.read_csv(cfg.generated_dir / "stations.csv")
    stations = {
        str(row.station_id): {
            "site_power_kw": float(row.site_power_kw),
            "charger_ports": float(row.charger_ports),
            "charger_power_kw": float(row.charger_power_kw),
            "mean_dwell_hours": float(row.mean_dwell_hours),
            "repair_capacity": int(row.repair_capacity),
        }
        for row in stations_frame.itertuples(index=False)
    }
    allocation = {
        (str(row.station_id), str(row.vehicle_type)): int(row.planned_count)
        for row in plan.allocation.itertuples(index=False)
    }

    compiled: dict[int, dict[str, Any]] = {}
    scenario_weights: list[float] = []
    for scenario_id, scenario_frame in route_template.groupby("scenario_id", sort=True):
        station_groups: dict[str, dict[str, Any]] = {}
        for station_id, group in scenario_frame.groupby("station_id", sort=True):
            route_rows = sorted(
                [
                    {
                        "route_id": str(row.route_id),
                        "required_cube": float(row.required_cube),
                        "energy_kwh": float(row.energy_kwh),
                        "distance_miles": float(row.distance_miles),
                        "critical": str(row.criticality) == "critical",
                    }
                    for row in group.itertuples(index=False)
                ],
                key=lambda route: (not route["critical"], -route["required_cube"], route["route_id"]),
            )
            station_groups[str(station_id)] = {
                "failure_multiplier": float(group["vehicle_failure_multiplier"].iloc[0]),
                "return_delay_minutes": float(group["return_delay_minutes"].iloc[0]),
                "charger_capacity_factor": float(group["charger_capacity_factor"].iloc[0]),
                "routes": route_rows,
            }
        scenario_key = int(scenario_id)
        compiled[scenario_key] = {
            "weight": float(scenario_frame["scenario_weight"].iloc[0]),
            "regime": str(scenario_frame["regime"].iloc[0]),
            "stations": station_groups,
            "route_ids": sorted(scenario_frame["route_id"].astype(str).unique()),
            "route_count": int(len(scenario_frame)),
            "critical_count": int((scenario_frame["criticality"] == "critical").sum()),
        }
        scenario_weights.append(compiled[scenario_key]["weight"])
    weights = np.array(scenario_weights, dtype=float, copy=True)
    weights /= weights.sum()
    return vehicle_types, specs, stations, compiled, weights, allocation


def simulate_plan(
    cfg: ProjectConfig,
    plan: PlanResult,
    route_template: pd.DataFrame,
    seed_offset: int = 0,
) -> SimulationOutput:
    """Replay a plan under common random numbers and a distinct operational model.

    The optimizer sees aggregate availability and charging constraints. The replay
    adds route-level energy noise, stochastic breakdown rounding, charger derates,
    late returns, nonlinear charging taper, and capacity-limited emergency leasing.
    Using the same ``seed_offset`` gives policies exactly the same exogenous draws.
    """
    sim = cfg.section("simulation")
    opt = cfg.section("optimization")
    run_count = int(sim.get("runs", 250))
    base_seed = cfg.seed + 5000 + seed_offset
    vehicle_types, specs, stations, scenarios, weights, allocation = _compile_inputs(cfg, plan, route_template)
    scenario_ids = sorted(scenarios)
    max_cube = max(spec["cube"] for spec in specs.values())
    base_failure_probability = float(sim.get("failure_probability", 0.025))
    outage_probability = float(sim.get("charger_outage_probability", 0.06))
    late_return_sigma = float(sim.get("late_return_sigma_minutes", 28.0))
    energy_sigma = float(sim.get("energy_sigma", 0.12))
    route_split_distance_overhead = float(sim.get("route_split_distance_overhead", 0.10))
    emergency_cap = int(opt.get("max_emergency_leases_per_station", 3))
    emergency_fulfillment = float(sim.get("emergency_lease_fulfillment_probability", 0.85))
    repair_success_probability = float(sim.get("same_day_repair_success_probability", 0.55))
    individual_charge_taper = float(sim.get("individual_charge_taper_factor", 0.90))
    charge_efficiency_mean = float(sim.get("charge_efficiency_mean", 0.91))
    charge_efficiency_sigma = float(sim.get("charge_efficiency_sigma", 0.015))
    minimum_charge_efficiency = float(sim.get("minimum_charge_efficiency", 0.82))
    emergency_cost = float(opt.get("emergency_lease_cost", 450.0))
    unserved_cost = float(opt.get("unserved_cost", 1200.0))
    critical_multiplier = float(opt.get("critical_multiplier", 3.0))
    overage_cost = float(opt.get("charging_overage_cost", 12.0))
    fixed_move_cost = (
        float(plan.moves["count"].sum()) * float(opt.get("redeployment_cost", 90.0)) if not plan.moves.empty else 0.0
    )
    temporary_additions = (
        int(plan.allocation["temporary_addition"].sum()) if "temporary_addition" in plan.allocation else 0
    )
    fixed_temporary_vehicle_cost = temporary_additions * emergency_cost
    route_split_factor = float(plan.solver.get("route_split_factor", 1.0))
    temporary_charging_capacity = {
        str(key): float(value) for key, value in dict(plan.solver.get("temporary_charging_capacity_kwh", {})).items()
    }
    fixed_temporary_charging_cost = float(sum(temporary_charging_capacity.values())) * float(
        opt.get("temporary_charging_capacity_cost_per_kwh", 2.0)
    )
    fixed_route_split_program_cost = float(plan.solver.get("route_split_program_cost", 0.0))
    fixed_corrective_cost = (
        fixed_temporary_vehicle_cost + fixed_temporary_charging_cost + fixed_route_split_program_cost
    )

    run_rows: list[dict[str, Any]] = []
    for run_index in range(run_count):
        rng = np.random.default_rng(_stable_seed(base_seed, "run", run_index))
        scenario_id = int(rng.choice(scenario_ids, p=weights))
        scenario = scenarios[scenario_id]
        global_energy_multiplier = float(rng.lognormal(-0.5 * energy_sigma**2, energy_sigma))
        route_energy_noise = {route_id: float(rng.normal(1.0, 0.035)) for route_id in scenario["route_ids"]}
        charge_efficiency = {
            route_id: max(
                float(rng.normal(charge_efficiency_mean, charge_efficiency_sigma)),
                minimum_charge_efficiency,
            )
            for route_id in scenario["route_ids"]
        }
        emergency_draw = {
            (route_id, segment): float(rng.random()) for route_id in scenario["route_ids"] for segment in range(4)
        }

        station_ids = sorted(scenario["stations"])
        failure_noise = {
            (station_id, vehicle_type): (float(rng.lognormal(-0.5 * 0.30**2, 0.30)), float(rng.random()))
            for station_id in station_ids
            for vehicle_type in vehicle_types
        }
        late_return_noise = {station_id: float(rng.normal(0.0, late_return_sigma)) for station_id in station_ids}
        additional_outage = {station_id: bool(rng.random() < outage_probability) for station_id in station_ids}
        outage_derate = {station_id: float(rng.uniform(0.50, 0.76)) for station_id in station_ids}
        repair_draws = {
            station_id: [float(rng.random()) for _ in range(int(stations[station_id]["repair_capacity"]))]
            for station_id in station_ids
        }

        served = 0
        critical_served = 0
        shortages = 0
        critical_shortages = 0
        total_idle = 0
        total_emergency = 0
        variable_operating_cost = 0.0
        charging_overage_kwh = 0.0
        physical_energy_compatible = True
        charging_feasible = True
        max_requirement_ratio = 1.0
        split_routes = 0
        total_failed_vehicles = 0
        total_repaired_vehicles = 0
        total_repair_capacity = 0
        max_charge_acceptance_ratio = 0.0
        station_shortages: dict[str, int] = {}
        station_charging_overage: dict[str, float] = {}
        station_emergency: dict[str, int] = {}
        station_repaired: dict[str, int] = {}

        for station_id in station_ids:
            station_scenario = scenario["stations"][station_id]
            station = stations[station_id]
            random_delay = max(0.0, station_scenario["return_delay_minutes"] + late_return_noise[station_id])
            dwell = max(station["mean_dwell_hours"] - random_delay / 60.0, 2.0)
            outage_factor = outage_derate[station_id] if additional_outage[station_id] else 1.0

            failure_multiplier = station_scenario["failure_multiplier"]
            failed_by_type: dict[str, int] = {}
            for vehicle_type in vehicle_types:
                count = allocation.get((station_id, vehicle_type), 0)
                probability = min(0.75, base_failure_probability * failure_multiplier)
                noise, rounding_draw = failure_noise[(station_id, vehicle_type)]
                expected_failures = count * min(0.75, probability * noise)
                failed_by_type[vehicle_type] = int(
                    np.floor(expected_failures) + (rounding_draw < expected_failures % 1.0)
                )

            station_failed = int(sum(failed_by_type.values()))
            repair_capacity = int(station["repair_capacity"])
            failed_by_type, station_repaired_count = _apply_same_day_repairs(
                failed_by_type,
                specs,
                repair_capacity,
                repair_draws[station_id],
                repair_success_probability,
            )

            available = {
                vehicle_type: max(allocation.get((station_id, vehicle_type), 0) - failed_by_type[vehicle_type], 0)
                for vehicle_type in vehicle_types
            }
            total_failed_vehicles += station_failed
            total_repaired_vehicles += station_repaired_count
            total_repair_capacity += repair_capacity
            station_repaired[station_id] = station_repaired_count
            initial_available = sum(available.values())
            assigned_here = 0
            energy_here = 0.0
            shortage_here = 0
            emergency_here = 0

            for route in station_scenario["routes"]:
                route_id = route["route_id"]
                original_energy = route["energy_kwh"] * global_energy_multiplier * route_energy_noise[route_id]
                original_cube = route["required_cube"]
                usable_for_cube = [spec["usable"] for spec in specs.values() if spec["cube"] >= original_cube]
                max_usable = max(usable_for_cube) if usable_for_cube else max(spec["usable"] for spec in specs.values())
                requirement_ratio = max(original_cube / max_cube, original_energy / max(max_usable, 1e-6))
                max_requirement_ratio = max(max_requirement_ratio, requirement_ratio)
                apply_split = route_split_factor < 1.0 and requirement_ratio > 1.0
                split_routes += int(apply_split)
                segment_fractions = (route_split_factor, 1.0 - route_split_factor) if apply_split else (1.0,)
                segment_fractions = tuple(fraction for fraction in segment_fractions if fraction > 1e-9)
                tentative_available = available.copy()
                tentative_emergency = emergency_here
                tentative_assignments: list[tuple[str, bool, float]] = []
                route_physically_compatible = True
                for segment_index, fraction in enumerate(segment_fractions):
                    sampled_energy = original_energy * fraction
                    effective_cube = original_cube * fraction
                    physically_compatible = [
                        vehicle_type
                        for vehicle_type in vehicle_types
                        if specs[vehicle_type]["cube"] >= effective_cube
                        and specs[vehicle_type]["usable"] >= sampled_energy
                    ]
                    if not physically_compatible:
                        route_physically_compatible = False
                        break
                    required_grid_energy = sampled_energy / charge_efficiency[route_id]
                    compatible = []
                    for vehicle_type in physically_compatible:
                        replenishable_energy = (
                            min(specs[vehicle_type]["charge_power_kw"], station["charger_power_kw"])
                            * dwell
                            * individual_charge_taper
                        )
                        max_charge_acceptance_ratio = max(
                            max_charge_acceptance_ratio,
                            required_grid_energy / max(replenishable_energy, 1e-9),
                        )
                        if replenishable_energy + 1e-9 >= required_grid_energy:
                            compatible.append(vehicle_type)
                    if not compatible:
                        charging_feasible = False
                        break
                    chosen = next(
                        (vehicle_type for vehicle_type in compatible if tentative_available[vehicle_type] > 0),
                        None,
                    )
                    leased = False
                    if (
                        chosen is None
                        and tentative_emergency < emergency_cap
                        and emergency_draw[(route_id, segment_index)] < emergency_fulfillment
                    ):
                        chosen = compatible[0]
                        leased = True
                        tentative_emergency += 1
                    if chosen is None:
                        break
                    if not leased:
                        tentative_available[chosen] -= 1
                    tentative_assignments.append((chosen, leased, fraction))

                if not route_physically_compatible:
                    physical_energy_compatible = False
                if len(tentative_assignments) != len(segment_fractions):
                    shortages += 1
                    shortage_here += 1
                    critical_shortages += int(route["critical"])
                    continue

                available = tentative_available
                new_emergency = tentative_emergency - emergency_here
                emergency_here = tentative_emergency
                total_emergency += new_emergency
                assigned_here += sum(not leased for _, leased, _ in tentative_assignments)
                served += 1
                critical_served += int(route["critical"])
                distance_multiplier = 1.0 + route_split_distance_overhead if apply_split else 1.0
                for chosen, _, fraction in tentative_assignments:
                    variable_operating_cost += (
                        route["distance_miles"]
                        * fraction
                        * distance_multiplier
                        * specs[chosen]["variable_cost_per_mile"]
                    )
                    energy_here += original_energy * fraction / charge_efficiency[route_id]

            station_shortages[station_id] = shortage_here
            station_emergency[station_id] = emergency_here
            total_idle += max(initial_available - assigned_here, 0)
            site_capacity = (
                min(
                    station["site_power_kw"] * dwell,
                    station["charger_ports"] * station["charger_power_kw"] * dwell,
                )
                * station_scenario["charger_capacity_factor"]
                * outage_factor
            )
            taper = 0.88 if energy_here > 0.75 * site_capacity else 0.96
            effective_capacity = site_capacity * taper + temporary_charging_capacity.get(station_id, 0.0)
            overage = max(energy_here - effective_capacity, 0.0)
            station_charging_overage[station_id] = overage
            charging_overage_kwh += overage
            charging_feasible = charging_feasible and overage <= 1e-9

        total_routes = scenario["route_count"]
        critical_total = scenario["critical_count"]
        overcapacity_cost = total_idle * float(sim.get("idle_vehicle_cost", 85.0))
        shortage_cost = (shortages - critical_shortages) * unserved_cost
        shortage_cost += critical_shortages * unserved_cost * critical_multiplier
        charging_cost = charging_overage_kwh * overage_cost
        realized_cost = (
            fixed_move_cost
            + fixed_corrective_cost
            + total_emergency * emergency_cost
            + variable_operating_cost
            + overcapacity_cost
            + shortage_cost
            + charging_cost
        )
        run_rows.append(
            {
                "run": run_index,
                "scenario_id": scenario_id,
                "regime": scenario["regime"],
                "service_rate": served / max(total_routes, 1),
                "critical_service_rate": 1.0 if critical_total == 0 else critical_served / critical_total,
                "energy_feasible": int(physical_energy_compatible),
                "charging_feasible": int(charging_feasible),
                "unserved_routes": shortages,
                "critical_unserved": critical_shortages,
                "emergency_leases": total_emergency,
                "variable_operating_cost": variable_operating_cost,
                "idle_vehicles": total_idle,
                "overcapacity_cost": overcapacity_cost,
                "charging_overage_kwh": charging_overage_kwh,
                "realized_total_cost": realized_cost,
                "temporary_additions": temporary_additions,
                "fixed_temporary_vehicle_cost": fixed_temporary_vehicle_cost,
                "fixed_temporary_charging_cost": fixed_temporary_charging_cost,
                "fixed_route_split_program_cost": fixed_route_split_program_cost,
                "fixed_corrective_cost": fixed_corrective_cost,
                "split_routes": split_routes,
                "max_requirement_ratio": max_requirement_ratio,
                "energy_multiplier": global_energy_multiplier,
                "failed_vehicles": total_failed_vehicles,
                "repaired_vehicles": total_repaired_vehicles,
                "repair_capacity_slots": total_repair_capacity,
                "repair_capacity_utilization": total_repaired_vehicles / max(total_repair_capacity, 1),
                "max_charge_acceptance_ratio": max_charge_acceptance_ratio,
                "station_shortages": json.dumps(station_shortages, sort_keys=True),
                "station_emergency_leases": json.dumps(station_emergency, sort_keys=True),
                "station_repaired_vehicles": json.dumps(station_repaired, sort_keys=True),
                "station_charging_overage_kwh": json.dumps(station_charging_overage, sort_keys=True),
            }
        )

    frame = pd.DataFrame(run_rows)
    summary = {
        "runs": run_count,
        "mean_service_coverage": float(frame.service_rate.mean()),
        "service_coverage_lcb": _mean_lower(frame.service_rate),
        "mean_critical_coverage": float(frame.critical_service_rate.mean()),
        "critical_coverage_lcb": _mean_lower(frame.critical_service_rate),
        "energy_feasibility_lcb": _wilson_lower(int(frame.energy_feasible.sum()), run_count),
        "charging_feasibility_lcb": _wilson_lower(int(frame.charging_feasible.sum()), run_count),
        "shortage_cvar95": _tail_mean(frame.unserved_routes, 0.95),
        "cost_cvar95": _tail_mean(frame.realized_total_cost, 0.95),
        "mean_realized_total_cost": float(frame.realized_total_cost.mean()),
        "mean_overcapacity_cost": float(frame.overcapacity_cost.mean()),
        "mean_idle_vehicles": float(frame.idle_vehicles.mean()),
        "mean_emergency_leases": float(frame.emergency_leases.mean()),
        "mean_variable_operating_cost": float(frame.variable_operating_cost.mean()),
        "mean_charging_overage_kwh": float(frame.charging_overage_kwh.mean()),
        "requirement_ratio_p95": float(frame.max_requirement_ratio.quantile(0.95)),
        "mean_split_routes": float(frame.split_routes.mean()),
        "fixed_corrective_cost": fixed_corrective_cost,
        "mean_failed_vehicles": float(frame.failed_vehicles.mean()),
        "mean_repaired_vehicles": float(frame.repaired_vehicles.mean()),
        "mean_repair_capacity_utilization": float(frame.repair_capacity_utilization.mean()),
        "charge_acceptance_ratio_p95": float(frame.max_charge_acceptance_ratio.quantile(0.95)),
    }
    return SimulationOutput(runs=frame, summary=summary)


def compare_simulations(
    reference: SimulationOutput,
    challenger: SimulationOutput,
    label: str,
    bootstrap_replications: int = 1000,
) -> dict[str, float | str]:
    if int(bootstrap_replications) <= 0:
        raise ValueError("bootstrap_replications must be positive")
    keys = ["run", "scenario_id"]
    reference_keys = reference.runs[keys].sort_values(keys).reset_index(drop=True)
    challenger_keys = challenger.runs[keys].sort_values(keys).reset_index(drop=True)
    if reference_keys.empty or challenger_keys.empty:
        raise ValueError("Paired simulation comparison requires non-empty run tables")
    if not reference_keys.equals(challenger_keys):
        raise ValueError("Paired simulation runs must have identical run/scenario keys")
    merged = reference.runs.merge(
        challenger.runs,
        on=keys,
        suffixes=("_reference", "_challenger"),
        validate="one_to_one",
    )
    cost_difference = merged["realized_total_cost_reference"] - merged["realized_total_cost_challenger"]
    service_difference = merged["service_rate_challenger"] - merged["service_rate_reference"]
    shortage_difference = merged["unserved_routes_reference"] - merged["unserved_routes_challenger"]
    rng = np.random.default_rng(20260616)
    n = len(merged)
    indices = rng.integers(0, n, size=(int(bootstrap_replications), n))
    bootstrap_cost = cost_difference.to_numpy(float)[indices].mean(axis=1)
    bootstrap_service = service_difference.to_numpy(float)[indices].mean(axis=1)
    bootstrap_shortage = shortage_difference.to_numpy(float)[indices].mean(axis=1)
    return {
        "comparison": label,
        "paired_runs": n,
        "mean_cost_savings": float(cost_difference.mean()),
        "cost_savings_ci95_low": float(np.quantile(bootstrap_cost, 0.025)),
        "cost_savings_ci95_high": float(np.quantile(bootstrap_cost, 0.975)),
        "mean_service_gain": float(service_difference.mean()),
        "service_gain_ci95_low": float(np.quantile(bootstrap_service, 0.025)),
        "service_gain_ci95_high": float(np.quantile(bootstrap_service, 0.975)),
        "mean_shortage_reduction": float(shortage_difference.mean()),
        "shortage_reduction_ci95_low": float(np.quantile(bootstrap_shortage, 0.025)),
        "shortage_reduction_ci95_high": float(np.quantile(bootstrap_shortage, 0.975)),
        "challenger_win_rate_cost": float((cost_difference > 0).mean()),
    }
