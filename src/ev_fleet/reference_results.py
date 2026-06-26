from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path

from .config import ProjectConfig
from .io_utils import sha256_file, write_json_atomic
from .validation import validate_repository_outputs

REFERENCE_FILES = (
    "run_summary.json",
    "forecast_model_comparison.csv",
    "base_policy_selection.csv",
    "stochastic_value.json",
    "paired_policy_comparison.csv",
    "corrective_frontier.csv",
    "recommended_plan.json",
    "recommended_rcce.json",
    "saa_summary.json",
    "reports/executive_decision_memo.md",
    "reports/model_and_optimization_card.md",
    "reports/limitations.md",
)


def _assert_safe_reference_target(cfg: ProjectConfig, target: Path) -> None:
    resolved = target.resolve()
    project_root = cfg.project_root.resolve()
    if resolved == project_root:
        raise ValueError("Reference target must not equal project_root")
    protected = [cfg.artifacts_dir.resolve(), cfg.generated_dir.resolve()]
    if cfg.public_calibration_dir is not None:
        protected.append(cfg.public_calibration_dir.resolve())
    for path in protected:
        if resolved == path or resolved in path.parents or path in resolved.parents:
            raise ValueError(f"Reference target must be separate from protected runtime path: {path}")


def export_reference_results(
    cfg: ProjectConfig,
    *,
    output_root: str | Path | None = None,
    profile_name: str | None = None,
) -> dict[str, object]:
    """Export a compact, GitHub-friendly evidence snapshot from a validated run."""
    validation = validate_repository_outputs(cfg)
    if validation.get("status") != "PASS":
        raise ValueError("Repository outputs must pass validation before reference export")

    profile = profile_name or str(cfg.raw.get("mode") or cfg.source_path.stem)
    if not profile or any(part in profile for part in ("/", "\\", "..")):
        raise ValueError("profile_name must be a simple directory name")
    root = Path(output_root).expanduser().resolve() if output_root else cfg.project_root / "reference_results"
    root.mkdir(parents=True, exist_ok=True)
    target = root / profile
    _assert_safe_reference_target(cfg, target)

    missing = [relative for relative in REFERENCE_FILES if not (cfg.artifacts_dir / relative).is_file()]
    if missing:
        raise FileNotFoundError(f"Missing reference-result files: {missing}")

    temporary = Path(tempfile.mkdtemp(prefix=f".{profile}-", dir=root))
    try:
        records: list[dict[str, object]] = []
        for relative in REFERENCE_FILES:
            source = cfg.artifacts_dir / relative
            destination = temporary / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            if destination.suffix.lower() in {".json", ".md", ".csv", ".txt"}:
                content = destination.read_text(encoding="utf-8")
                sanitized = content.replace(str(cfg.project_root), "${PROJECT_ROOT}")
                if sanitized != content:
                    destination.write_text(sanitized, encoding="utf-8")
            records.append(
                {
                    "path": relative,
                    "size_bytes": destination.stat().st_size,
                    "sha256": sha256_file(destination),
                }
            )
        write_json_atomic(
            temporary / "reference_manifest.json",
            {
                "profile": profile,
                "config": cfg.source_path.name,
                "claim_boundary": "offline public-data-informed synthetic reference run",
                "files": records,
            },
        )
        backup = root / f".{profile}-backup-{uuid.uuid4().hex}"
        had_target = target.exists()
        try:
            if had_target:
                os.replace(target, backup)
            os.replace(temporary, target)
        except Exception as promotion_error:
            restore_errors: list[str] = []
            try:
                if target.exists():
                    shutil.rmtree(target)
                if had_target and backup.exists():
                    os.replace(backup, target)
            except Exception as exc:
                restore_errors.append(str(exc))
            if restore_errors:
                raise RuntimeError(
                    f"Reference-result promotion failed and rollback was incomplete; "
                    f"backup preserved at {backup}: {restore_errors}"
                ) from promotion_error
            raise
        else:
            shutil.rmtree(backup, ignore_errors=True)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    return {
        "status": "PASS",
        "profile": profile,
        "output": str(target),
        "files": len(REFERENCE_FILES),
    }
