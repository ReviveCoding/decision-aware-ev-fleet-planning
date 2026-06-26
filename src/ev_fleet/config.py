from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ProjectConfig:
    raw: dict[str, Any]
    source_path: Path
    project_root: Path

    @property
    def seed(self) -> int:
        return int(self.raw.get("seed", 42))

    def _resolve_path(self, value: str | Path) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else (self.project_root / path).resolve()

    @property
    def generated_dir(self) -> Path:
        return self._resolve_path(self.raw["paths"]["generated_data"])

    @property
    def artifacts_dir(self) -> Path:
        return self._resolve_path(self.raw["paths"]["artifacts"])

    @property
    def database_path(self) -> Path:
        return self._resolve_path(self.raw["paths"]["database"])

    @property
    def public_calibration_dir(self) -> Path | None:
        value = self.raw["paths"].get("public_calibration")
        return self._resolve_path(value) if value else None

    def section(self, name: str) -> dict[str, Any]:
        return dict(self.raw.get(name, {}))


def _require_number(
    section: dict[str, Any], key: str, *, minimum: float | None = None, maximum: float | None = None
) -> None:
    if key not in section:
        raise ValueError(f"Missing configuration value: {key}")
    raw_value = section[key]
    if type(raw_value) is bool:
        raise ValueError(f"Configuration value {key} must be numeric, not boolean")
    try:
        value = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Configuration value {key} must be numeric; got {raw_value!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"Configuration value {key} must be finite; got {value}")
    if minimum is not None and value < minimum:
        raise ValueError(f"Configuration value {key} must be >= {minimum}; got {value}")
    if maximum is not None and value > maximum:
        raise ValueError(f"Configuration value {key} must be <= {maximum}; got {value}")


def _require_integer(
    section: dict[str, Any], key: str, *, minimum: int | None = None, maximum: int | None = None
) -> int:
    _require_number(section, key, minimum=minimum, maximum=maximum)
    numeric = float(section[key])
    integer = int(numeric)
    if numeric != integer:
        raise ValueError(f"Configuration value {key} must be an integer; got {section[key]}")
    return integer


def _require_bool(section: dict[str, Any], key: str) -> bool:
    if key not in section:
        raise ValueError(f"Missing configuration value: {key}")
    value = section[key]
    if type(value) is not bool:
        raise ValueError(f"Configuration value {key} must be a YAML boolean; got {value!r}")
    return value


