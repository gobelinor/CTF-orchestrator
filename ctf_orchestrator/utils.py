from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def nullable_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def nullable_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_env_file_arg(argv: list[str]) -> Path | None:
    default_env_file = Path(".env")
    if "--env-file" in argv:
        index = argv.index("--env-file")
        if index + 1 >= len(argv):
            raise SystemExit("--env-file requires a path value.")
        return Path(argv[index + 1]).expanduser().resolve()
    for item in argv:
        if item.startswith("--env-file="):
            _, value = item.split("=", 1)
            if not value:
                raise SystemExit("--env-file requires a path value.")
            return Path(value).expanduser().resolve()
    if default_env_file.exists():
        return default_env_file.resolve()
    return None


def load_env_file(path: Path | None) -> None:
    if path is None:
        return
    if not path.exists():
        raise SystemExit(f"env file not found: {path}")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        os.environ.setdefault(key, _parse_env_value(value.strip()))


def _parse_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
