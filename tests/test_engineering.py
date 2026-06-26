from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from ev_fleet.api import create_app
from ev_fleet.config import ProjectConfig, load_config
from ev_fleet.maintenance import (
    _promote_runtime_directories,
    clean_runtime,
    pipeline_run_lock,
    recover_incomplete_promotions,
)
from ev_fleet.manifest import (
    build_artifact_manifest,
    build_generated_data_manifest,
    verify_artifact_manifest,
    verify_generated_data_manifest,
)
from ev_fleet.optimization import heuristic_fallback
from ev_fleet.pipeline import run_pipeline
from ev_fleet.reliability import evaluate_rcce
from ev_fleet.simulation import simulate_plan
from ev_fleet.sql_pipeline import read_mart
from ev_fleet.validation import validate_cross_artifact_contracts


def _reject_constant(value: str):
    raise ValueError(f"Non-standard JSON constant: {value}")


def test_load_config_has_no_runtime_directory_side_effects(cfg, tmp_path):
    raw = copy.deepcopy(cfg.raw)
    raw["project_root"] = str(tmp_path)
    raw["paths"]["generated_data"] = "runtime/generated"
    raw["paths"]["artifacts"] = "runtime/artifacts"
    raw["paths"]["database"] = "runtime/artifacts/fleet.sqlite"
    raw["paths"]["public_calibration"] = "data/public_calibration"
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    path = config_dir / "side-effect-free.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    loaded = load_config(path)
    assert loaded.project_root == tmp_path.resolve()
    assert not loaded.generated_dir.exists()
    assert not loaded.artifacts_dir.exists()


