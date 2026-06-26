from __future__ import annotations

import copy
import shutil

import yaml
from fastapi.testclient import TestClient

from ev_fleet.api import create_app


def test_health_endpoint(cfg, pipeline_summary):
    with TestClient(create_app(cfg.source_path)) as client:
        response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["artifact_ready"] is True
    assert payload["manifest_files_verified"] > 0
    assert payload["run_id"] == pipeline_summary["run_id"]


def test_liveness_is_independent_but_readiness_rejects_missing_artifacts(cfg, tmp_path):
    raw = copy.deepcopy(cfg.raw)
    raw["paths"] = {
        "generated_data": str(tmp_path / "generated"),
        "artifacts": str(tmp_path / "artifacts"),
        "database": str(tmp_path / "artifacts" / "fleet.sqlite"),
    }
    config_path = tmp_path / "api.yaml"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with TestClient(create_app(config_path)) as client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")
        plan = client.get("/plans/latest")
    assert live.status_code == 200
    assert ready.status_code == 503
    assert ready.json()["artifact_ready"] is False
    assert plan.status_code == 503


def test_readiness_detects_manifest_tampering(cfg, pipeline_summary, tmp_path):
    artifact_copy = tmp_path / "artifacts"
    generated_copy = tmp_path / "generated"
    shutil.copytree(cfg.artifacts_dir, artifact_copy)
    shutil.copytree(cfg.generated_dir, generated_copy)
    raw = copy.deepcopy(cfg.raw)
    raw["paths"] = {
        "generated_data": str(generated_copy),
        "artifacts": str(artifact_copy),
        "database": str(artifact_copy / "fleet_planning.sqlite"),
    }
    config_path = tmp_path / "tampered.yaml"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    (artifact_copy / "recommended_plan.json").write_text("{}\n", encoding="utf-8")
    with TestClient(create_app(config_path)) as client:
        ready = client.get("/health")
    assert ready.status_code == 503
    assert "mismatch" in ready.json()["detail"].lower()


def test_readiness_rejects_semantically_inconsistent_but_rehashed_artifacts(cfg, pipeline_summary, tmp_path):
    import json

    from ev_fleet.manifest import build_artifact_manifest

    artifact_copy = tmp_path / "artifacts"
    generated_copy = tmp_path / "generated"
    shutil.copytree(cfg.artifacts_dir, artifact_copy)
    shutil.copytree(cfg.generated_dir, generated_copy)
    raw = copy.deepcopy(cfg.raw)
    raw["paths"] = {
        "generated_data": str(generated_copy),
        "artifacts": str(artifact_copy),
        "database": str(artifact_copy / "fleet_planning.sqlite"),
    }
    config_path = tmp_path / "semantic-tamper.yaml"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    summary_path = artifact_copy / "run_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["selected_forecast_model"] = "not-the-selected-model"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    build_artifact_manifest(artifact_copy)

    with TestClient(create_app(config_path)) as client:
        ready = client.get("/health/ready")
    assert ready.status_code == 503
    assert "selected forecast" in ready.json()["detail"].lower()


