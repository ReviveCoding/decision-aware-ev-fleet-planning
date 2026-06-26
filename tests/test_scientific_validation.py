from __future__ import annotations

import json

import pandas as pd


def test_formal_stochastic_value_invariants(cfg, pipeline_summary):
    value = json.loads((cfg.artifacts_dir / "stochastic_value.json").read_text(encoding="utf-8"))
    formal = value["formal_sample"]
    heldout = value["heldout_empirical"]
    assert formal["vss"] >= -1e-7
    assert formal["evpi"] >= -1e-7
    assert formal["wait_and_see_cost"] <= formal["risk_neutral_stochastic_cost"] + 1e-7
    assert formal["risk_neutral_stochastic_cost"] <= formal["expected_value_plan_cost"] + 1e-7
    assert "may be negative" in value["interpretation"]
    assert heldout["scenario_count"] > 0


def test_common_random_numbers_are_preserved(cfg, pipeline_summary):
    det = pd.read_csv(cfg.artifacts_dir / "deterministic_simulation_runs.csv")
    neutral = pd.read_csv(cfg.artifacts_dir / "risk_neutral_simulation_runs.csv")
    aware = pd.read_csv(cfg.artifacts_dir / "risk_aware_simulation_runs.csv")
    columns = ["run", "scenario_id", "regime", "energy_multiplier"]
    pd.testing.assert_frame_equal(det[columns], neutral[columns])
    pd.testing.assert_frame_equal(det[columns], aware[columns])


def test_complexity_is_selected_by_reliability_not_model_name(cfg, pipeline_summary):
    selection = pd.read_csv(cfg.artifacts_dir / "base_policy_selection.csv")
    assert selection["selected"].sum() == 1
    selected = selection.loc[selection["selected"], "policy"].iloc[0]
    assert selected == pipeline_summary["selected_base_policy"]
    ranked = selection.assign(status_rank=selection["status"].map({"RELEASE": 0, "REVIEW": 1, "BLOCK": 2}))
    if (ranked["status"] == "RELEASE").any():
        best_key = (
            ranked[ranked["status"] == "RELEASE"]
            .sort_values(["failed_checks", "mean_realized_total_cost", "shortage_cvar95"], kind="stable")
            .iloc[0]["policy"]
        )
    else:
        best_key = ranked.sort_values(
            ["status_rank", "failed_checks", "shortage_cvar95", "mean_realized_total_cost"],
            kind="stable",
        ).iloc[0]["policy"]
    assert selected == best_key


def test_corrective_frontier_is_evaluated_and_selected(cfg, pipeline_summary):
    frontier = pd.read_csv(cfg.artifacts_dir / "corrective_frontier.csv")
    selected_plan = json.loads((cfg.artifacts_dir / "corrective_plan.json").read_text(encoding="utf-8"))
    maximum_candidates = len(cfg.raw["reliability"]["corrective_shortage_quantiles"]) * int(
        cfg.raw["reliability"]["corrective_max_rounds"]
    )
    assert 1 <= len(frontier) <= maximum_candidates
    assert {
        "round",
        "temporary_additions",
        "temporary_charging_capacity_kwh",
        "status",
        "service_coverage_lcb",
        "charging_feasibility_lcb",
        "shortage_cvar95",
    }.issubset(frontier.columns)
    assert selected_plan["name"] in set(frontier["candidate"])
    assert (frontier["temporary_additions"] >= 0).all()


def test_saa_uses_out_of_sample_metrics(cfg, pipeline_summary):
    runs = pd.read_csv(cfg.artifacts_dir / "saa_runs.csv")
    required = {
        "scenario_size",
        "replication",
        "in_sample_objective",
        "out_of_sample_cost",
        "out_of_sample_service_rate",
        "out_of_sample_shortage_cvar95",
    }
    assert required.issubset(runs.columns)
    assert runs["scenario_size"].nunique() >= 2


