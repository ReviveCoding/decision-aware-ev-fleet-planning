from __future__ import annotations

import argparse
from pathlib import Path

from ev_fleet.config import load_config
from ev_fleet.io_utils import write_csv_atomic
from ev_fleet.maintenance import pipeline_run_lock, recover_incomplete_promotions
from ev_fleet.public_data import (
    ingest_amazon_route_package_files,
    inspect_nlr_charging_profile_archive,
    read_nlr_operations_workbook,
)


def main():
    parser = argparse.ArgumentParser(description="Optional public-data calibration adapters")
    parser.add_argument("--amazon-route-data")
    parser.add_argument("--amazon-package-data")
    parser.add_argument("--nlr-operations-workbook")
    parser.add_argument("--nlr-charging-archive")
    parser.add_argument("--config", default="configs/smoke.yaml")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    has_amazon_route = bool(args.amazon_route_data)
    has_amazon_package = bool(args.amazon_package_data)
    if has_amazon_route != has_amazon_package:
        parser.error("--amazon-route-data and --amazon-package-data must be provided together")
    if not any((has_amazon_route, args.nlr_operations_workbook, args.nlr_charging_archive)):
        parser.error("at least one public-data source must be provided")

    cfg = load_config(args.config)
    output = Path(args.output).expanduser().resolve() if args.output else cfg.public_calibration_dir
    if output is None:
        raise ValueError("No public calibration output is configured")

    def ingest() -> None:
        output.mkdir(parents=True, exist_ok=True)
        if args.amazon_route_data and args.amazon_package_data:
            frame = ingest_amazon_route_package_files(args.amazon_route_data, args.amazon_package_data)
            write_csv_atomic(output / "amazon_route_profiles.csv", frame)
            print(f"Wrote {len(frame)} Amazon route profiles")
        if args.nlr_operations_workbook:
            sheets = read_nlr_operations_workbook(args.nlr_operations_workbook)
            for name, frame in sheets.items():
                safe = "".join(ch if ch.isalnum() else "_" for ch in name).strip("_").lower()
                write_csv_atomic(output / f"nlr_operations_{safe}.csv", frame)
            print(f"Wrote {len(sheets)} NLR workbook sheets")
        if args.nlr_charging_archive:
            inventory = inspect_nlr_charging_profile_archive(args.nlr_charging_archive)
            write_csv_atomic(output / "nlr_charging_archive_inventory.csv", inventory)
            print(f"Indexed {len(inventory)} NLR charging resources")

    if output.resolve() == cfg.public_calibration_dir:
        with pipeline_run_lock(cfg):
            recover_incomplete_promotions(cfg.project_root)
            ingest()
    else:
        ingest()


if __name__ == "__main__":
    main()
