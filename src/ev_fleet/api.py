from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .config import ProjectConfig, load_config
from .manifest import verify_artifact_manifest, verify_generated_data_manifest
from .optimization import PlanResult, load_plan
from .reliability import evaluate_rcce
from .simulation import simulate_plan
from .validation import validate_cross_artifact_contracts


class OverrideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    station_id: str = Field(min_length=1)
    vehicle_type: str = Field(min_length=1)
    delta: int = Field(ge=-100, le=100)
    reason: str = Field(default="planner_override", min_length=3, max_length=300)


def _default_config_path() -> Path:
    configured = os.getenv("EV_FLEET_CONFIG")
    if configured:
        return Path(configured).expanduser().resolve()
    repository_root = Path(__file__).resolve().parents[2]
    return repository_root / "configs" / "smoke.yaml"


def create_app(config_path: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="Decision-Aware EV Fleet Planning API", version=__version__)
    selected_config = Path(config_path).expanduser().resolve() if config_path is not None else _default_config_path()

    def config() -> ProjectConfig:
        try:
            return load_config(selected_config)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=f"Configuration unavailable: {exc}") from exc

    def artifact_readiness() -> tuple[bool, dict[str, object], ProjectConfig | None]:
        try:
            cfg = config()
            summary_path = cfg.artifacts_dir / "run_summary.json"
            if not summary_path.is_file():
                return (
                    False,
                    {"status": "degraded", "artifact_ready": False, "detail": "run_summary.json missing"},
                    cfg,
                )
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            if summary.get("pipeline_status") != "PASS":
                return (
                    False,
                    {
                        "status": "degraded",
                        "artifact_ready": False,
                        "detail": "latest pipeline run is not PASS",
                    },
                    cfg,
                )
            run_id = summary.get("run_id")
            if not isinstance(run_id, str) or not run_id.strip():
                return (
                    False,
                    {
                        "status": "degraded",
                        "artifact_ready": False,
                        "detail": "run_summary.json is missing run_id",
                    },
                    cfg,
                )
            verified = verify_artifact_manifest(cfg.artifacts_dir, expected_run_id=run_id)
            generated_verified = verify_generated_data_manifest(
                cfg.generated_dir,
                cfg.artifacts_dir / "generated_data_manifest.json",
                expected_run_id=run_id,
            )
            semantic = validate_cross_artifact_contracts(cfg)
            return (
                True,
                {
                    "status": "ok",
                    "artifact_ready": True,
                    "config": str(cfg.source_path),
                    "run_id": run_id,
                    "manifest_files_verified": len(verified),
                    "generated_files_verified": len(generated_verified),
                    "semantic_contract": semantic,
                },
                cfg,
            )
        except HTTPException as exc:
            return False, {"status": "degraded", "artifact_ready": False, "detail": str(exc.detail)}, None
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            return False, {"status": "degraded", "artifact_ready": False, "detail": str(exc)}, None

    def require_ready() -> tuple[dict[str, object], ProjectConfig]:
        ready, payload, cfg = artifact_readiness()
        if not ready or cfg is None:
            raise HTTPException(status_code=503, detail=payload.get("detail", "Artifacts are not ready"))
        return payload, cfg

    def ensure_same_run(cfg: ProjectConfig, expected_run_id: str) -> None:
        summary_path = cfg.artifacts_dir / "run_summary.json"
        try:
            current = json.loads(summary_path.read_text(encoding="utf-8")).get("run_id")
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=503, detail="Artifacts changed during request") from exc
        if current != expected_run_id:
            raise HTTPException(status_code=503, detail="Artifacts changed during request")

    def read_json(name: str):
        readiness_payload, cfg = require_ready()
        path = cfg.artifacts_dir / name
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"Artifact not found: {name}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=500, detail=f"Artifact is invalid JSON: {name}") from exc
        ensure_same_run(cfg, str(readiness_payload["run_id"]))
        return payload

    @app.get("/health/live")
    def liveness():
        return {"status": "ok"}

    @app.get("/health")
    @app.get("/health/ready")
    def readiness():
        ready, payload, _ = artifact_readiness()
        return payload if ready else JSONResponse(status_code=503, content=payload)

    @app.get("/plans/latest")
    def latest_plan():
        return read_json("recommended_plan.json")

    @app.get("/plans/latest/reliability")
    def latest_reliability():
        return read_json("recommended_rcce.json")

    @app.get("/plans/base-stochastic")
    def base_stochastic_plan():
        return read_json("stochastic_plan.json")

    @app.get("/runs/latest")
    def latest_run():
        return read_json("run_summary.json")

    @app.post("/plans/latest/simulate-override")
    def simulate_override(request: OverrideRequest):
        readiness_payload, cfg = require_ready()
        plan = load_plan(cfg.artifacts_dir / "recommended_plan.json")
        allocation = plan.allocation.copy()
        mask = allocation.station_id.astype(str).eq(request.station_id) & allocation.vehicle_type.astype(str).eq(
            request.vehicle_type
        )
        if not mask.any():
            raise HTTPException(status_code=404, detail="Station/vehicle pair not found")
        proposed = int(allocation.loc[mask, "planned_count"].iloc[0]) + request.delta
        if proposed < 0:
            raise HTTPException(status_code=422, detail="Override would create a negative fleet count")
        if "temporary_addition" not in allocation.columns:
            allocation["temporary_addition"] = 0
        current_temporary = int(allocation.loc[mask, "temporary_addition"].iloc[0])
        if request.delta < 0 and -request.delta > current_temporary:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Negative override can only remove existing temporary additions; "
                    "use a paired redeployment workflow for permanent fleet movement"
                ),
            )
        allocation.loc[mask, "planned_count"] = proposed
        if request.delta > 0:
            allocation.loc[mask, "temporary_addition"] += request.delta
        elif request.delta < 0:
            allocation.loc[mask, "temporary_addition"] -= -request.delta

        stations_path = cfg.generated_dir / "stations.csv"
        if not stations_path.exists():
            raise HTTPException(status_code=409, detail="Station-capacity data are unavailable")
        stations = pd.read_csv(stations_path).set_index("station_id")
        if request.station_id not in stations.index:
            raise HTTPException(status_code=404, detail="Station capacity not found")
        station_total = int(
            allocation.loc[allocation.station_id.astype(str).eq(request.station_id), "planned_count"].sum()
        )
        parking_capacity = int(stations.loc[request.station_id, "parking_capacity"])
        if station_total > parking_capacity:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Override would exceed parking capacity at {request.station_id}: "
                    f"proposed {station_total}, capacity {parking_capacity}"
                ),
            )

        emergency_lease_cost = float(cfg.section("optimization").get("emergency_lease_cost", 450.0))
        objective_delta = float(request.delta) * emergency_lease_cost
        candidate = PlanResult(
            name=f"{plan.name}_planner_override",
            status="OVERRIDE_CANDIDATE",
            objective=(float(plan.objective) + objective_delta) if plan.objective is not None else None,
            allocation=allocation,
            moves=plan.moves.copy(),
            scenario_metrics=plan.scenario_metrics.copy(),
            solver={
                **plan.solver,
                "certificate_scope": "base_plan_only",
                "override_replay_only": True,
                "override": {
                    "station_id": request.station_id,
                    "vehicle_type": request.vehicle_type,
                    "delta": request.delta,
                    "reason": request.reason,
                    "objective_delta": objective_delta,
                },
            },
        )
        scenario_path = cfg.artifacts_dir / "evaluation_scenarios.csv"
        if not scenario_path.exists():
            scenario_path = cfg.generated_dir / "scenario_routes.csv"
        if not scenario_path.exists():
            raise HTTPException(status_code=409, detail="No scenario artifact available for override simulation")
        scenarios = pd.read_csv(scenario_path)
        simulation = simulate_plan(cfg, candidate, scenarios, seed_offset=0)
        decision = evaluate_rcce(cfg, candidate, simulation)
        ensure_same_run(cfg, str(readiness_payload["run_id"]))
        return {
            "status": decision.status,
            "approval_required": True,
            "message": (
                "Override was independently replayed and requires planner approval; "
                "it is not persisted or automatically released."
            ),
            "override": request.model_dump(),
            "reliability": decision.to_dict(),
            "proposed_plan": candidate.to_dict(),
        }

    return app


app = create_app()
