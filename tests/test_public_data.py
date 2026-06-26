from __future__ import annotations

import copy
import json

import pandas as pd

from ev_fleet.config import ProjectConfig
from ev_fleet.data_generation import generate_public_informed_proxy_data
from ev_fleet.public_data import ingest_amazon_route_package_files


def test_amazon_public_adapter(tmp_path):
    routes = {
        "R1": {
            "station_code": "D01",
            "date_YYYY_MM_DD": "2018-01-01",
            "departure_time_utc": "08:00:00",
            "executor_capacity_cm3": 100000,
            "route_score": "High",
            "stops": {"AA": {"type": "Station"}, "AB": {"type": "Dropoff"}},
        }
    }
    packages = {
        "R1": {
            "AB": {
                "P1": {
                    "planned_service_time_seconds": 30,
                    "dimensions": {"depth_cm": 10, "height_cm": 5, "width_cm": 4},
                    "time_window": {"start_time_utc": None, "end_time_utc": None},
                }
            }
        }
    }
    route_path = tmp_path / "route_data.json"
    package_path = tmp_path / "package_data.json"
    route_path.write_text(json.dumps(routes), encoding="utf-8")
    package_path.write_text(json.dumps(packages), encoding="utf-8")
    frame = ingest_amazon_route_package_files(route_path, package_path)
    assert frame.loc[0, "package_count"] == 1
    assert frame.loc[0, "package_cube_cm3"] == 200
    assert frame.loc[0, "dropoff_count"] == 1


def test_amazon_station_workload_calibration_is_consumed(cfg, tmp_path):
    calibration = tmp_path / "public_calibration"
    calibration.mkdir()
    pd.DataFrame(
        [
            {"station_id": "PUBLIC_A", "package_count": 20, "stop_count": 10},
            {"station_id": "PUBLIC_A", "package_count": 24, "stop_count": 12},
            {"station_id": "PUBLIC_B", "package_count": 120, "stop_count": 60},
            {"station_id": "PUBLIC_B", "package_count": 130, "stop_count": 65},
        ]
    ).to_csv(calibration / "amazon_route_profiles.csv", index=False)
    raw = copy.deepcopy(cfg.raw)
    raw["paths"] = {
        "generated_data": str(tmp_path / "generated"),
        "artifacts": str(tmp_path / "artifacts"),
        "database": str(tmp_path / "artifacts" / "fleet.sqlite"),
        "public_calibration": str(calibration),
    }
    raw["history"]["stations"] = 2
    isolated = ProjectConfig(raw=raw, source_path=cfg.source_path, project_root=tmp_path)
    generate_public_informed_proxy_data(isolated)
    provenance = json.loads((isolated.generated_dir / "data_provenance.json").read_text(encoding="utf-8"))
    factors = provenance["public_calibration"]["factors"]
    assert provenance["public_calibration"]["status"] == "APPLIED"
    assert len(factors) == 2
    assert factors[0] < factors[1]
    history = pd.read_csv(isolated.generated_dir / "station_day_history.csv")
    means = history.groupby("station_id")["route_count"].mean().sort_values()
    assert means.iloc[-1] > means.iloc[0]


def test_public_data_cli_rejects_noop_and_partial_amazon_inputs(cfg):
    import os
    import subprocess
    import sys

    environment = os.environ.copy()
    source_root = str(cfg.project_root / "src")
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_root if not existing_pythonpath else source_root + os.pathsep + existing_pythonpath
    )
    script = cfg.project_root / "scripts" / "ingest_public_data.py"

    no_source = subprocess.run(
        [sys.executable, str(script), "--config", str(cfg.source_path)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert no_source.returncode == 2
    assert "at least one public-data source must be provided" in no_source.stderr

    partial_amazon = subprocess.run(
        [
            sys.executable,
            str(script),
            "--config",
            str(cfg.source_path),
            "--amazon-route-data",
            "route_data.json",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert partial_amazon.returncode == 2
    assert "must be provided together" in partial_amazon.stderr
