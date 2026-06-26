from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .config import ProjectConfig
from .io_utils import sha256_file, write_csv_atomic, write_json_atomic

VEHICLE_SPECS = pd.DataFrame(
    [
        {
            "vehicle_type": "compact_ev",
            "cube_capacity": 120.0,
            "battery_kwh": 140.0,
            "usable_fraction": 0.80,
            "reserve_kwh": 15.0,
            "charge_power_kw": 80.0,
            "daily_fixed_cost": 95.0,
            "variable_cost_per_mile": 0.28,
        },
        {
            "vehicle_type": "delivery_ev",
            "cube_capacity": 180.0,
            "battery_kwh": 220.0,
            "usable_fraction": 0.80,
            "reserve_kwh": 20.0,
            "charge_power_kw": 120.0,
            "daily_fixed_cost": 135.0,
            "variable_cost_per_mile": 0.36,
        },
        {
            "vehicle_type": "large_ev",
            "cube_capacity": 260.0,
            "battery_kwh": 340.0,
            "usable_fraction": 0.82,
            "reserve_kwh": 30.0,
            "charge_power_kw": 200.0,
            "daily_fixed_cost": 190.0,
            "variable_cost_per_mile": 0.48,
        },
    ]
)


def _station_names(n: int) -> list[str]:
    return [f"STN_{idx:02d}" for idx in range(1, n + 1)]


def _public_station_workload_factors(cfg: ProjectConfig, station_count: int) -> tuple[np.ndarray, dict]:
    """Return dimensionless station-workload factors from optional public profiles.

    The calibration preserves only the cross-station workload distribution. It
    never maps public station identities to synthetic stations and never creates
    EV labels that are absent from the public source.
    """
    default = np.ones(station_count, dtype=float)
    directory = cfg.public_calibration_dir
    if directory is None:
        return default, {"status": "NOT_CONFIGURED"}
    path = directory / "amazon_route_profiles.csv"
    if not path.is_file():
        return default, {"status": "NOT_FOUND", "expected_path": str(path)}
    frame = pd.read_csv(path)
    required = {"station_id", "package_count", "stop_count"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"amazon_route_profiles.csv missing calibration columns: {missing}")
    package_count = pd.to_numeric(frame["package_count"], errors="coerce")
    stop_count = pd.to_numeric(frame["stop_count"], errors="coerce")
    valid = frame["station_id"].notna() & package_count.notna() & stop_count.notna()
    valid &= (package_count >= 0) & (stop_count > 0)
    if not valid.any():
        raise ValueError("amazon_route_profiles.csv has no valid station workload rows")
    workload = pd.DataFrame(
        {
            "station_id": frame.loc[valid, "station_id"].astype(str),
            "workload": package_count.loc[valid].to_numpy(float) + stop_count.loc[valid].to_numpy(float),
        }
    )
    station_workload = workload.groupby("station_id")["workload"].median()
    if station_workload.empty or float(station_workload.mean()) <= 0:
        raise ValueError("amazon_route_profiles.csv produced an invalid station workload distribution")
    observed = np.sort((station_workload / station_workload.mean()).to_numpy(float))
    quantiles = np.linspace(0.0, 1.0, station_count)
    factors = np.quantile(observed, quantiles)
    factors = np.clip(factors / factors.mean(), 0.65, 1.45)
    factors = factors / factors.mean()
    return factors, {
        "status": "APPLIED",
        "source": str(path),
        "sha256": sha256_file(path),
        "public_station_count": int(len(station_workload)),
        "synthetic_station_count": station_count,
        "method": "rank-preserving normalized median(package_count + stop_count)",
        "factors": [round(float(value), 6) for value in factors],
    }


