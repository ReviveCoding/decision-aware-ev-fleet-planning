from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence

from . import __version__
from .config import load_config
from .maintenance import clean_runtime, pipeline_run_lock, recover_incomplete_promotions
from .pipeline import run_pipeline
from .reference_results import export_reference_results
from .release_handoff import build_release_candidate_handoff
from .validation import validate_repository_outputs


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ev-fleet", description="Decision-aware EV fleet planning system")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "validate", "train-gpu", "clean"):
        child = subparsers.add_parser(command)
        child.add_argument("--config", default="configs/smoke.yaml")
    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--config", default="configs/smoke.yaml")
    serve_parser.add_argument("--host", default="0.0.0.0")
    serve_parser.add_argument("--port", type=int, default=8000)
    export_parser = subparsers.add_parser("export-reference")
    export_parser.add_argument("--config", default="configs/smoke.yaml")
    export_parser.add_argument("--output", default=None)
    export_parser.add_argument("--name", default=None)
    handoff_parser = subparsers.add_parser("handoff")
    handoff_parser.add_argument("--project-root", default=".")
    handoff_parser.add_argument("--evidence", required=True)
    handoff_parser.add_argument("--artifact", action="append", required=True)
    handoff_parser.add_argument("--output", default="release_candidate_handoff.json")
    return parser


def _main_impl(args: argparse.Namespace) -> int:
    if args.command == "handoff":
        output = build_release_candidate_handoff(
            project_root=args.project_root,
            evidence_path=args.evidence,
            artifacts=args.artifact,
            output_path=args.output,
        )
        payload = json.loads(output.read_text(encoding="utf-8"))
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "output": str(output),
                    "candidate_version": payload["candidate_version"],
                    "source_fingerprint": payload["source_fingerprint"],
                    "artifact_count": len(payload["build_artifacts"]),
                },
                indent=2,
            )
        )
        return 0

    cfg = load_config(args.config)
    if args.command == "run":
        result = run_pipeline(cfg)
    elif args.command == "validate":
        with pipeline_run_lock(cfg):
            recover_incomplete_promotions(cfg.project_root)
            result = validate_repository_outputs(cfg)
    elif args.command == "train-gpu":
        if cfg.section("forecast").get("use_torch_challenger") is not True:
            raise ValueError("train-gpu requires forecast.use_torch_challenger: true")
        summary = run_pipeline(cfg)
        gpu_result = dict(summary.get("gpu_challenger", {}))
        result = {
            "status": gpu_result.get("status", "FAILED"),
            "pipeline_status": summary.get("pipeline_status"),
            "selected_forecast_model": summary.get("selected_forecast_model"),
            "gpu_challenger": gpu_result,
            "artifacts_dir": summary.get("artifacts_dir"),
        }
    elif args.command == "export-reference":
        with pipeline_run_lock(cfg):
            recover_incomplete_promotions(cfg.project_root)
            result = export_reference_results(cfg, output_root=args.output, profile_name=args.name)
    elif args.command == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(cfg.source_path), host=args.host, port=args.port)
        return 0
    else:
        result = clean_runtime(cfg)
    print(json.dumps(result, indent=2))
    if args.command == "train-gpu" and result.get("status") != "PASS":
        return 2
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        return _main_impl(args)
    except (FileNotFoundError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        if args.log_level == "DEBUG":
            raise
        print(json.dumps({"status": "ERROR", "error": str(exc)}, indent=2), file=sys.stderr)
        return 1
