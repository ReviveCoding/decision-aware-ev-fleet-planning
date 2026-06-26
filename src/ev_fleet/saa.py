from __future__ import annotations

from pathlib import Path

import pandas as pd

from .config import ProjectConfig
from .data_generation import create_scenario_replication
from .evaluation import cvar, evaluate_allocation_on_scenarios, weighted_mean
from .io_utils import write_csv_atomic, write_json_atomic
from .optimization import solve_fleet_plan


def run_saa(
    cfg: ProjectConfig,
    forecast_path: str | Path,
    evaluation_scenarios: pd.DataFrame,
) -> pd.DataFrame:
    saa_config = cfg.section("saa")
    rows: list[dict] = []
    allocations: list[pd.DataFrame] = []
    for scenario_size in saa_config.get("scenario_sizes", [3, 5]):
        for replication in range(int(saa_config.get("replications", 2))):
            scenarios = create_scenario_replication(cfg, forecast_path, int(scenario_size), replication)
            plan = solve_fleet_plan(cfg, scenarios, name=f"saa_{scenario_size}_{replication}", stochastic=True)
            evaluation = evaluate_allocation_on_scenarios(cfg, plan, evaluation_scenarios)
            rows.append(
                {
                    "scenario_size": int(scenario_size),
                    "replication": replication,
                    "in_sample_objective": plan.objective,
                    "out_of_sample_cost": weighted_mean(evaluation, "realized_cost"),
                    "out_of_sample_service_rate": weighted_mean(evaluation, "service_rate"),
                    "out_of_sample_shortage_cvar95": cvar(evaluation.unserved_routes.to_numpy(), 0.95),
                    "status": plan.status,
                    "mip_gap": plan.solver.get("mip_gap"),
                    "allocation_churn": int(plan.moves["count"].sum()) if not plan.moves.empty else 0,
                }
            )
            allocation = plan.allocation[["station_id", "vehicle_type", "planned_count"]].copy()
            allocation["scenario_size"] = int(scenario_size)
            allocation["replication"] = replication
            allocations.append(allocation)

    result = pd.DataFrame(rows)
    write_csv_atomic(cfg.artifacts_dir / "saa_runs.csv", result)
    summary: dict[str, object] = {"scenario_sizes": []}
    if allocations:
        allocation_frame = pd.concat(allocations, ignore_index=True)
        stability = (
            allocation_frame.groupby(["scenario_size", "station_id", "vehicle_type"])["planned_count"]
            .agg(["mean", "std", "min", "max"])
            .reset_index()
        )
        stability["std"] = stability["std"].fillna(0.0)
        write_csv_atomic(cfg.artifacts_dir / "saa_allocation_stability.csv", stability)
        grouped = result.groupby("scenario_size")
        summary["scenario_sizes"] = [
            {
                "scenario_size": int(size),
                "mean_out_of_sample_cost": float(group["out_of_sample_cost"].mean()),
                "std_out_of_sample_cost": float(group["out_of_sample_cost"].std(ddof=1) if len(group) > 1 else 0.0),
                "mean_shortage_cvar95": float(group["out_of_sample_shortage_cvar95"].mean()),
                "max_allocation_std": float(stability[stability.scenario_size == size]["std"].max()),
            }
            for size, group in grouped
        ]
    write_json_atomic(cfg.artifacts_dir / "saa_summary.json", summary)
    return result
