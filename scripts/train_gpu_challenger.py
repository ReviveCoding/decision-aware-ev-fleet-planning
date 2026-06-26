from __future__ import annotations

import argparse
import json

from ev_fleet.config import load_config
from ev_fleet.pipeline import run_pipeline


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/full.yaml")
    args = parser.parse_args()
    cfg = load_config(args.config)
    if cfg.section("forecast").get("use_torch_challenger") is not True:
        raise ValueError("GPU training requires forecast.use_torch_challenger: true")
    summary = run_pipeline(cfg)
    result = {
        "status": summary.get("gpu_challenger", {}).get("status", "FAILED"),
        "pipeline_status": summary.get("pipeline_status"),
        "selected_forecast_model": summary.get("selected_forecast_model"),
        "gpu_challenger": summary.get("gpu_challenger"),
        "artifacts_dir": summary.get("artifacts_dir"),
    }
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
