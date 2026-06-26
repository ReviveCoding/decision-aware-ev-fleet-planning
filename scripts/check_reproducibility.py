from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def _run_isolated(config: Path, timeout_seconds: int) -> None:
    environment = os.environ.copy()
    for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        environment.setdefault(variable, "1")
    subprocess.run(
        [sys.executable, "-m", "ev_fleet", "run", "--config", str(config)],
        check=True,
        env=environment,
        timeout=timeout_seconds,
        stdout=subprocess.DEVNULL,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/smoke.yaml")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    config = Path(args.config).expanduser().resolve()
    if args.timeout_seconds < 1:
        raise ValueError("--timeout-seconds must be positive")

    from ev_fleet.config import load_config
    from ev_fleet.validation import semantic_artifact_hashes

    cfg = load_config(config)
    _run_isolated(config, args.timeout_seconds)
    first = semantic_artifact_hashes(cfg.artifacts_dir)
    _run_isolated(config, args.timeout_seconds)
    second = semantic_artifact_hashes(cfg.artifacts_dir)
    mismatches = sorted(key for key in first if first[key] != second[key])
    result = {"status": "PASS" if not mismatches else "FAIL", "checked": len(first), "mismatches": mismatches}
    print(json.dumps(result, indent=2))
    if mismatches:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
