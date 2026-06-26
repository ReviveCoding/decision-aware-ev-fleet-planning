from __future__ import annotations

import copy
import json
import os
import shutil
import socket
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .config import ProjectConfig
from .io_utils import write_json_atomic


def _process_is_alive(pid: int) -> bool:
    """Return whether a process appears alive without crashing on Windows."""
    if pid <= 0:
        return False

    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259

            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION,
                False,
                int(pid),
            )
            if not handle:
                return ctypes.get_last_error() == 5

            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True
                return exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, SystemError):
        return False
    return True


def _lock_path(cfg: ProjectConfig) -> Path:
    # Promotion recovery and public-calibration writes are project-wide operations,
    # so every profile must share one writer lock even when artifact paths differ.
    return cfg.project_root / ".ev_fleet_locks" / "project.lock"


@contextmanager
def pipeline_run_lock(cfg: ProjectConfig, *, stale_seconds: float = 86_400.0):
    """Prevent overlapping writers with a cross-platform OS advisory lock.

    The lock file is intentionally persistent. Ownership belongs to the open file
    descriptor, so the operating system releases it automatically after normal
    exit, exceptions, or process termination. ``stale_seconds`` is retained for
    backward API compatibility but is no longer needed for lock reclamation.
    """
    del stale_seconds
    path = _lock_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b", buffering=0)

    def acquire() -> None:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            handle.close()
            raise RuntimeError(f"Another pipeline run holds lock: {path}") from exc

    acquire()
    payload = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "started_at_epoch": time.time(),
        "config": str(cfg.source_path),
        "artifacts": str(cfg.artifacts_dir),
    }
    encoded = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    handle.seek(0)
    handle.truncate(0)
    handle.write(encoded)
    handle.flush()
    try:
        yield path
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _replace_runtime_path_strings(value, replacements: dict[str, str]):
    if isinstance(value, dict):
        return {key: _replace_runtime_path_strings(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_runtime_path_strings(item, replacements) for item in value]
    if isinstance(value, tuple):
        return tuple(_replace_runtime_path_strings(item, replacements) for item in value)
    if isinstance(value, str):
        result = value
        for old, new in replacements.items():
            result = result.replace(old, new)
        return result
    return value


def remap_staged_result(value, staged_cfg: ProjectConfig, final_cfg: ProjectConfig):
    replacements = {
        str(staged_cfg.database_path): str(final_cfg.database_path),
        str(staged_cfg.artifacts_dir): str(final_cfg.artifacts_dir),
        str(staged_cfg.generated_dir): str(final_cfg.generated_dir),
    }
    return _replace_runtime_path_strings(value, replacements)


def _rewrite_staged_text_files(staged_cfg: ProjectConfig, final_cfg: ProjectConfig) -> None:
    replacements = {
        str(staged_cfg.database_path): str(final_cfg.database_path),
        str(staged_cfg.artifacts_dir): str(final_cfg.artifacts_dir),
        str(staged_cfg.generated_dir): str(final_cfg.generated_dir),
    }
    text_replacements: dict[str, str] = {}
    for old, new in replacements.items():
        text_replacements[old] = new
        # JSON output is written with ensure_ascii=True by default, so non-ASCII
        # paths are escaped. Replace both literal and JSON-escaped spellings to
        # keep promoted artifacts free of staging paths on Windows/Linux/Unicode
        # source directories.
        text_replacements[json.dumps(old)[1:-1]] = json.dumps(new)[1:-1]
    for root in (staged_cfg.artifacts_dir, staged_cfg.generated_dir):
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".json", ".md", ".csv", ".txt"}:
                continue
            content = path.read_text(encoding="utf-8")
            updated = content
            for old, new in text_replacements.items():
                updated = updated.replace(old, new)
            if updated != content:
                path.write_text(updated, encoding="utf-8")


