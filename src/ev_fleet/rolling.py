from __future__ import annotations

import pandas as pd

from .config import ProjectConfig
from .evaluation import evaluate_allocation_on_scenarios, weighted_mean
from .optimization import PlanResult, solve_fleet_plan


def _post_trigger_stale_plan(base_plan: PlanResult) -> PlanResult:
    """Represent the already-executed base allocation with no incremental move cost."""
    return PlanResult(
        name=f"{base_plan.name}_post_trigger_stale",
        status=base_plan.status,
        objective=base_plan.objective,
        allocation=base_plan.allocation.copy(),
        moves=pd.DataFrame(columns=["from_station", "to_station", "vehicle_type", "count"]),
        scenario_metrics=base_plan.scenario_metrics.copy(),
        solver={**base_plan.solver, "cost_boundary": "post_trigger_incremental"},
    )


def build_compound_disruption(scenarios: pd.DataFrame, demand_multiplier: float = 1.25) -> pd.DataFrame:
    """Create a deterministic compound stress set from held-out scenarios.

    The highest-volume station receives extra routes while charging capacity and
    vehicle availability are simultaneously derated. Route duplication is used
    only to construct a transparent offline stress case.
    """
    stressed_frames: list[pd.DataFrame] = []
    global_counter = 0
    for scenario_id, group in scenarios.groupby("scenario_id", sort=True):
        frame = group.copy()
        station_counts = frame.groupby("station_id").size()
        target_station = str(station_counts.idxmax())
        target_routes = frame[frame.station_id.astype(str) == target_station].copy()
        additions = max(1, int(round(len(target_routes) * (demand_multiplier - 1.0))))
        extra = (
            target_routes.sort_values(["criticality", "required_cube"], ascending=[True, False]).head(additions).copy()
        )
        extra["required_cube"] = extra["required_cube"] * 1.05
        extra["energy_kwh"] = extra["energy_kwh"] * 1.08
        extra["regime"] = "compound_reoptimization_trigger"
        frame.loc[frame.station_id.astype(str) == target_station, "charger_capacity_factor"] = frame.loc[
            frame.station_id.astype(str) == target_station, "charger_capacity_factor"
        ].clip(upper=0.62)
        frame.loc[frame.station_id.astype(str) == target_station, "vehicle_failure_multiplier"] = frame.loc[
            frame.station_id.astype(str) == target_station, "vehicle_failure_multiplier"
        ].clip(lower=1.7)
        frame.loc[frame.station_id.astype(str) == target_station, "return_delay_minutes"] = frame.loc[
            frame.station_id.astype(str) == target_station, "return_delay_minutes"
        ].clip(lower=45.0)
        for index in extra.index:
            global_counter += 1
            extra.loc[index, "route_id"] = f"D{int(scenario_id):03d}_EXTRA_{global_counter:06d}"
        stressed_frames.append(pd.concat([frame, extra], ignore_index=True))
    stressed = pd.concat(stressed_frames, ignore_index=True)
    return stressed


def run_rolling_reoptimization(
    cfg: ProjectConfig,
    base_plan: PlanResult,
    evaluation_scenarios: pd.DataFrame,
) -> tuple[PlanResult, pd.DataFrame, dict[str, float | str]]:
    if evaluation_scenarios.empty:
        raise ValueError("Rolling reoptimization requires non-empty evaluation scenarios")
    scenario_limit = int(
        cfg.section("optimization").get(
            "rolling_evaluation_scenarios",
            evaluation_scenarios["scenario_id"].nunique(),
        )
    )
    selected_ids = sorted(evaluation_scenarios["scenario_id"].unique())[:scenario_limit]
    rolling_scenarios = evaluation_scenarios[evaluation_scenarios["scenario_id"].isin(selected_ids)].copy()
    per_scenario_weight = 1.0 / max(len(selected_ids), 1)
    rolling_scenarios["scenario_weight"] = per_scenario_weight
    stressed = build_compound_disruption(rolling_scenarios)
    rolling_plan = solve_fleet_plan(
        cfg,
        stressed,
        name="rolling_compound_disruption_reoptimization",
        stochastic=True,
        initial_fleet_override=base_plan.allocation,
    )
    stale_plan = _post_trigger_stale_plan(base_plan)
    stale_evaluation = evaluate_allocation_on_scenarios(cfg, stale_plan, stressed)
    rolling_evaluation = evaluate_allocation_on_scenarios(cfg, rolling_plan, stressed)
    stale_keys = stale_evaluation[["scenario_id"]].sort_values("scenario_id").reset_index(drop=True)
    rolling_keys = rolling_evaluation[["scenario_id"]].sort_values("scenario_id").reset_index(drop=True)
    if not stale_keys.equals(rolling_keys):
        raise ValueError("Rolling comparison requires identical stale and reoptimized scenario keys")
    comparison = stale_evaluation.merge(
        rolling_evaluation,
        on="scenario_id",
        suffixes=("_stale", "_rolling"),
        validate="one_to_one",
    )
    starting_fleet = int(base_plan.allocation["planned_count"].sum())
    rolling_fleet = int(rolling_plan.allocation["planned_count"].sum())
    if rolling_fleet != starting_fleet:
        raise ValueError(f"Rolling fleet conservation failed: starting={starting_fleet}, rolling={rolling_fleet}")
    metrics = {
        "stale_expected_cost": weighted_mean(stale_evaluation, "realized_cost"),
        "rolling_expected_cost": weighted_mean(rolling_evaluation, "realized_cost"),
        "expected_cost_savings": weighted_mean(stale_evaluation, "realized_cost")
        - weighted_mean(rolling_evaluation, "realized_cost"),
        "stale_service_rate": weighted_mean(stale_evaluation, "service_rate"),
        "rolling_service_rate": weighted_mean(rolling_evaluation, "service_rate"),
        "service_rate_gain": weighted_mean(rolling_evaluation, "service_rate")
        - weighted_mean(stale_evaluation, "service_rate"),
        "starting_fleet": float(starting_fleet),
        "rolling_fleet": float(rolling_fleet),
        "fleet_balance_difference": float(rolling_fleet - starting_fleet),
        "cost_boundary": "post_trigger_incremental; pre-trigger base redeployment is sunk and excluded from both policies",
    }
    return rolling_plan, comparison, metrics
