from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.linear_model import PoissonRegressor
from sklearn.metrics import mean_absolute_error, mean_pinball_loss
from sklearn.preprocessing import StandardScaler

from .config import ProjectConfig
from .io_utils import write_csv_atomic, write_json_atomic
from .sql_pipeline import read_mart

FEATURES = [
    "route_count_lag_1",
    "route_count_lag_7",
    "route_count_rolling_7",
    "avg_cube_lag_1",
    "avg_distance_miles_lag_1",
    "avg_route_energy_kwh_lag_1",
    "cold_regime",
    "capacity_event",
    "dow_sin",
    "dow_cos",
]


@dataclass
class ForecastRun:
    comparison: pd.DataFrame
    next_forecast: pd.DataFrame
    selected_model: str


def _prepare_features(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"])
    out = out.sort_values(["station_id", "date"]).reset_index(drop=True)
    out["day_of_week"] = out["date"].dt.dayofweek
    out["dow_sin"] = np.sin(2 * np.pi * out["day_of_week"] / 7.0)
    out["dow_cos"] = np.cos(2 * np.pi * out["day_of_week"] / 7.0)
    station_dummies = pd.get_dummies(out["station_id"], prefix="station", dtype=float)
    out = pd.concat([out, station_dummies], axis=1)
    station_cols = list(station_dummies.columns)
    for col in FEATURES:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out, FEATURES + station_cols


def _fill_from_training_statistics(
    training: pd.DataFrame,
    *others: pd.DataFrame,
    feature_cols: list[str],
) -> tuple[pd.DataFrame, ...]:
    """Impute features using training-only statistics to prevent temporal leakage."""
    training_filled = training.copy()
    medians = training_filled[feature_cols].median(numeric_only=True).reindex(feature_cols).fillna(0.0)
    training_filled[feature_cols] = training_filled[feature_cols].fillna(medians)
    outputs = [training_filled]
    for frame in others:
        filled = frame.copy()
        filled[feature_cols] = filled[feature_cols].fillna(medians)
        outputs.append(filled)
    return tuple(outputs)


def _split_fit_calibration(frame: pd.DataFrame, calibration_days: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    unique_dates = sorted(pd.to_datetime(frame["date"]).unique())
    if len(unique_dates) <= calibration_days + 2:
        raise ValueError(
            f"Insufficient pre-holdout dates for conformal calibration: "
            f"dates={len(unique_dates)}, calibration_days={calibration_days}"
        )
    cutoff = unique_dates[-calibration_days]
    fit = frame[frame["date"] < cutoff].copy()
    calibration = frame[frame["date"] >= cutoff].copy()
    if fit.empty or calibration.empty:
        raise ValueError("Conformal fit/calibration split must be non-empty")
    return fit, calibration


def _one_sided_conformal_correction(
    actual: np.ndarray,
    predicted_upper: np.ndarray,
    target_coverage: float,
) -> float:
    actual_values = np.asarray(actual, dtype=float)
    upper_values = np.asarray(predicted_upper, dtype=float)
    if actual_values.shape != upper_values.shape or actual_values.size == 0:
        raise ValueError("Conformal calibration requires non-empty aligned arrays")
    scores = actual_values - upper_values
    level = min(1.0, np.ceil((scores.size + 1) * target_coverage) / scores.size)
    correction = float(np.quantile(scores, level, method="higher"))
    return max(0.0, correction)


def _coverage_calibration_error(observed_coverage: float, target_coverage: float) -> float:
    return abs(float(observed_coverage) - float(target_coverage))


def _apply_conformal_calibration(
    calibration: pd.DataFrame,
    calibration_predictions: dict[str, tuple[np.ndarray, np.ndarray]],
    target_predictions: dict[str, tuple[np.ndarray, np.ndarray]],
    target_coverage: float,
) -> tuple[dict[str, tuple[np.ndarray, np.ndarray]], dict[str, dict[str, float]]]:
    actual = calibration["route_count"].to_numpy(dtype=float)
    corrected: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    metadata: dict[str, dict[str, float]] = {}
    for model, (target_p50, target_p90) in target_predictions.items():
        calibration_p50, calibration_p90 = calibration_predictions[model]
        del calibration_p50
        correction = _one_sided_conformal_correction(actual, calibration_p90, target_coverage)
        corrected_p90 = np.maximum(np.asarray(target_p90, dtype=float) + correction, target_p50)
        before = float((actual <= np.asarray(calibration_p90, dtype=float)).mean())
        after = float((actual <= np.asarray(calibration_p90, dtype=float) + correction).mean())
        corrected[model] = (np.asarray(target_p50, dtype=float), corrected_p90)
        metadata[model] = {
            "conformal_correction": correction,
            "calibration_samples": float(actual.size),
            "calibration_coverage_before": before,
            "calibration_coverage_after": after,
            "target_coverage": float(target_coverage),
        }
    return corrected, metadata


def _proportional_allocation(forecast: pd.Series, total_fleet: int) -> pd.Series:
    values = np.maximum(forecast.to_numpy(dtype=float), 1e-6)
    raw = values / values.sum() * total_fleet
    base = np.floor(raw).astype(int)
    remainder = int(total_fleet - base.sum())
    if remainder > 0:
        order = np.argsort(-(raw - base))
        base[order[:remainder]] += 1
    return pd.Series(base, index=forecast.index)


def decision_replay_context(cfg: ProjectConfig) -> dict[str, object]:
    initial = pd.read_csv(cfg.generated_dir / "initial_fleet.csv")
    stations = pd.read_csv(cfg.generated_dir / "stations.csv")
    initial_by_station = initial.groupby("station_id")["initial_count"].sum().astype(int)
    parking = stations.set_index("station_id")["parking_capacity"].astype(int)
    opt = cfg.section("optimization")
    sim = cfg.section("simulation")
    return {
        "initial_allocation": initial_by_station.to_dict(),
        "parking_capacity": parking.to_dict(),
        "redeployment_cost": float(opt.get("redeployment_cost", 90.0)),
        "unserved_cost": float(opt.get("unserved_cost", 1200.0)),
        "idle_cost": float(sim.get("idle_vehicle_cost", 85.0)),
        "reserve_quantile_weight": float(cfg.section("forecast").get("decision_reserve_quantile_weight", 0.5)),
    }


def _capacity_aware_redeployment(
    forecast: pd.Series,
    initial_allocation: pd.Series,
    parking_capacity: pd.Series,
) -> tuple[pd.Series, int]:
    stations = forecast.index
    allocation = initial_allocation.reindex(stations).fillna(0).astype(int).copy()
    parking = parking_capacity.reindex(stations).fillna(allocation).astype(int)
    target = np.maximum(np.rint(forecast.to_numpy(dtype=float)), 0).astype(int)
    target = pd.Series(np.minimum(target, parking.to_numpy(dtype=int)), index=stations)

    deficit = (target - allocation).clip(lower=0)
    surplus = (allocation - target).clip(lower=0)
    moved = 0
    while int(deficit.sum()) > 0 and int(surplus.sum()) > 0:
        destination = str(deficit.idxmax())
        origin = str(surplus.idxmax())
        destination_headroom = int(parking.loc[destination] - allocation.loc[destination])
        amount = min(int(deficit.loc[destination]), int(surplus.loc[origin]), destination_headroom)
        if amount <= 0:
            deficit.loc[destination] = 0
            continue
        allocation.loc[origin] -= amount
        allocation.loc[destination] += amount
        surplus.loc[origin] -= amount
        deficit.loc[destination] -= amount
        moved += amount
    return allocation, moved


def decision_replay(
    pred: pd.DataFrame,
    total_fleet: int,
    *,
    initial_allocation: dict[str, int] | None = None,
    parking_capacity: dict[str, int] | None = None,
    redeployment_cost: float = 90.0,
    unserved_cost: float = 1200.0,
    idle_cost: float = 85.0,
    reserve_quantile_weight: float = 0.5,
) -> dict[str, float]:
    daily_costs: list[float] = []
    service_rates: list[float] = []
    shortages: list[float] = []
    movements: list[float] = []
    active_fleet: list[float] = []
    for _, group in pred.groupby("date"):
        indexed_prediction = group.set_index("station_id")["prediction"].astype(float)
        indexed_p90 = (
            group.set_index("station_id")["p90"].astype(float) if "p90" in group.columns else indexed_prediction
        )
        commitment = np.ceil(
            indexed_prediction + float(reserve_quantile_weight) * np.maximum(indexed_p90 - indexed_prediction, 0.0)
        ).clip(lower=0)
        if initial_allocation is not None and parking_capacity is not None:
            physical_allocation, moved = _capacity_aware_redeployment(
                commitment,
                pd.Series(initial_allocation, dtype=int),
                pd.Series(parking_capacity, dtype=int),
            )
            active = np.minimum(commitment, physical_allocation).astype(int)
        else:
            requested = int(commitment.sum())
            if requested <= total_fleet:
                active = commitment.astype(int)
            else:
                active = _proportional_allocation(commitment, total_fleet)
            moved = 0
        actual = group.set_index("station_id")["actual"].astype(float)
        active = active.reindex(actual.index).fillna(0)
        shortage = np.maximum(actual - active, 0)
        idle = np.maximum(active - actual, 0)
        cost = float((shortage * unserved_cost + idle * idle_cost).sum() + moved * redeployment_cost)
        daily_costs.append(cost)
        shortages.append(float(shortage.sum()))
        movements.append(float(moved))
        active_fleet.append(float(active.sum()))
        service_rates.append(float(np.minimum(actual, active).sum() / max(actual.sum(), 1.0)))
    q = max(1, int(np.ceil(len(daily_costs) * 0.1)))
    tail = float(np.mean(sorted(shortages, reverse=True)[:q]))
    return {
        "decision_cost": float(np.mean(daily_costs)),
        "decision_service_rate": float(np.mean(service_rates)),
        "decision_shortage_cvar90": tail,
        "decision_redeployment_count": float(np.mean(movements)),
        "decision_redeployment_cost": float(np.mean(movements) * redeployment_cost),
        "decision_active_fleet": float(np.mean(active_fleet)),
    }


def _build_next_day_features(all_df: pd.DataFrame, feature_cols: list[str]) -> pd.DataFrame:
    """Create one-day-ahead rows using only observed history and known calendar inputs."""
    next_date = all_df["date"].max() + pd.Timedelta(days=1)
    latest = all_df.sort_values("date").groupby("station_id").tail(1).copy()
    next_rows = []
    station_columns = [column for column in feature_cols if column.startswith("station_")]
    for _, row in latest.iterrows():
        hist = all_df[all_df["station_id"] == row["station_id"]].sort_values("date")
        if len(hist) < 7:
            raise ValueError(f"At least seven observations are required for station {row['station_id']}")
        new = row.copy()
        new["date"] = next_date
        new["day_of_week"] = next_date.dayofweek
        new["dow_sin"] = np.sin(2 * np.pi * next_date.dayofweek / 7.0)
        new["dow_cos"] = np.cos(2 * np.pi * next_date.dayofweek / 7.0)
        new["route_count_lag_1"] = float(hist.iloc[-1]["route_count"])
        new["route_count_lag_7"] = float(hist.iloc[-7]["route_count"])
        new["route_count_rolling_7"] = float(hist.tail(7)["route_count"].mean())
        new["avg_cube_lag_1"] = float(hist.iloc[-1]["avg_cube"])
        new["avg_distance_miles_lag_1"] = float(hist.iloc[-1]["avg_distance_miles"])
        new["avg_route_energy_kwh_lag_1"] = float(hist.iloc[-1]["avg_route_energy_kwh"])
        new["cold_regime"] = float(next_date.month in (1, 2, 12))
        # Capacity events are future-known inputs. In the absence of a supplied schedule,
        # do not carry a transient event forward from the last observed date.
        new["capacity_event"] = 0.0
        for column in station_columns:
            new[column] = 1.0 if column == f"station_{row['station_id']}" else 0.0
        next_rows.append(new)
    return pd.DataFrame(next_rows)


def _fit_predict_models(train: pd.DataFrame, test: pd.DataFrame, feature_cols: list[str], cfg: ProjectConfig):
    X_train = train[feature_cols].astype(float)
    y_train = train["route_count"].astype(float)
    X_test = test[feature_cols].astype(float)

    predictions: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    seasonal = np.maximum(test["route_count_lag_7"].to_numpy(dtype=float), 0)
    residual = train["route_count"] - train["route_count_lag_7"]
    seasonal_width = max(float(np.nanquantile(np.abs(residual), 0.9)), 2.0)
    predictions["seasonal_naive"] = (seasonal, seasonal + seasonal_width)

    scaler = StandardScaler()
    Xs = scaler.fit_transform(X_train)
    poisson = PoissonRegressor(alpha=0.15, max_iter=1000)
    poisson.fit(Xs, y_train)
    poisson_p50 = np.maximum(poisson.predict(scaler.transform(X_test)), 0)
    poisson_width = max(float(np.nanquantile(np.abs(y_train - poisson.predict(Xs)), 0.9)), 2.0)
    predictions["poisson"] = (poisson_p50, poisson_p50 + poisson_width)

    use_lightgbm = bool(cfg.section("forecast").get("use_lightgbm", True))
    lgbm_cls = None
    if use_lightgbm:
        try:
            from lightgbm import LGBMRegressor as _LGBMRegressor

            lgbm_cls = _LGBMRegressor
        except ImportError:
            lgbm_cls = None
    if lgbm_cls is not None:
        common = dict(
            n_estimators=160,
            n_jobs=1,
            learning_rate=0.045,
            num_leaves=15,
            min_child_samples=12,
            random_state=cfg.seed,
            verbosity=-1,
        )
        p50_model = lgbm_cls(objective="quantile", alpha=0.5, **common)
        p90_model = lgbm_cls(objective="quantile", alpha=0.9, **common)
    else:
        p50_model = GradientBoostingRegressor(loss="quantile", alpha=0.5, random_state=cfg.seed)
        p90_model = GradientBoostingRegressor(loss="quantile", alpha=0.9, random_state=cfg.seed)
    p50_model.fit(X_train, y_train)
    p90_model.fit(X_train, y_train)
    p50 = np.maximum(p50_model.predict(X_test), 0)
    p90 = np.maximum(p90_model.predict(X_test), p50)
    predictions["quantile_boosting"] = (p50, p90)
    return predictions


def _select_champion(comparison: pd.DataFrame, min_coverage: float) -> tuple[pd.DataFrame, str]:
    ranked = comparison.copy()
    ranked["calibration_gate"] = ranked["p90_coverage"] >= min_coverage
    eligible = ranked[ranked["calibration_gate"]].copy()
    if eligible.empty:
        eligible = ranked.copy()
    eligible = eligible.sort_values(
        ["decision_cost", "decision_shortage_cvar90", "p90_calibration_error", "worst_station_mae", "mae"]
    )
    selected = str(eligible.iloc[0]["model"])
    ranked["selected"] = ranked["model"].eq(selected)
    ranked = ranked.sort_values(
        ["selected", "decision_cost", "decision_shortage_cvar90", "mae"],
        ascending=[False, True, True, True],
    ).reset_index(drop=True)
    return ranked, selected


def integrate_gpu_candidate(
    cfg: ProjectConfig,
    forecast: ForecastRun,
    gpu_result: dict,
) -> ForecastRun:
    """Compete an optional GPU forecast under the same calibration and decision gate."""
    if gpu_result.get("status") != "PASS":
        return forecast
    candidate_row = gpu_result.get("comparison_row")
    backtest_path = cfg.artifacts_dir / "gpu_backtest_predictions.csv"
    next_path = cfg.artifacts_dir / "gpu_next_day_forecast.csv"
    if not isinstance(candidate_row, dict) or not backtest_path.exists() or not next_path.exists():
        raise ValueError("GPU challenger passed without complete candidate artifacts")

    comparison = pd.concat(
        [
            forecast.comparison.drop(columns=["selected", "calibration_gate"], errors="ignore"),
            pd.DataFrame([candidate_row]),
        ],
        ignore_index=True,
    )
    min_coverage = float(cfg.section("forecast").get("min_p90_coverage", 0.75))
    comparison, selected = _select_champion(comparison, min_coverage)
    gpu_selected = selected == str(candidate_row["model"])
    next_forecast = pd.read_csv(next_path) if gpu_selected else forecast.next_forecast.copy()
    next_forecast["date"] = pd.to_datetime(next_forecast["date"])
    next_forecast["selected_model"] = selected

    classical_backtest = pd.read_csv(cfg.artifacts_dir / "forecast_backtest_predictions.csv")
    gpu_backtest = pd.read_csv(backtest_path)
    combined_backtest = pd.concat([classical_backtest, gpu_backtest], ignore_index=True)
    write_csv_atomic(cfg.artifacts_dir / "forecast_model_comparison.csv", comparison)
    write_csv_atomic(cfg.artifacts_dir / "forecast_backtest_predictions.csv", combined_backtest)
    write_csv_atomic(cfg.artifacts_dir / "next_day_forecast.csv", next_forecast)
    write_json_atomic(
        cfg.artifacts_dir / "forecast_selection.json",
        {
            "selected_model": selected,
            "selection_basis": "calibration_gate_then_station_capacity_aware_active_fleet_decision_cost_then_tail_shortage_then_calibration_then_worst_station_mae",
            "minimum_p90_coverage": min_coverage,
            "gpu_candidate_evaluated": True,
            "gpu_candidate_selected": gpu_selected,
        },
    )
    gpu_result["promoted_to_pipeline_champion"] = gpu_selected
    gpu_result["final_selected_model"] = selected
    write_json_atomic(cfg.artifacts_dir / "gpu_forecast_result.json", gpu_result)
    return ForecastRun(comparison=comparison, next_forecast=next_forecast, selected_model=selected)


def run_forecasting(cfg: ProjectConfig) -> ForecastRun:
    mart = read_mart(cfg, "mart_station_day_features")
    df, feature_cols = _prepare_features(mart)
    unique_dates = sorted(df["date"].unique())
    test_days = int(cfg.section("forecast").get("test_days", 14))
    cutoff = unique_dates[-test_days]
    pretest = df[df["date"] < cutoff].copy()
    test = df[df["date"] >= cutoff].copy()
    calibration_days = int(cfg.section("forecast").get("calibration_days", 14))
    target_coverage = float(cfg.section("forecast").get("p90_target_coverage", 0.90))
    fit, calibration = _split_fit_calibration(pretest, calibration_days)
    fit, calibration, test = _fill_from_training_statistics(
        fit,
        calibration,
        test,
        feature_cols=feature_cols,
    )
    calibration_predictions = _fit_predict_models(fit, calibration, feature_cols, cfg)
    pretest_filled = pd.concat([fit, calibration], ignore_index=True)
    raw_predictions = _fit_predict_models(pretest_filled, test, feature_cols, cfg)
    predictions, calibration_metadata = _apply_conformal_calibration(
        calibration,
        calibration_predictions,
        raw_predictions,
        target_coverage,
    )

    initial_fleet = pd.read_csv(cfg.generated_dir / "initial_fleet.csv")
    total_fleet = int(initial_fleet["initial_count"].sum())
    replay_context = decision_replay_context(cfg)
    rows = []
    prediction_frames = []
    for name, (p50, p90) in predictions.items():
        frame = test[["date", "station_id", "route_count"]].copy()
        frame = frame.rename(columns={"route_count": "actual"})
        frame["prediction"] = p50
        frame["p90"] = p90
        frame["model"] = name
        replay = decision_replay(frame, total_fleet, **replay_context)
        station_mae = (
            frame.assign(abs_error=(frame["actual"] - frame["prediction"]).abs())
            .groupby("station_id")["abs_error"]
            .mean()
        )
        coverage = float((frame["actual"] <= frame["p90"]).mean())
        rows.append(
            {
                "model": name,
                "mae": mean_absolute_error(frame["actual"], frame["prediction"]),
                "worst_station_mae": float(station_mae.max()),
                "pinball_p90": mean_pinball_loss(frame["actual"], frame["p90"], alpha=0.9),
                "p90_coverage": coverage,
                "p90_calibration_error": _coverage_calibration_error(coverage, target_coverage),
                "mean_interval_width": float((frame["p90"] - frame["prediction"]).mean()),
                **calibration_metadata[name],
                **replay,
            }
        )
        prediction_frames.append(frame)
    comparison = pd.DataFrame(rows)
    min_coverage = float(cfg.section("forecast").get("min_p90_coverage", 0.75))
    comparison, selected = _select_champion(comparison, min_coverage)

    # Refit through the last observed date and create one-day-ahead rows.
    all_df = df.copy()
    next_df = _build_next_day_features(all_df, feature_cols)
    final_fit, final_calibration = _split_fit_calibration(all_df, calibration_days)
    final_fit, final_calibration, next_df = _fill_from_training_statistics(
        final_fit,
        final_calibration,
        next_df,
        feature_cols=feature_cols,
    )
    final_calibration_predictions = _fit_predict_models(final_fit, final_calibration, feature_cols, cfg)
    final_training = pd.concat([final_fit, final_calibration], ignore_index=True)
    raw_next_predictions = _fit_predict_models(final_training, next_df, feature_cols, cfg)
    all_predictions, final_calibration_metadata = _apply_conformal_calibration(
        final_calibration,
        final_calibration_predictions,
        raw_next_predictions,
        target_coverage,
    )
    p50, p90 = all_predictions[selected]
    next_forecast = next_df[["date", "station_id"]].copy()
    next_forecast["p50"] = np.maximum(p50, 1.0)
    next_forecast["p90"] = np.maximum(p90, next_forecast["p50"])
    next_forecast["selected_model"] = selected

    cfg.artifacts_dir.mkdir(parents=True, exist_ok=True)
    write_csv_atomic(cfg.artifacts_dir / "forecast_model_comparison.csv", comparison)
    write_csv_atomic(cfg.artifacts_dir / "forecast_backtest_predictions.csv", pd.concat(prediction_frames))
    write_csv_atomic(cfg.artifacts_dir / "next_day_forecast.csv", next_forecast)
    write_json_atomic(
        cfg.artifacts_dir / "forecast_selection.json",
        {
            "selected_model": selected,
            "selection_basis": "calibration_gate_then_station_capacity_aware_active_fleet_decision_cost_then_tail_shortage_then_calibration_then_worst_station_mae",
            "minimum_p90_coverage": min_coverage,
            "p90_target_coverage": target_coverage,
            "calibration_days": calibration_days,
            "selected_model_conformal": final_calibration_metadata[selected],
        },
    )
    return ForecastRun(comparison=comparison, next_forecast=next_forecast, selected_model=selected)
