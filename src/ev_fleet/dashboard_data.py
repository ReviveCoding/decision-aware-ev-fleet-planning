from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .manifest import verify_artifact_manifest
from .validation import validate_cross_artifact_contracts_at_root

_REQUIRED_SUMMARY_KEYS = {
    "pipeline_status",
    "selected_forecast_model",
    "selected_base_policy",
    "recommended_rcce",
    "formal_sample_vss",
    "formal_sample_evpi",
}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Dashboard artifact not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Dashboard artifact must contain a JSON object: {path}")
    return payload


def load_dashboard_payload(artifacts_dir: str | Path) -> dict[str, Any]:
    root = Path(artifacts_dir)
    summary_path = root / "run_summary.json"
    summary = _read_json(summary_path)
    run_id = summary.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_summary.json is missing a non-empty run_id")
    verify_artifact_manifest(root, expected_run_id=run_id)
    semantic_contract = validate_cross_artifact_contracts_at_root(root)
    missing = sorted(_REQUIRED_SUMMARY_KEYS.difference(summary))
    if missing:
        raise ValueError(f"run_summary.json is missing dashboard fields: {missing}")

    base_policy = str(summary["selected_base_policy"])
    base_rcce_key = {
        "deterministic": "deterministic_rcce",
        "risk_neutral": "risk_neutral_rcce",
        "risk_aware": "stochastic_rcce",
    }.get(base_policy)
    if base_rcce_key is None or base_rcce_key not in summary:
        raise ValueError(f"Unsupported or incomplete base-policy summary: {base_policy}")

    rcce = _read_json(root / "recommended_rcce.json")
    plan = _read_json(root / "recommended_plan.json")
    current_run_id = _read_json(summary_path).get("run_id")
    if current_run_id != run_id:
        raise ValueError("Artifacts changed while loading dashboard payload")

    return {
        "summary": summary,
        "rcce": rcce,
        "plan": plan,
        "semantic_contract": semantic_contract,
        "metrics": {
            "forecast_champion": summary["selected_forecast_model"],
            "base_policy": base_policy,
            "base_rcce": summary[base_rcce_key],
            "recommended_rcce": summary["recommended_rcce"],
            "formal_sample_vss": float(summary["formal_sample_vss"]),
            "formal_sample_evpi": float(summary["formal_sample_evpi"]),
        },
    }
