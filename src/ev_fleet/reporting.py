from __future__ import annotations

import pandas as pd

from .config import ProjectConfig
from .io_utils import write_json_atomic, write_text_atomic
from .optimization import PlanResult
from .reliability import ReliabilityDecision


def _format_objective(value: float | None) -> str:
    return "unavailable" if value is None else f"{value:,.2f}"


def _format_solver_value(value: object, digits: int = 3) -> str:
    if value is None:
        return "unavailable"
    try:
        return f"{float(value):.{digits}g}"
    except (TypeError, ValueError):
        return str(value)


def _solver_evidence_frame(plans: dict[str, PlanResult], recommended_plan: PlanResult) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for name, plan in {**plans, "recommended": recommended_plan}.items():
        solver = plan.solver
        rows.append(
            {
                "policy": name,
                "plan_status": plan.status,
                "termination": solver.get("termination_class"),
                "mip_gap": solver.get("mip_gap"),
                "max_constraint_violation": solver.get("max_constraint_violation"),
                "max_integrality_violation": solver.get("max_integrality_violation"),
                "objective_reconciliation_error": solver.get("objective_reconciliation_error"),
            }
        )
    return pd.DataFrame(rows)


def _markdown_table(frame: pd.DataFrame) -> str:
    """Render a compact Markdown table without the optional tabulate package."""
    if frame.empty:
        return "_No rows._"
    display = frame.copy()
    for column in display.columns:
        display[column] = display[column].map(lambda value: f"{value:.6g}" if isinstance(value, float) else str(value))
    headers = [str(column).replace("|", "\\|") for column in display.columns]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in display.itertuples(index=False, name=None):
        cells = [str(value).replace("|", "\\|").replace("\n", " ") for value in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def write_reports(
    *,
    cfg: ProjectConfig,
    forecast_comparison: pd.DataFrame,
    plans: dict[str, PlanResult],
    gates: dict[str, ReliabilityDecision],
    value: dict,
    saa: pd.DataFrame,
    base_policy_name: str,
    corrective_plan: PlanResult,
    corrective_gate: ReliabilityDecision,
    corrective_actions: pd.DataFrame,
    corrective_frontier: pd.DataFrame,
    paired_comparison: pd.DataFrame,
    rolling_metrics: dict[str, float | str],
    recommended_plan: PlanResult,
    recommended_gate: ReliabilityDecision,
    recommendation_source: str,
) -> None:
    reports = cfg.artifacts_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    selected_row = forecast_comparison.loc[forecast_comparison["selected"]].iloc[0]
    formal = value["formal_sample"]
    heldout = value["heldout_empirical"]
    recommended_solver = recommended_plan.solver
    solver_evidence = _solver_evidence_frame(plans, recommended_plan)
    release_text = (
        "Automatic release is permitted by the RCCE gate."
        if recommended_gate.status == "RELEASE"
        else "Automatic release is not permitted; planner review is required."
    )
    memo = f"""# Executive Decision Memo

## Recommendation

Use **{recommended_plan.name}** as the current decision candidate. Its RCCE status is **{recommended_gate.status}**. {release_text}
The selected base policy was **{base_policy_name}**, and the selected recommendation source was **{recommendation_source}**.

## Decision evidence

- Selected forecast model: `{selected_row["model"]}` after calibration gating and station-capacity-aware active-fleet decision replay.
- Selected-model P90 coverage: {selected_row["p90_coverage"]:.3f}
- Selected-model replay cost: {selected_row["decision_cost"]:,.2f}
- Selected-model active-fleet commitment: {selected_row["decision_active_fleet"]:.2f} vehicles/day
- Selected-model shortage CVaR90: {selected_row["decision_shortage_cvar90"]:.3f} routes
- Deterministic expected-value objective: {_format_objective(plans["deterministic"].objective)}
- Risk-neutral stochastic objective: {_format_objective(plans["risk_neutral"].objective)}
- Risk-aware CVaR objective: {_format_objective(plans["risk_aware"].objective)}
- Recommended solver termination: `{recommended_solver.get("termination_class", "unavailable")}`
- Recommended solver MIP gap: {_format_solver_value(recommended_solver.get("mip_gap"))}
- Recommended max linear-constraint violation: {_format_solver_value(recommended_solver.get("max_constraint_violation"))}
- Recommended max integrality violation: {_format_solver_value(recommended_solver.get("max_integrality_violation"))}
- Recommended objective reconciliation error: {_format_solver_value(recommended_solver.get("objective_reconciliation_error"))}
- Formal sample VSS: {formal["vss"]:,.2f}
- Formal sample EVPI: {formal["evpi"]:,.2f}
- Held-out empirical VSS: {heldout["vss"]:,.2f}
- Held-out EVPI estimate: {heldout["evpi"]:,.2f}
- Risk-aware service lower bound: {gates["risk_aware"].metrics["service_coverage_lcb"]:.3f}
- Corrective service lower bound: {corrective_gate.metrics["service_coverage_lcb"]:.3f}
- Corrective shortage CVaR95: {corrective_gate.metrics["shortage_cvar95"]:.3f}
- Corrective fixed capacity/program cost: {corrective_gate.metrics.get("fixed_corrective_cost", 0.0):,.2f}
- Mean failed vehicles in replay: {corrective_gate.metrics.get("mean_failed_vehicles", 0.0):.3f}
- Mean same-day repaired vehicles in replay: {corrective_gate.metrics.get("mean_repaired_vehicles", 0.0):.3f}
- P95 individual charge-acceptance ratio: {corrective_gate.metrics.get("charge_acceptance_ratio_p95", 0.0):.3f}
- Rolling reoptimization expected cost savings under compound disruption: {rolling_metrics["expected_cost_savings"]:,.2f}
- Rolling reoptimization service-rate gain: {rolling_metrics["service_rate_gain"]:.4f}

## Planned redeployment

{_move_text(recommended_plan)}

## Corrective action assessment

{_corrective_text(corrective_actions)}

## Release interpretation

The system separates nominal solver feasibility from empirical reliability under an independently parameterized replay. Policies are compared with common exogenous random draws. Advanced policies are not promoted solely because they are more complex; base-policy selection uses reliability status, failed gates, tail shortage, and realized cost.

## Claim boundary

This is an offline, public-data-informed synthetic decision-support prototype. It does not use actual Amazon EV fleet, charging, repair, DSP allocation, or production cost records.
"""
    write_text_atomic(reports / "executive_decision_memo.md", memo)

    gate_report = {
        "policies": {name: gate.to_dict() for name, gate in gates.items()},
        "corrective": corrective_gate.to_dict(),
        "recommended": recommended_gate.to_dict(),
        "recommendation_source": recommendation_source,
        "stochastic_value": value,
        "paired_policy_comparison": paired_comparison.to_dict(orient="records"),
        "corrective_frontier": corrective_frontier.to_dict(orient="records"),
        "rolling_reoptimization": rolling_metrics,
    }
    write_json_atomic(reports / "reliability_gate_report.json", gate_report)

    model_card = f"""# Model and Optimization Card

## Forecast ladder

{_markdown_table(forecast_comparison)}

## Optimization ladder

1. Carry-forward and proportional business baselines
2. Deterministic MILP built from a weighted expected-value workload
3. Risk-neutral two-stage scenario MILP for formal VSS/EVPI analysis
4. Risk-aware scenario MILP with CVaR tail-risk penalty
5. Independent held-out exact-recourse and Monte Carlo evaluation
6. Reliability-based base-policy selection rather than automatic complexity promotion
7. RCCE-triggered temporary-capacity frontier
8. Compound-disruption rolling reoptimization
9. SAA out-of-sample stability analysis and degraded-mode fallback

## Stochastic-value interpretation

- Formal sample VSS: {formal["vss"]:,.3f}
- Formal sample EVPI: {formal["evpi"]:,.3f}
- Held-out empirical VSS: {heldout["vss"]:,.3f}
- Held-out EVPI estimate: {heldout["evpi"]:,.3f}

Formal quantities use the same optimization sample and a risk-neutral policy. Held-out VSS measures generalization and may be negative without violating the stochastic-programming inequality.

## Solver release evidence

{_markdown_table(solver_evidence)}

An incumbent is release-eligible only when its termination class is allowed and its bound, linear-constraint, integrality, and objective-reconciliation residuals remain within configured tolerances. Solver feasibility is necessary but not sufficient; RCCE independently evaluates replay reliability.

## Paired policy comparison

{_markdown_table(paired_comparison)}

## Corrective frontier

{_markdown_table(corrective_frontier)}

## Reliability specialty

RCCE tracks service coverage, critical coverage, range/energy feasibility, individual charge acceptance, aggregate depot charging feasibility, shortage CVaR, overcapacity cost, allocation churn, and solver degradation as a vector gate rather than a single opaque score. Repair-bay capacity and probabilistic same-day repair success affect replay availability and are reported separately.

## Corrective-action claim boundary

Corrective candidates are triggered only when the selected base policy does not pass RCCE. They are derived from station-level shortage and charging-overage quantiles observed in independent replay and allocated under parking and temporary-charging limits. They are compared as a small reliability/cost frontier. Exact global minimality is not claimed.

## SAA runs

{_markdown_table(saa)}
"""
    write_text_atomic(reports / "model_and_optimization_card.md", model_card)

    limitations = """# Limitations

- Public sources are used for distributional inspiration and optional calibration, not row-level joining.
- Vehicle, charging-infrastructure, cost, repair, and dependence assumptions are synthetic and versioned.
- The optimizer uses route-level assignment, vehicle charge-acceptance limits, and aggregate depot-energy constraints rather than a full minute-level charger schedule.
- Repair-bay capacity is represented through expected availability in optimization and probabilistic same-day recovery in replay; detailed work-order queues and parts logistics are out of scope.
- Emergency leasing is capacity-limited in optimization and probabilistically fulfilled in replay; this is still a synthetic operating assumption.
- The independent replay is a decision-quality stress environment, not a validated production digital twin.
- Formal VSS and EVPI are sample-average estimates; held-out values are reported separately and can differ.
- The corrective frontier is simulation-targeted but is not an exact minimum-capacity proof.
- Offline results are simulated and must not be described as deployed business impact.
"""
    write_text_atomic(reports / "limitations.md", limitations)


def _move_text(plan: PlanResult) -> str:
    if plan.moves.empty:
        return "- Retain the existing station allocation; no planned redeployment was justified."
    return "\n".join(
        f"- Move {int(row['count'])} `{row['vehicle_type']}` vehicle(s) from {row['from_station']} to {row['to_station']}."
        for _, row in plan.moves.iterrows()
    )


def _corrective_text(actions: pd.DataFrame) -> str:
    if actions.empty:
        return "- No corrective action was required because the selected base policy already passed every RCCE gate."
    lines: list[str] = []
    for _, row in actions.iterrows():
        action_type = str(row.get("action_type", "temporary_vehicle"))
        station_id = str(row.get("station_id", "NETWORK"))
        round_number = row.get("corrective_round")
        round_suffix = f" in corrective round {int(round_number)}" if pd.notna(round_number) else ""
        if action_type == "route_split_or_midroute_charge":
            lines.append(
                "- Apply a route-split or planned mid-route charging control"
                f" with effective requirement factor {float(row['route_split_factor']):.3f}{round_suffix}."
            )
        elif action_type == "temporary_charging_capacity":
            lines.append(
                f"- Add {float(row['temporary_charging_capacity_kwh']):.1f} kWh/day of temporary "
                f"overnight charging capacity or managed charging-window extension at {station_id}{round_suffix}."
            )
        elif action_type == "charging_infrastructure_review":
            lines.append(
                f"- Escalate {station_id} for charging-infrastructure review; "
                f"{float(row.get('unmet_charging_capacity_kwh', 0.0)):.1f} kWh/day remains outside "
                f"the configured temporary-capacity limit{round_suffix}."
            )
        elif action_type == "parking_capacity_expansion_required":
            lines.append(
                f"- Escalate {station_id} for parking-capacity expansion; "
                f"{int(row.get('unmet_vehicle_capacity', 0))} requested vehicle space(s) remain unavailable"
                f"{round_suffix}."
            )
        else:
            lines.append(
                f"- Add {int(row['temporary_addition'])} temporary `{row['vehicle_type']}` vehicle(s) "
                f"at {station_id} based on the {float(row['shortage_quantile']):.0%} "
                f"station-shortage quantile{round_suffix}."
            )
    return "\n".join(lines)
