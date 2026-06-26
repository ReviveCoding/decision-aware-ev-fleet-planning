from __future__ import annotations

import logging
import platform
import sys
import time
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import numpy as np
import pandas as pd

from . import __version__
from .config import ProjectConfig
from .contracts import validate_generated_data, validate_required_artifacts, validate_scenarios
from .corrective_action import build_resilience_candidate
from .data_generation import build_route_scenarios, generate_public_informed_proxy_data
from .evaluation import stochastic_value_analysis
from .forecasting import integrate_gpu_candidate, run_forecasting
from .gpu_forecast import train_temporal_quantile_challenger
from .io_utils import sha256_file, write_csv_atomic, write_json_atomic
from .maintenance import (
    assert_safe_runtime_directory,
    clean_directory,
    pipeline_run_lock,
    recover_incomplete_promotions,
    remap_staged_result,
    staged_pipeline_config,
)
from .manifest import build_artifact_manifest, build_generated_data_manifest
from .optimization import PlanResult, save_plan, solve_fleet_plan
from .reliability import ReliabilityDecision, evaluate_rcce
from .reporting import write_reports
from .rolling import run_rolling_reoptimization
from .saa import run_saa
from .simulation import SimulationOutput, compare_simulations, simulate_plan
from .sql_pipeline import build_sql_marts

LOGGER = logging.getLogger(__name__)


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "not-installed"


def _environment_metadata(cfg: ProjectConfig) -> dict[str, object]:
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "packages": {
            "decision-aware-ev-fleet-planning": __version__,
            "installed-distribution-version": _package_version("decision-aware-ev-fleet-planning"),
            "numpy": _package_version("numpy"),
            "pandas": _package_version("pandas"),
            "scipy": _package_version("scipy"),
            "scikit-learn": _package_version("scikit-learn"),
        },
        "config_path": str(cfg.source_path),
        "config_sha256": sha256_file(cfg.source_path),
    }


def _solver_summary(plan: PlanResult) -> dict[str, object]:
    fields = (
        "termination_class",
        "status_code",
        "mip_gap",
        "mip_dual_bound",
        "max_bound_violation",
        "max_constraint_violation",
        "max_integrality_violation",
        "objective_reconciliation_error",
        "fallback_used",
    )
    return {field: plan.solver.get(field) for field in fields}


@contextmanager
def _stage(timings: dict[str, float], name: str):
    started = time.perf_counter()
    LOGGER.info("stage_started name=%s", name)
    try:
        yield
    finally:
        elapsed = round(time.perf_counter() - started, 6)
        timings[name] = elapsed
        LOGGER.info("stage_finished name=%s seconds=%.3f", name, elapsed)


def _clean_runtime_artifacts(root: Path, project_root: Path) -> None:
    assert_safe_runtime_directory(root, project_root)
    clean_directory(root)


def _decision_rank(decision: ReliabilityDecision, simulation: SimulationOutput) -> tuple[int, int, float, float]:
    status_rank = {"RELEASE": 0, "REVIEW": 1, "BLOCK": 2}[decision.status]
    failed_checks = sum(not passed for passed in decision.checks.values())
    cost = float(simulation.summary["mean_realized_total_cost"])
    shortage = float(simulation.summary["shortage_cvar95"])
    if decision.status == "RELEASE":
        # Once every reliability gate passes, minimize overreach/cost rather than
        # buying statistically small tail improvements with unnecessary capacity.
        return status_rank, failed_checks, cost, shortage
    return status_rank, failed_checks, shortage, cost


def _choose_policy(
    candidates: list[tuple[str, PlanResult, SimulationOutput, ReliabilityDecision]],
) -> tuple[str, PlanResult, SimulationOutput, ReliabilityDecision]:
    return min(candidates, key=lambda item: _decision_rank(item[3], item[2]))