def _validate(raw: dict[str, Any]) -> None:
    required_sections = ("paths", "history", "forecast", "optimization", "simulation", "reliability", "saa")
    for required in required_sections:
        if required not in raw or not isinstance(raw[required], dict):
            raise ValueError(f"Missing configuration section: {required}")
    for key in ("generated_data", "artifacts", "database"):
        if key not in raw["paths"]:
            raise ValueError(f"Missing configuration path: {key}")
        if not isinstance(raw["paths"][key], (str, Path)) or not str(raw["paths"][key]).strip():
            raise ValueError(f"Configuration path {key} must be a non-empty path")

    seed_section = {"seed": raw.get("seed", 42)}
    _require_integer(seed_section, "seed", minimum=0, maximum=2**32 - 1)
    mode = raw.get("mode")
    if not isinstance(mode, str) or not mode.strip():
        raise ValueError("mode must be a non-empty string")

    history_days = _require_integer(raw["history"], "days", minimum=14)
    _require_integer(raw["history"], "stations", minimum=1)

    test_days = _require_integer(raw["forecast"], "test_days", minimum=2)
    if test_days >= history_days:
        raise ValueError("forecast.test_days must be smaller than history.days")
    calibration_days = _require_integer(raw["forecast"], "calibration_days", minimum=2)
    if calibration_days + test_days >= history_days:
        raise ValueError("forecast.calibration_days + forecast.test_days must be smaller than history.days")
    _require_number(raw["forecast"], "min_p90_coverage", minimum=0.0, maximum=1.0)
    _require_number(raw["forecast"], "p90_target_coverage", minimum=0.5, maximum=0.999)
    if "decision_reserve_quantile_weight" in raw["forecast"]:
        _require_number(
            raw["forecast"],
            "decision_reserve_quantile_weight",
            minimum=0.0,
            maximum=1.0,
        )
    for key in ("use_lightgbm", "use_torch_challenger"):
        if key in raw["forecast"]:
            _require_bool(raw["forecast"], key)
    for key, minimum in (("torch_epochs", 1), ("torch_refit_epochs", 1), ("sequence_length", 2)):
        if key in raw["forecast"]:
            _require_integer(raw["forecast"], key, minimum=minimum)

    optimization = raw["optimization"]
    for key in (
        "scenarios",
        "evaluation_scenarios",
        "value_evaluation_scenarios",
        "evpi_scenarios",
        "rolling_evaluation_scenarios",
    ):
        if key in optimization:
            _require_integer(optimization, key, minimum=1)
    _require_number(optimization, "cvar_alpha", minimum=0.5, maximum=0.999)
    _require_number(optimization, "risk_weight", minimum=0.0)
    _require_number(optimization, "mip_time_limit_seconds", minimum=0.1)
    if "mip_relative_gap" in optimization:
        _require_number(optimization, "mip_relative_gap", minimum=0.0, maximum=1.0)
    for key in (
        "emergency_lease_cost",
        "max_emergency_leases_per_station",
        "unserved_cost",
        "critical_multiplier",
        "charging_overage_cost",
        "redeployment_cost",
        "temporary_charging_capacity_cost_per_kwh",
        "route_split_program_cost",
    ):
        if key in optimization:
            _require_number(optimization, key, minimum=0.0)
    if "critical_multiplier" in optimization and float(optimization["critical_multiplier"]) < 1.0:
        raise ValueError("optimization.critical_multiplier must be >= 1.0")
    if "charge_efficiency" in optimization:
        _require_number(optimization, "charge_efficiency", minimum=0.000001, maximum=1.0)
    if "max_emergency_leases_per_station" in optimization:
        _require_integer(optimization, "max_emergency_leases_per_station", minimum=0)

    simulation = raw["simulation"]
    _require_integer(simulation, "runs", minimum=10)
    for key in (
        "failure_probability",
        "charger_outage_probability",
        "emergency_lease_fulfillment_probability",
        "same_day_repair_success_probability",
        "individual_charge_taper_factor",
    ):
        if key in simulation:
            _require_number(simulation, key, minimum=0.0, maximum=1.0)
    for key in (
        "energy_sigma",
        "late_return_sigma_minutes",
        "route_split_distance_overhead",
        "idle_vehicle_cost",
        "charge_efficiency_sigma",
    ):
        if key in simulation:
            _require_number(simulation, key, minimum=0.0)

    for key in ("charge_efficiency_mean", "minimum_charge_efficiency"):
        if key in simulation:
            _require_number(simulation, key, minimum=0.000001, maximum=1.0)
    if (
        "charge_efficiency_mean" in simulation
        and "minimum_charge_efficiency" in simulation
        and float(simulation["minimum_charge_efficiency"]) > float(simulation["charge_efficiency_mean"])
    ):
        raise ValueError("simulation.minimum_charge_efficiency must not exceed charge_efficiency_mean")

    for key in (
        "service_coverage_lcb",
        "critical_coverage_lcb",
        "energy_feasibility_lcb",
        "charging_feasibility_lcb",
    ):
        _require_number(raw["reliability"], key, minimum=0.0, maximum=1.0)
    simulation_runs = int(simulation["runs"])
    maximum_wilson_lcb = simulation_runs / (simulation_runs + 1.96**2)
    for key in ("energy_feasibility_lcb", "charging_feasibility_lcb"):
        target = float(raw["reliability"][key])
        if target > maximum_wilson_lcb + 1e-12:
            raise ValueError(
                f"reliability.{key}={target} is unattainable with simulation.runs="
                f"{simulation_runs}; maximum all-success Wilson LCB is {maximum_wilson_lcb:.6f}"
            )
    _require_number(raw["reliability"], "max_shortage_cvar", minimum=0.0)
    _require_number(raw["reliability"], "max_overcapacity_cost", minimum=0.0)
    _require_number(raw["reliability"], "max_churn", minimum=0.0)
    if "max_solver_mip_gap" in raw["reliability"]:
        _require_number(raw["reliability"], "max_solver_mip_gap", minimum=0.0, maximum=1.0)
    for key in (
        "max_solver_constraint_violation",
        "max_solver_integrality_violation",
        "max_solver_objective_error",
    ):
        if key in raw["reliability"]:
            _require_number(raw["reliability"], key, minimum=0.0)
    if "max_temporary_charging_capacity_kwh_per_station" in raw["reliability"]:
        _require_number(
            raw["reliability"],
            "max_temporary_charging_capacity_kwh_per_station",
            minimum=0.0,
        )
    if "corrective_charging_overage_quantile" in raw["reliability"]:
        _require_number(
            raw["reliability"],
            "corrective_charging_overage_quantile",
            minimum=0.000001,
            maximum=0.999999,
        )
    if "corrective_max_rounds" in raw["reliability"]:
        _require_integer(raw["reliability"], "corrective_max_rounds", minimum=1)
    quantiles = raw["reliability"].get("corrective_shortage_quantiles", [0.75, 0.90, 0.95])
    if not isinstance(quantiles, list) or not quantiles:
        raise ValueError("reliability.corrective_shortage_quantiles must be a non-empty list")
    if any(type(value) is bool for value in quantiles):
        raise ValueError("reliability.corrective_shortage_quantiles must be numeric, not boolean")
    try:
        numeric_quantiles = [float(value) for value in quantiles]
    except (TypeError, ValueError) as exc:
        raise ValueError("reliability.corrective_shortage_quantiles must be numeric") from exc
    if any(not math.isfinite(value) for value in numeric_quantiles):
        raise ValueError("reliability.corrective_shortage_quantiles must be finite")
    if any(value <= 0.0 or value >= 1.0 for value in numeric_quantiles):
        raise ValueError("reliability.corrective_shortage_quantiles must be strictly between 0 and 1")
    if numeric_quantiles != sorted(set(numeric_quantiles)):
        raise ValueError("reliability.corrective_shortage_quantiles must be unique and increasing")

    scenario_sizes = raw["saa"].get("scenario_sizes", [])
    if not isinstance(scenario_sizes, list) or not scenario_sizes:
        raise ValueError("saa.scenario_sizes must contain positive integers")
    for value in scenario_sizes:
        if type(value) is bool:
            raise ValueError("saa.scenario_sizes must contain positive integers, not booleans")
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("saa.scenario_sizes must contain positive integers") from exc
        if not math.isfinite(numeric) or numeric <= 0 or numeric != int(numeric):
            raise ValueError("saa.scenario_sizes must contain positive integers")
    _require_integer(raw["saa"], "replications", minimum=1)