def test_config_paths_are_independent_of_current_working_directory(cfg, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    loaded = load_config(cfg.source_path)
    assert loaded.generated_dir == cfg.generated_dir
    assert loaded.artifacts_dir == cfg.artifacts_dir
    assert loaded.database_path == cfg.database_path


def test_artifact_manifest_hashes(cfg, pipeline_summary):
    run_id = pipeline_summary["run_id"]
    verified = verify_artifact_manifest(cfg.artifacts_dir, expected_run_id=run_id)
    assert "run_summary.json" in verified
    assert "reports/executive_decision_memo.md" in verified
    artifact_manifest = json.loads((cfg.artifacts_dir / "artifact_manifest.json").read_text(encoding="utf-8"))
    generated_manifest = json.loads((cfg.artifacts_dir / "generated_data_manifest.json").read_text(encoding="utf-8"))
    assert artifact_manifest["run_id"] == run_id
    assert generated_manifest["run_id"] == run_id


def test_manifest_rejects_path_traversal(tmp_path):
    manifest = {
        "schema_version": 1,
        "files": [
            {
                "path": "../outside.txt",
                "size_bytes": 0,
                "sha256": "0" * 64,
            }
        ],
    }
    (tmp_path / "artifact_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="Unsafe manifest path"):
        verify_artifact_manifest(tmp_path)


def test_manifest_rejects_untracked_artifact_and_generated_files(cfg, pipeline_summary):
    extra_artifact = cfg.artifacts_dir / "untracked.tmp"
    extra_generated = cfg.generated_dir / "untracked.csv"
    extra_artifact.write_text("unexpected", encoding="utf-8")
    try:
        with pytest.raises(ValueError, match="Untracked artifact files"):
            verify_artifact_manifest(cfg.artifacts_dir, expected_run_id=pipeline_summary["run_id"])
    finally:
        extra_artifact.unlink(missing_ok=True)

    extra_generated.write_text("unexpected", encoding="utf-8")
    try:
        with pytest.raises(ValueError, match="Untracked generated-data files"):
            verify_generated_data_manifest(
                cfg.generated_dir,
                cfg.artifacts_dir / "generated_data_manifest.json",
                expected_run_id=pipeline_summary["run_id"],
            )
    finally:
        extra_generated.unlink(missing_ok=True)


def test_manifest_requires_supported_schema_and_rejects_symlinks(tmp_path):
    artifact_root = tmp_path / "artifacts"
    generated_root = tmp_path / "generated"
    artifact_root.mkdir()
    generated_root.mkdir()
    (artifact_root / "run_summary.json").write_text('{"run_id": "run"}\n', encoding="utf-8")
    (artifact_root / "payload.json").write_text("{}\n", encoding="utf-8")
    (generated_root / "data.csv").write_text("x\n1\n", encoding="utf-8")
    build_generated_data_manifest(generated_root, artifact_root / "generated_data_manifest.json", run_id="run")
    build_artifact_manifest(artifact_root, run_id="run")

    manifest_path = artifact_root / "artifact_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 999
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version"):
        verify_artifact_manifest(artifact_root, expected_run_id="run")

    target = tmp_path / "outside.txt"
    target.write_text("outside", encoding="utf-8")
    symlink = artifact_root / "outside-link.txt"
    try:
        symlink.symlink_to(target)
    except OSError:
        pytest.skip("Symlink creation is unavailable on this platform")
    with pytest.raises(ValueError, match="Symlinks are not allowed"):
        build_artifact_manifest(artifact_root, run_id="run")


def test_runtime_paths_under_data_are_limited_to_generated_directories(cfg):
    original = copy.deepcopy(cfg.raw["paths"])
    try:
        cfg.raw["paths"]["artifacts"] = "data/artifacts"
        cfg.raw["paths"]["database"] = "data/artifacts/fleet.sqlite"
        with pytest.raises(ValueError, match="Only explicitly generated-data directories"):
            run_pipeline(cfg)

        cfg.raw["paths"] = copy.deepcopy(original)
        cfg.raw["paths"]["generated_data"] = "data/raw-runtime"
        with pytest.raises(ValueError, match="Only explicitly generated-data directories"):
            run_pipeline(cfg)
    finally:
        cfg.raw["paths"] = original


def test_json_artifacts_are_strict_json(cfg, pipeline_summary):
    for path in cfg.artifacts_dir.rglob("*.json"):
        json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)


def test_sql_mart_allowlist_blocks_injection(cfg, pipeline_summary):
    with pytest.raises(ValueError, match="Unsupported mart"):
        read_mart(cfg, "mart_station_capacity; DROP TABLE stg_stations;")


def test_degraded_fallback_cannot_auto_release(cfg, scenarios, pipeline_summary):
    fallback = heuristic_fallback(cfg, scenarios, reason="test fallback")
    old_runs = cfg.raw["simulation"]["runs"]
    cfg.raw["simulation"]["runs"] = 12
    try:
        replay = simulate_plan(cfg, fallback, scenarios, seed_offset=991)
    finally:
        cfg.raw["simulation"]["runs"] = old_runs
    decision = evaluate_rcce(cfg, fallback, replay)
    assert decision.status != "RELEASE"
    assert not decision.checks["solver_not_degraded"]


def test_override_endpoint_replays_and_rejects_coercion(cfg, pipeline_summary):
    app = create_app(cfg.source_path)
    plan = json.loads((cfg.artifacts_dir / "recommended_plan.json").read_text(encoding="utf-8"))
    row = plan["allocation"][0]
    payload = {
        "station_id": row["station_id"],
        "vehicle_type": row["vehicle_type"],
        "delta": 1,
        "reason": "stress-test override",
    }
    with TestClient(app) as client:
        response = client.post("/plans/latest/simulate-override", json=payload)
        strict_response = client.post(
            "/plans/latest/simulate-override",
            json={**payload, "delta": "1", "unexpected": True},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"REVIEW", "BLOCK"}
    assert body["approval_required"] is True
    assert "reliability" in body
    assert body["reliability"]["checks"]["solver_feasible"] is True
    assert body["reliability"]["checks"]["manual_override_approval"] is False
    assert body["proposed_plan"]["solver"]["certificate_scope"] == "base_plan_only"
    assert strict_response.status_code == 422


def test_override_endpoint_can_remove_only_existing_temporary_capacity(cfg, pipeline_summary):
    app = create_app(cfg.source_path)
    plan = json.loads((cfg.artifacts_dir / "recommended_plan.json").read_text(encoding="utf-8"))
    row = next(item for item in plan["allocation"] if int(item.get("temporary_addition", 0)) > 0)
    payload = {
        "station_id": row["station_id"],
        "vehicle_type": row["vehicle_type"],
        "delta": -1,
        "reason": "remove one temporary lease",
    }
    with TestClient(app) as client:
        response = client.post("/plans/latest/simulate-override", json=payload)
        invalid = client.post(
            "/plans/latest/simulate-override",
            json={**payload, "delta": -(int(row["temporary_addition"]) + 1)},
        )
    assert response.status_code == 200
    body = response.json()
    proposed_row = next(
        item
        for item in body["proposed_plan"]["allocation"]
        if item["station_id"] == row["station_id"] and item["vehicle_type"] == row["vehicle_type"]
    )
    assert proposed_row["temporary_addition"] == int(row["temporary_addition"]) - 1
    assert body["proposed_plan"]["solver"]["override"]["objective_delta"] < 0
    assert invalid.status_code == 422
    assert "temporary additions" in invalid.json()["detail"]


def test_config_rejects_invalid_probability_and_unsorted_quantiles(cfg, tmp_path):
    payload = yaml.safe_load(cfg.source_path.read_text(encoding="utf-8"))
    payload["simulation"]["emergency_lease_fulfillment_probability"] = 1.2
    invalid_probability = tmp_path / "invalid_probability.yaml"
    invalid_probability.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="emergency_lease_fulfillment_probability"):
        load_config(invalid_probability)

    payload = yaml.safe_load(cfg.source_path.read_text(encoding="utf-8"))
    payload["reliability"]["corrective_shortage_quantiles"] = [0.90, 0.75]
    invalid_quantiles = tmp_path / "invalid_quantiles.yaml"
    invalid_quantiles.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unique and increasing"):
        load_config(invalid_quantiles)


def test_override_endpoint_rejects_parking_capacity_violation(cfg, pipeline_summary):
    app = create_app(cfg.source_path)
    plan = json.loads((cfg.artifacts_dir / "recommended_plan.json").read_text(encoding="utf-8"))
    row = plan["allocation"][0]
    payload = {
        "station_id": row["station_id"],
        "vehicle_type": row["vehicle_type"],
        "delta": 100,
        "reason": "capacity-boundary test",
    }
    with TestClient(app) as client:
        response = client.post("/plans/latest/simulate-override", json=payload)
    assert response.status_code == 422
    assert "parking capacity" in response.json()["detail"]