def test_readiness_detects_generated_data_tampering(cfg, pipeline_summary, tmp_path):
    artifact_copy = tmp_path / "artifacts"
    generated_copy = tmp_path / "generated"
    shutil.copytree(cfg.artifacts_dir, artifact_copy)
    shutil.copytree(cfg.generated_dir, generated_copy)
    raw = copy.deepcopy(cfg.raw)
    raw["paths"] = {
        "generated_data": str(generated_copy),
        "artifacts": str(artifact_copy),
        "database": str(artifact_copy / "fleet_planning.sqlite"),
    }
    config_path = tmp_path / "generated-tamper.yaml"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    history = generated_copy / "station_day_history.csv"
    history.write_text(history.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with TestClient(create_app(config_path)) as client:
        ready = client.get("/health/ready")
    assert ready.status_code == 503
    assert "generated-data" in ready.json()["detail"].lower()


def test_readiness_rejects_manifest_run_id_mismatch(cfg, pipeline_summary, tmp_path):
    import json

    artifact_copy = tmp_path / "artifacts"
    generated_copy = tmp_path / "generated"
    shutil.copytree(cfg.artifacts_dir, artifact_copy)
    shutil.copytree(cfg.generated_dir, generated_copy)
    raw = copy.deepcopy(cfg.raw)
    raw["paths"] = {
        "generated_data": str(generated_copy),
        "artifacts": str(artifact_copy),
        "database": str(artifact_copy / "fleet_planning.sqlite"),
    }
    config_path = tmp_path / "run-id-mismatch.yaml"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    manifest_path = artifact_copy / "artifact_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["run_id"] = "different-run"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    with TestClient(create_app(config_path)) as client:
        ready = client.get("/health/ready")
    assert ready.status_code == 503
    assert "run_id" in ready.json()["detail"]


def test_readiness_rejects_truthy_string_in_boolean_artifact(cfg, pipeline_summary, tmp_path):
    import json

    from ev_fleet.manifest import build_artifact_manifest

    artifact_copy = tmp_path / "artifacts"
    generated_copy = tmp_path / "generated"
    shutil.copytree(cfg.artifacts_dir, artifact_copy)
    shutil.copytree(cfg.generated_dir, generated_copy)
    raw = copy.deepcopy(cfg.raw)
    raw["paths"] = {
        "generated_data": str(generated_copy),
        "artifacts": str(artifact_copy),
        "database": str(artifact_copy / "fleet_planning.sqlite"),
    }
    config_path = tmp_path / "truthy-string.yaml"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    summary_path = artifact_copy / "run_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["automatic_release_allowed"] = "false"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    build_artifact_manifest(artifact_copy)

    with TestClient(create_app(config_path)) as client:
        ready = client.get("/health/ready")
    assert ready.status_code == 503
    assert "json boolean" in ready.json()["detail"].lower()


def test_plan_read_rejects_run_change_during_request(cfg, pipeline_summary, tmp_path, monkeypatch):
    import json
    from pathlib import Path

    artifact_copy = tmp_path / "artifacts"
    generated_copy = tmp_path / "generated"
    shutil.copytree(cfg.artifacts_dir, artifact_copy)
    shutil.copytree(cfg.generated_dir, generated_copy)
    raw = copy.deepcopy(cfg.raw)
    raw["paths"] = {
        "generated_data": str(generated_copy),
        "artifacts": str(artifact_copy),
        "database": str(artifact_copy / "fleet_planning.sqlite"),
    }
    config_path = tmp_path / "run-change.yaml"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    summary_path = (artifact_copy / "run_summary.json").resolve()
    plan_path = (artifact_copy / "recommended_plan.json").resolve()
    original_read_text = Path.read_text
    state = {"plan_read": False}

    def changing_read_text(path, *args, **kwargs):
        resolved = path.resolve()
        content = original_read_text(path, *args, **kwargs)
        if resolved == plan_path:
            state["plan_read"] = True
        elif resolved == summary_path and state["plan_read"]:
            payload = json.loads(content)
            payload["run_id"] = "replacement-run"
            return json.dumps(payload)
        return content

    monkeypatch.setattr(Path, "read_text", changing_read_text)
    with TestClient(create_app(config_path)) as client:
        response = client.get("/plans/latest")
    assert response.status_code == 503
    assert "changed during request" in response.json()["detail"].lower()


def test_plan_read_uses_one_config_snapshot(cfg, pipeline_summary, monkeypatch):
    import ev_fleet.api as api_module

    calls = {"count": 0}
    real_load = api_module.load_config

    def one_shot_load(path):
        calls["count"] += 1
        if calls["count"] > 1:
            raise AssertionError("Config was reloaded during one request")
        return real_load(path)

    monkeypatch.setattr(api_module, "load_config", one_shot_load)
    with TestClient(api_module.create_app(cfg.source_path)) as client:
        response = client.get("/plans/latest")
    assert response.status_code == 200
    assert calls["count"] == 1