def _remove_runtime_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _safe_relative_runtime_path(project_root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise RuntimeError(f"Unsafe promotion journal path: {relative}")
    resolved_root = project_root.resolve()
    resolved = (project_root / candidate).resolve()
    if resolved == resolved_root or resolved_root not in resolved.parents:
        raise RuntimeError(f"Promotion journal path escapes project root: {relative}")
    return resolved


def _restore_previous_runtime_path(final: Path, backup: Path, had_final: bool) -> None:
    """Restore one runtime path after an interrupted or failed promotion.

    If the old final existed but no backup was created yet, the promotion had not
    reached that path. In that case the untouched final is already the correct
    last-known-good state.
    """
    if had_final:
        if backup.exists():
            _remove_runtime_path(final)
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(backup, final)
            return
        if final.exists():
            return
        raise FileNotFoundError(f"Neither prior backup nor untouched final exists: {final}")
    _remove_runtime_path(final)


def recover_incomplete_promotions(project_root: Path) -> list[str]:
    """Roll back interrupted two-directory promotions to the last-known-good state.

    Any leftover promotion journal means the previous process did not complete its
    cleanup. Recovery therefore prefers the prior backed-up directories over a
    possibly complete but uncommitted new run. Invalid journals are never deleted
    automatically because doing so could destroy the only remaining good copy.
    """
    root = project_root.resolve()
    backups_parent = root / ".ev_fleet_backups"
    if not backups_parent.exists():
        return []
    recovered: list[str] = []
    for backup_root in sorted(backups_parent.iterdir()):
        if not backup_root.is_dir() or backup_root.is_symlink():
            continue
        journal_path = backup_root / "promotion.json"
        if not journal_path.exists():
            substantive_entries = [
                path
                for path in backup_root.iterdir()
                if path.name not in {"promotion.json"} and not path.name.endswith(".tmp")
            ]
            if not substantive_entries:
                shutil.rmtree(backup_root)
                recovered.append(backup_root.name)
                continue
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            if journal.get("version") != 1:
                raise ValueError(f"Unsupported promotion journal version: {journal.get('version')}")
            records = journal["pairs"]
        except (FileNotFoundError, OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Incomplete promotion has no valid recovery journal: {backup_root}. Preserved for manual recovery."
            ) from exc
        if not isinstance(records, list) or not records:
            raise RuntimeError(f"Invalid empty promotion journal preserved at: {backup_root}")

        resolved_records: list[tuple[Path, Path, bool]] = []
        for record in records:
            if not isinstance(record, dict):
                raise RuntimeError(f"Invalid promotion journal record preserved at: {backup_root}")
            final = _safe_relative_runtime_path(root, str(record.get("final", "")))
            backup = _safe_relative_runtime_path(root, str(record.get("backup", "")))
            if backup_root.resolve() not in backup.parents:
                raise RuntimeError(f"Promotion backup path is outside its journal directory: {backup}")
            had_final = record.get("had_final")
            if type(had_final) is not bool:
                raise RuntimeError(f"Promotion journal had_final must be boolean: {backup_root}")
            resolved_records.append((final, backup, had_final))

        restore_errors: list[str] = []
        for final, backup, had_final in reversed(resolved_records):
            try:
                _restore_previous_runtime_path(final, backup, had_final)
            except Exception as exc:  # Preserve every remaining backup for manual recovery.
                restore_errors.append(f"{final}: {exc}")
        if restore_errors:
            raise RuntimeError(
                "Interrupted promotion rollback was incomplete; backups were preserved at "
                f"{backup_root}: {restore_errors}"
            )
        shutil.rmtree(backup_root)
        recovered.append(backup_root.name)

    try:
        backups_parent.rmdir()
    except OSError:
        pass
    return recovered


def _promote_runtime_directories(staged_cfg: ProjectConfig, final_cfg: ProjectConfig, token: str) -> None:
    backup_root = final_cfg.project_root / ".ev_fleet_backups" / token
    backup_root.mkdir(parents=True, exist_ok=False)
    pairs = (
        (staged_cfg.generated_dir, final_cfg.generated_dir, backup_root / "generated"),
        (staged_cfg.artifacts_dir, final_cfg.artifacts_dir, backup_root / "artifacts"),
    )
    journal_pairs = [
        {
            "final": final.relative_to(final_cfg.project_root).as_posix(),
            "backup": backup.relative_to(final_cfg.project_root).as_posix(),
            "had_final": final.exists(),
        }
        for _, final, backup in pairs
    ]
    journal_path = backup_root / "promotion.json"
    try:
        write_json_atomic(
            journal_path,
            {
                "version": 1,
                "token": token,
                "created_at_epoch": time.time(),
                "pairs": journal_pairs,
            },
        )
    except Exception:
        shutil.rmtree(backup_root, ignore_errors=True)
        try:
            backup_root.parent.rmdir()
        except OSError:
            pass
        raise

    promoted: list[Path] = []
    cleanup_backup = False
    try:
        for staged, final, backup in pairs:
            final.parent.mkdir(parents=True, exist_ok=True)
            if final.exists():
                os.replace(final, backup)
            os.replace(staged, final)
            promoted.append(final)
        cleanup_backup = True
    except Exception as promotion_error:
        restore_errors: list[str] = []
        for final in reversed(promoted):
            try:
                _remove_runtime_path(final)
            except Exception as exc:
                restore_errors.append(f"remove promoted {final}: {exc}")
        for record, (_, final, backup) in zip(journal_pairs, pairs, strict=True):
            try:
                _restore_previous_runtime_path(final, backup, bool(record["had_final"]))
            except Exception as exc:
                restore_errors.append(f"restore {final}: {exc}")
        if restore_errors:
            raise RuntimeError(
                "Runtime promotion failed and rollback was incomplete. "
                f"Backups were preserved at {backup_root}: {restore_errors}"
            ) from promotion_error
        cleanup_backup = True
        raise
    finally:
        if cleanup_backup:
            shutil.rmtree(backup_root, ignore_errors=True)
            backup_parent = backup_root.parent
            try:
                backup_parent.rmdir()
            except OSError:
                pass


def _cleanup_stale_staging(project_root: Path, *, stale_seconds: float = 86_400.0) -> None:
    parent = project_root / ".ev_fleet_staging"
    if not parent.exists():
        return
    now = time.time()
    for directory in parent.iterdir():
        if not directory.is_dir() or directory.is_symlink():
            continue
        owner_path = directory / "owner.json"
        try:
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            owner = {}
        try:
            age = now - float(owner.get("started_at_epoch", directory.stat().st_mtime))
        except FileNotFoundError:
            continue
        same_host = owner.get("host") == socket.gethostname()
        active = same_host and _process_is_alive(int(owner.get("pid", -1)))
        invalid_grace_seconds = min(5.0, stale_seconds)
        removable = (same_host and not active) or (not owner and age > invalid_grace_seconds) or age > stale_seconds
        if removable:
            shutil.rmtree(directory, ignore_errors=True)
    try:
        parent.rmdir()
    except OSError:
        pass


@contextmanager
def staged_pipeline_config(cfg: ProjectConfig):
    """Build a complete run off-line, validate it, then promote it transactionally.

    A handled failure leaves the prior generated data and artifacts untouched. During
    the brief two-directory promotion window, generated-data hash verification keeps
    the API unready rather than serving mismatched plans and inputs.
    """
    _cleanup_stale_staging(cfg.project_root)
    token = uuid.uuid4().hex
    staging_root = cfg.project_root / ".ev_fleet_staging" / token
    staged_generated = staging_root / "generated"
    staged_artifacts = staging_root / "artifacts"
    staged_generated.mkdir(parents=True, exist_ok=False)
    staged_artifacts.mkdir(parents=True, exist_ok=False)
    (staging_root / "owner.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "started_at_epoch": time.time(),
                "config": str(cfg.source_path),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    raw = copy.deepcopy(cfg.raw)
    raw["paths"] = dict(raw["paths"])
    raw["paths"]["generated_data"] = str(staged_generated)
    raw["paths"]["artifacts"] = str(staged_artifacts)
    raw["paths"]["database"] = str(staged_artifacts / cfg.database_path.name)
    raw.setdefault("_runtime", {})["run_id"] = token
    if cfg.public_calibration_dir is not None:
        raw["paths"]["public_calibration"] = str(cfg.public_calibration_dir)
    staged_cfg = ProjectConfig(raw=raw, source_path=cfg.source_path, project_root=cfg.project_root)

    try:
        yield staged_cfg
        _rewrite_staged_text_files(staged_cfg, cfg)

        from .manifest import build_artifact_manifest, build_generated_data_manifest
        from .validation import validate_repository_outputs

        build_generated_data_manifest(
            staged_cfg.generated_dir, staged_cfg.artifacts_dir / "generated_data_manifest.json"
        )
        build_artifact_manifest(staged_cfg.artifacts_dir)
        validate_repository_outputs(staged_cfg)
        _promote_runtime_directories(staged_cfg, cfg, token)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
        staging_parent = staging_root.parent
        try:
            staging_parent.rmdir()
        except OSError:
            pass


def assert_safe_runtime_directory(
    path: Path,
    project_root: Path,
    *,
    allow_generated_data_path: bool = False,
) -> None:
    resolved = path.resolve()
    root = project_root.resolve()
    if resolved == Path(resolved.anchor):
        raise ValueError(f"Refusing to modify filesystem root: {resolved}")
    if resolved == root:
        raise ValueError(f"Refusing to modify project root: {resolved}")
    if root not in resolved.parents:
        raise ValueError(f"Refusing to modify path outside project root: {resolved}")

    protected_names = {
        ".git",
        ".github",
        "src",
        "tests",
        "configs",
        "docs",
        "scripts",
        "dashboard",
        "reference_results",
        "constraints",
    }
    for protected in (root / name for name in protected_names):
        protected = protected.resolve()
        if resolved == protected or protected in resolved.parents:
            raise ValueError(f"Refusing to modify protected source/evidence path: {resolved}")

    data_root = (root / "data").resolve()
    if resolved == data_root:
        raise ValueError(f"Refusing to modify protected data root: {resolved}")
    if data_root in resolved.parents:
        if not allow_generated_data_path or not resolved.name.startswith("generated"):
            raise ValueError(
                f"Only explicitly generated-data directories named generated* may be modified under data/: {resolved}"
            )


def clean_directory(root: str | Path, *, preserve: tuple[str, ...] = (".gitkeep",)) -> None:
    directory = Path(root)
    directory.mkdir(parents=True, exist_ok=True)
    for path in directory.iterdir():
        if path.name in preserve:
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)


def clean_runtime(cfg: ProjectConfig) -> dict[str, str | list[str]]:
    assert_safe_runtime_directory(cfg.artifacts_dir, cfg.project_root)
    assert_safe_runtime_directory(cfg.generated_dir, cfg.project_root, allow_generated_data_path=True)
    with pipeline_run_lock(cfg):
        recovered = recover_incomplete_promotions(cfg.project_root)
        clean_directory(cfg.artifacts_dir)
        clean_directory(cfg.generated_dir)
    return {
        "status": "CLEAN",
        "artifacts": str(cfg.artifacts_dir),
        "generated_data": str(cfg.generated_dir),
        "recovered_promotions": recovered,
    }
