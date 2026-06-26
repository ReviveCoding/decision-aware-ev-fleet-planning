from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .io_utils import write_json_atomic

HANDOFF_SCHEMA_VERSION = "1.0"
_REQUIRED_EVIDENCE_KEYS = {
    "project_name",
    "baseline_source_fingerprint",
    "diff_checksum",
    "supported_environment_claims",
    "required_entrypoints",
    "datasets",
    "baseline_metrics",
    "final_metrics",
    "metric_gates",
    "command_evidence",
    "known_limitations",
    "unresolved_items",
    "evidence_level",
    "next_qualification_gates",
}
_RUNTIME_DIR_NAMES = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".ev_fleet_locks",
    ".ev_fleet_staging",
    ".ev_fleet_backups",
    "build",
    "dist",
    "htmlcov",
}
_RUNTIME_PREFIXES = (
    "artifacts",
    "data/generated",
)
_GENERATED_RELEASE_FILE_NAMES = {
    "qualification_manifest.json",
    "release_bundle_manifest.json",
}
_GENERATED_RELEASE_SUFFIXES = (
    "-SHA256SUMS.txt",
    ".patch",
)


def sha256_file(path: str | Path) -> str:
    target = Path(path)
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_excluded(relative: Path, output_relative: Path | None) -> bool:
    if output_relative is not None and relative == output_relative:
        return True
    if any(part in _RUNTIME_DIR_NAMES or part.endswith((".egg-info", ".dist-info")) for part in relative.parts):
        return True
    rendered = relative.as_posix()
    if relative.name in _GENERATED_RELEASE_FILE_NAMES:
        return True
    if relative.name.startswith("release_candidate_handoff") and relative.suffix.lower() == ".json":
        return True
    if relative.name.startswith("qualification_local_summary") and relative.suffix.lower() == ".json":
        return True
    if any(relative.name.endswith(suffix) for suffix in _GENERATED_RELEASE_SUFFIXES):
        return True
    if any(
        rendered == prefix or rendered.startswith(prefix + "-") or rendered.startswith(prefix + "/")
        for prefix in _RUNTIME_PREFIXES
    ):
        return True
    return relative.suffix.lower() in {".pyc", ".pyo"} or relative.name in {".coverage", ".DS_Store"}


def source_fingerprint(project_root: str | Path, output_path: str | Path | None = None) -> tuple[str, int]:
    root = Path(project_root).resolve()
    output_relative: Path | None = None
    if output_path is not None:
        output = Path(output_path).resolve()
        if root in output.parents:
            output_relative = output.relative_to(root)

    digest = hashlib.sha256()
    count = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if _is_excluded(relative, output_relative):
            continue
        file_digest = sha256_file(path)
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_digest.encode("ascii"))
        digest.update(b"\n")
        count += 1
    if count == 0:
        raise ValueError(f"No source files found under {root}")
    return digest.hexdigest(), count


def _git_state(project_root: Path) -> dict[str, Any]:
    try:
        inside = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--is-inside-work-tree"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return {"available": False, "commit": None, "dirty_tree": None}
    if inside != "true":
        return {"available": False, "commit": None, "dirty_tree": None}
    commit = subprocess.run(
        ["git", "-C", str(project_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "-C", str(project_root), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    return {"available": True, "commit": commit, "dirty_tree": dirty}


def _dependency_manifests(project_root: Path) -> list[dict[str, Any]]:
    candidates = [project_root / "pyproject.toml", project_root / "constraints" / "runtime.txt"]
    candidates.extend(sorted(project_root.glob("requirements*.txt")))
    records: list[dict[str, Any]] = []
    for path in candidates:
        if path.is_file():
            records.append(
                {
                    "path": path.relative_to(project_root).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    return records


def _artifact_record(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Release artifact not found: {path}")
    return {
        "path": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _load_evidence(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Evidence file is invalid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Evidence payload must be a JSON object")
    missing = sorted(_REQUIRED_EVIDENCE_KEYS - set(payload))
    if missing:
        raise ValueError(f"Evidence payload is missing required keys: {missing}")
    return payload


def build_release_candidate_handoff(
    project_root: str | Path,
    evidence_path: str | Path,
    artifacts: list[str | Path],
    output_path: str | Path,
) -> Path:
    root = Path(project_root).resolve()
    evidence_file = Path(evidence_path).resolve()
    output = Path(output_path).resolve()
    evidence = _load_evidence(evidence_file)
    if not artifacts:
        raise ValueError("At least one release artifact is required")

    source_sha, source_file_count = source_fingerprint(root, output)
    git_state = _git_state(root)
    artifact_records = [_artifact_record(Path(path).resolve()) for path in artifacts]

    payload = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "project_name": evidence["project_name"],
        "candidate_version": __version__,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "source_control": "git" if git_state["available"] else "snapshot-no-git",
        "source_commit": git_state["commit"],
        "source_fingerprint": source_sha,
        "source_file_count": source_file_count,
        "baseline_source_fingerprint": evidence["baseline_source_fingerprint"],
        "dirty_tree": git_state["dirty_tree"],
        "diff_checksum": evidence["diff_checksum"],
        "runtime": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "executable": Path(sys.executable).name,
        },
        "dependency_manifests": _dependency_manifests(root),
        "supported_environment_claims": evidence["supported_environment_claims"],
        "required_entrypoints": evidence["required_entrypoints"],
        "datasets": evidence["datasets"],
        "baseline_metrics": evidence["baseline_metrics"],
        "final_metrics": evidence["final_metrics"],
        "metric_gates": evidence["metric_gates"],
        "command_evidence": evidence["command_evidence"],
        "build_artifacts": artifact_records,
        "known_limitations": evidence["known_limitations"],
        "unresolved_items": evidence["unresolved_items"],
        "verification_evidence_level": evidence["evidence_level"],
        "next_qualification_gates": evidence["next_qualification_gates"],
        "evidence_source": evidence_file.name,
        "qualification_status": "RELEASE_CANDIDATE_ONLY",
    }
    return write_json_atomic(output, payload)