def test_run_summary_contains_environment_and_config_hash(cfg, pipeline_summary):
    summary = json.loads((cfg.artifacts_dir / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["environment"]["python"]
    assert len(summary["environment"]["config_sha256"]) == 64
    assert summary["environment"]["packages"]["scipy"] != "not-installed"


def test_dashboard_rejects_tampered_artifact(cfg, pipeline_summary, tmp_path):
    import shutil

    from ev_fleet.dashboard_data import load_dashboard_payload

    artifact_copy = tmp_path / "artifacts"
    shutil.copytree(cfg.artifacts_dir, artifact_copy)
    plan_path = artifact_copy / "recommended_plan.json"
    plan_path.write_text(plan_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="Manifest size mismatch|Manifest hash mismatch"):
        load_dashboard_payload(artifact_copy)


def test_reference_export_rejects_semantically_inconsistent_rehashed_run(cfg, pipeline_summary, tmp_path):
    import shutil

    from ev_fleet.reference_results import export_reference_results

    generated_copy = tmp_path / "generated"
    artifact_copy = tmp_path / "artifacts"
    shutil.copytree(cfg.generated_dir, generated_copy)
    shutil.copytree(cfg.artifacts_dir, artifact_copy)
    raw = copy.deepcopy(cfg.raw)
    raw["paths"] = {
        "generated_data": str(generated_copy),
        "artifacts": str(artifact_copy),
        "database": str(artifact_copy / "fleet_planning.sqlite"),
    }
    isolated = ProjectConfig(raw=raw, source_path=cfg.source_path, project_root=tmp_path)
    summary_path = artifact_copy / "run_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["selected_forecast_model"] = "semantically-wrong-model"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    build_artifact_manifest(artifact_copy, run_id=summary["run_id"])

    with pytest.raises(ValueError, match="Selected forecast model"):
        export_reference_results(isolated, output_root=tmp_path / "references", profile_name="invalid")


def test_dashboard_rejects_semantically_inconsistent_rehashed_artifacts(cfg, pipeline_summary, tmp_path):
    import shutil

    from ev_fleet.dashboard_data import load_dashboard_payload

    artifact_copy = tmp_path / "artifacts"
    shutil.copytree(cfg.artifacts_dir, artifact_copy)
    summary_path = artifact_copy / "run_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["selected_base_policy"] = "risk_aware"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    build_artifact_manifest(artifact_copy, run_id=summary["run_id"])

    with pytest.raises(ValueError, match="Selected base policy"):
        load_dashboard_payload(artifact_copy)


def test_dashboard_payload_matches_current_artifact_contract(cfg, pipeline_summary):
    from ev_fleet.dashboard_data import load_dashboard_payload

    payload = load_dashboard_payload(cfg.artifacts_dir)
    assert payload["metrics"]["base_policy"] == pipeline_summary["selected_base_policy"]
    assert payload["metrics"]["formal_sample_vss"] == pipeline_summary["formal_sample_vss"]
    assert payload["metrics"]["formal_sample_evpi"] == pipeline_summary["formal_sample_evpi"]
    assert payload["metrics"]["recommended_rcce"] == pipeline_summary["recommended_rcce"]


def test_package_version_is_single_sourced_and_source_tree_safe():
    import ev_fleet
    from ev_fleet._version import __version__ as source_version

    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["project"]["dynamic"] == ["version"]
    assert metadata["tool"]["setuptools"]["dynamic"]["version"]["attr"] == "ev_fleet._version.__version__"
    assert ev_fleet.__version__ == source_version
    try:
        installed_version = version("decision-aware-ev-fleet-planning")
    except PackageNotFoundError:
        installed_version = None
    if installed_version is not None:
        assert installed_version == source_version


def test_environment_metadata_uses_source_version_even_if_distribution_metadata_is_stale(cfg, monkeypatch):
    import ev_fleet.pipeline as pipeline_module
    from ev_fleet import __version__

    monkeypatch.setattr(
        pipeline_module,
        "_package_version",
        lambda name: "stale-installed-version" if name == "decision-aware-ev-fleet-planning" else "test-version",
    )
    metadata = pipeline_module._environment_metadata(cfg)
    assert metadata["packages"]["decision-aware-ev-fleet-planning"] == __version__
    assert metadata["packages"]["installed-distribution-version"] == "stale-installed-version"


def test_all_execution_profiles_validate():
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    for name in ("smoke.yaml", "medium.yaml", "gpu-smoke.yaml", "full.yaml"):
        loaded = load_config(root / "configs" / name)
        assert loaded.raw["mode"] == name.removesuffix(".yaml")

    smoke = load_config(root / "configs" / "smoke.yaml")
    full = load_config(root / "configs" / "full.yaml")
    assert smoke.generated_dir != full.generated_dir
    assert smoke.artifacts_dir != full.artifacts_dir


def test_config_rejects_unattainable_wilson_reliability_gate(cfg, tmp_path):
    raw = copy.deepcopy(cfg.raw)
    raw["simulation"]["runs"] = 20
    raw["reliability"]["energy_feasibility_lcb"] = 0.90
    path = tmp_path / "unattainable.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="unattainable"):
        load_config(path)


def test_config_rejects_overlapping_runtime_paths(cfg, tmp_path):
    raw = copy.deepcopy(cfg.raw)
    raw["paths"]["generated_data"] = "runtime"
    raw["paths"]["artifacts"] = "runtime/artifacts"
    raw["paths"]["database"] = "runtime/artifacts/fleet.sqlite"
    raw["paths"]["public_calibration"] = "public-calibration"
    path = tmp_path / "overlap.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="separate, non-nested"):
        load_config(path)

    raw = copy.deepcopy(cfg.raw)
    raw["paths"]["generated_data"] = "generated"
    raw["paths"]["artifacts"] = "artifacts"
    raw["paths"]["database"] = "database-outside.sqlite"
    raw["paths"]["public_calibration"] = "public-calibration"
    path = tmp_path / "database-outside.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="inside paths.artifacts"):
        load_config(path)


def test_reference_result_export_is_compact_and_valid(cfg, pipeline_summary, tmp_path):
    from ev_fleet.reference_results import REFERENCE_FILES, export_reference_results

    result = export_reference_results(cfg, output_root=tmp_path, profile_name="smoke-test")
    target = tmp_path / "smoke-test"
    assert result["status"] == "PASS"
    assert result["files"] == len(REFERENCE_FILES)
    assert (target / "reference_manifest.json").is_file()
    assert (target / "reports" / "executive_decision_memo.md").is_file()
    for path in target.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".json", ".md", ".csv", ".txt"}:
            assert str(cfg.project_root) not in path.read_text(encoding="utf-8")


def test_reference_export_rejects_runtime_and_ancestor_targets(cfg, pipeline_summary):
    from ev_fleet.reference_results import export_reference_results

    with pytest.raises(ValueError, match="protected runtime path"):
        export_reference_results(cfg, output_root=cfg.project_root, profile_name=cfg.artifacts_dir.name)
    with pytest.raises(ValueError, match="protected runtime path"):
        export_reference_results(cfg, output_root=cfg.project_root, profile_name="data")


def test_reference_result_export_failure_preserves_previous_snapshot(cfg, pipeline_summary, tmp_path, monkeypatch):
    import ev_fleet.reference_results as reference_module

    reference_module.export_reference_results(cfg, output_root=tmp_path, profile_name="stable")
    target = tmp_path / "stable"
    marker = target / "last-known-good.txt"
    marker.write_text("preserve-me", encoding="utf-8")
    original_replace = reference_module.os.replace
    failed = False

    def fail_new_snapshot(source, destination):
        nonlocal failed
        source_path = Path(source)
        destination_path = Path(destination)
        if source_path.name.startswith(".stable-") and "backup" not in source_path.name and not failed:
            failed = True
            raise OSError("synthetic reference promotion failure")
        return original_replace(source_path, destination_path)

    monkeypatch.setattr(reference_module.os, "replace", fail_new_snapshot)
    with pytest.raises(OSError, match="synthetic reference promotion failure"):
        reference_module.export_reference_results(cfg, output_root=tmp_path, profile_name="stable")

    assert marker.read_text(encoding="utf-8") == "preserve-me"
    assert not list(tmp_path.glob(".stable-backup-*"))


def test_cross_artifact_contracts(cfg, pipeline_summary):
    result = validate_cross_artifact_contracts(cfg)
    assert result["selected_forecast_model"] == pipeline_summary["selected_forecast_model"]
    assert result["recommended_rcce"] == pipeline_summary["recommended_rcce"]


def test_semantic_hash_ignores_solver_runtime_but_detects_scientific_change(cfg, pipeline_summary, tmp_path):
    import shutil

    from ev_fleet.validation import semantic_artifact_hashes

    artifacts = tmp_path / "artifacts"
    shutil.copytree(cfg.artifacts_dir, artifacts)
    baseline = semantic_artifact_hashes(artifacts)

    plan_path = artifacts / "deterministic_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["solver"]["runtime_seconds"] = 9999.0
    plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    runtime_changed = semantic_artifact_hashes(artifacts)
    assert runtime_changed == baseline

    plan["allocation"][0]["planned_count"] += 1
    plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    scientific_change = semantic_artifact_hashes(artifacts)
    assert scientific_change["deterministic_plan.json"] != baseline["deterministic_plan.json"]


def test_clean_runtime_refuses_project_root(cfg):
    original = cfg.raw["paths"]["artifacts"]
    cfg.raw["paths"]["artifacts"] = str(cfg.project_root)
    try:
        with pytest.raises(ValueError, match="project root"):
            clean_runtime(cfg)
    finally:
        cfg.raw["paths"]["artifacts"] = original


def test_pipeline_and_clean_reject_source_controlled_runtime_paths(cfg):
    original_artifacts = cfg.raw["paths"]["artifacts"]
    original_database = cfg.raw["paths"]["database"]
    cfg.raw["paths"]["artifacts"] = "src/runtime-output"
    cfg.raw["paths"]["database"] = "src/runtime-output/fleet.sqlite"
    try:
        with pytest.raises(ValueError, match="protected source/evidence path"):
            run_pipeline(cfg)
        with pytest.raises(ValueError, match="protected source/evidence path"):
            clean_runtime(cfg)
    finally:
        cfg.raw["paths"]["artifacts"] = original_artifacts
        cfg.raw["paths"]["database"] = original_database


def test_pipeline_refuses_project_root_as_artifact_directory(cfg):
    original = cfg.raw["paths"]["artifacts"]
    cfg.raw["paths"]["artifacts"] = str(cfg.project_root)
    try:
        with pytest.raises(ValueError, match="project root"):
            run_pipeline(cfg)
    finally:
        cfg.raw["paths"]["artifacts"] = original


def test_qualify_local_skip_e2e_keeps_version_check():
    script = Path("scripts/qualify_local.py").read_text(encoding="utf-8")
    assert '[sys.executable, "-m", "ev_fleet", "--version"]' in script
    assert "commands = commands[:6]" in script


def test_cli_version_reports_package_version(capsys):
    from ev_fleet import __version__
    from ev_fleet.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_cli_reports_operational_errors_without_traceback(monkeypatch, capsys):
    import ev_fleet.cli as cli_module

    def fail_run(cfg):
        raise RuntimeError("synthetic lock collision")

    monkeypatch.setattr(cli_module, "run_pipeline", fail_run)
    assert cli_module.main(["run", "--config", "configs/smoke.yaml"]) == 1
    captured = capsys.readouterr()
    assert "synthetic lock collision" in captured.err
    assert "Traceback" not in captured.err


def test_train_gpu_cli_uses_transactional_pipeline(cfg, monkeypatch, capsys):
    import ev_fleet.cli as cli_module

    cfg.raw["forecast"]["use_torch_challenger"] = True
    monkeypatch.setattr(cli_module, "load_config", lambda path: cfg)
    monkeypatch.setattr(
        cli_module,
        "run_pipeline",
        lambda loaded: {
            "pipeline_status": "PASS",
            "selected_forecast_model": "seasonal_naive",
            "gpu_challenger": {"status": "PASS", "promoted_to_pipeline_champion": False},
            "artifacts_dir": str(loaded.artifacts_dir),
        },
    )

    assert cli_module.main(["train-gpu", "--config", str(cfg.source_path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["pipeline_status"] == "PASS"
    assert output["gpu_challenger"]["status"] == "PASS"


def test_pipeline_lock_blocks_overlapping_writer(cfg):
    with pipeline_run_lock(cfg):
        with pytest.raises(RuntimeError, match="holds lock"):
            with pipeline_run_lock(cfg):
                pass


def test_config_rejects_nonfinite_values_and_external_runtime_paths(cfg, tmp_path):
    raw = copy.deepcopy(cfg.raw)
    raw["optimization"]["risk_weight"] = float("nan")
    path = tmp_path / "nan.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="must be finite"):
        load_config(path)

    raw = copy.deepcopy(cfg.raw)
    raw["project_root"] = str(cfg.project_root)
    raw["paths"]["generated_data"] = str(tmp_path / "outside-generated")
    raw["paths"]["artifacts"] = str(tmp_path / "outside-artifacts")
    raw["paths"]["database"] = str(tmp_path / "outside-artifacts" / "fleet.sqlite")
    path = tmp_path / "external-runtime.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="inside project_root"):
        load_config(path)


def test_pipeline_lock_serializes_different_profiles(cfg):
    medium_cfg = load_config(cfg.project_root / "configs" / "medium.yaml")
    assert medium_cfg.artifacts_dir != cfg.artifacts_dir
    with pipeline_run_lock(cfg):
        with pytest.raises(RuntimeError, match="holds lock"):
            with pipeline_run_lock(medium_cfg):
                pass


def test_pipeline_lock_is_released_after_exception(cfg):
    with pytest.raises(RuntimeError, match="intentional"):
        with pipeline_run_lock(cfg):
            raise RuntimeError("intentional")
    with pipeline_run_lock(cfg):
        pass


def test_pipeline_lock_is_released_after_process_crash(cfg):
    script = f"""
import os
from ev_fleet.config import load_config
from ev_fleet.maintenance import pipeline_run_lock
cfg = load_config(r'{cfg.source_path}')
with pipeline_run_lock(cfg):
    os._exit(17)
"""
    environment = os.environ.copy()
    source_root = str(cfg.project_root / "src")
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_root if not existing_pythonpath else source_root + os.pathsep + existing_pythonpath
    )
    result = subprocess.run([sys.executable, "-c", script], check=False, env=environment)
    assert result.returncode == 17
    with pipeline_run_lock(cfg):
        pass


def test_failed_staged_pipeline_preserves_last_known_good(cfg, pipeline_summary, monkeypatch):
    import hashlib

    import ev_fleet.pipeline as pipeline_module

    summary_path = cfg.artifacts_dir / "run_summary.json"
    before = hashlib.sha256(summary_path.read_bytes()).hexdigest()

    def fail_after_partial_write(staged_cfg):
        (staged_cfg.artifacts_dir / "partial.txt").write_text("partial", encoding="utf-8")
        raise RuntimeError("synthetic staged failure")

    monkeypatch.setattr(pipeline_module, "_run_pipeline_unlocked", fail_after_partial_write)
    with pytest.raises(RuntimeError, match="synthetic staged failure"):
        pipeline_module.run_pipeline(cfg)

    after = hashlib.sha256(summary_path.read_bytes()).hexdigest()
    assert after == before
    assert not (cfg.artifacts_dir / "partial.txt").exists()
    assert not any((cfg.project_root / ".ev_fleet_staging").glob("*"))


def test_staged_path_rewrite_handles_json_escaped_unicode_paths(tmp_path, cfg):
    from ev_fleet.config import ProjectConfig
    from ev_fleet.maintenance import _rewrite_staged_text_files

    project_root = tmp_path / "프로젝트 root"
    staged_root = project_root / ".ev_fleet_staging" / "abc123"
    staged_artifacts = staged_root / "artifacts"
    staged_generated = staged_root / "generated"
    final_artifacts = project_root / "artifacts"
    final_generated = project_root / "data" / "generated"
    staged_artifacts.mkdir(parents=True)
    staged_generated.mkdir(parents=True)

    raw = dict(cfg.raw)
    raw["paths"] = dict(cfg.raw["paths"])
    staged_raw = dict(raw)
    staged_raw["paths"] = dict(raw["paths"])
    staged_raw["paths"]["artifacts"] = str(staged_artifacts)
    staged_raw["paths"]["generated_data"] = str(staged_generated)
    staged_raw["paths"]["database"] = str(staged_artifacts / "fleet.sqlite")
    final_raw = dict(raw)
    final_raw["paths"] = dict(raw["paths"])
    final_raw["paths"]["artifacts"] = str(final_artifacts)
    final_raw["paths"]["generated_data"] = str(final_generated)
    final_raw["paths"]["database"] = str(final_artifacts / "fleet.sqlite")

    staged_cfg = ProjectConfig(raw=staged_raw, source_path=cfg.source_path, project_root=project_root)
    final_cfg = ProjectConfig(raw=final_raw, source_path=cfg.source_path, project_root=project_root)
    payload_path = staged_artifacts / "run_summary.json"
    payload_path.write_text(
        json.dumps({"artifacts_dir": str(staged_artifacts), "generated_dir": str(staged_generated)}, indent=2),
        encoding="utf-8",
    )

    _rewrite_staged_text_files(staged_cfg, final_cfg)
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["artifacts_dir"] == str(final_artifacts)
    assert payload["generated_dir"] == str(final_generated)
    assert ".ev_fleet_staging" not in payload_path.read_text(encoding="utf-8")


def test_promoted_run_contains_final_paths_and_generated_manifest(cfg, pipeline_summary):
    from ev_fleet.manifest import verify_generated_data_manifest

    summary = json.loads((cfg.artifacts_dir / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["artifacts_dir"] == str(cfg.artifacts_dir)
    assert ".ev_fleet_staging" not in json.dumps(summary)
    verified = verify_generated_data_manifest(cfg.generated_dir, cfg.artifacts_dir / "generated_data_manifest.json")
    assert "station_day_history.csv" in verified


def test_config_rejects_string_booleans_fractional_counts_and_nonfinite_quantiles(cfg, tmp_path):
    cases = []

    raw = copy.deepcopy(cfg.raw)
    raw["forecast"]["use_torch_challenger"] = "false"
    cases.append((raw, "YAML boolean"))

    raw = copy.deepcopy(cfg.raw)
    raw["forecast"]["test_days"] = 14.5
    cases.append((raw, "must be an integer"))

    raw = copy.deepcopy(cfg.raw)
    raw["reliability"]["corrective_shortage_quantiles"] = [0.75, float("nan")]
    cases.append((raw, "must be finite"))

    raw = copy.deepcopy(cfg.raw)
    raw["seed"] = -1
    cases.append((raw, "must be >= 0"))

    raw = copy.deepcopy(cfg.raw)
    raw["history"]["days"] = True
    cases.append((raw, "must be numeric, not boolean"))

    raw = copy.deepcopy(cfg.raw)
    raw["optimization"]["risk_weight"] = None
    cases.append((raw, "must be numeric"))

    raw = copy.deepcopy(cfg.raw)
    raw["saa"]["scenario_sizes"] = [2, True]
    cases.append((raw, "not booleans"))

    raw = copy.deepcopy(cfg.raw)
    raw["reliability"]["max_solver_constraint_violation"] = -1.0
    cases.append((raw, "must be >= 0"))

    raw = copy.deepcopy(cfg.raw)
    raw["forecast"]["calibration_days"] = raw["history"]["days"]
    cases.append((raw, r"calibration_days \+ forecast.test_days"))

    for index, (payload, message) in enumerate(cases):
        path = tmp_path / f"invalid-{index}.yaml"
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            load_config(path)


def test_clean_runtime_obeys_pipeline_lock(cfg, pipeline_summary):
    with pipeline_run_lock(cfg):
        with pytest.raises(RuntimeError, match="holds lock"):
            clean_runtime(cfg)


def test_atomic_json_write_cleans_temporary_file_on_replace_failure(tmp_path, monkeypatch):
    import ev_fleet.io_utils as io_module

    target = tmp_path / "state.json"
    target.write_text('{"status": "old"}\n', encoding="utf-8")

    def fail_replace(source, destination):
        raise OSError("synthetic atomic replace failure")

    monkeypatch.setattr(io_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic atomic replace failure"):
        io_module.write_json_atomic(target, {"status": "new"})

    assert target.read_text(encoding="utf-8") == '{"status": "old"}\n'
    assert not list(tmp_path.glob("*.tmp"))


def test_recovery_removes_empty_pre_journal_backup_directory(tmp_path):
    root = tmp_path / "project"
    abandoned = root / ".ev_fleet_backups" / "pre-journal-crash"
    abandoned.mkdir(parents=True)

    recovered = recover_incomplete_promotions(root)
    assert recovered == ["pre-journal-crash"]
    assert not abandoned.exists()


def test_recovery_preserves_backup_with_unsupported_journal_version(tmp_path):
    root = tmp_path / "project"
    backup_root = root / ".ev_fleet_backups" / "future-version"
    (backup_root / "generated").mkdir(parents=True)
    (backup_root / "promotion.json").write_text(json.dumps({"version": 999, "pairs": []}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="no valid recovery journal"):
        recover_incomplete_promotions(root)
    assert backup_root.exists()


def test_interrupted_promotion_recovery_restores_last_known_good(tmp_path):
    root = tmp_path / "project"
    generated = root / "data" / "generated"
    artifacts = root / "artifacts"
    generated.mkdir(parents=True)
    artifacts.mkdir(parents=True)
    (generated / "state.txt").write_text("new-generated", encoding="utf-8")
    (artifacts / "state.txt").write_text("new-artifacts", encoding="utf-8")

    backup_root = root / ".ev_fleet_backups" / "crashed-token"
    backup_generated = backup_root / "generated"
    backup_artifacts = backup_root / "artifacts"
    backup_generated.mkdir(parents=True)
    backup_artifacts.mkdir(parents=True)
    (backup_generated / "state.txt").write_text("old-generated", encoding="utf-8")
    (backup_artifacts / "state.txt").write_text("old-artifacts", encoding="utf-8")
    (backup_root / "promotion.json").write_text(
        json.dumps(
            {
                "version": 1,
                "pairs": [
                    {
                        "final": "data/generated",
                        "backup": ".ev_fleet_backups/crashed-token/generated",
                        "had_final": True,
                    },
                    {
                        "final": "artifacts",
                        "backup": ".ev_fleet_backups/crashed-token/artifacts",
                        "had_final": True,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    recovered = recover_incomplete_promotions(root)
    assert recovered == ["crashed-token"]
    assert (generated / "state.txt").read_text(encoding="utf-8") == "old-generated"
    assert (artifacts / "state.txt").read_text(encoding="utf-8") == "old-artifacts"
    assert not backup_root.exists()


def test_interrupted_promotion_recovery_accepts_untouched_later_final(tmp_path):
    root = tmp_path / "project"
    generated = root / "data" / "generated"
    artifacts = root / "artifacts"
    generated.mkdir(parents=True)
    artifacts.mkdir(parents=True)
    (generated / "state.txt").write_text("new-generated", encoding="utf-8")
    (artifacts / "state.txt").write_text("old-artifacts", encoding="utf-8")

    backup_root = root / ".ev_fleet_backups" / "partial-token"
    backup_generated = backup_root / "generated"
    backup_generated.mkdir(parents=True)
    (backup_generated / "state.txt").write_text("old-generated", encoding="utf-8")
    (backup_root / "promotion.json").write_text(
        json.dumps(
            {
                "version": 1,
                "pairs": [
                    {
                        "final": "data/generated",
                        "backup": ".ev_fleet_backups/partial-token/generated",
                        "had_final": True,
                    },
                    {
                        "final": "artifacts",
                        "backup": ".ev_fleet_backups/partial-token/artifacts",
                        "had_final": True,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    recovered = recover_incomplete_promotions(root)
    assert recovered == ["partial-token"]
    assert (generated / "state.txt").read_text(encoding="utf-8") == "old-generated"
    assert (artifacts / "state.txt").read_text(encoding="utf-8") == "old-artifacts"
    assert not backup_root.exists()


def test_promotion_failure_before_first_backup_keeps_untouched_finals(tmp_path, monkeypatch):
    root = tmp_path / "project"
    final_generated = root / "data" / "generated"
    final_artifacts = root / "artifacts"
    staged_generated = root / "staging" / "generated"
    staged_artifacts = root / "staging" / "artifacts"
    for directory, value in (
        (final_generated, "old-generated"),
        (final_artifacts, "old-artifacts"),
        (staged_generated, "new-generated"),
        (staged_artifacts, "new-artifacts"),
    ):
        directory.mkdir(parents=True)
        (directory / "state.txt").write_text(value, encoding="utf-8")

    final_cfg = ProjectConfig(
        {"paths": {"generated_data": str(final_generated), "artifacts": str(final_artifacts)}},
        root / "config.yaml",
        root,
    )
    staged_cfg = ProjectConfig(
        {"paths": {"generated_data": str(staged_generated), "artifacts": str(staged_artifacts)}},
        root / "config.yaml",
        root,
    )
    original_replace = os.replace
    failed = False

    def fail_first_replace(source, destination):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("synthetic first promotion failure")
        return original_replace(source, destination)

    monkeypatch.setattr("ev_fleet.maintenance.os.replace", fail_first_replace)
    with pytest.raises(OSError, match="synthetic first promotion failure"):
        _promote_runtime_directories(staged_cfg, final_cfg, "first-failure-token")

    assert (final_generated / "state.txt").read_text(encoding="utf-8") == "old-generated"
    assert (final_artifacts / "state.txt").read_text(encoding="utf-8") == "old-artifacts"
    assert not (root / ".ev_fleet_backups" / "first-failure-token").exists()


def test_promotion_rollback_failure_preserves_backup_for_manual_recovery(tmp_path, monkeypatch):
    root = tmp_path / "project"
    final_generated = root / "data" / "generated"
    final_artifacts = root / "artifacts"
    staged_generated = root / "staging" / "generated"
    staged_artifacts = root / "staging" / "artifacts"
    for directory, value in (
        (final_generated, "old-generated"),
        (final_artifacts, "old-artifacts"),
        (staged_generated, "new-generated"),
        (staged_artifacts, "new-artifacts"),
    ):
        directory.mkdir(parents=True)
        (directory / "state.txt").write_text(value, encoding="utf-8")

    final_raw = {
        "paths": {
            "generated_data": str(final_generated),
            "artifacts": str(final_artifacts),
            "database": str(final_artifacts / "fleet.sqlite"),
        }
    }
    staged_raw = {
        "paths": {
            "generated_data": str(staged_generated),
            "artifacts": str(staged_artifacts),
            "database": str(staged_artifacts / "fleet.sqlite"),
        }
    }
    final_cfg = ProjectConfig(final_raw, root / "config.yaml", root)
    staged_cfg = ProjectConfig(staged_raw, root / "config.yaml", root)
    original_replace = os.replace
    promotion_failed = False

    def flaky_replace(source, destination):
        nonlocal promotion_failed
        source_path = __import__("pathlib").Path(source)
        destination_path = __import__("pathlib").Path(destination)
        if source_path == staged_artifacts and not promotion_failed:
            promotion_failed = True
            raise OSError("synthetic promotion failure")
        if promotion_failed and source_path.name == "generated" and ".ev_fleet_backups" in source_path.parts:
            raise OSError("synthetic rollback failure")
        return original_replace(source_path, destination_path)

    monkeypatch.setattr("ev_fleet.maintenance.os.replace", flaky_replace)
    with pytest.raises(RuntimeError, match="rollback was incomplete"):
        _promote_runtime_directories(staged_cfg, final_cfg, "rollback-token")

    backup_root = root / ".ev_fleet_backups" / "rollback-token"
    assert backup_root.is_dir()
    assert (backup_root / "generated" / "state.txt").read_text(encoding="utf-8") == "old-generated"
    assert (backup_root / "promotion.json").is_file()


def test_semantic_hash_ignores_prefixed_runtime_but_not_scientific_change(cfg, pipeline_summary):
    from ev_fleet.validation import semantic_artifact_hashes

    path = cfg.artifacts_dir / "recommended_rcce.json"
    original = json.loads(path.read_text(encoding="utf-8"))
    before = semantic_artifact_hashes(cfg.artifacts_dir)["recommended_rcce.json"]

    try:
        runtime_only = copy.deepcopy(original)
        runtime_only.setdefault("metrics", {})["solver_runtime_seconds"] = 9999.0
        path.write_text(json.dumps(runtime_only, indent=2) + "\n", encoding="utf-8")
        after_runtime = semantic_artifact_hashes(cfg.artifacts_dir)["recommended_rcce.json"]
        assert after_runtime == before

        scientific = copy.deepcopy(original)
        scientific.setdefault("metrics", {})["shortage_cvar90"] = (
            float(scientific.get("metrics", {}).get("shortage_cvar90", 0.0)) + 1.0
        )
        path.write_text(json.dumps(scientific, indent=2) + "\n", encoding="utf-8")
        after_scientific = semantic_artifact_hashes(cfg.artifacts_dir)["recommended_rcce.json"]
        assert after_scientific != before
    finally:
        path.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")


def test_handoff_cli_is_available_from_package(tmp_path, monkeypatch, capsys):
    import ev_fleet.cli as cli_module

    artifact = tmp_path / "candidate.whl"
    artifact.write_bytes(b"wheel")
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "project_name": "demo",
                "baseline_source_fingerprint": "baseline",
                "diff_checksum": "diff",
                "supported_environment_claims": [],
                "required_entrypoints": [],
                "datasets": [],
                "baseline_metrics": {},
                "final_metrics": {},
                "metric_gates": {},
                "command_evidence": [],
                "known_limitations": [],
                "unresolved_items": [],
                "evidence_level": "E2",
                "next_qualification_gates": [],
            }
        ),
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("demo\n", encoding="utf-8")
    output = tmp_path / "handoff.json"

    assert (
        cli_module.main(
            [
                "handoff",
                "--project-root",
                str(project),
                "--evidence",
                str(evidence),
                "--artifact",
                str(artifact),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["status"] == "PASS"
    assert output.is_file()


def test_release_handoff_builder_hashes_source_and_artifacts(tmp_path):
    from ev_fleet.release_handoff import build_release_candidate_handoff, sha256_file

    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "constraints").mkdir()
    (project / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (project / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (project / "constraints" / "runtime.txt").write_text("numpy==2.3.5\n", encoding="utf-8")
    artifact = tmp_path / "candidate.zip"
    artifact.write_bytes(b"candidate")
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "project_name": "demo",
                "baseline_source_fingerprint": "baseline-sha",
                "diff_checksum": "diff-sha",
                "supported_environment_claims": ["Python 3.13 local"],
                "required_entrypoints": ["demo run"],
                "datasets": [{"name": "synthetic", "type": "synthetic"}],
                "baseline_metrics": {"tests_passed": 1},
                "final_metrics": {"tests_passed": 2},
                "metric_gates": {"tests_failed": 0},
                "command_evidence": [{"command": "pytest", "exit_code": 0}],
                "known_limitations": ["none for fixture"],
                "unresolved_items": [],
                "evidence_level": "E3",
                "next_qualification_gates": ["hosted CI"],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "handoff.json"

    build_release_candidate_handoff(project, evidence, [artifact], output)
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert payload["schema_version"] == "1.0"
    assert payload["qualification_status"] == "RELEASE_CANDIDATE_ONLY"
    assert payload["build_artifacts"][0]["sha256"] == sha256_file(artifact)
    first_fingerprint = payload["source_fingerprint"]
    build_release_candidate_handoff(project, evidence, [artifact], output)
    second = json.loads(output.read_text(encoding="utf-8"))
    assert second["source_fingerprint"] == first_fingerprint


def test_release_handoff_builder_rejects_missing_evidence_and_artifact(tmp_path):
    from ev_fleet.release_handoff import build_release_candidate_handoff

    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("demo\n", encoding="utf-8")
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"project_name": "demo"}), encoding="utf-8")

    with pytest.raises(ValueError, match="missing required keys"):
        build_release_candidate_handoff(project, evidence, [tmp_path / "missing.zip"], tmp_path / "handoff.json")


def test_qualify_local_default_cleans_runtime_outputs(tmp_path, monkeypatch):
    import importlib.util

    module_path = Path("scripts/qualify_local.py").resolve()
    spec = importlib.util.spec_from_file_location("qualify_local_under_test", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)

    for relative in (
        "artifacts/fleet_planning.sqlite",
        "artifacts-medium/report.json",
        "data/generated/stations.csv",
        "data/generated-medium/stations.csv",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("runtime", encoding="utf-8")

    module._clean_runtime_outputs()

    assert {path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")} == {
        "artifacts",
        "artifacts/.gitkeep",
        "artifacts-medium",
        "artifacts-medium/.gitkeep",
        "data",
        "data/generated",
        "data/generated/.gitkeep",
        "data/generated-medium",
        "data/generated-medium/.gitkeep",
    }


def test_qualify_local_has_preserve_runtime_outputs_escape_hatch():
    script = Path("scripts/qualify_local.py").read_text(encoding="utf-8")
    assert "--preserve-runtime-outputs" in script
    assert "runtime_outputs_preserved" in script


def test_source_fingerprint_excludes_generated_release_metadata(tmp_path):
    from ev_fleet.release_handoff import source_fingerprint

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "module.py").write_text("print('stable')\n", encoding="utf-8")
    first = source_fingerprint(tmp_path)
    for name in (
        "qualification_manifest.json",
        "release_bundle_manifest.json",
        "release_candidate_handoff.json",
        "release_candidate_handoff-v0.7.1rc4.json",
        "qualification_local_summary.json",
        "decision-aware-ev-fleet-planning-v0.7.1rc4-SHA256SUMS.txt",
        "decision-aware-ev-fleet-planning-v0.7.1rc4.patch",
    ):
        (tmp_path / name).write_text(name, encoding="utf-8")
    assert source_fingerprint(tmp_path) == first


def test_process_liveness_handles_systemerror_from_stale_pid(monkeypatch):
    import ev_fleet.maintenance as maintenance

    monkeypatch.setattr(maintenance.os, "name", "posix", raising=False)

    def broken_kill(pid, sig):
        raise SystemError("<class 'OSError'> returned a result with an exception set")

    monkeypatch.setattr(maintenance.os, "kill", broken_kill)
    assert maintenance._process_is_alive(12345) is False