def generate_public_informed_proxy_data(cfg: ProjectConfig) -> dict[str, Path]:
    """Create deterministic public-style proxy data.

    Public sources motivate workload and depot-operation distributions. Vehicle,
    charger, cost, failure, and dependence assumptions remain synthetic and are
    recorded explicitly in the provenance manifest.
    """
    rng = np.random.default_rng(cfg.seed)
    out = cfg.generated_dir
    history_cfg = cfg.section("history")
    days = int(history_cfg.get("days", 70))
    station_count = int(history_cfg.get("stations", 3))
    stations = _station_names(station_count)
    public_workload_factors, public_calibration = _public_station_workload_factors(cfg, station_count)

    station_rows: list[dict] = []
    vehicle_rows: list[dict] = []
    for i, station in enumerate(stations):
        charger_ports = 7 + 2 * (i % 3)
        charger_kw = 100.0 + 25.0 * (i % 2)
        site_power_kw = charger_ports * charger_kw * 0.72
        station_rows.append(
            {
                "station_id": station,
                "charger_ports": charger_ports,
                "charger_power_kw": charger_kw,
                "site_power_kw": site_power_kw,
                "mean_dwell_hours": 10.0 - 0.4 * (i % 3),
                "repair_capacity": 2 + (i % 2),
                "parking_capacity": 45 + 7 * i,
                "region": f"REGION_{1 + i // 3}",
            }
        )
        base_scale = 1.0 + 0.14 * i
        for _, spec in VEHICLE_SPECS.iterrows():
            type_factor = {"compact_ev": 0.28, "delivery_ev": 0.54, "large_ev": 0.18}[spec.vehicle_type]
            vehicle_rows.append(
                {
                    "station_id": station,
                    "vehicle_type": spec.vehicle_type,
                    "initial_count": int(max(2, round((24 + 3 * i) * type_factor * base_scale))),
                }
            )

    station_df = pd.DataFrame(station_rows)
    vehicle_df = pd.DataFrame(vehicle_rows)
    history_rows: list[dict] = []
    start = pd.Timestamp("2025-01-01")
    for day_index in range(days):
        date = start + pd.Timedelta(days=day_index)
        dow = date.dayofweek
        weekly = 1.0 + 0.16 * np.sin(2 * np.pi * dow / 7.0)
        trend = 1.0 + 0.0015 * day_index
        seasonal_peak = 1.18 if date.month in (11, 12) else 1.0
        network_event = rng.random() < 0.035
        for station_index, station in enumerate(stations):
            station_level = (13.5 + 2.2 * station_index) * float(public_workload_factors[station_index])
            local_event = network_event or rng.random() < 0.015
            event_multiplier = 1.25 if local_event else 1.0
            mu = station_level * weekly * trend * seasonal_peak * event_multiplier
            dispersion = 4.0
            probability = dispersion / (dispersion + mu)
            route_count = int(max(3, rng.negative_binomial(dispersion, probability)))
            avg_cube = float(np.clip(rng.normal(150 + 4 * station_index, 14), 105, 210))
            avg_distance = float(np.clip(rng.lognormal(np.log(68 + 3 * station_index), 0.22), 30, 150))
            cold_regime = date.month in (1, 2, 12)
            route_energy = avg_distance * rng.normal(1.08, 0.06) * (1.10 if cold_regime else 1.0)
            history_rows.append(
                {
                    "date": date.date().isoformat(),
                    "station_id": station,
                    "route_count": route_count,
                    "avg_cube": round(avg_cube, 3),
                    "avg_distance_miles": round(avg_distance, 3),
                    "avg_route_energy_kwh": round(route_energy, 3),
                    "cold_regime": int(cold_regime),
                    "capacity_event": int(local_event),
                }
            )
    history_df = pd.DataFrame(history_rows)

    paths = {
        "vehicle_specs": out / "vehicle_specs.csv",
        "stations": out / "stations.csv",
        "initial_fleet": out / "initial_fleet.csv",
        "history": out / "station_day_history.csv",
        "provenance": out / "data_provenance.json",
    }
    write_csv_atomic(paths["vehicle_specs"], VEHICLE_SPECS)
    write_csv_atomic(paths["stations"], station_df)
    write_csv_atomic(paths["initial_fleet"], vehicle_df)
    write_csv_atomic(paths["history"], history_df)

    provenance = {
        "claim_boundary": {
            "public_distribution_inspiration": [
                "Amazon Last Mile route/stop/package workload structure",
                "NLR depot-based daily-distance and dwell-time summaries",
                "NLR 15-minute charging-profile shape references",
            ],
            "synthetic_assumptions": [
                "vehicle specifications",
                "station charger counts, parking, and site power",
                "repair/failure probabilities",
                "costs and penalties",
                "joint dependence between public marginal distributions",
            ],
            "not_claimed": [
                "actual Amazon EV fleet records",
                "actual Amazon charging infrastructure",
                "production DSP allocations",
                "deployed business impact",
            ],
        },
        "seed": cfg.seed,
        "stations": station_count,
        "history_days": days,
        "generator_version": "2.0",
        "public_calibration": public_calibration,
    }
    write_json_atomic(paths["provenance"], provenance)
    return paths


