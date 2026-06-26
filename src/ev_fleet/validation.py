from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .config import ProjectConfig
from .contracts import validate_generated_data, validate_required_artifacts
from .manifest import verify_artifact_manifest, verify_generated_data_manifest
from .sql_pipeline import read_mart


def _read_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return payload


def _strict_boolean_series(series: pd.Series, name: str) -> pd.Series:
    """Parse a persisted boolean column without Python truthiness surprises."""
    if series.isna().any():
        raise ValueError(f"{name} must not contain missing values")
    if pd.api.types.is_bool_dtype(series.dtype):
        return series.astype(bool)
    normalized = series.astype(str).str.strip().str.lower()
    mapping = {"true": True, "false": False, "1": True, "0": False}
    invalid = sorted(set(normalized) - set(mapping))
    if invalid:
        raise ValueError(f"{name} must contain only true/false or 1/0; invalid={invalid[:5]}")
    return normalized.map(mapping).astype(bool)


def _strict_json_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a JSON boolean")
    return value


def validate_cross_artifact_contracts_at_root(artifacts_dir: str | Path) -> dict[str, object]:
    root = Path(artifacts_dir)
    summary = _read_json(root / "run_summary.json")
    recommended_plan = _read_json(root / "recommended_plan.json")
    recommended_rcce = _read_json(root / "recommended_rcce.json")
    forecast_comparison = pd.read_csv(root / "forecast_model_comparison.csv")
    base_selection = pd.read_csv(root / "base_policy_selection.csv")

    forecast_selected = _strict_boolean_series(forecast_comparison["selected"], "forecast_model_comparison.selected")
    selected_forecasts = forecast_comparison.loc[forecast_selected, "model"].astype(str)
    if len(selected_forecasts) != 1:
        raise ValueError("forecast_model_comparison.csv must select exactly one model")
    if selected_forecasts.iloc[0] != str(summary.get("selected_forecast_model")):
        raise ValueError("Selected forecast model does not match run_summary.json")

    policy_selected = _strict_boolean_series(base_selection["selected"], "base_policy_selection.selected")
    selected_policies = base_selection.loc[policy_selected, "policy"].astype(str)
    if len(selected_policies) != 1:
        raise ValueError("base_policy_selection.csv must select exactly one base policy")
    if selected_policies.iloc[0] != str(summary.get("selected_base_policy")):
        raise ValueError("Selected base policy does not match run_summary.json")

    rcce_status = str(recommended_rcce.get("status"))
    if rcce_status != str(summary.get("recommended_rcce")):
        raise ValueError("recommended_rcce.json status does not match run_summary.json")
    automatic_release = _strict_json_bool(
        summary.get("automatic_release_allowed"), "run_summary.automatic_release_allowed"
    )
    if automatic_release != (rcce_status == "RELEASE"):
        raise ValueError("automatic_release_allowed is inconsistent with the recommended RCCE status")

    source = str(summary.get("recommendation_source"))
    if source == "corrective_capacity_candidate":
        source_plan = _read_json(root / "corrective_plan.json")
    else:
        source_map = {
            "deterministic": "deterministic_plan.json",
            "risk_neutral": "risk_neutral_plan.json",
            "risk_aware": "stochastic_plan.json",
        }
        if source not in source_map:
            raise ValueError(f"Unknown recommendation_source: {source}")
        source_plan = _read_json(root / source_map[source])
    if recommended_plan != source_plan:
        raise ValueError("recommended_plan.json does not match recommendation_source")

    return {
        "selected_forecast_model": selected_forecasts.iloc[0],
        "selected_base_policy": selected_policies.iloc[0],
        "recommended_plan_name": recommended_plan.get("name"),
        "recommended_rcce": rcce_status,
        "recommendation_source": source,
    }


def validate_cross_artifact_contracts(cfg: ProjectConfig) -> dict[str, object]:
    return validate_cross_artifact_contracts_at_root(cfg.artifacts_dir)


def validate_repository_outputs(cfg: ProjectConfig) -> dict[str, object]:
    required = validate_required_artifacts(cfg.artifacts_dir)
    summary_path = cfg.artifacts_dir / "run_summary.json"
    summary = _read_json(summary_path)
    run_id = summary.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_summary.json must contain a non-empty run_id")
    verified = verify_artifact_manifest(cfg.artifacts_dir, expected_run_id=run_id)
    generated_verified = verify_generated_data_manifest(
        cfg.generated_dir,
        cfg.artifacts_dir / "generated_data_manifest.json",
        expected_run_id=run_id,
    )
    data_contract = validate_generated_data(cfg)
    capacity = read_mart(cfg, "mart_station_capacity")
    if summary.get("pipeline_status") != "PASS":
        raise ValueError("Latest pipeline run did not pass")
    if not capacity["station_id"].is_unique:
        raise ValueError("mart_station_capacity must have one row per station")
    cross_artifact = validate_cross_artifact_contracts(cfg)
    return {
        "status": "PASS",
        "required_artifacts": len(required),
        "manifest_files_verified": len(verified),
        "generated_files_verified": len(generated_verified),
        "data_contract": data_contract,
        "pipeline_status": summary["pipeline_status"],
        "recommended_rcce": summary["recommended_rcce"],
        "run_id": run_id,
        "cross_artifact_contract": cross_artifact,
    }


def semantic_artifact_hashes(artifacts_dir: str | Path) -> dict[str, str]:
    """Hashes of deterministic science artifacts, excluding timings and manifests."""
    import hashlib

    from .io_utils import sha256_file

    root = Path(artifacts_dir)
    included = [
        "forecast_model_comparison.csv",
        "forecast_backtest_predictions.csv",
        "next_day_forecast.csv",
        "deterministic_plan.json",
        "risk_neutral_plan.json",
        "stochastic_plan.json",
        "stochastic_value.json",
        "base_policy_selection.csv",
        "corrective_frontier.csv",
        "recommended_plan.json",
        "recommended_rcce.json",
        "paired_policy_comparison.csv",
        "rolling_reoptimization_metrics.json",
        "saa_runs.csv",
        "saa_summary.json",
    ]
    missing = [relative for relative in included if not (root / relative).exists()]
    if missing:
        raise FileNotFoundError(f"Missing semantic artifacts: {missing}")
    volatile_keys = {"generated_at_utc", "stage_seconds"}

    def is_volatile_key(key: str) -> bool:
        return key in volatile_keys or key == "runtime_seconds" or key.endswith("_runtime_seconds")

    def strip_volatile(value):
        if isinstance(value, dict):
            return {key: strip_volatile(item) for key, item in sorted(value.items()) if not is_volatile_key(key)}
        if isinstance(value, list):
            return [strip_volatile(item) for item in value]
        return value

    hashes: dict[str, str] = {}
    for relative in included:
        path = root / relative
        if path.suffix.lower() != ".json":
            hashes[relative] = sha256_file(path)
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        canonical = json.dumps(
            strip_volatile(payload),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        hashes[relative] = hashlib.sha256(canonical).hexdigest()
    return hashes
