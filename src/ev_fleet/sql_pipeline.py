from __future__ import annotations

import sqlite3
from importlib.resources import files
from pathlib import Path

import pandas as pd

from .config import ProjectConfig

_ALLOWED_MARTS = {
    "int_station_features",
    "mart_station_day_features",
    "mart_station_capacity",
    "reconciliation_fleet",
}


def _sql_files() -> list[Path]:
    root = files("ev_fleet").joinpath("sql")
    paths: list[Path] = []
    for section in ("01_intermediate", "02_marts", "03_reconciliations"):
        section_root = root.joinpath(section)
        for item in section_root.iterdir():
            if item.name.endswith(".sql"):
                paths.append(Path(str(item)))
    return sorted(paths)


def build_sql_marts(cfg: ProjectConfig) -> Path:
    database_path = cfg.database_path
    database_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = database_path.with_suffix(database_path.suffix + ".tmp")
    if temporary_path.exists():
        temporary_path.unlink()
    connection = sqlite3.connect(temporary_path)
    try:
        mapping = {
            "stg_station_day": cfg.generated_dir / "station_day_history.csv",
            "stg_stations": cfg.generated_dir / "stations.csv",
            "stg_initial_fleet": cfg.generated_dir / "initial_fleet.csv",
            "stg_vehicle_specs": cfg.generated_dir / "vehicle_specs.csv",
        }
        for table, path in mapping.items():
            pd.read_csv(path).to_sql(table, connection, index=False, if_exists="replace")
        for sql_file in _sql_files():
            connection.executescript(sql_file.read_text(encoding="utf-8"))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    temporary_path.replace(database_path)
    return database_path


def read_mart(cfg: ProjectConfig, table: str) -> pd.DataFrame:
    if table not in _ALLOWED_MARTS:
        raise ValueError(f"Unsupported mart: {table}")
    connection = sqlite3.connect(cfg.database_path)
    try:
        return pd.read_sql_query(f'SELECT * FROM "{table}"', connection)
    finally:
        connection.close()
