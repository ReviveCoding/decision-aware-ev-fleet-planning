from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, Path):
        return str(value)
    return value


def write_json_atomic(path: str | Path, payload: Any) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    safe_payload = _json_safe(payload)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=target.parent, delete=False, suffix=".tmp"
        ) as handle:
            json.dump(safe_payload, handle, indent=2, allow_nan=False)
            handle.write("\n")
            temp_path = Path(handle.name)
        os.replace(temp_path, target)
        return target
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def write_csv_atomic(path: str | Path, frame: pd.DataFrame) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=target.parent, delete=False, suffix=".tmp", newline=""
        ) as handle:
            frame.to_csv(handle, index=False)
            temp_path = Path(handle.name)
        os.replace(temp_path, target)
        return target
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def write_text_atomic(path: str | Path, content: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=target.parent, delete=False, suffix=".tmp"
        ) as handle:
            handle.write(content)
            if content and not content.endswith("\n"):
                handle.write("\n")
            temp_path = Path(handle.name)
        os.replace(temp_path, target)
        return target
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
