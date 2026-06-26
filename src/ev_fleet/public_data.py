from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pandas as pd


def ingest_amazon_route_package_files(
    route_data_path: str | Path,
    package_data_path: str | Path,
) -> pd.DataFrame:
    """Parse official challenge route_data.json and package_data.json files.

    Output is an aggregated route profile suitable for distributional calibration.
    It intentionally does not create EV energy or charger labels that are absent
    from the public source.
    """
    routes = json.loads(Path(route_data_path).read_text(encoding="utf-8"))
    packages = json.loads(Path(package_data_path).read_text(encoding="utf-8"))
    rows = []
    for route_id, route in routes.items():
        route_packages = packages.get(route_id, {})
        package_count = 0
        service_seconds = 0.0
        cube_cm3 = 0.0
        time_window_packages = 0
        for stop_packages in route_packages.values():
            for package in stop_packages.values():
                package_count += 1
                service_seconds += float(package.get("planned_service_time_seconds") or 0.0)
                dims = package.get("dimensions") or {}
                depth = float(dims.get("depth_cm") or 0.0)
                height = float(dims.get("height_cm") or 0.0)
                width = float(dims.get("width_cm") or 0.0)
                cube_cm3 += depth * height * width
                tw = package.get("time_window") or {}
                time_window_packages += int(bool(tw.get("start_time_utc") or tw.get("end_time_utc")))
        stops = route.get("stops") or {}
        dropoffs = sum(1 for stop in stops.values() if stop.get("type") == "Dropoff")
        rows.append(
            {
                "route_id": route_id,
                "station_id": route.get("station_code"),
                "service_date": route.get("date_YYYY_MM_DD"),
                "departure_time_utc": route.get("departure_time_utc"),
                "executor_capacity_cm3": route.get("executor_capacity_cm3"),
                "route_score": route.get("route_score"),
                "stop_count": len(stops),
                "dropoff_count": dropoffs,
                "package_count": package_count,
                "planned_service_seconds": service_seconds,
                "package_cube_cm3": cube_cm3,
                "time_window_package_count": time_window_packages,
            }
        )
    return pd.DataFrame(rows)


def read_nlr_operations_workbook(path: str | Path) -> dict[str, pd.DataFrame]:
    """Read every sheet from the NLR MHDV operations summary workbook."""
    return pd.read_excel(path, sheet_name=None)


def inspect_nlr_charging_profile_archive(path: str | Path) -> pd.DataFrame:
    """Inventory CSV/XLSX resources in the NLR 15-minute profile archive."""
    records = []
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            suffix = Path(info.filename).suffix.lower()
            if suffix in {".csv", ".xlsx", ".xls"}:
                records.append(
                    {
                        "resource": info.filename,
                        "size_bytes": info.file_size,
                        "extension": suffix,
                    }
                )
    return pd.DataFrame(records)
