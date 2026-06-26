from __future__ import annotations

import pandas as pd

from ev_fleet.optimization import load_plan
from ev_fleet.reliability import evaluate_rcce
from ev_fleet.simulation import SimulationOutput, compare_simulations, simulate_plan


def test_simulator_replay_and_rcce(cfg, pipeline_summary, scenarios):
    plan = load_plan(cfg.artifacts_dir / "stochastic_plan.json")
    original_runs = cfg.raw["simulation"]["runs"]
    cfg.raw["simulation"]["runs"] = 20
    try:
        one = simulate_plan(cfg, plan, scenarios, seed_offset=55)
        two = simulate_plan(cfg, plan, scenarios, seed_offset=55)
    finally:
        cfg.raw["simulation"]["runs"] = original_runs
    assert one.runs.equals(two.runs)
    decision = evaluate_rcce(cfg, plan, one)
    assert decision.status in {"RELEASE", "REVIEW", "BLOCK"}
    assert "service_coverage_lcb" in decision.metrics


def test_rcce_hard_blocks_parking_capacity_violation(cfg, pipeline_summary, scenarios):
    from ev_fleet.optimization import PlanResult

    plan = load_plan(cfg.artifacts_dir / "stochastic_plan.json")
    original_runs = cfg.raw["simulation"]["runs"]
    cfg.raw["simulation"]["runs"] = 12
    try:
        replay = simulate_plan(cfg, plan, scenarios, seed_offset=77)
    finally:
        cfg.raw["simulation"]["runs"] = original_runs
    allocation = plan.allocation.copy()
    allocation["temporary_addition"] = 0
    allocation.loc[allocation.index[0], "planned_count"] += 100
    allocation.loc[allocation.index[0], "temporary_addition"] += 100
    invalid = PlanResult(
        name="parking_invalid",
        status=plan.status,
        objective=plan.objective,
        allocation=allocation,
        moves=plan.moves.copy(),
        scenario_metrics=plan.scenario_metrics.copy(),
        solver=plan.solver.copy(),
    )
    decision = evaluate_rcce(cfg, invalid, replay)
    assert decision.status == "BLOCK"
    assert not decision.checks["parking_capacity"]
    assert decision.metrics["max_parking_capacity_excess"] > 0


def test_simulator_reports_variable_operating_cost(cfg, pipeline_summary):
    import pandas as pd

    runs = pd.read_csv(cfg.artifacts_dir / "stochastic_simulation_runs.csv")
    assert "variable_operating_cost" in runs.columns
    assert (runs["variable_operating_cost"] > 0).all()


def test_rcce_reviews_large_solver_gap(cfg, pipeline_summary, scenarios):
    from ev_fleet.optimization import PlanResult

    plan = load_plan(cfg.artifacts_dir / "stochastic_plan.json")
    original_runs = cfg.raw["simulation"]["runs"]
    cfg.raw["simulation"]["runs"] = 12
    try:
        replay = simulate_plan(cfg, plan, scenarios, seed_offset=91)
    finally:
        cfg.raw["simulation"]["runs"] = original_runs

    solver = plan.solver.copy()
    solver["mip_gap"] = 0.9
    solver["degraded_mode"] = False
    high_gap = PlanResult(
        name="high_gap",
        status=plan.status,
        objective=plan.objective,
        allocation=plan.allocation.copy(),
        moves=plan.moves.copy(),
        scenario_metrics=plan.scenario_metrics.copy(),
        solver=solver,
    )
    decision = evaluate_rcce(cfg, high_gap, replay)
    assert decision.status != "RELEASE"
    assert not decision.checks["solver_gap"]
    assert decision.metrics["solver_mip_gap"] == 0.9