def _build_corrective_frontier(
    cfg: ProjectConfig,
    base_plan: PlanResult,
    base_simulation: SimulationOutput,
    evaluation_scenarios: pd.DataFrame,
    base_gate: ReliabilityDecision,
) -> tuple[PlanResult, pd.DataFrame, SimulationOutput, ReliabilityDecision, pd.DataFrame]:
    if base_gate.status == "RELEASE":
        frontier = pd.DataFrame(
            [
                {
                    "candidate": base_plan.name,
                    "shortage_quantile": float("nan"),
                    "round": 0,
                    "temporary_additions": int(
                        base_plan.allocation.get("temporary_addition", pd.Series(dtype=int)).sum()
                    ),
                    "temporary_charging_capacity_kwh": float(
                        sum(dict(base_plan.solver.get("temporary_charging_capacity_kwh", {})).values())
                    ),
                    "status": base_gate.status,
                    "failed_checks": 0,
                    "service_coverage_lcb": base_gate.metrics["service_coverage_lcb"],
                    "charging_feasibility_lcb": base_gate.metrics["charging_feasibility_lcb"],
                    "shortage_cvar95": base_gate.metrics["shortage_cvar95"],
                    "mean_realized_total_cost": base_gate.metrics["mean_realized_total_cost"],
                    "mean_emergency_leases": base_gate.metrics.get("mean_emergency_leases", 0.0),
                    "route_split_factor": base_plan.solver.get("route_split_factor", 1.0),
                    "triggered": False,
                }
            ]
        )
        return base_plan, pd.DataFrame(), base_simulation, base_gate, frontier

    quantiles = [
        float(value) for value in cfg.section("reliability").get("corrective_shortage_quantiles", [0.75, 0.90, 0.95])
    ]
    maximum_rounds = int(cfg.section("reliability").get("corrective_max_rounds", 2))
    candidates: list[tuple[str, PlanResult, SimulationOutput, ReliabilityDecision, pd.DataFrame]] = []
    frontier_rows: list[dict] = []
    seen_signatures: set[tuple] = set()

    def signature(plan: PlanResult) -> tuple:
        charging = tuple(
            sorted(
                (str(key), round(float(value), 6))
                for key, value in dict(plan.solver.get("temporary_charging_capacity_kwh", {})).items()
            )
        )
        return (
            tuple(plan.allocation["planned_count"].astype(int)),
            round(float(plan.solver.get("route_split_factor", 1.0)), 8),
            charging,
        )

    def record(
        quantile: float,
        round_number: int,
        plan: PlanResult,
        simulation: SimulationOutput,
        gate: ReliabilityDecision,
        actions: pd.DataFrame,
    ) -> None:
        temporary_charging = float(sum(dict(plan.solver.get("temporary_charging_capacity_kwh", {})).values()))
        frontier_rows.append(
            {
                "candidate": plan.name,
                "shortage_quantile": quantile,
                "round": round_number,
                "temporary_additions": int(plan.allocation.get("temporary_addition", pd.Series(dtype=int)).sum()),
                "temporary_charging_capacity_kwh": temporary_charging,
                "status": gate.status,
                "failed_checks": sum(not passed for passed in gate.checks.values()),
                "service_coverage_lcb": gate.metrics["service_coverage_lcb"],
                "charging_feasibility_lcb": gate.metrics["charging_feasibility_lcb"],
                "shortage_cvar95": gate.metrics["shortage_cvar95"],
                "mean_realized_total_cost": gate.metrics["mean_realized_total_cost"],
                "mean_emergency_leases": gate.metrics.get("mean_emergency_leases", 0.0),
                "route_split_factor": plan.solver.get("route_split_factor", 1.0),
                "triggered": True,
            }
        )
        candidates.append((plan.name, plan, simulation, gate, actions))

    for quantile in quantiles:
        current_plan = base_plan
        current_simulation = base_simulation
        accumulated_actions: list[pd.DataFrame] = []
        previous_signature = signature(current_plan)
        for round_number in range(1, maximum_rounds + 1):
            candidate, actions = build_resilience_candidate(
                cfg,
                current_plan,
                evaluation_scenarios,
                current_simulation,
                shortage_quantile=quantile,
            )
            candidate.name = f"{base_plan.name}_resilience_q{int(round(quantile * 100)):02d}_r{round_number}"
            current_signature = signature(candidate)
            if current_signature == previous_signature or current_signature in seen_signatures:
                break
            seen_signatures.add(current_signature)
            if not actions.empty:
                actions = actions.copy()
                actions["corrective_round"] = round_number
                accumulated_actions.append(actions)
            simulation = simulate_plan(cfg, candidate, evaluation_scenarios, seed_offset=0)
            gate = evaluate_rcce(cfg, candidate, simulation)
            all_actions = pd.concat(accumulated_actions, ignore_index=True) if accumulated_actions else pd.DataFrame()
            record(quantile, round_number, candidate, simulation, gate, all_actions)
            if gate.status == "RELEASE":
                break
            current_plan = candidate
            current_simulation = simulation
            previous_signature = current_signature

    if not candidates:
        candidate, actions = build_resilience_candidate(cfg, base_plan, evaluation_scenarios, base_simulation)
        candidate.name = f"{base_plan.name}_resilience_fallback"
        simulation = simulate_plan(cfg, candidate, evaluation_scenarios, seed_offset=0)
        gate = evaluate_rcce(cfg, candidate, simulation)
        record(
            float(candidate.solver.get("shortage_quantile", 0.95)),
            1,
            candidate,
            simulation,
            gate,
            actions,
        )

    selected = min(candidates, key=lambda item: _decision_rank(item[3], item[2]))
    _, plan, simulation, gate, actions = selected
    frontier = pd.DataFrame(frontier_rows)
    frontier["_status_rank"] = frontier["status"].map({"RELEASE": 0, "REVIEW": 1, "BLOCK": 2})
    frontier["_primary_rank"] = np.where(
        frontier["status"].eq("RELEASE"),
        frontier["mean_realized_total_cost"],
        frontier["shortage_cvar95"],
    )
    frontier["_secondary_rank"] = np.where(
        frontier["status"].eq("RELEASE"),
        frontier["shortage_cvar95"],
        frontier["mean_realized_total_cost"],
    )
    frontier = (
        frontier.sort_values(
            ["_status_rank", "failed_checks", "_primary_rank", "_secondary_rank"],
            kind="stable",
        )
        .drop(columns=["_status_rank", "_primary_rank", "_secondary_rank"])
        .reset_index(drop=True)
    )
    return plan, actions, simulation, gate, frontier