def test_post_trigger_stale_plan_excludes_sunk_redeployment_cost(cfg, pipeline_summary):
    from ev_fleet.optimization import load_plan
    from ev_fleet.rolling import _post_trigger_stale_plan

    base = load_plan(cfg.artifacts_dir / "risk_neutral_plan.json")
    stale = _post_trigger_stale_plan(base)
    assert stale.moves.empty
    assert stale.allocation.equals(base.allocation)
    assert stale.solver["cost_boundary"] == "post_trigger_incremental"


def test_rolling_reoptimization_preserves_scenario_pairs_and_fleet(cfg, pipeline_summary):
    metrics = json.loads((cfg.artifacts_dir / "rolling_reoptimization_metrics.json").read_text(encoding="utf-8"))
    comparison = pd.read_csv(cfg.artifacts_dir / "rolling_reoptimization_comparison.csv")
    assert comparison["scenario_id"].is_unique
    assert metrics["fleet_balance_difference"] == 0.0
    assert metrics["starting_fleet"] == metrics["rolling_fleet"]


def test_corrective_candidate_respects_physical_parking_capacity(cfg, pipeline_summary, monkeypatch):
    import ev_fleet.corrective_action as corrective_module
    from ev_fleet.corrective_action import build_resilience_candidate
    from ev_fleet.optimization import load_plan
    from ev_fleet.simulation import SimulationOutput

    plan = load_plan(cfg.artifacts_dir / "deterministic_plan.json")
    runs = pd.read_csv(cfg.artifacts_dir / "deterministic_simulation_runs.csv")
    gate = json.loads((cfg.artifacts_dir / "deterministic_rcce.json").read_text(encoding="utf-8"))
    replay = SimulationOutput(runs=runs, summary=gate["metrics"])
    routes = pd.read_csv(cfg.artifacts_dir / "evaluation_scenarios.csv")
    current_totals = plan.allocation.groupby("station_id")["planned_count"].sum().astype(int)
    original_read_csv = corrective_module.pd.read_csv

    def constrained_read_csv(path, *args, **kwargs):
        frame = original_read_csv(path, *args, **kwargs)
        if str(path).endswith("stations.csv"):
            frame["parking_capacity"] = frame["station_id"].map(current_totals).astype(int)
        return frame

    monkeypatch.setattr(corrective_module.pd, "read_csv", constrained_read_csv)
    candidate, actions = build_resilience_candidate(cfg, plan, routes, replay, shortage_quantile=0.95)
    totals = candidate.allocation.groupby("station_id")["planned_count"].sum().astype(int)
    assert totals.to_dict() == current_totals.to_dict()
    assert candidate.solver["unmet_capacity_due_to_parking"] > 0
    assert "parking_capacity_expansion_required" in set(actions["action_type"])


def test_corrective_builder_adds_bounded_charging_capacity_from_replay(cfg, pipeline_summary):
    from ev_fleet.corrective_action import build_resilience_candidate
    from ev_fleet.optimization import load_plan
    from ev_fleet.simulation import SimulationOutput

    plan = load_plan(cfg.artifacts_dir / "deterministic_plan.json")
    routes = pd.read_csv(cfg.artifacts_dir / "evaluation_scenarios.csv")
    runs = pd.DataFrame(
        {
            "station_shortages": [json.dumps({"STN_01": 0})] * 20,
            "station_charging_overage_kwh": [json.dumps({"STN_01": 125.2})] * 20,
        }
    )
    replay = SimulationOutput(
        runs=runs,
        summary={"shortage_cvar95": 0.0, "requirement_ratio_p95": 1.0},
    )
    candidate, actions = build_resilience_candidate(cfg, plan, routes, replay, shortage_quantile=0.95)
    capacity = candidate.solver["temporary_charging_capacity_kwh"]
    configured_limit = float(cfg.raw["reliability"]["max_temporary_charging_capacity_kwh_per_station"])
    assert 0.0 < capacity["STN_01"] <= configured_limit
    assert "temporary_charging_capacity" in set(actions["action_type"])