def test_rcce_hard_blocks_numerically_invalid_solver_solution(cfg, pipeline_summary, scenarios):
    from ev_fleet.optimization import PlanResult

    plan = load_plan(cfg.artifacts_dir / "stochastic_plan.json")
    original_runs = cfg.raw["simulation"]["runs"]
    cfg.raw["simulation"]["runs"] = 12
    try:
        replay = simulate_plan(cfg, plan, scenarios, seed_offset=109)
    finally:
        cfg.raw["simulation"]["runs"] = original_runs

    solver = plan.solver.copy()
    solver["max_constraint_violation"] = 0.1
    invalid = PlanResult(
        name="numerically_invalid",
        status=plan.status,
        objective=plan.objective,
        allocation=plan.allocation.copy(),
        moves=plan.moves.copy(),
        scenario_metrics=plan.scenario_metrics.copy(),
        solver=solver,
    )
    decision = evaluate_rcce(cfg, invalid, replay)
    assert decision.status == "BLOCK"
    assert not decision.checks["solver_constraints"]
    assert decision.metrics["solver_max_constraint_violation"] == 0.1


def test_corrective_charging_capacity_is_bounded_and_improves_replay(cfg, pipeline_summary, scenarios):
    import json

    corrective = load_plan(cfg.artifacts_dir / "corrective_plan.json")
    capacity = {
        str(key): float(value)
        for key, value in dict(corrective.solver.get("temporary_charging_capacity_kwh", {})).items()
    }
    configured_limit = float(cfg.raw["reliability"]["max_temporary_charging_capacity_kwh_per_station"])
    assert all(0.0 <= value <= configured_limit for value in capacity.values())

    base_runs = pd.read_csv(cfg.artifacts_dir / "stochastic_simulation_runs.csv")
    corrective_runs = pd.read_csv(cfg.artifacts_dir / "corrective_simulation_runs.csv")
    assert corrective_runs["charging_feasible"].mean() >= base_runs["charging_feasible"].mean()
    assert pipeline_summary["recommended_rcce"] in {"RELEASE", "REVIEW", "BLOCK"}

    manifest = json.loads((cfg.artifacts_dir / "corrective_plan.json").read_text(encoding="utf-8"))
    if capacity:
        assert "temporary_charging_capacity_kwh" in manifest["solver"]


def test_route_split_consumes_an_additional_vehicle_segment(cfg, pipeline_summary):
    from ev_fleet.optimization import PlanResult

    allocation = pd.read_csv(cfg.generated_dir / "initial_fleet.csv")
    allocation["planned_count"] = 0
    allocation["initial_count"] = 0
    allocation["inbound"] = 0
    allocation["outbound"] = 0
    mask = allocation["station_id"].eq("STN_01") & allocation["vehicle_type"].eq("large_ev")
    allocation.loc[mask, ["planned_count", "initial_count"]] = 1
    plan = PlanResult(
        name="split_requires_two_vehicles",
        status="CORRECTIVE_CANDIDATE",
        objective=0.0,
        allocation=allocation,
        moves=pd.DataFrame(columns=["from_station", "to_station", "vehicle_type", "count"]),
        scenario_metrics=pd.DataFrame(),
        solver={"route_split_factor": 0.6, "temporary_charging_capacity_kwh": {}},
    )
    route = pd.DataFrame(
        [
            {
                "scenario_id": 0,
                "scenario_weight": 1.0,
                "route_id": "oversized",
                "station_id": "STN_01",
                "required_cube": 280.0,
                "distance_miles": 50.0,
                "energy_kwh": 100.0,
                "criticality": "standard",
                "regime": "test",
                "cold_regime": 0,
                "charger_outage_regime": 0,
                "charger_capacity_factor": 1.0,
                "vehicle_failure_multiplier": 1.0,
                "return_delay_minutes": 0.0,
            }
        ]
    )
    original_runs = cfg.raw["simulation"]["runs"]
    original_failure = cfg.raw["simulation"]["failure_probability"]
    original_emergency = cfg.raw["optimization"]["max_emergency_leases_per_station"]
    cfg.raw["simulation"]["runs"] = 12
    cfg.raw["simulation"]["failure_probability"] = 0.0
    cfg.raw["optimization"]["max_emergency_leases_per_station"] = 0
    try:
        output = simulate_plan(cfg, plan, route, seed_offset=303)
    finally:
        cfg.raw["simulation"]["runs"] = original_runs
        cfg.raw["simulation"]["failure_probability"] = original_failure
        cfg.raw["optimization"]["max_emergency_leases_per_station"] = original_emergency
    assert output.runs["unserved_routes"].eq(1).all()
    assert output.runs["split_routes"].eq(1).all()