def load_config(path: str | Path) -> ProjectConfig:
    source = Path(path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Configuration not found: {source}")
    with source.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise ValueError("Configuration must contain a YAML mapping")
    _validate(raw)

    configured_root = raw.get("project_root")
    if configured_root:
        root = Path(configured_root).expanduser()
        project_root = (source.parent / root).resolve() if not root.is_absolute() else root.resolve()
    elif source.parent.name == "configs":
        project_root = source.parent.parent.resolve()
    else:
        project_root = source.parent.resolve()

    cfg = ProjectConfig(raw=raw, source_path=source, project_root=project_root)
    generated = cfg.generated_dir.resolve()
    artifacts = cfg.artifacts_dir.resolve()
    database = cfg.database_path.resolve()
    if generated == cfg.project_root or artifacts == cfg.project_root:
        raise ValueError("generated_data and artifacts must not equal project_root")
    if cfg.project_root not in generated.parents or cfg.project_root not in artifacts.parents:
        raise ValueError("generated_data and artifacts must be located inside project_root")
    if generated == artifacts or generated in artifacts.parents or artifacts in generated.parents:
        raise ValueError("generated_data and artifacts must be separate, non-nested directories")
    if database != artifacts and artifacts not in database.parents:
        raise ValueError("paths.database must be located inside paths.artifacts")
    calibration = cfg.public_calibration_dir
    if calibration is not None:
        calibration = calibration.resolve()
        if calibration == generated or generated in calibration.parents or calibration in generated.parents:
            raise ValueError("public_calibration must be separate from generated_data")
        if calibration == artifacts or artifacts in calibration.parents or calibration in artifacts.parents:
            raise ValueError("public_calibration must be separate from artifacts")
    return cfg
