from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd

from .config import ProjectConfig
from .forecasting import (
    _coverage_calibration_error,
    _one_sided_conformal_correction,
    decision_replay,
    decision_replay_context,
)
from .io_utils import write_csv_atomic, write_json_atomic


def train_temporal_quantile_challenger(cfg: ProjectConfig) -> dict:
    """Train and evaluate a compact CUDA-capable temporal quantile Transformer.

    The holdout boundary matches the classical forecasting test window. Validation
    predictions are scored with the same calibration and downstream fleet-decision
    replay used by the classical candidates. A final model is then refit on all
    observed windows solely to create the next-day candidate forecast.
    """
    try:
        import torch
        from torch import nn
        from torch.nn import functional as functional
        from torch.utils.data import DataLoader, TensorDataset
    except ImportError as exc:  # pragma: no cover
        return {"status": "SKIPPED", "reason": f"torch unavailable: {exc}"}

    torch.set_num_threads(max(1, min(4, torch.get_num_threads())))
    torch.manual_seed(cfg.seed)
    np.random.seed(cfg.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(cfg.seed)
    try:
        torch.use_deterministic_algorithms(True, warn_only=True)
    except AttributeError:  # pragma: no cover
        pass

    history = pd.read_csv(cfg.generated_dir / "station_day_history.csv")
    history["date"] = pd.to_datetime(history["date"])
    seq_len = int(cfg.section("forecast").get("sequence_length", 14))
    epochs = int(cfg.section("forecast").get("torch_epochs", 5))
    refit_epochs = int(cfg.section("forecast").get("torch_refit_epochs", max(1, epochs // 2)))
    observed_dates = np.sort(history["date"].unique())
    test_days = min(int(cfg.section("forecast").get("test_days", 14)), len(observed_dates) - 1)
    calibration_days = int(cfg.section("forecast").get("calibration_days", 14))
    target_coverage = float(cfg.section("forecast").get("p90_target_coverage", 0.90))
    if test_days < 1:
        return {"status": "SKIPPED", "reason": "insufficient target dates for temporal validation"}
    cutoff = observed_dates[-test_days]
    pretest_dates = observed_dates[observed_dates < cutoff]
    if len(pretest_dates) <= calibration_days + 2:
        return {"status": "SKIPPED", "reason": "insufficient dates for temporal conformal calibration"}
    calibration_cutoff = pretest_dates[-calibration_days]
    series: list[np.ndarray] = []
    targets: list[float] = []
    means: list[float] = []
    scales: list[float] = []
    dates: list[np.datetime64] = []
    sample_stations: list[str] = []
    latest_sequences: list[np.ndarray] = []
    latest_means: list[float] = []
    latest_scales: list[float] = []
    latest_stations: list[str] = []

    for station_id, group in history.sort_values("date").groupby("station_id", sort=True):
        values = group["route_count"].to_numpy(dtype=np.float32)
        target_dates = group["date"].to_numpy()
        if len(values) <= seq_len:
            continue
        training_values = group.loc[group["date"] < calibration_cutoff, "route_count"].to_numpy(dtype=np.float32)
        if len(training_values) <= seq_len:
            continue
        mean = float(training_values.mean())
        scale = float(max(training_values.std(), 1.0))
        normalized = (values - mean) / scale
        for index in range(seq_len, len(values)):
            series.append(normalized[index - seq_len : index, None])
            targets.append(float(normalized[index]))
            means.append(mean)
            scales.append(scale)
            dates.append(target_dates[index])
            sample_stations.append(str(station_id))
        latest_sequences.append(normalized[-seq_len:, None])
        latest_means.append(mean)
        latest_scales.append(scale)
        latest_stations.append(str(station_id))

    if not series:
        return {"status": "SKIPPED", "reason": "insufficient history for configured sequence length"}
    x = np.asarray(series, dtype=np.float32)
    y = np.asarray(targets, dtype=np.float32)
    means_array = np.asarray(means, dtype=np.float32)
    scales_array = np.asarray(scales, dtype=np.float32)
    date_array = np.asarray(dates)
    station_array = np.asarray(sample_stations)
    train_mask = date_array < calibration_cutoff
    calibration_mask = (date_array >= calibration_cutoff) & (date_array < cutoff)
    validation_mask = date_array >= cutoff
    if not train_mask.any() or not calibration_mask.any() or not validation_mask.any():
        return {"status": "SKIPPED", "reason": "temporal fit/calibration/validation split is empty"}

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    class Model(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.proj = nn.Linear(1, 32)
            layer = nn.TransformerEncoderLayer(
                d_model=32,
                nhead=4,
                dim_feedforward=64,
                dropout=0.1,
                batch_first=True,
            )
            self.encoder = nn.TransformerEncoder(layer, num_layers=2)
            self.head = nn.Linear(32, 2)

        def forward(self, features):
            hidden = self.encoder(self.proj(features))
            raw = self.head(hidden[:, -1])
            p50 = raw[:, 0]
            p90 = p50 + functional.softplus(raw[:, 1])
            return torch.stack((p50, p90), dim=1)

    def pinball(prediction, target, quantile: float):
        error = target - prediction
        return torch.maximum(quantile * error, (quantile - 1) * error).mean()

    def train_model(model, loader, epoch_count: int) -> None:
        optimizer = torch.optim.AdamW(model.parameters(), lr=2e-3, weight_decay=1e-4)
        model.train()
        for _ in range(epoch_count):
            for batch_x, batch_y in loader:
                batch_x, batch_y = batch_x.to(device), batch_y.to(device)
                optimizer.zero_grad(set_to_none=True)
                output = model(batch_x)
                loss = pinball(output[:, 0], batch_y, 0.5) + pinball(output[:, 1], batch_y, 0.9)
                loss.backward()
                optimizer.step()

    train_loader = DataLoader(
        TensorDataset(torch.from_numpy(x[train_mask]), torch.from_numpy(y[train_mask])),
        batch_size=64,
        shuffle=True,
        generator=torch.Generator().manual_seed(cfg.seed),
    )
    model = Model().to(device)
    started = time.perf_counter()
    train_model(model, train_loader, epochs)
    validation_training_seconds = time.perf_counter() - started

    model.eval()
    with torch.no_grad():
        calibration_output = model(torch.from_numpy(x[calibration_mask]).to(device)).cpu().numpy()
    calibration_actual = y[calibration_mask] * scales_array[calibration_mask] + means_array[calibration_mask]
    calibration_p90 = calibration_output[:, 1] * scales_array[calibration_mask] + means_array[calibration_mask]
    calibration_p50 = calibration_output[:, 0] * scales_array[calibration_mask] + means_array[calibration_mask]
    calibration_p50 = np.maximum(calibration_p50, 0.0)
    calibration_p90 = np.maximum(calibration_p90, calibration_p50)
    conformal_correction = _one_sided_conformal_correction(
        calibration_actual,
        calibration_p90,
        target_coverage,
    )
    calibration_coverage_before = float((calibration_actual <= calibration_p90).mean())
    calibration_coverage_after = float((calibration_actual <= calibration_p90 + conformal_correction).mean())

    pretest_mask = train_mask | calibration_mask
    pretest_loader = DataLoader(
        TensorDataset(torch.from_numpy(x[pretest_mask]), torch.from_numpy(y[pretest_mask])),
        batch_size=64,
        shuffle=True,
        generator=torch.Generator().manual_seed(cfg.seed + 1),
    )
    validation_model = Model().to(device)
    validation_model.load_state_dict(model.state_dict())
    refit_started = time.perf_counter()
    train_model(validation_model, pretest_loader, refit_epochs)
    validation_refit_seconds = time.perf_counter() - refit_started
    validation_model.eval()
    with torch.no_grad():
        output = validation_model(torch.from_numpy(x[validation_mask]).to(device)).cpu().numpy()
    actual = y[validation_mask] * scales_array[validation_mask] + means_array[validation_mask]
    p50 = output[:, 0] * scales_array[validation_mask] + means_array[validation_mask]
    p90 = output[:, 1] * scales_array[validation_mask] + means_array[validation_mask]
    p50 = np.maximum(p50, 0.0)
    raw_validation_p90 = np.maximum(p90, p50)
    p90 = raw_validation_p90 + conformal_correction

    backtest = pd.DataFrame(
        {
            "date": pd.to_datetime(date_array[validation_mask]),
            "station_id": station_array[validation_mask],
            "actual": actual,
            "prediction": p50,
            "p90": p90,
            "model": "temporal_quantile_transformer",
        }
    ).sort_values(["date", "station_id"])
    initial_fleet = pd.read_csv(cfg.generated_dir / "initial_fleet.csv")
    replay = decision_replay(
        backtest,
        int(initial_fleet["initial_count"].sum()),
        **decision_replay_context(cfg),
    )
    station_mae = (
        backtest.assign(abs_error=(backtest["actual"] - backtest["prediction"]).abs())
        .groupby("station_id")["abs_error"]
        .mean()
    )
    coverage = float((backtest["actual"] <= backtest["p90"]).mean())
    error90 = backtest["actual"].to_numpy() - backtest["p90"].to_numpy()
    comparison_row = {
        "model": "temporal_quantile_transformer",
        "mae": float((backtest["actual"] - backtest["prediction"]).abs().mean()),
        "worst_station_mae": float(station_mae.max()),
        "pinball_p90": float(np.mean(np.maximum(0.9 * error90, -0.1 * error90))),
        "p90_coverage": coverage,
        "p90_calibration_error": _coverage_calibration_error(coverage, target_coverage),
        "mean_interval_width": float((backtest["p90"] - backtest["prediction"]).mean()),
        "conformal_correction": conformal_correction,
        "calibration_samples": float(calibration_actual.size),
        "calibration_coverage_before": calibration_coverage_before,
        "calibration_coverage_after": calibration_coverage_after,
        "target_coverage": target_coverage,
        **replay,
    }

    all_loader = DataLoader(
        TensorDataset(torch.from_numpy(x), torch.from_numpy(y)),
        batch_size=64,
        shuffle=True,
        generator=torch.Generator().manual_seed(cfg.seed + 1),
    )
    final_model = Model().to(device)
    final_model.load_state_dict(validation_model.state_dict())
    refit_started = time.perf_counter()
    train_model(final_model, all_loader, refit_epochs)
    final_refit_seconds = time.perf_counter() - refit_started
    final_model.eval()
    next_x = np.asarray(latest_sequences, dtype=np.float32)
    with torch.no_grad():
        next_output = final_model(torch.from_numpy(next_x).to(device)).cpu().numpy()
    latest_means_array = np.asarray(latest_means, dtype=np.float32)
    latest_scales_array = np.asarray(latest_scales, dtype=np.float32)
    next_p50 = np.maximum(next_output[:, 0] * latest_scales_array + latest_means_array, 1.0)
    raw_next_p90 = np.maximum(next_output[:, 1] * latest_scales_array + latest_means_array, next_p50)
    next_correction = _one_sided_conformal_correction(actual, raw_validation_p90, target_coverage)
    next_p90 = raw_next_p90 + next_correction
    next_date = history["date"].max() + pd.Timedelta(days=1)
    next_forecast = pd.DataFrame(
        {
            "date": next_date,
            "station_id": latest_stations,
            "p50": next_p50,
            "p90": next_p90,
            "selected_model": "temporal_quantile_transformer",
        }
    )

    write_csv_atomic(cfg.artifacts_dir / "gpu_backtest_predictions.csv", backtest)
    write_csv_atomic(cfg.artifacts_dir / "gpu_next_day_forecast.csv", next_forecast)
    checkpoint = cfg.artifacts_dir / "temporal_quantile_transformer.pt"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{checkpoint.name}.", suffix=".tmp", dir=checkpoint.parent)
    os.close(descriptor)
    temporary_checkpoint = Path(temporary_name)
    try:
        torch.save(
            {
                "state_dict": final_model.state_dict(),
                "sequence_length": seq_len,
                "validation_start_date": str(pd.Timestamp(cutoff).date()),
                "calibration_start_date": str(pd.Timestamp(calibration_cutoff).date()),
                "stations": latest_stations,
                "refit_on_all_observed_windows": True,
            },
            temporary_checkpoint,
        )
        os.replace(temporary_checkpoint, checkpoint)
    finally:
        temporary_checkpoint.unlink(missing_ok=True)
    result = {
        "status": "PASS",
        "device": str(device),
        "epochs": epochs,
        "refit_epochs": refit_epochs,
        "validation_training_seconds": round(validation_training_seconds, 4),
        "validation_refit_seconds": round(validation_refit_seconds, 4),
        "final_refit_seconds": round(final_refit_seconds, 4),
        "training_samples": int(train_mask.sum()),
        "calibration_samples": int(calibration_mask.sum()),
        "validation_samples": int(validation_mask.sum()),
        "calibration_start_date": str(pd.Timestamp(calibration_cutoff).date()),
        "validation_start_date": str(pd.Timestamp(cutoff).date()),
        "normalization_statistics_boundary": "station statistics computed from pre-calibration observations only",
        "conformal_correction": conformal_correction,
        "next_day_conformal_correction": next_correction,
        "comparison_row": comparison_row,
        "checkpoint": str(checkpoint),
        "backtest_predictions": str(cfg.artifacts_dir / "gpu_backtest_predictions.csv"),
        "next_day_forecast": str(cfg.artifacts_dir / "gpu_next_day_forecast.csv"),
        "model_name": "temporal_quantile_transformer",
        "promoted_to_pipeline_champion": False,
        "claim_boundary": "GPU-capable temporal quantile Transformer challenger; not a full TFT implementation",
    }
    write_json_atomic(cfg.artifacts_dir / "gpu_forecast_result.json", result)
    return result
