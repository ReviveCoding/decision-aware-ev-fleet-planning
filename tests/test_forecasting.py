from __future__ import annotations

import pandas as pd


def test_decision_aware_forecast_selection(cfg, pipeline_summary):
    comparison = pd.read_csv(cfg.artifacts_dir / "forecast_model_comparison.csv")
    next_forecast = pd.read_csv(cfg.artifacts_dir / "next_day_forecast.csv")
    assert pipeline_summary["selected_forecast_model"] in set(comparison.model)
    assert {
        "mae",
        "decision_cost",
        "decision_shortage_cvar90",
        "conformal_correction",
        "calibration_coverage_before",
        "calibration_coverage_after",
    }.issubset(comparison.columns)
    assert (comparison["conformal_correction"] >= 0).all()
    assert (comparison["calibration_coverage_after"] >= comparison["calibration_coverage_before"]).all()
    assert (next_forecast.p90 >= next_forecast.p50).all()


def test_gpu_candidate_can_be_promoted_only_through_common_decision_gate(cfg, pipeline_summary, tmp_path):
    import copy
    import shutil

    from ev_fleet.config import ProjectConfig
    from ev_fleet.forecasting import ForecastRun, integrate_gpu_candidate

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    shutil.copy2(cfg.artifacts_dir / "forecast_backtest_predictions.csv", artifacts)
    comparison = pd.read_csv(cfg.artifacts_dir / "forecast_model_comparison.csv")
    next_forecast = pd.read_csv(cfg.artifacts_dir / "next_day_forecast.csv")
    candidate = comparison.iloc[0].drop(labels=["selected", "calibration_gate"]).to_dict()
    candidate.update(
        {
            "model": "temporal_quantile_transformer",
            "decision_cost": -1.0,
            "decision_shortage_cvar90": 0.0,
            "p90_coverage": 0.90,
            "p90_calibration_error": 0.0,
            "worst_station_mae": 0.1,
            "mae": 0.1,
        }
    )
    gpu_backtest = pd.DataFrame(
        [
            {
                "date": next_forecast.iloc[0]["date"],
                "station_id": next_forecast.iloc[0]["station_id"],
                "actual": 10.0,
                "prediction": 10.0,
                "p90": 11.0,
                "model": "temporal_quantile_transformer",
            }
        ]
    )
    gpu_next = next_forecast.copy()
    gpu_next["p50"] = 10.0
    gpu_next["p90"] = 11.0
    gpu_backtest.to_csv(artifacts / "gpu_backtest_predictions.csv", index=False)
    gpu_next.to_csv(artifacts / "gpu_next_day_forecast.csv", index=False)

    raw = copy.deepcopy(cfg.raw)
    raw["paths"]["artifacts"] = str(artifacts)
    isolated = ProjectConfig(raw=raw, source_path=cfg.source_path, project_root=tmp_path)
    run = ForecastRun(
        comparison=comparison, next_forecast=next_forecast, selected_model=pipeline_summary["selected_forecast_model"]
    )
    result = {"status": "PASS", "comparison_row": candidate}
    integrated = integrate_gpu_candidate(isolated, run, result)

    assert integrated.selected_model == "temporal_quantile_transformer"
    assert result["promoted_to_pipeline_champion"] is True
    selected_next = pd.read_csv(artifacts / "next_day_forecast.csv")
    assert selected_next["selected_model"].eq("temporal_quantile_transformer").all()


def test_feature_imputation_uses_training_statistics_only():
    from ev_fleet.forecasting import _fill_from_training_statistics

    training = pd.DataFrame({"x": [1.0, float("nan")]})
    holdout = pd.DataFrame({"x": [1000.0, float("nan")]})
    filled_training, filled_holdout = _fill_from_training_statistics(training, holdout, feature_cols=["x"])
    assert filled_training.loc[1, "x"] == 1.0
    assert filled_holdout.loc[1, "x"] == 1.0


def test_one_sided_conformal_correction_repairs_undercoverage():
    import numpy as np

    from ev_fleet.forecasting import _one_sided_conformal_correction

    actual = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    predicted_upper = np.array([8.0, 9.0, 10.0, 11.0, 12.0])
    correction = _one_sided_conformal_correction(actual, predicted_upper, 0.90)
    assert correction == 2.0
    assert ((actual <= predicted_upper + correction).mean()) == 1.0


def test_next_day_features_do_not_carry_forward_transient_capacity_event():
    from ev_fleet.forecasting import _build_next_day_features, _prepare_features

    raw = pd.DataFrame(
        {
            "date": pd.date_range("2025-02-20", periods=8, freq="D"),
            "station_id": ["STN_01"] * 8,
            "route_count": list(range(10, 18)),
            "avg_cube": [150.0] * 8,
            "avg_distance_miles": [70.0] * 8,
            "avg_route_energy_kwh": [80.0] * 8,
            "cold_regime": [1] * 8,
            "capacity_event": [0] * 7 + [1],
            "route_count_rolling_7": [float("nan")] * 8,
            "route_count_lag_1": [float("nan")] * 8,
            "route_count_lag_7": [float("nan")] * 8,
            "avg_cube_lag_1": [float("nan")] * 8,
            "avg_distance_miles_lag_1": [float("nan")] * 8,
            "avg_route_energy_kwh_lag_1": [float("nan")] * 8,
        }
    )
    prepared, features = _prepare_features(raw)
    next_rows = _build_next_day_features(prepared, features)
    assert next_rows.iloc[0]["capacity_event"] == 0.0
    assert next_rows.iloc[0]["route_count_lag_1"] == 17.0
    assert next_rows.iloc[0]["route_count_lag_7"] == 11.0


def test_decision_replay_respects_station_parking_and_redeployment_cost():
    from ev_fleet.forecasting import decision_replay

    predictions = pd.DataFrame(
        {
            "date": ["2025-01-01", "2025-01-01"],
            "station_id": ["A", "B"],
            "actual": [2.0, 8.0],
            "prediction": [2.0, 8.0],
            "p90": [3.0, 9.0],
        }
    )
    replay = decision_replay(
        predictions,
        10,
        initial_allocation={"A": 10, "B": 0},
        parking_capacity={"A": 10, "B": 5},
        redeployment_cost=10.0,
        unserved_cost=100.0,
        idle_cost=1.0,
    )
    assert replay["decision_redeployment_count"] == 5.0
    assert replay["decision_shortage_cvar90"] == 3.0
    assert replay["decision_cost"] == 351.0
    assert replay["decision_active_fleet"] == 8.0


def test_calibration_error_uses_configured_target_not_hardcoded_p90():
    import pytest

    from ev_fleet.forecasting import _coverage_calibration_error

    assert _coverage_calibration_error(0.90, 0.95) == pytest.approx(0.05)
    assert _coverage_calibration_error(0.90, 0.80) == pytest.approx(0.10)
