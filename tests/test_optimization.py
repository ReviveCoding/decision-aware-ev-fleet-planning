from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from ev_fleet.optimization import load_plan


def test_solver_termination_contract_accepts_only_optimal_or_limit_incumbent():
    from ev_fleet.optimization import _accepted_plan_status

    assert _accepted_plan_status(0, True) == "OPTIMAL"
    assert _accepted_plan_status(1, False) == "FEASIBLE_LIMIT"
    assert _accepted_plan_status(2, False) is None
    assert _accepted_plan_status(3, False) is None
    assert _accepted_plan_status(4, False) is None
    assert _accepted_plan_status(0, False) is None


def test_milp_fleet_conservation(cfg, pipeline_summary):
    plan = load_plan(cfg.artifacts_dir / "stochastic_plan.json")
    assert plan.status != "FAILED"
    assert (plan.allocation.planned_count >= 0).all()
    assert plan.allocation.planned_count.sum() == plan.allocation.initial_count.sum()


def test_deterministic_and_stochastic_artifacts(cfg, pipeline_summary):
    det = load_plan(cfg.artifacts_dir / "deterministic_plan.json")
    stoch = load_plan(cfg.artifacts_dir / "stochastic_plan.json")
    assert det.solver["success"]
    assert stoch.solver["success"]
    assert det.solver["solver_backend"].endswith("(HiGHS)")
    for plan in (det, stoch):
        assert plan.solver["termination_class"] == "OPTIMAL"
        assert plan.solver["runtime_seconds"] >= 0.0
        assert plan.solver["max_bound_violation"] <= 1e-6
        assert plan.solver["max_constraint_violation"] <= 1e-6
        assert plan.solver["max_integrality_violation"] <= 1e-6
        assert plan.solver["objective_reconciliation_error"] <= 1e-6


def test_route_variable_operating_cost_is_included(cfg, pipeline_summary):
    plan = load_plan(cfg.artifacts_dir / "stochastic_plan.json")
    assert "variable_operating_cost" in plan.scenario_metrics.columns
    assert (plan.scenario_metrics["variable_operating_cost"] > 0).all()


def test_vehicle_route_compatibility_requires_recharge_within_dwell():
    import pandas as pd

    from ev_fleet.optimization import _compatible

    route = pd.Series(
        {
            "required_cube": 100.0,
            "energy_kwh": 90.0,
            "return_delay_minutes": 0.0,
        }
    )
    low_power_spec = pd.Series(
        {
            "cube_capacity": 150.0,
            "battery_kwh": 180.0,
            "usable_fraction": 0.9,
            "reserve_kwh": 20.0,
            "charge_power_kw": 5.0,
        }
    )
    high_power_spec = low_power_spec.copy()
    high_power_spec["charge_power_kw"] = 80.0
    station = pd.Series({"mean_dwell_hours": 8.0, "charger_power_kw": 100.0})

    assert not _compatible(route, low_power_spec, station)
    assert _compatible(route, high_power_spec, station)


def test_invalid_solver_incumbent_is_rejected_and_falls_back(cfg, pipeline_summary, scenarios, monkeypatch):
    import ev_fleet.optimization as optimization_module

    def invalid_milp(*, c, **kwargs):
        del kwargs
        return SimpleNamespace(
            x=np.full_like(np.asarray(c, dtype=float), 0.25),
            fun=float(np.asarray(c, dtype=float) @ np.full_like(np.asarray(c, dtype=float), 0.25)),
            success=False,
            status=1,
            message="synthetic invalid incumbent",
            mip_gap=0.25,
            mip_node_count=1,
            mip_dual_bound=0.0,
        )

    monkeypatch.setattr(optimization_module, "milp", invalid_milp)
    plan = optimization_module.solve_fleet_plan(cfg, scenarios, name="invalid_incumbent")
    assert plan.status == "HEURISTIC_FALLBACK"
    assert plan.solver["degraded_mode"] is True
    assert plan.solver["termination_class"] == "INVALID_SOLUTION"
    assert plan.solver["max_integrality_violation"] > 0.0


def test_structural_feasibility_diagnostics_identify_impossible_route(cfg, pipeline_summary, scenarios):
    import pandas as pd

    from ev_fleet.optimization import diagnose_structural_feasibility

    impossible = scenarios.iloc[[0]].copy()
    impossible["scenario_id"] = 0
    impossible["scenario_weight"] = 1.0
    impossible["route_id"] = "IMPOSSIBLE_ROUTE"
    impossible["required_cube"] = 1_000_000.0
    impossible["energy_kwh"] = 1_000_000.0
    impossible = pd.concat([scenarios[scenarios["scenario_id"].eq(0)].head(2), impossible], ignore_index=True)
    diagnostics = diagnose_structural_feasibility(cfg, impossible)
    assert diagnostics["structurally_unserviceable_route_count"] >= 1
    assert any(row["route_id"] == "IMPOSSIBLE_ROUTE" for row in diagnostics["structurally_unserviceable_routes"])
    assert diagnostics["maximum_station_charging_deficit_kwh"] > 0.0
    assert "not an exact IIS" in diagnostics["claim_boundary"]


def test_no_solution_fallback_contains_structural_diagnostics(cfg, pipeline_summary, scenarios, monkeypatch):
    import ev_fleet.optimization as optimization_module

    def infeasible_milp(**kwargs):
        del kwargs
        return SimpleNamespace(
            x=None,
            fun=None,
            success=False,
            status=2,
            message="synthetic infeasible model",
            mip_gap=None,
            mip_node_count=0,
            mip_dual_bound=None,
        )

    monkeypatch.setattr(optimization_module, "milp", infeasible_milp)
    plan = optimization_module.solve_fleet_plan(cfg, scenarios, name="synthetic_infeasible")
    assert plan.status == "HEURISTIC_FALLBACK"
    assert plan.solver["termination_class"] == "INFEASIBLE"
    assert "structural_diagnostics" in plan.solver
    assert "network_vehicle_shortage_lower_bound_by_scenario" in plan.solver["structural_diagnostics"]
