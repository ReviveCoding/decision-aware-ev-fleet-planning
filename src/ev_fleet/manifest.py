from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .io_utils import sha256_file, write_json_atomic

MANIFEST_SCHEMA_VERSION = 1


def _run_id_from_summary(artifacts_dir: Path) -> str | None:
    summary_path = artifacts_dir / "run_summary.json"
    if not summary_path.is_file():
        return None
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    value = payload.get("run_id")
    return str(value) if isinstance(value, str) and value.strip() else None


def _resolve_manifest_path(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"Unsafe manifest path: {relative}")
    resolved_root = root.resolve()
    resolved = (root / candidate).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError(f"Manifest path escapes artifact directory: {relative}")
    return resolved


def _inventory_files(root: Path, excluded_names: set[str]) -> set[str]:
    files: set[str] = set()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Symlinks are not allowed in signed directories: {path}")
        if path.is_file() and path.name not in excluded_names:
            files.add(path.relative_to(root).as_posix())
    return files


def build_artifact_manifest(artifacts_dir: str | Path, run_id: str | None = None) -> Path:
    root = Path(artifacts_dir)
    resolved_run_id = run_id or _run_id_from_summary(root)
    records = []
    for relative in sorted(_inventory_files(root, {"artifact_manifest.json", ".gitkeep"})):
        path = root / relative
        records.append(
            {
                "path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return write_json_atomic(
        root / "artifact_manifest.json",
        {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "run_id": resolved_run_id,
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "files": records,
        },
    )


def verify_artifact_manifest(artifacts_dir: str | Path, expected_run_id: str | None = None) -> list[str]:
    root = Path(artifacts_dir)
    manifest_path = root / "artifact_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Artifact manifest not found: {manifest_path}")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("Unsupported artifact manifest schema_version")
    if expected_run_id is not None and payload.get("run_id") != expected_run_id:
        raise ValueError("Artifact manifest run_id does not match run_summary.json")
    records = payload.get("files", [])
    if not isinstance(records, list) or not records:
        raise ValueError("Artifact manifest must contain a non-empty files list")
    verified: list[str] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or not {"path", "size_bytes", "sha256"}.issubset(record):
            raise ValueError("Artifact manifest contains an invalid file record")
        relative = str(record["path"])
        if relative in seen:
            raise ValueError(f"Duplicate manifest path: {relative}")
        seen.add(relative)
        path = _resolve_manifest_path(root, relative)
        if not path.is_file():
            raise ValueError(f"Manifest file missing: {relative}")
        if path.stat().st_size != int(record["size_bytes"]):
            raise ValueError(f"Manifest size mismatch: {relative}")
        if sha256_file(path) != str(record["sha256"]):
            raise ValueError(f"Manifest hash mismatch: {relative}")
        verified.append(relative)
    actual = _inventory_files(root, {"artifact_manifest.json", ".gitkeep"})
    untracked = sorted(actual - seen)
    if untracked:
        raise ValueError(f"Untracked artifact files are not allowed: {untracked[:5]}")
    return verified


def build_generated_data_manifest(
    generated_dir: str | Path,
    output_path: str | Path,
    run_id: str | None = None,
) -> Path:
    root = Path(generated_dir)
    output = Path(output_path)
    resolved_run_id = run_id or _run_id_from_summary(output.parent)
    records = []
    for relative in sorted(_inventory_files(root, {".gitkeep"})):
        path = root / relative
        records.append(
            {
                "path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    if not records:
        raise ValueError(f"Generated-data directory contains no files: {root}")
    return write_json_atomic(
        output,
        {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "run_id": resolved_run_id,
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "files": records,
        },
    )


def verify_generated_data_manifest(
    generated_dir: str | Path,
    manifest_path: str | Path,
    expected_run_id: str | None = None,
) -> list[str]:
    root = Path(generated_dir)
    path = Path(manifest_path)
    if not path.is_file():
        raise FileNotFoundError(f"Generated-data manifest not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("Unsupported generated-data manifest schema_version")
    if expected_run_id is not None and payload.get("run_id") != expected_run_id:
        raise ValueError("Generated-data manifest run_id does not match run_summary.json")
    records = payload.get("files", [])
    if not isinstance(records, list) or not records:
        raise ValueError("Generated-data manifest must contain a non-empty files list")
    verified: list[str] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or not {"path", "size_bytes", "sha256"}.issubset(record):
            raise ValueError("Generated-data manifest contains an invalid file record")
        relative = str(record["path"])
        if relative in seen:
            raise ValueError(f"Duplicate generated-data manifest path: {relative}")
        seen.add(relative)
        candidate = _resolve_manifest_path(root, relative)
        if not candidate.is_file():
            raise ValueError(f"Generated-data file missing: {relative}")
        if candidate.stat().st_size != int(record["size_bytes"]):
            raise ValueError(f"Generated-data size mismatch: {relative}")
        if sha256_file(candidate) != str(record["sha256"]):
            raise ValueError(f"Generated-data hash mismatch: {relative}")
        verified.append(relative)
    actual = _inventory_files(root, {".gitkeep"})
    untracked = sorted(actual - seen)
    if untracked:
        raise ValueError(f"Untracked generated-data files are not allowed: {untracked[:5]}")
    return verified
