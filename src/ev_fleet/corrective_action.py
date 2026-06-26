from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd

from .config import ProjectConfig
from .optimization import PlanResult
from .simulation import SimulationOutput


def _preferred_vehicle_mix(cfg: ProjectConfig, routes: pd.DataFrame, station_id: str) -> dict[str, float]:
    specs = pd.read_csv(cfg.generated_dir / "vehicle_specs.csv").set_index("vehicle_type")
    choices: list[str] = []
    for _, route in routes[routes.station_id.astype(str) == str(station_id)].iterrows():
        compatible = []
        for vehicle_type, spec in specs.iterrows():
            usable = float(spec.battery_kwh) * float(spec.usable_fraction) - float(spec.reserve_kwh)
            if float(spec.cube_capacity) >= float(route.required_cube) and usable >= float(route.energy_kwh):
                compatible.append((float(spec.daily_fixed_cost), str(vehicle_type)))
        if compatible:
            choices.append(min(compatible)[1])
        else:
            choices.append("large_ev")
    if not choices:
        return {"delivery_ev": 1.0}
    counts = pd.Series(choices).value_counts(normalize=True)
    return {str(key): float(value) for key, value in counts.items()}


def _station_shortage_quantiles(simulation: SimulationOutput, quantile: float) -> dict[str, int]:
    observations: dict[str, list[int]] = {}
    for encoded in simulation.runs["station_shortages"]:
        values = json.loads(encoded)
        for station_id, shortage in values.items():
            observations.setdefault(str(station_id), []).append(int(shortage))
    return {
        station_id: int(math.ceil(float(np.quantile(values, quantile))))
        for station_id, values in observations.items()
        if values
    }


def _station_charging_overage_quantiles(simulation: SimulationOutput, quantile: float) -> dict[str, float]:
    observations: dict[str, list[float]] = {}
    for encoded in simulation.runs["station_charging_overage_kwh"]:
        values = json.loads(encoded)
        for station_id, overage in values.items():
            observations.setdefault(str(station_id), []).append(float(overage))
    return {
        station_id: float(max(0.0, np.quantile(values, quantile)))
        for station_id, values in observations.items()
        if values
    }


