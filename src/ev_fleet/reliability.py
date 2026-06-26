from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from .config import ProjectConfig
from .optimization import PlanResult
from .simulation import SimulationOutput


@dataclass
class ReliabilityDecision:
    status: str
    metrics: dict
    checks: dict
    reasons: list[str]

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "metrics": self.metrics,
            "checks": self.checks,
            "reasons": self.reasons,
        }


def evaluate_rcce(cfg: ProjectConfig, plan: PlanResult, simulation: SimulationOutput) -> ReliabilityDecision:
    thresholds = cfg.section("reliability")
    metrics = dict(simulation.summary)
    metrics["allocation_churn"] = int(plan.moves["count"].sum()) if not plan.moves.empty else 0
    allocation = plan.allocation.copy()
    planned_total = int(allocation["planned_count"].sum())
    initial_total = int(allocation["initial_count"].sum())
    temporary_total = int(allocation.get("temporary_addition", pd.Series(0, index=allocation.index)).sum())
    metrics["fleet_balance_difference"] = planned_total - initial_total - temporary_total
    metrics["negative_planned_count_rows"] = int((allocation["planned_count"] < 0).sum())

    stations = pd.read_csv(cfg.generated_dir / "stations.csv").set_index("station_id")
    station_totals = allocation.groupby("station_id")["planned_count"].sum()
    parking_excess = {
        str(station_id): max(0, int(total) - int(stations.loc[station_id, "parking_capacity"]))
        for station_id, total in station_totals.items()
    }
    metrics["max_parking_capacity_excess"] = max(parking_excess.values(), default=0)
    metrics["parking_capacity_excess_by_station"] = parking_excess
    unresolved_parking_target = int(plan.solver.get("unmet_capacity_due_to_parking", 0))
    metrics["unmet_corrective_capacity_due_to_parking"] = unresolved_parking_target
    unresolved_charging_target = float(plan.solver.get("unmet_charging_capacity_due_to_limit_kwh", 0.0))
    metrics["unmet_corrective_charging_capacity_kwh"] = unresolved_charging_target

    degraded_mode = bool(plan.solver.get("degraded_mode", False))
    raw_mip_gap = plan.solver.get("mip_gap")
    mip_gap = float(raw_mip_gap) if raw_mip_gap is not None else float("nan")
    metrics["solver_mip_gap"] = mip_gap if math.isfinite(mip_gap) else None
    raw_bound_violation = plan.solver.get("max_bound_violation")
    bound_violation = float(raw_bound_violation) if raw_bound_violation is not None else float("nan")
    raw_constraint_violation = plan.solver.get("max_constraint_violation")
    constraint_violation = float(raw_constraint_violation) if raw_constraint_violation is not None else float("nan")
    raw_integrality_violation = plan.solver.get("max_integrality_violation")
    integrality_violation = float(raw_integrality_violation) if raw_integrality_violation is not None else float("nan")
    raw_objective_error = plan.solver.get("objective_reconciliation_error")
    objective_error = float(raw_objective_error) if raw_objective_error is not None else float("nan")
    metrics["solver_max_bound_violation"] = bound_violation if math.isfinite(bound_violation) else None
    metrics["solver_max_constraint_violation"] = constraint_violation if math.isfinite(constraint_violation) else None
    metrics["solver_max_integrality_violation"] = (
        integrality_violation if math.isfinite(integrality_violation) else None
    )
    metrics["solver_objective_reconciliation_error"] = objective_error if math.isfinite(objective_error) else None
    metrics["solver_termination_class"] = plan.solver.get("termination_class")
    metrics["solver_runtime_seconds"] = plan.solver.get("runtime_seconds")
    maximum_mip_gap = float(
        thresholds.get("max_solver_mip_gap", cfg.section("optimization").get("mip_relative_gap", 0.03))
    )
    maximum_constraint_violation = float(thresholds.get("max_solver_constraint_violation", 1e-6))
    maximum_integrality_violation = float(thresholds.get("max_solver_integrality_violation", 1e-6))
    maximum_objective_error = float(thresholds.get("max_solver_objective_error", 1e-6))
    is_manual_override = plan.status == "OVERRIDE_CANDIDATE"
    metrics["manual_override_requires_approval"] = is_manual_override
    valid_plan_status = plan.status in {
        "OPTIMAL",
        "FEASIBLE_LIMIT",
        "CORRECTIVE_CANDIDATE",
        "OVERRIDE_CANDIDATE",
    }
    valid_termination_class = plan.solver.get("termination_class") in {"OPTIMAL", "LIMIT_REACHED"}
    checks = {
        "service_coverage": metrics["service_coverage_lcb"] >= float(thresholds["service_coverage_lcb"]),
        "critical_coverage": metrics["critical_coverage_lcb"] >= float(thresholds["critical_coverage_lcb"]),
        "energy_feasibility": metrics["energy_feasibility_lcb"] >= float(thresholds["energy_feasibility_lcb"]),
        "charging_feasibility": metrics["charging_feasibility_lcb"] >= float(thresholds["charging_feasibility_lcb"]),
        "shortage_cvar": metrics["shortage_cvar95"] <= float(thresholds["max_shortage_cvar"]),
        "overcapacity": metrics["mean_overcapacity_cost"] <= float(thresholds["max_overcapacity_cost"]),
        "churn": metrics["allocation_churn"] <= int(thresholds["max_churn"]),
        "fleet_balance": metrics["fleet_balance_difference"] == 0 and metrics["negative_planned_count_rows"] == 0,
        "parking_capacity": metrics["max_parking_capacity_excess"] == 0,
        "corrective_capacity_implementable": unresolved_parking_target == 0,
        "corrective_charging_capacity_implementable": unresolved_charging_target <= 1e-9,
        "solver_feasible": valid_plan_status and valid_termination_class,
        "solver_bounds": math.isfinite(bound_violation) and bound_violation <= maximum_constraint_violation,
        "solver_constraints": (
            math.isfinite(constraint_violation) and constraint_violation <= maximum_constraint_violation
        ),
        "solver_integrality": (
            math.isfinite(integrality_violation) and integrality_violation <= maximum_integrality_violation
        ),
        "solver_objective_reconciliation": (
            math.isfinite(objective_error) and objective_error <= maximum_objective_error
        ),
        "solver_gap": math.isfinite(mip_gap) and mip_gap <= maximum_mip_gap,
        "solver_not_degraded": not degraded_mode,
        "manual_override_approval": not is_manual_override,
    }
    hard_checks = (
        "energy_feasibility",
        "charging_feasibility",
        "fleet_balance",
        "parking_capacity",
        "solver_feasible",
        "solver_bounds",
        "solver_constraints",
        "solver_integrality",
        "solver_objective_reconciliation",
    )
    hard_fail = any(not checks[name] for name in hard_checks)
    failures = [name for name, passed in checks.items() if not passed]
    if hard_fail:
        status = "BLOCK"
    elif failures:
        status = "REVIEW"
    else:
        status = "RELEASE"
    reasons = [f"{name}: {'PASS' if passed else 'FAIL'}" for name, passed in checks.items()]
    return ReliabilityDecision(status=status, metrics=metrics, checks=checks, reasons=reasons)
