from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COMMANDS = [
    [sys.executable, "-m", "pytest", "-q"],
    [sys.executable, "-m", "ruff", "check", "."],
    [sys.executable, "-m", "ruff", "format", "--check", "."],
    [sys.executable, "-m", "compileall", "-q", "src", "tests", "scripts", "dashboard"],
    [sys.executable, "-m", "ev_fleet", "--help"],
    [sys.executable, "-m", "ev_fleet", "--version"],
    [sys.executable, "-m", "ev_fleet", "run", "--config", "configs/smoke.yaml"],
    [sys.executable, "-m", "ev_fleet", "validate", "--config", "configs/smoke.yaml"],
]
FORBIDDEN_PATTERNS = (
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".ev_fleet_locks",
    ".ev_fleet_staging",
    ".ev_fleet_backups",
)
FORBIDDEN_SUFFIXES = (".pyc", ".pyo", ".sqlite", ".pt")


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    env.setdefault("OMP_NUM_THREADS", "1")
    env.setdefault("OPENBLAS_NUM_THREADS", "1")
    env.setdefault("MKL_NUM_THREADS", "1")
    env.setdefault("PYTHONUTF8", "1")
    return env


def _run(command: list[str], timeout: int) -> dict[str, object]:
    start = time.monotonic()
    proc = subprocess.run(command, cwd=ROOT, env=_env(), text=True, capture_output=True, timeout=timeout)
    return {
        "command": command,
        "exit_code": proc.returncode,
        "duration_seconds": round(time.monotonic() - start, 3),
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-2000:],
    }


def _clean_transient() -> None:
    for name in (".pytest_cache", ".ruff_cache", ".ev_fleet_locks", ".ev_fleet_staging", ".ev_fleet_backups"):
        target = ROOT / name
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
    for pattern in ("**/__pycache__",):
        for target in ROOT.glob(pattern):
            shutil.rmtree(target, ignore_errors=True)
    for suffix in ("*.pyc", "*.pyo"):
        for target in ROOT.rglob(suffix):
            target.unlink(missing_ok=True)


def _clean_runtime_outputs() -> None:
    """Remove runtime outputs created by qualification while keeping canonical empty roots."""
    runtime_roots = [ROOT / "artifacts", ROOT / "data" / "generated"]
    # Include profile-specific roots without touching source/evidence directories.
    runtime_roots.extend(sorted(ROOT.glob("artifacts-*")))
    runtime_roots.extend(sorted((ROOT / "data").glob("generated-*")) if (ROOT / "data").exists() else [])
    for directory in runtime_roots:
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ".gitkeep").write_text("", encoding="utf-8")


def _residue() -> list[str]:
    findings: list[str] = []
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT).as_posix()
        if any(part in FORBIDDEN_PATTERNS for part in path.parts):
            findings.append(rel)
        elif path.is_file() and path.suffix in FORBIDDEN_SUFFIXES:
            if not rel.startswith("artifacts/") and not rel.startswith("data/generated/"):
                findings.append(rel)
    return sorted(set(findings))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the canonical local qualification gate.")
    parser.add_argument("--timeout-seconds", type=int, default=240)
    parser.add_argument("--output", default="qualification_local_summary.json")
    parser.add_argument("--skip-e2e", action="store_true")
    parser.add_argument(
        "--preserve-runtime-outputs",
        action="store_true",
        help="Keep artifacts/ and data/generated/ outputs after qualification for manual inspection.",
    )
    args = parser.parse_args()

    commands = list(DEFAULT_COMMANDS)
    if args.skip_e2e:
        # Keep all static/interface checks, including --version, while skipping
        # only the E2E smoke run and validation commands.
        commands = commands[:6]

    _clean_transient()
    summary = {
        "schema_version": "1.0",
        "project_root": str(ROOT),
        "python": sys.version,
        "commands": [],
        "residue_before": _residue(),
        "result": "PASS",
    }
    if summary["residue_before"]:
        summary["result"] = "FAIL"
        Path(args.output).write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary, indent=2))
        return 2

    for command in commands:
        if shutil.which(command[0]) is None and not Path(command[0]).exists():
            result = {"command": command, "exit_code": 127, "duration_seconds": 0.0, "stderr_tail": "command not found"}
        else:
            result = _run(command, args.timeout_seconds)
        summary["commands"].append(result)
        if result["exit_code"] != 0:
            summary["result"] = "FAIL"
            break

    if not args.preserve_runtime_outputs:
        _clean_runtime_outputs()
    _clean_transient()
    summary["residue_after"] = _residue()
    summary["runtime_outputs_preserved"] = bool(args.preserve_runtime_outputs)
    if summary["residue_after"]:
        # Test runs may create runtime outputs under artifacts/data, but cache/pyc/lock files should not remain in the source tree.
        summary["result"] = "FAIL"
    Path(args.output).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if summary["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