def _run_pipeline_unlocked(cfg: ProjectConfig) -> dict:
    timings: dict[str, float] = {}
    _clean_runtime_artifacts(cfg.artifacts_dir, cfg.project_root)

    with _stage(timings, "data_generation_and_contracts"):
        generate_public_informed_proxy_data(cfg)
        data_contract_summary = validate_generated_data(cfg)
        build_sql_marts(cfg)

    with _stage(timings, "forecasting"):
        forecast = run_forecasting(cfg)
        gpu_result = {"status": "SKIPPED", "reason": "disabled by configuration"}
        if bool(cfg.section("forecast").get("use_torch_challenger", False)):
            gpu_result = train_temporal_quantile_challenger(cfg)
            forecast = integrate_gpu_candidate(cfg, forecast, gpu_result)

    with _stage(timings, "scenario_generation"):
        optimization_scenarios = build_route_scenarios(
            cfg,
            cfg.artifacts_dir / "next_day_forecast.csv",
            save_path=cfg.generated_dir / "scenario_routes.csv",
        )
        evaluation_count = int(
            cfg.section("optimization").get(
                "evaluation_scenarios",
                max(20, int(cfg.section("optimization").get("scenarios", 5)) * 4),
            )
        )
        evaluation_scenarios = build_route_scenarios(
            cfg,
            cfg.artifacts_dir / "next_day_forecast.csv",
            scenario_count=evaluation_count,
            seed_offset=90_000,
            save_path=cfg.artifacts_dir / "evaluation_scenarios.csv",
        )
        station_ids = set(pd.read_csv(cfg.generated_dir / "stations.csv")["station_id"].astype(str))
        optimization_contract = validate_scenarios(optimization_scenarios, known_station_ids=station_ids)
        evaluation_contract = validate_scenarios(evaluation_scenarios, known_station_ids=station_ids)

    with _stage(timings, "optimization"):
        deterministic = solve_fleet_plan(
            cfg, optimization_scenarios, name="deterministic_expected_value", stochastic=False
        )
        risk_neutral = solve_fleet_plan(
            cfg,
            optimization_scenarios,
            name="stochastic_risk_neutral",
            stochastic=True,
            risk_weight_override=0.0,
        )
        risk_aware = solve_fleet_plan(cfg, optimization_scenarios, name="stochastic_cvar", stochastic=True)
        save_plan(deterministic, cfg.artifacts_dir / "deterministic_plan.json")
        save_plan(risk_neutral, cfg.artifacts_dir / "risk_neutral_plan.json")
        save_plan(risk_aware, cfg.artifacts_dir / "stochastic_plan.json")
        value = stochastic_value_analysis(
            cfg,
            deterministic,
            risk_neutral,
            optimization_scenarios,
            evaluation_scenarios,
        )
        write_json_atomic(cfg.artifacts_dir / "stochastic_value.json", value)

    with _stage(timings, "paired_simulation_and_rcce"):
        deterministic_simulation = simulate_plan(cfg, deterministic, evaluation_scenarios, seed_offset=0)
        risk_neutral_simulation = simulate_plan(cfg, risk_neutral, evaluation_scenarios, seed_offset=0)
        risk_aware_simulation = simulate_plan(cfg, risk_aware, evaluation_scenarios, seed_offset=0)
        simulations = {
            "deterministic": deterministic_simulation,
            "risk_neutral": risk_neutral_simulation,
            "risk_aware": risk_aware_simulation,
        }
        plans = {
            "deterministic": deterministic,
            "risk_neutral": risk_neutral,
            "risk_aware": risk_aware,
        }
        gates = {name: evaluate_rcce(cfg, plans[name], output) for name, output in simulations.items()}
        for name, output in simulations.items():
            write_csv_atomic(cfg.artifacts_dir / f"{name}_simulation_runs.csv", output.runs)
            write_json_atomic(cfg.artifacts_dir / f"{name}_rcce.json", gates[name].to_dict())
        # Backward-compatible stochastic filenames refer to the risk-aware CVaR policy.
        write_csv_atomic(cfg.artifacts_dir / "stochastic_simulation_runs.csv", risk_aware_simulation.runs)
        write_json_atomic(cfg.artifacts_dir / "stochastic_rcce.json", gates["risk_aware"].to_dict())

        base_name, base_plan, base_simulation, base_gate = _choose_policy(
            [(name, plans[name], simulations[name], gates[name]) for name in plans]
        )
        base_policy_rows = []
        for name in plans:
            base_policy_rows.append(
                {
                    "policy": name,
                    "status": gates[name].status,
                    "failed_checks": sum(not value for value in gates[name].checks.values()),
                    "mean_realized_total_cost": simulations[name].summary["mean_realized_total_cost"],
                    "shortage_cvar95": simulations[name].summary["shortage_cvar95"],
                    "service_coverage_lcb": simulations[name].summary["service_coverage_lcb"],
                    "selected": name == base_name,
                }
            )
        write_csv_atomic(cfg.artifacts_dir / "base_policy_selection.csv", pd.DataFrame(base_policy_rows))

        corrective_plan, corrective_actions, corrective_simulation, corrective_gate, frontier = (
            _build_corrective_frontier(cfg, base_plan, base_simulation, evaluation_scenarios, base_gate)
        )
        save_plan(corrective_plan, cfg.artifacts_dir / "corrective_plan.json")
        write_csv_atomic(cfg.artifacts_dir / "corrective_actions.csv", corrective_actions)
        write_csv_atomic(cfg.artifacts_dir / "corrective_frontier.csv", frontier)
        write_csv_atomic(cfg.artifacts_dir / "corrective_simulation_runs.csv", corrective_simulation.runs)
        write_json_atomic(cfg.artifacts_dir / "corrective_rcce.json", corrective_gate.to_dict())

        comparison_specs = [
            (deterministic_simulation, risk_neutral_simulation, "deterministic_to_risk_neutral"),
            (risk_neutral_simulation, risk_aware_simulation, "risk_neutral_to_risk_aware"),
            (base_simulation, corrective_simulation, f"{base_name}_to_corrective"),
        ]
        paired_comparison = pd.DataFrame(
            [compare_simulations(reference, challenger, label) for reference, challenger, label in comparison_specs]
        )
        write_csv_atomic(cfg.artifacts_dir / "paired_policy_comparison.csv", paired_comparison)

    recommended_name, recommended_plan, recommended_simulation, recommended_gate = _choose_policy(
        [
            (base_name, base_plan, base_simulation, base_gate),
            ("corrective_capacity_candidate", corrective_plan, corrective_simulation, corrective_gate),
        ]
    )
    recommendation_source = recommended_name
    save_plan(recommended_plan, cfg.artifacts_dir / "recommended_plan.json")
    write_json_atomic(cfg.artifacts_dir / "recommended_rcce.json", recommended_gate.to_dict())

    with _stage(timings, "rolling_reoptimization"):
        rolling_plan, rolling_comparison, rolling_metrics = run_rolling_reoptimization(
            cfg, base_plan, evaluation_scenarios
        )
        save_plan(rolling_plan, cfg.artifacts_dir / "rolling_reoptimization_plan.json")
        write_csv_atomic(cfg.artifacts_dir / "rolling_reoptimization_comparison.csv", rolling_comparison)
        write_json_atomic(cfg.artifacts_dir / "rolling_reoptimization_metrics.json", rolling_metrics)

    with _stage(timings, "saa"):
        saa = run_saa(cfg, cfg.artifacts_dir / "next_day_forecast.csv", evaluation_scenarios)

    with _stage(timings, "reporting"):
        write_reports(
            cfg=cfg,
            forecast_comparison=forecast.comparison,
            plans=plans,
            gates=gates,
            value=value,
            saa=saa,
            base_policy_name=base_name,
            corrective_plan=corrective_plan,
            corrective_gate=corrective_gate,
            corrective_actions=corrective_actions,
            corrective_frontier=frontier,
            paired_comparison=paired_comparison,
            rolling_metrics=rolling_metrics,
            recommended_plan=recommended_plan,
            recommended_gate=recommended_gate,
            recommendation_source=recommendation_source,
        )

    summary = {
        "run_id": str(cfg.raw.get("_runtime", {}).get("run_id", "untracked-run")),
        "pipeline_status": "PASS",
        "selected_forecast_model": forecast.selected_model,
        "gpu_challenger": gpu_result,
        "deterministic_plan_status": deterministic.status,
        "risk_neutral_plan_status": risk_neutral.status,
        "stochastic_plan_status": risk_aware.status,
        "rolling_plan_status": rolling_plan.status,
        "deterministic_rcce": gates["deterministic"].status,
        "risk_neutral_rcce": gates["risk_neutral"].status,
        "stochastic_rcce": gates["risk_aware"].status,
        "selected_base_policy": base_name,
        "corrective_rcce": corrective_gate.status,
        "corrective_triggered": base_gate.status != "RELEASE",
        "recommended_rcce": recommended_gate.status,
        "automatic_release_allowed": recommended_gate.status == "RELEASE",
        "recommendation_source": recommendation_source,
        "solver_diagnostics": {
            "deterministic": _solver_summary(deterministic),
            "risk_neutral": _solver_summary(risk_neutral),
            "risk_aware": _solver_summary(risk_aware),
            "rolling": _solver_summary(rolling_plan),
            "recommended": _solver_summary(recommended_plan),
        },
        "formal_sample_vss": value["value_of_stochastic_solution"],
        "formal_sample_evpi": value["expected_value_of_perfect_information"],
        "heldout_empirical_vss": value["heldout_empirical_vss"],
        "rolling_expected_cost_savings": rolling_metrics["expected_cost_savings"],
        "data_contract": data_contract_summary,
        "optimization_scenario_contract": optimization_contract,
        "evaluation_scenario_contract": evaluation_contract,
        "stage_seconds": timings,
        "environment": _environment_metadata(cfg),
        "artifacts_dir": str(cfg.artifacts_dir),
    }
    write_json_atomic(cfg.artifacts_dir / "run_summary.json", summary)
    build_generated_data_manifest(
        cfg.generated_dir,
        cfg.artifacts_dir / "generated_data_manifest.json",
        run_id=summary["run_id"],
    )
    validate_required_artifacts(cfg.artifacts_dir)
    build_artifact_manifest(cfg.artifacts_dir, run_id=summary["run_id"])
    return summary


def run_pipeline(cfg: ProjectConfig) -> dict:
    assert_safe_runtime_directory(cfg.artifacts_dir, cfg.project_root)
    assert_safe_runtime_directory(cfg.generated_dir, cfg.project_root, allow_generated_data_path=True)
    with pipeline_run_lock(cfg):
        recover_incomplete_promotions(cfg.project_root)
        with staged_pipeline_config(cfg) as staged_cfg:
            result = _run_pipeline_unlocked(staged_cfg)
        return remap_staged_result(result, staged_cfg, cfg)