def test_same_day_repair_capacity_is_finite_and_prioritizes_flexible_vehicle():
    from ev_fleet.simulation import _apply_same_day_repairs

    specs = {
        "compact": {"cube": 100.0, "cost": 80.0},
        "large": {"cube": 250.0, "cost": 160.0},
    }
    failed = {"compact": 2, "large": 1}
    residual, repaired = _apply_same_day_repairs(
        failed,
        specs,
        repair_capacity=2,
        repair_draws=[0.1, 0.2],
        success_probability=0.5,
    )
    assert repaired == 2
    assert residual == {"compact": 1, "large": 0}


def test_simulator_reports_repair_and_charge_acceptance_metrics(cfg, pipeline_summary):
    runs = pd.read_csv(cfg.artifacts_dir / "stochastic_simulation_runs.csv")
    required = {
        "failed_vehicles",
        "repaired_vehicles",
        "repair_capacity_slots",
        "repair_capacity_utilization",
        "max_charge_acceptance_ratio",
    }
    assert required.issubset(runs.columns)
    assert (runs["repaired_vehicles"] <= runs["failed_vehicles"]).all()
    assert (runs["repair_capacity_utilization"].between(0.0, 1.0)).all()


def test_corrective_economic_costs_are_in_realized_replay(cfg, pipeline_summary):
    from ev_fleet.optimization import load_plan

    plan = load_plan(cfg.artifacts_dir / "corrective_plan.json")
    runs = pd.read_csv(cfg.artifacts_dir / "corrective_simulation_runs.csv")
    optimization = cfg.raw["optimization"]
    temporary_vehicles = int(plan.allocation.get("temporary_addition", pd.Series(dtype=int)).sum())
    charging_kwh = float(sum(dict(plan.solver.get("temporary_charging_capacity_kwh", {})).values()))
    expected = (
        temporary_vehicles * float(optimization["emergency_lease_cost"])
        + charging_kwh * float(optimization["temporary_charging_capacity_cost_per_kwh"])
        + float(plan.solver.get("route_split_program_cost", 0.0))
    )
    assert runs["fixed_corrective_cost"].nunique() == 1
    assert abs(float(runs["fixed_corrective_cost"].iloc[0]) - expected) < 1e-9
    assert (runs["realized_total_cost"] >= runs["fixed_corrective_cost"]).all()


def test_paired_comparison_rejects_missing_common_random_number_keys():
    reference_runs = pd.DataFrame(
        {
            "run": [0, 1],
            "scenario_id": [0, 1],
            "realized_total_cost": [10.0, 11.0],
            "service_rate": [1.0, 0.9],
            "unserved_routes": [0, 1],
        }
    )
    challenger_runs = reference_runs.iloc[:1].copy()
    reference = SimulationOutput(reference_runs, {})
    challenger = SimulationOutput(challenger_runs, {})
    with __import__("pytest").raises(ValueError, match="identical run/scenario keys"):
        compare_simulations(reference, challenger, "invalid-pair")


def test_rcce_hard_blocks_plan_status_and_termination_mismatch(cfg, pipeline_summary, scenarios):
    from ev_fleet.optimization import PlanResult

    plan = load_plan(cfg.artifacts_dir / "stochastic_plan.json")
    original_runs = cfg.raw["simulation"]["runs"]
    cfg.raw["simulation"]["runs"] = 12
    try:
        replay = simulate_plan(cfg, plan, scenarios, seed_offset=411)
    finally:
        cfg.raw["simulation"]["runs"] = original_runs

    solver = plan.solver.copy()
    solver["termination_class"] = "INFEASIBLE"
    inconsistent = PlanResult(
        name="inconsistent_solver_status",
        status="OPTIMAL",
        objective=plan.objective,
        allocation=plan.allocation.copy(),
        moves=plan.moves.copy(),
        scenario_metrics=plan.scenario_metrics.copy(),
        solver=solver,
    )
    decision = evaluate_rcce(cfg, inconsistent, replay)
    assert decision.status == "BLOCK"
    assert not decision.checks["solver_feasible"]