def build_resilience_candidate(
    cfg: ProjectConfig,
    plan: PlanResult,
    routes: pd.DataFrame,
    baseline_simulation: SimulationOutput,
    shortage_quantile: float | None = None,
) -> tuple[PlanResult, pd.DataFrame]:
    """Build a transparent, simulation-targeted temporary-capacity candidate.

    Additions are based on station shortage quantiles observed in the independent
    replay and distributed by route-compatible vehicle mix. Exact global
    minimality is not claimed; the resulting candidate must be replayed and gated.
    """
    allocation = plan.allocation.copy()
    if "temporary_addition" not in allocation.columns:
        allocation["temporary_addition"] = 0
    stations = pd.read_csv(cfg.generated_dir / "stations.csv").set_index("station_id")
    parking_capacity = stations["parking_capacity"].astype(int).to_dict()
    quantile = float(
        shortage_quantile
        if shortage_quantile is not None
        else cfg.section("reliability").get("corrective_shortage_quantile", 0.95)
    )
    shortage_targets = _station_shortage_quantiles(baseline_simulation, quantile)
    tail_target = float(cfg.section("reliability").get("max_shortage_cvar", 0.0))
    observed_tail = float(baseline_simulation.summary.get("shortage_cvar95", 0.0))
    tail_margin = max(0, int(math.ceil(observed_tail - tail_target)))
    if tail_margin > 0 and shortage_targets:
        stations = sorted(shortage_targets)
        weights = np.array([max(shortage_targets[station], 1) for station in stations], dtype=float)
        raw_margin = weights / weights.sum() * tail_margin
        margin_counts = np.floor(raw_margin).astype(int)
        remainder = tail_margin - int(margin_counts.sum())
        if remainder > 0:
            order = np.argsort(-(raw_margin - margin_counts))
            margin_counts[order[:remainder]] += 1
        for station, extra in zip(stations, margin_counts, strict=True):
            shortage_targets[station] += int(extra)
    action_rows: list[dict] = []
    previous_temporary_additions = int(allocation["temporary_addition"].sum())
    new_temporary_additions = 0
    unmet_due_to_parking = 0

    for station_id, target in sorted(shortage_targets.items()):
        if target <= 0:
            continue
        current_total = int(
            allocation.loc[allocation.station_id.astype(str).eq(str(station_id)), "planned_count"].sum()
        )
        headroom = max(0, int(parking_capacity.get(str(station_id), current_total)) - current_total)
        applied_target = min(int(target), headroom)
        parking_overflow = int(target) - applied_target
        if parking_overflow > 0:
            unmet_due_to_parking += parking_overflow
            action_rows.append(
                {
                    "action_type": "parking_capacity_expansion_required",
                    "station_id": station_id,
                    "vehicle_type": "n/a",
                    "temporary_addition": 0,
                    "shortage_quantile": quantile,
                    "observed_station_shortage_target": target,
                    "global_tail_margin": tail_margin,
                    "route_split_factor": 1.0,
                    "unmet_vehicle_capacity": parking_overflow,
                    "reason": "Temporary-vehicle target exceeds physical station parking capacity",
                }
            )
        if applied_target <= 0:
            continue
        mix = _preferred_vehicle_mix(cfg, routes, station_id)
        vehicle_types = sorted(mix)
        raw = np.array([mix[vehicle_type] * applied_target for vehicle_type in vehicle_types], dtype=float)
        additions = np.floor(raw).astype(int)
        remainder = applied_target - int(additions.sum())
        if remainder > 0:
            order = np.argsort(-(raw - additions))
            additions[order[:remainder]] += 1
        for vehicle_type, count in zip(vehicle_types, additions, strict=True):
            if count <= 0:
                continue
            mask = allocation.station_id.astype(str).eq(str(station_id)) & allocation.vehicle_type.astype(str).eq(
                str(vehicle_type)
            )
            if not mask.any():
                continue
            allocation.loc[mask, "planned_count"] += int(count)
            allocation.loc[mask, "temporary_addition"] += int(count)
            new_temporary_additions += int(count)
            action_rows.append(
                {
                    "station_id": station_id,
                    "vehicle_type": vehicle_type,
                    "temporary_addition": int(count),
                    "shortage_quantile": quantile,
                    "observed_station_shortage_target": target,
                    "global_tail_margin": tail_margin,
                    "reason": "Independent-replay station shortage quantile plus global shortage-CVaR margin",
                }
            )

    existing_charging_capacity = {
        str(key): float(value) for key, value in dict(plan.solver.get("temporary_charging_capacity_kwh", {})).items()
    }
    charging_quantile = max(
        quantile,
        float(cfg.section("reliability").get("corrective_charging_overage_quantile", 0.975)),
    )
    charging_targets = _station_charging_overage_quantiles(baseline_simulation, charging_quantile)
    max_charging_capacity = float(
        cfg.section("reliability").get("max_temporary_charging_capacity_kwh_per_station", 0.0)
    )
    added_charging_capacity: dict[str, float] = {}
    unmet_charging_capacity = 0.0
    for station_id, requested in sorted(charging_targets.items()):
        if requested <= 1e-9:
            continue
        existing = existing_charging_capacity.get(station_id, 0.0)
        available_increment = max(0.0, max_charging_capacity - existing)
        addition = min(float(math.ceil(requested)), available_increment)
        if addition > 0.0:
            existing_charging_capacity[station_id] = existing + addition
            added_charging_capacity[station_id] = addition
            action_rows.append(
                {
                    "action_type": "temporary_charging_capacity",
                    "station_id": station_id,
                    "vehicle_type": "n/a",
                    "temporary_addition": 0,
                    "temporary_charging_capacity_kwh": addition,
                    "shortage_quantile": quantile,
                    "charging_overage_quantile": charging_quantile,
                    "observed_station_shortage_target": shortage_targets.get(station_id, 0),
                    "global_tail_margin": tail_margin,
                    "route_split_factor": 1.0,
                    "unmet_vehicle_capacity": 0,
                    "reason": (
                        "Independent-replay charging-overage quantile addressed with temporary "
                        "overnight charging capacity or managed charging-window extension"
                    ),
                }
            )
        unmet = max(0.0, requested - addition)
        if unmet > 0.0:
            unmet_charging_capacity += unmet
            action_rows.append(
                {
                    "action_type": "charging_infrastructure_review",
                    "station_id": station_id,
                    "vehicle_type": "n/a",
                    "temporary_addition": 0,
                    "temporary_charging_capacity_kwh": 0.0,
                    "shortage_quantile": quantile,
                    "charging_overage_quantile": charging_quantile,
                    "observed_station_shortage_target": shortage_targets.get(station_id, 0),
                    "global_tail_margin": tail_margin,
                    "route_split_factor": 1.0,
                    "unmet_vehicle_capacity": 0,
                    "unmet_charging_capacity_kwh": unmet,
                    "reason": "Replay charging deficit exceeds configured temporary-capacity limit",
                }
            )

    requirement_ratio = float(baseline_simulation.summary.get("requirement_ratio_p95", 1.0))
    proposed_route_split_factor = min(1.0, 0.98 / max(requirement_ratio, 1.0))
    previous_route_split_factor = float(plan.solver.get("route_split_factor", 1.0))
    route_split_factor = min(previous_route_split_factor, proposed_route_split_factor)
    route_split_improved = route_split_factor < previous_route_split_factor - 1e-9
    if route_split_improved:
        action_rows.append(
            {
                "action_type": "route_split_or_midroute_charge",
                "station_id": "NETWORK",
                "vehicle_type": "n/a",
                "temporary_addition": 0,
                "shortage_quantile": quantile,
                "observed_station_shortage_target": 0,
                "global_tail_margin": tail_margin,
                "route_split_factor": route_split_factor,
                "reason": "Mitigate the replay P95 cube/energy requirement ratio with route splitting or planned mid-route charging",
            }
        )
    for row in action_rows:
        row.setdefault("action_type", "temporary_vehicle")
        row.setdefault("route_split_factor", route_split_factor)
        row.setdefault("unmet_vehicle_capacity", 0)
        row.setdefault("temporary_charging_capacity_kwh", 0.0)
        row.setdefault("unmet_charging_capacity_kwh", 0.0)
        row.setdefault("charging_overage_quantile", charging_quantile)

    emergency_cost = float(cfg.section("optimization").get("emergency_lease_cost", 450.0))
    charging_capacity_cost = float(cfg.section("optimization").get("temporary_charging_capacity_cost_per_kwh", 2.0))
    route_split_program_cost = (
        float(cfg.section("optimization").get("route_split_program_cost", 600.0)) if route_split_improved else 0.0
    )
    new_charging_capacity_total = float(sum(added_charging_capacity.values()))
    base_objective = float(plan.objective) if plan.objective is not None else 0.0
    candidate = PlanResult(
        name=f"{plan.name}_resilience_q{int(round(quantile * 100)):02d}",
        status="CORRECTIVE_CANDIDATE",
        objective=(
            base_objective
            + new_temporary_additions * emergency_cost
            + new_charging_capacity_total * charging_capacity_cost
            + route_split_program_cost
        ),
        allocation=allocation,
        moves=plan.moves.copy(),
        scenario_metrics=plan.scenario_metrics.copy(),
        solver={
            **plan.solver,
            "corrective_action": "simulation_shortage_quantile",
            "shortage_quantile": quantile,
            "global_tail_margin": tail_margin,
            "temporary_additions": previous_temporary_additions + new_temporary_additions,
            "new_temporary_additions": new_temporary_additions,
            "unmet_capacity_due_to_parking": max(
                int(plan.solver.get("unmet_capacity_due_to_parking", 0)), unmet_due_to_parking
            ),
            "temporary_charging_capacity_kwh": existing_charging_capacity,
            "new_temporary_charging_capacity_kwh": added_charging_capacity,
            "unmet_charging_capacity_due_to_limit_kwh": max(
                float(plan.solver.get("unmet_charging_capacity_due_to_limit_kwh", 0.0)),
                unmet_charging_capacity,
            ),
            "route_split_factor": min(float(plan.solver.get("route_split_factor", 1.0)), route_split_factor),
            "route_split_program_cost": (
                float(plan.solver.get("route_split_program_cost", 0.0)) + route_split_program_cost
            ),
            "minimality_claim": "No exact global minimality claim; candidate requires independent paired replay",
        },
    )
    return candidate, pd.DataFrame(action_rows)
