#!/usr/bin/env python3
"""Shared fail-closed helpers for the P02B SF10 sentinel."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set


HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
CPUSET_RE = re.compile(r"^[0-9,-]+$")


class GateError(RuntimeError):
    """A validation error that must keep the downstream gate closed."""


def sha256_file(path: Path) -> str:
    path = path.resolve()
    if not path.is_file():
        raise GateError("missing file for SHA-256: {}".format(path))
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GateError("cannot read JSON {}: {}".format(path, exc)) from exc
    if not isinstance(value, dict):
        raise GateError("{} must contain one JSON object".format(path))
    return value


def atomic_write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def parse_env_file(path: Path) -> Dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise GateError("cannot read {}: {}".format(path, exc)) from exc
    result: Dict[str, str] = {}
    for line_number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise GateError("{}:{} is not key=value".format(path, line_number))
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise GateError("{}:{} has invalid key {!r}".format(path, line_number, key))
        if key in result:
            raise GateError("{}:{} duplicates key {!r}".format(path, line_number, key))
        result[key] = value
    return result


def require_keys(value: Dict[str, Any], required: Iterable[str], context: str) -> None:
    missing = sorted(set(required) - set(value))
    if missing:
        raise GateError("{} is missing keys: {}".format(context, ", ".join(missing)))


def reject_unknown_keys(value: Dict[str, Any], allowed: Iterable[str], context: str) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise GateError("{} has unknown keys: {}".format(context, ", ".join(unknown)))


def require_hex64(value: Any, context: str) -> str:
    if not isinstance(value, str) or not HEX64_RE.fullmatch(value):
        raise GateError("{} must be a lowercase SHA-256 hex string".format(context))
    return value


def expand_cpuset(raw: str, context: str) -> Set[int]:
    if not isinstance(raw, str) or not raw or not CPUSET_RE.fullmatch(raw):
        raise GateError("{} has invalid cpuset syntax".format(context))
    cpus: Set[int] = set()
    for token in raw.split(","):
        if not token:
            raise GateError("{} contains an empty cpuset token".format(context))
        if "-" in token:
            if token.count("-") != 1:
                raise GateError("{} has invalid range {!r}".format(context, token))
            left_text, right_text = token.split("-", 1)
            left = int(left_text)
            right = int(right_text)
            if left > right:
                raise GateError("{} has descending range {!r}".format(context, token))
            cpus.update(range(left, right + 1))
        else:
            cpus.add(int(token))
    if not cpus:
        raise GateError("{} expands to no CPUs".format(context))
    return cpus


def ensure_new_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise GateError("run directory must be absolute: {}".format(path))
    if path.exists() and any(path.iterdir()):
        raise GateError("refusing non-empty run directory: {}".format(path))
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def resolved_existing_file(path: Path, context: str, executable: bool = False) -> Path:
    resolved = path.resolve()
    if not resolved.is_file():
        raise GateError("{} is not a file: {}".format(context, resolved))
    if executable and not os.access(str(resolved), os.X_OK):
        raise GateError("{} is not executable: {}".format(context, resolved))
    return resolved


def resolved_existing_dir(path: Path, context: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_dir():
        raise GateError("{} is not a directory: {}".format(context, resolved))
    return resolved


def file_ref(path: Path) -> Dict[str, Any]:
    resolved = resolved_existing_file(path, "artifact")
    return {
        "path": str(resolved),
        "size_bytes": resolved.stat().st_size,
        "sha256": sha256_file(resolved),
    }


def same_resolved_path(left: Any, right: Path) -> bool:
    if not isinstance(left, str) or not left:
        return False
    return Path(left).resolve() == right.resolve()