def _sample_regime(rng: np.random.Generator) -> dict[str, float | int | str]:
    draw = rng.random()
    if draw < 0.08:
        return {
            "regime": "compound_stress",
            "demand_multiplier": float(rng.uniform(1.22, 1.42)),
            "energy_multiplier": float(rng.uniform(1.10, 1.20)),
            "charger_capacity_factor": float(rng.uniform(0.50, 0.70)),
            "failure_multiplier": float(rng.uniform(1.5, 2.2)),
            "return_delay_minutes": float(rng.uniform(35, 75)),
            "cold_regime": 1,
        }
    if draw < 0.24:
        return {
            "regime": "cold_weather",
            "demand_multiplier": float(rng.uniform(0.95, 1.12)),
            "energy_multiplier": float(rng.uniform(1.10, 1.18)),
            "charger_capacity_factor": 1.0,
            "failure_multiplier": float(rng.uniform(1.0, 1.3)),
            "return_delay_minutes": float(rng.uniform(5, 30)),
            "cold_regime": 1,
        }
    if draw < 0.38:
        return {
            "regime": "demand_surge",
            "demand_multiplier": float(rng.uniform(1.16, 1.35)),
            "energy_multiplier": float(rng.uniform(1.00, 1.08)),
            "charger_capacity_factor": float(rng.uniform(0.90, 1.0)),
            "failure_multiplier": 1.0,
            "return_delay_minutes": float(rng.uniform(15, 45)),
            "cold_regime": 0,
        }
    if draw < 0.48:
        return {
            "regime": "charger_outage",
            "demand_multiplier": float(rng.uniform(0.95, 1.10)),
            "energy_multiplier": float(rng.uniform(1.00, 1.07)),
            "charger_capacity_factor": float(rng.uniform(0.50, 0.72)),
            "failure_multiplier": float(rng.uniform(1.0, 1.2)),
            "return_delay_minutes": float(rng.uniform(5, 25)),
            "cold_regime": 0,
        }
    return {
        "regime": "normal",
        "demand_multiplier": float(np.clip(rng.normal(1.0, 0.08), 0.78, 1.18)),
        "energy_multiplier": float(np.clip(rng.normal(1.0, 0.03), 0.94, 1.08)),
        "charger_capacity_factor": 1.0,
        "failure_multiplier": 1.0,
        "return_delay_minutes": float(max(0.0, rng.normal(5.0, 8.0))),
        "cold_regime": 0,
    }


def build_route_scenarios(
    cfg: ProjectConfig,
    forecast_path: str | Path,
    scenario_count: int | None = None,
    seed_offset: int = 0,
    save_path: str | Path | None = None,
) -> pd.DataFrame:
    rng = np.random.default_rng(cfg.seed + 1000 + seed_offset)
    forecast = pd.read_csv(forecast_path)
    count = int(scenario_count or cfg.section("optimization").get("scenarios", 5))
    rows: list[dict] = []
    route_global = 0
    for scenario_id in range(count):
        scenario_weight = 1.0 / count
        regime = _sample_regime(rng)
        for _, forecast_row in forecast.iterrows():
            station_id = str(forecast_row["station_id"])
            p50 = float(forecast_row["p50"])
            p90 = float(forecast_row["p90"])
            uncertainty_scale = max(p90 - p50, 1.0)
            local_demand = float(regime["demand_multiplier"]) * float(np.clip(rng.normal(1.0, 0.04), 0.90, 1.10))
            route_count = int(max(1, round(p50 * local_demand + rng.normal(0, uncertainty_scale * 0.35))))
            for _ in range(route_count):
                route_global += 1
                cube = float(np.clip(rng.normal(154, 42), 75, 280))
                distance = float(np.clip(rng.lognormal(np.log(68), 0.28), 22, 165))
                payload_factor = 1.0 + max(cube - 150, 0) / 700
                energy = min(
                    distance * rng.normal(1.05, 0.07) * payload_factor * float(regime["energy_multiplier"]),
                    240.0,
                )
                rows.append(
                    {
                        "scenario_id": scenario_id,
                        "scenario_weight": scenario_weight,
                        "route_id": f"W{scenario_id:03d}_R{route_global:06d}",
                        "station_id": station_id,
                        "required_cube": round(cube, 3),
                        "distance_miles": round(distance, 3),
                        "energy_kwh": round(float(energy), 3),
                        "criticality": "critical" if rng.random() < 0.18 else "standard",
                        "regime": str(regime["regime"]),
                        "cold_regime": int(regime["cold_regime"]),
                        "charger_outage_regime": int(float(regime["charger_capacity_factor"]) < 0.95),
                        "charger_capacity_factor": round(float(regime["charger_capacity_factor"]), 4),
                        "vehicle_failure_multiplier": round(float(regime["failure_multiplier"]), 4),
                        "return_delay_minutes": round(float(regime["return_delay_minutes"]), 3),
                    }
                )
    scenario_df = pd.DataFrame(rows)
    target = Path(save_path) if save_path is not None else None
    if target is not None:
        write_csv_atomic(target, scenario_df)
    return scenario_df


def create_scenario_replication(
    cfg: ProjectConfig,
    forecast_path: str | Path,
    scenario_count: int,
    replication: int,
) -> pd.DataFrame:
    return build_route_scenarios(
        cfg,
        forecast_path,
        scenario_count=scenario_count,
        seed_offset=10_000 + scenario_count * 100 + replication,
        save_path=None,
    )
