#!/usr/bin/env python3
"""Formal NebulaGraph cluster preflight/start/stop controller.

All lifecycle actions happen outside the timed P10 adapter.  The controller
only operates on three uniquely named containers and one network that are
fully described by a SHA-bound preflight receipt.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - formal execution is Linux-only
    fcntl = None  # type: ignore[assignment]

import nebula_adapter as adapter
import store_contract as store_contract
from p10_contract import ContractError, sha256_file


LAUNCH_SPEC_SCHEMA = "cidr-p10-nebulagraph-launch-spec-v1"
PREFLIGHT_SCHEMA = "cidr-p10-nebulagraph-preflight-v1"
START_SCHEMA = "cidr-p10-nebulagraph-start-receipt-v1"
STOP_SCHEMA = "cidr-p10-nebulagraph-stop-receipt-v1"
PARTIAL_START_SCHEMA = "cidr-p10-nebulagraph-partial-start-v1"
CLEANUP_SCHEMA = "cidr-p10-nebulagraph-cleanup-receipt-v1"
START_CLEANUP_SCHEMA = "cidr-p10-nebulagraph-start-cleanup-receipt-v1"
SEALED_ADMISSION_SCHEMA = "cidr-p10-nebulagraph-sealed-admission-v1"
EXPECTED_PARTITIONS = 64
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
ROLE_ORDER = ("metad", "storaged", "graphd")
START_ORDER = ("metad", "storaged", "graphd")
STOP_ORDER = ("graphd", "storaged", "metad")
RESOURCE_STATES = {"NOT_ATTEMPTED", "KNOWN", "UNCERTAIN"}


class _OverallDeadlineExceeded(ContractError):
    """Internal sentinel that must not be swallowed by the readiness retry loop."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def require_real_directory(path: Path, context: str, *, empty: bool = False) -> Path:
    store_contract.require(path.is_dir() and not path.is_symlink(), f"{context} must be a real directory")
    resolved = path.resolve()
    if empty:
        store_contract.require(not any(resolved.iterdir()), f"{context} must be empty")
    return resolved


def role_images(runtime: dict[str, Any]) -> dict[str, dict[str, str]]:
    images = store_contract.validate_images(runtime["images"], "runtime.images")
    return {item["role"]: item for item in images}


def _mount(source: Path, destination: str) -> dict[str, Any]:
    return {
        "type": "bind",
        "source": str(source.resolve()),
        "destination": destination,
        "read_only": False,
    }


def build_launch_spec(
    request: dict[str, Any],
    runtime: dict[str, Any],
    store_manifest: dict[str, Any],
    logs_root: Path,
    *,
    query_timeout_ms: int,
) -> dict[str, Any]:
    """Build the exact Docker and live-gate contract; perform no Docker I/O."""

    store_contract.require(request.get("execution_mode") == "formal", "launch spec requires formal request")
    store_contract.require(query_timeout_ms >= 100 and query_timeout_ms <= 10_000,
                           "query timeout must be in [100, 10000] ms")
    data_root = require_real_directory(Path(store_manifest["data_root"]["path"]), "store data root")
    logs = require_real_directory(logs_root, "formal logs root", empty=True)
    store_contract.assert_nonoverlapping({"data_root": data_root, "logs_root": logs})

    containers, logical_hosts, network = store_contract.validate_identity_names(
        store_manifest["containers"],
        store_manifest["logical_hosts"],
        store_manifest["network"],
        formal=True,
    )
    request_names = request["external_service"]["containers"]
    store_contract.require(
        request_names == [containers[role] for role in ("graphd", "metad", "storaged")],
        "request/store container identity drift",
    )
    images = role_images(runtime)
    graph_port = store_contract.integer(store_manifest["graph_endpoint"]["port"], "graph port", 1)
    store_contract.require(graph_port <= 65535, "graph port exceeds 65535")
    space = store_contract.nonempty(store_manifest["space"], "store space")
    labels = {
        "cidr.p10.owner": "nebulagraph-formal-cluster-v1",
        "cidr.p10.run_id": request["run_id"],
        "cidr.p10.repeat_index": str(request["repeat_index"]),
    }
    data_subdirs = {"metad": "meta", "storaged": "storage", "graphd": "graph"}
    destinations = {"metad": "/data/meta", "storaged": "/data/storage", "graphd": "/data/graph"}
    commands = {
        "metad": [
            f"--meta_server_addrs={logical_hosts['metad']}:9559",
            f"--local_ip={logical_hosts['metad']}",
            "--ws_ip=0.0.0.0",
            "--port=9559",
            "--ws_http_port=19559",
            "--data_path=/data/meta",
            "--log_dir=/logs",
        ],
        "storaged": [
            f"--meta_server_addrs={logical_hosts['metad']}:9559",
            f"--local_ip={logical_hosts['storaged']}",
            "--ws_ip=0.0.0.0",
            "--port=9779",
            "--ws_http_port=19779",
            "--data_path=/data/storage",
            "--log_dir=/logs",
        ],
        "graphd": [
            f"--meta_server_addrs={logical_hosts['metad']}:9559",
            f"--local_ip={logical_hosts['graphd']}",
            "--ws_ip=0.0.0.0",
            "--port=9669",
            "--ws_http_port=19669",
            "--log_dir=/logs",
        ],
    }
    roles: dict[str, Any] = {}
    for role in ROLE_ORDER:
        data_path = require_real_directory(data_root / data_subdirs[role], f"{role} data directory")
        log_path = logs / role
        store_contract.require(not log_path.exists() and not log_path.is_symlink(),
                               f"{role} log directory must not exist before start")
        mounts = [_mount(data_path, destinations[role]), _mount(log_path, "/logs")]
        port_bindings = (
            {"9669/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(graph_port)}]}
            if role == "graphd"
            else {}
        )
        roles[role] = {
            "container_name": containers[role],
            "logical_host": logical_hosts[role],
            "image": images[role],
            "mounts": mounts,
            "port_bindings": port_bindings,
            "command": commands[role],
        }
    # The role data roots are intentionally nested in data_root; only ensure logs are disjoint.
    for role in ROLE_ORDER:
        store_contract.require(not store_contract.paths_overlap(logs, Path(roles[role]["mounts"][0]["source"])),
                               f"logs root overlaps {role} data")
    return {
        "schema_version": LAUNCH_SPEC_SCHEMA,
        "run_id": request["run_id"],
        "repeat_index": request["repeat_index"],
        "network": {"name": network, "driver": "bridge"},
        "roles": roles,
        "labels": labels,
        "endpoint": {"host": "127.0.0.1", "port": graph_port},
        "space": space,
        "authentication": store_manifest["authentication"],
        "edge_labels": sorted(item["label"] for item in store_manifest["edge_type_labels"]),
        "expected_partition_count": EXPECTED_PARTITIONS,
        "query_timeout_ms": query_timeout_ms,
        "logs_root": str(logs),
        "client": runtime["client"]["tree"],
    }


def docker_run_argv(spec: dict[str, Any], role: str, preflight_sha256: str) -> list[str]:
    store_contract.exact_sha(preflight_sha256, "Docker run preflight SHA")
    item = spec["roles"][role]
    labels = {**spec["labels"], "cidr.p10.role": role, "cidr.p10.preflight_sha256": preflight_sha256}
    argv = [
        "run",
        "--detach",
        "--pull=never",
        "--name",
        item["container_name"],
        "--hostname",
        item["logical_host"],
        "--network",
        spec["network"]["name"],
        "--network-alias",
        item["logical_host"],
        "--restart=no",
    ]
    for key in sorted(labels):
        argv.extend(("--label", f"{key}={labels[key]}"))
    for mount in item["mounts"]:
        argv.extend(
            (
                "--mount",
                f"type=bind,src={mount['source']},dst={mount['destination']}",
            )
        )
    if role == "graphd":
        binding = item["port_bindings"]["9669/tcp"][0]
        argv.extend(("--publish", f"{binding['HostIp']}:{binding['HostPort']}:9669/tcp"))
    argv.append(item["image"]["repo_digest"])
    argv.extend(item["command"])
    return argv


def _normal_port_bindings(value: object) -> dict[str, list[dict[str, str]]]:
    if value in (None, {}):
        return {}
    store_contract.require(isinstance(value, dict), "container PortBindings is malformed")
    normalized: dict[str, list[dict[str, str]]] = {}
    for key, entries in value.items():
        store_contract.require(isinstance(key, str) and isinstance(entries, list),
                               "container PortBindings entry is malformed")
        normalized[key] = []
        for entry in entries:
            item = store_contract.exact_keys(entry, ("HostIp", "HostPort"), "container port binding")
            normalized[key].append({"HostIp": item["HostIp"], "HostPort": item["HostPort"]})
    return normalized


def validate_container_inspect(
    spec: dict[str, Any],
    role: str,
    value: object,
    *,
    preflight_sha256: str,
    require_running: bool = True,
) -> dict[str, Any]:
    """Validate one Docker inspect object against every launch dimension."""

    store_contract.require(role in ROLE_ORDER, f"unknown role: {role}")
    store_contract.exact_sha(preflight_sha256, f"{role} preflight SHA")
    store_contract.require(isinstance(value, dict), f"{role} inspect is not an object")
    expected = spec["roles"][role]
    container_id = value.get("Id")
    store_contract.require(isinstance(container_id, str) and CONTAINER_ID_RE.fullmatch(container_id),
                           f"{role} container ID is malformed")
    store_contract.require(value.get("Name") == f"/{expected['container_name']}", f"{role} name drift")
    store_contract.require(value.get("Image") == expected["image"]["image_id"], f"{role} image ID drift")
    config = value.get("Config")
    host_config = value.get("HostConfig")
    state = value.get("State")
    network_settings = value.get("NetworkSettings")
    store_contract.require(isinstance(config, dict) and isinstance(host_config, dict),
                           f"{role} config/host config is malformed")
    store_contract.require(isinstance(state, dict) and isinstance(network_settings, dict),
                           f"{role} state/network settings is malformed")
    store_contract.require(config.get("Image") == expected["image"]["repo_digest"],
                           f"{role} Config.Image RepoDigest drift")
    store_contract.require(config.get("Cmd") == expected["command"], f"{role} command drift")
    store_contract.require(config.get("Hostname") == expected["logical_host"], f"{role} hostname drift")
    labels = config.get("Labels")
    store_contract.require(isinstance(labels, dict), f"{role} labels are malformed")
    expected_labels = {
        **spec["labels"],
        "cidr.p10.role": role,
        "cidr.p10.preflight_sha256": preflight_sha256,
    }
    for key, expected_value in expected_labels.items():
        store_contract.require(labels.get(key) == expected_value, f"{role} label drift: {key}")
    restart = host_config.get("RestartPolicy")
    store_contract.require(
        isinstance(restart, dict)
        and restart.get("Name") == "no"
        and restart.get("MaximumRetryCount", 0) == 0,
        f"{role} restart policy drift",
    )
    store_contract.require(host_config.get("NetworkMode") == spec["network"]["name"],
                           f"{role} network mode drift")
    store_contract.require(
        _normal_port_bindings(host_config.get("PortBindings")) == expected["port_bindings"],
        f"{role} host port binding drift",
    )
    mounts = value.get("Mounts")
    store_contract.require(isinstance(mounts, list), f"{role} mounts are malformed")
    actual_mounts = []
    for index, raw in enumerate(mounts):
        store_contract.require(isinstance(raw, dict), f"{role} mount[{index}] is malformed")
        actual_mounts.append(
            {
                "type": raw.get("Type"),
                "source": str(Path(str(raw.get("Source", ""))).resolve()),
                "destination": raw.get("Destination"),
                "read_only": raw.get("RW") is not True,
            }
        )
    actual_mounts.sort(key=lambda item: item["destination"])
    expected_mounts = sorted(expected["mounts"], key=lambda item: item["destination"])
    store_contract.require(actual_mounts == expected_mounts, f"{role} mount contract drift")
    networks = network_settings.get("Networks")
    store_contract.require(isinstance(networks, dict) and set(networks) == {spec["network"]["name"]},
                           f"{role} network attachment drift")
    endpoint = networks[spec["network"]["name"]]
    store_contract.require(isinstance(endpoint, dict), f"{role} network endpoint is malformed")
    aliases = endpoint.get("Aliases")
    store_contract.require(isinstance(aliases, list) and expected["logical_host"] in aliases,
                           f"{role} logical network alias is missing")
    store_contract.require(isinstance(endpoint.get("IPAddress"), str) and endpoint["IPAddress"],
                           f"{role} network IP is missing")
    started_at = state.get("StartedAt")
    store_contract.parse_timestamp(started_at, f"{role} StartedAt")
    running = state.get("Running") is True and state.get("Status") == "running"
    pid = state.get("Pid")
    start_ticks: int | None = None
    if require_running:
        store_contract.require(running, f"{role} is not running")
        store_contract.require(not isinstance(pid, bool) and isinstance(pid, int) and pid > 0,
                               f"{role} PID is invalid")
    if running:
        store_contract.require(not isinstance(pid, bool) and isinstance(pid, int) and pid > 0,
                               f"{role} running PID is invalid")
        start_ticks = process_start_ticks(pid)
    else:
        store_contract.require(
            not isinstance(pid, bool) and isinstance(pid, int) and pid >= 0,
            f"{role} non-running PID is invalid",
        )
    store_contract.require(state.get("Restarting") is False, f"{role} is restarting")
    store_contract.require(state.get("Paused") is False, f"{role} is paused")
    store_contract.require(state.get("Dead") is False, f"{role} is dead")
    store_contract.require(value.get("RestartCount") == 0, f"{role} restart count is nonzero")
    return {
        "role": role,
        "name": expected["container_name"],
        "logical_host": expected["logical_host"],
        "container_id": container_id,
        "image_id": value["Image"],
        "config_image": config["Image"],
        "pid": pid,
        "process_start_ticks": start_ticks,
        "started_at_utc": started_at,
        "restart_count": value["RestartCount"],
        "running": running,
    }


def _normalized_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _field(row: dict[str, Any], names: tuple[str, ...], context: str) -> Any:
    mapping = {_normalized_key(key): value for key, value in row.items()}
    for name in names:
        key = _normalized_key(name)
        if key in mapping:
            return mapping[key]
    raise ContractError(f"{context}: missing one of columns {names}")


def _host_text(value: object) -> str:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContractError("NebulaGraph returned non-UTF-8 text") from exc
    store_contract.require(isinstance(value, str), "NebulaGraph returned a non-text value")
    return value


def _strict_integer(value: object, context: str, minimum: int = 0) -> int:
    """Accept the integer forms emitted by nebula3, but reject coercive Python values."""

    if isinstance(value, bytes):
        try:
            value = value.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ContractError(f"{context}: non-ASCII integer") from exc
    if isinstance(value, str):
        store_contract.require(value == value.strip() and value.isascii() and value.isdecimal(),
                               f"{context}: malformed integer text")
        value = int(value)
    return store_contract.integer(value, context, minimum)


_RAFT_ENDPOINT_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]{0,127}):([0-9]{1,5})$")


def _raft_endpoint(value: object, context: str) -> tuple[str, int]:
    text = _host_text(value).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1]
    matched = _RAFT_ENDPOINT_RE.fullmatch(text)
    store_contract.require(matched is not None, f"{context}: malformed RAFT endpoint")
    assert matched is not None
    port = int(matched.group(2))
    store_contract.require(1 <= port <= 65535, f"{context}: RAFT port is out of range")
    return matched.group(1), port


def _raft_endpoints(value: object, context: str) -> list[tuple[str, int]]:
    """Parse scalar/list Nebula endpoint rendering without substring acceptance."""

    raw_items: list[object]
    if isinstance(value, (list, tuple)):
        raw_items = list(value)
    elif isinstance(value, (str, bytes)):
        text = _host_text(value).strip()
        if text == "" or text == "[]":
            return []
        if text.startswith("[") or text.endswith("]"):
            store_contract.require(text.startswith("[") and text.endswith("]"),
                                   f"{context}: malformed endpoint collection")
            text = text[1:-1].strip()
            if not text:
                return []
        raw_items = [item.strip() for item in text.split(",")]
    else:
        raise ContractError(f"{context}: unsupported endpoint collection type")
    store_contract.require(bool(raw_items), f"{context}: endpoint collection is empty")
    endpoints = [_raft_endpoint(item, context) for item in raw_items]
    store_contract.require(len(endpoints) == len(set(endpoints)),
                           f"{context}: duplicate RAFT endpoint")
    return endpoints


def _require_host_row(rows: object, host: str, port: int, context: str) -> dict[str, Any]:
    store_contract.require(isinstance(rows, list) and len(rows) == 1, f"{context}: expected exactly one row")
    row = rows[0]
    store_contract.require(isinstance(row, dict), f"{context}: row is malformed")
    raw_host = _host_text(_field(row, ("Host", "Host Address"), context))
    raw_port = _field(row, ("Port",), context)
    store_contract.require(raw_host in (host, f"{host}:{port}"), f"{context}: host identity drift")
    store_contract.require(_strict_integer(raw_port, f"{context} port", 1) == port,
                           f"{context}: port drift")
    status = _host_text(_field(row, ("Status",), context)).upper()
    store_contract.require(status == "ONLINE", f"{context}: host is not exactly ONLINE")
    return {"host": host, "port": port, "status": status}


def _names(rows: object, context: str) -> list[str]:
    store_contract.require(isinstance(rows, list), f"{context}: expected row array")
    names = []
    for row in rows:
        store_contract.require(isinstance(row, dict), f"{context}: row is malformed")
        names.append(_host_text(_field(row, ("Name",), context)))
    return sorted(names)


def validate_live_results(spec: dict[str, Any], results: dict[str, object]) -> dict[str, Any]:
    """Validate exact host, partition, space, schema, and no-index readiness."""

    expected_statements = {
        "SHOW HOSTS META",
        "SHOW HOSTS STORAGE",
        "SHOW HOSTS GRAPH",
        "SHOW PARTS",
        f"DESCRIBE SPACE {spec['space']}",
        "SHOW EDGES",
        "SHOW TAGS",
        "SHOW EDGE INDEXES",
        "SHOW TAG INDEXES",
    }
    store_contract.require(set(results) == expected_statements,
                           "live-gate statement coverage drift")
    hosts = {
        "metad": _require_host_row(
            results["SHOW HOSTS META"], spec["roles"]["metad"]["logical_host"], 9559, "SHOW HOSTS META"
        ),
        "storaged": _require_host_row(
            results["SHOW HOSTS STORAGE"], spec["roles"]["storaged"]["logical_host"], 9779,
            "SHOW HOSTS STORAGE",
        ),
        "graphd": _require_host_row(
            results["SHOW HOSTS GRAPH"], spec["roles"]["graphd"]["logical_host"], 9669,
            "SHOW HOSTS GRAPH",
        ),
    }
    parts = results["SHOW PARTS"]
    store_contract.require(isinstance(parts, list) and len(parts) == spec["expected_partition_count"],
                           "SHOW PARTS row count drift")
    partition_ids: list[int] = []
    expected_storage = spec["roles"]["storaged"]["logical_host"]
    normalized_parts = []
    for row in parts:
        store_contract.require(isinstance(row, dict), "SHOW PARTS row is malformed")
        partition_id = _strict_integer(
            _field(row, ("Partition ID", "Part ID", "PartId"), "SHOW PARTS"),
            "SHOW PARTS partition ID",
            1,
        )
        leader = _raft_endpoints(_field(row, ("Leader",), "SHOW PARTS"), "SHOW PARTS leader")
        peers = _raft_endpoints(_field(row, ("Peers",), "SHOW PARTS"), "SHOW PARTS peers")
        losts_value = _field(row, ("Losts", "Lost Peers"), "SHOW PARTS")
        if isinstance(losts_value, (str, bytes)) and _host_text(losts_value).strip() in ("", "[]"):
            losts: list[tuple[str, int]] = []
        elif isinstance(losts_value, (list, tuple)) and len(losts_value) == 0:
            losts = []
        else:
            losts = _raft_endpoints(losts_value, "SHOW PARTS lost peers")
        expected_endpoint = [(expected_storage, 9779)]
        store_contract.require(leader == expected_endpoint, "SHOW PARTS leader drift")
        store_contract.require(peers == expected_endpoint, "SHOW PARTS peers drift")
        store_contract.require(losts == [], "SHOW PARTS reports lost peers")
        partition_ids.append(partition_id)
        normalized_parts.append(
            {"partition_id": partition_id, "leader": leader, "peers": peers, "losts": losts}
        )
    store_contract.require(sorted(partition_ids) == list(range(1, EXPECTED_PARTITIONS + 1)),
                           "SHOW PARTS partition IDs are not exactly 1..64")

    space_rows = results[f"DESCRIBE SPACE {spec['space']}"]
    store_contract.require(isinstance(space_rows, list) and len(space_rows) == 1,
                           "DESCRIBE SPACE must return exactly one row")
    space_row = space_rows[0]
    store_contract.require(isinstance(space_row, dict), "DESCRIBE SPACE row is malformed")
    store_contract.require(_host_text(_field(space_row, ("Name",), "DESCRIBE SPACE")) == spec["space"],
                           "space name drift")
    store_contract.require(_strict_integer(
        _field(space_row, ("Partition Number", "Partition Num"), "DESCRIBE SPACE"),
        "DESCRIBE SPACE partition count", 1,
    ) == 64,
                           "space partition count drift")
    store_contract.require(_strict_integer(
        _field(space_row, ("Replica Factor",), "DESCRIBE SPACE"),
        "DESCRIBE SPACE replica factor", 1,
    ) == 1,
                           "space replica factor drift")
    vid_type = _host_text(_field(space_row, ("Vid Type",), "DESCRIBE SPACE")).upper().replace(" ", "")
    store_contract.require(vid_type == "INT64", "space VID type drift")

    edges = _names(results["SHOW EDGES"], "SHOW EDGES")
    store_contract.require(edges == sorted(spec["edge_labels"]), "SHOW EDGES schema set drift")
    store_contract.require(len(edges) == 34, "SHOW EDGES must contain exactly 34 edge schemas")
    store_contract.require(_names(results["SHOW TAGS"], "SHOW TAGS") == [], "formal space must not contain tags")
    store_contract.require(_names(results["SHOW EDGE INDEXES"], "SHOW EDGE INDEXES") == [],
                           "formal space must not contain edge indexes")
    store_contract.require(_names(results["SHOW TAG INDEXES"], "SHOW TAG INDEXES") == [],
                           "formal space must not contain tag indexes")
    digest = hashlib.sha256(
        json.dumps(results, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    return {
        "state": "PASS",
        "hosts": hosts,
        "partition_count": len(normalized_parts),
        "partition_ids": sorted(partition_ids),
        "edge_labels": edges,
        "tag_count": 0,
        "edge_index_count": 0,
        "tag_index_count": 0,
        "results_sha256": digest,
    }


def docker_call(
    docker: Path,
    args: list[str],
    *,
    check: bool = True,
    timeout: int | None = 60,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [str(docker), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )
    if check and completed.returncode != 0:
        raise ContractError(f"Docker command failed ({args[:3]}): {completed.stderr.strip()}")
    return completed


def docker_inspect_one(
    docker: Path, name_or_id: str, *, timeout: int | None = 60
) -> dict[str, Any]:
    completed = docker_call(
        docker, ["container", "inspect", name_or_id], check=True, timeout=timeout
    )
    try:
        values = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError(f"Docker inspect returned malformed JSON for {name_or_id}") from exc
    store_contract.require(isinstance(values, list) and len(values) == 1 and isinstance(values[0], dict),
                           f"Docker inspect returned unexpected shape for {name_or_id}")
    return values[0]


def assert_owned_names_absent(docker: Path, spec: dict[str, Any]) -> None:
    for role in ROLE_ORDER:
        name = spec["roles"][role]["container_name"]
        completed = docker_call(
            docker,
            ["container", "ls", "--all", "--quiet", "--no-trunc", "--filter", f"name=^/{name}$"],
            check=True,
        )
        store_contract.require(not completed.stdout.strip(), f"container name already exists: {name}")
    network = spec["network"]["name"]
    completed = docker_call(
        docker,
        ["network", "ls", "--quiet", "--no-trunc", "--filter", f"name=^{network}$"],
        check=True,
    )
    store_contract.require(not completed.stdout.strip(), f"network name already exists: {network}")


def assert_port_available(host: str, port: int) -> None:
    store_contract.require(host == "127.0.0.1", "formal port reservation must use 127.0.0.1")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            reservation.bind((host, port))
    except OSError as exc:
        raise ContractError(f"formal graph port is unavailable: {host}:{port}: {exc}") from exc


def validate_launch_spec(value: object) -> dict[str, Any]:
    spec = store_contract.exact_keys(
        value,
        (
            "schema_version", "run_id", "repeat_index", "network", "roles", "labels", "endpoint",
            "space", "authentication", "edge_labels", "expected_partition_count", "query_timeout_ms",
            "logs_root", "client",
        ),
        "launch spec",
    )
    store_contract.require(spec["schema_version"] == LAUNCH_SPEC_SCHEMA, "launch spec schema drift")
    store_contract.nonempty(spec["run_id"], "launch spec run_id")
    store_contract.integer(spec["repeat_index"], "launch spec repeat_index", 1)
    network = store_contract.exact_keys(spec["network"], ("name", "driver"), "launch spec network")
    store_contract.require(network["driver"] == "bridge", "launch network driver drift")
    store_contract.nonempty(network["name"], "launch network name")
    endpoint = store_contract.exact_keys(spec["endpoint"], ("host", "port"), "launch endpoint")
    store_contract.require(endpoint["host"] == "127.0.0.1", "launch endpoint host drift")
    port = store_contract.integer(endpoint["port"], "launch endpoint port", 1)
    store_contract.require(port <= 65535, "launch endpoint port exceeds 65535")
    store_contract.require(spec["expected_partition_count"] == EXPECTED_PARTITIONS,
                           "launch partition count drift")
    timeout_ms = store_contract.integer(spec["query_timeout_ms"], "launch query timeout", 100)
    store_contract.require(timeout_ms <= 10_000, "launch query timeout exceeds bound")
    logs_root = require_real_directory(Path(spec["logs_root"]), "launch logs root")
    client = store_contract.exact_keys(
        spec["client"], ("path", "hash_method", "sha256", "file_count", "total_bytes"), "launch client"
    )
    # The client is part of the executable runtime, not a large store tree.  Recompute it
    # whenever a receipt is consumed so a post-preflight mutation cannot change query behavior.
    adapter.validate_tree_ref(client, "launch client", recompute=True)
    labels = store_contract.exact_keys(
        spec["labels"], ("cidr.p10.owner", "cidr.p10.run_id", "cidr.p10.repeat_index"), "launch labels"
    )
    store_contract.require(labels["cidr.p10.owner"] == "nebulagraph-formal-cluster-v1",
                           "launch owner label drift")
    store_contract.require(labels["cidr.p10.run_id"] == spec["run_id"], "launch run label drift")
    store_contract.require(labels["cidr.p10.repeat_index"] == str(spec["repeat_index"]),
                           "launch repeat label drift")
    roles = store_contract.exact_keys(spec["roles"], ROLE_ORDER, "launch roles")
    parsed_roles = {
        role: store_contract.exact_keys(
            roles[role],
            ("container_name", "logical_host", "image", "mounts", "port_bindings", "command"),
            f"launch role {role}",
        )
        for role in ROLE_ORDER
    }
    containers = {role: parsed_roles[role]["container_name"] for role in ROLE_ORDER}
    logical_hosts = {role: parsed_roles[role]["logical_host"] for role in ROLE_ORDER}
    store_contract.validate_identity_names(containers, logical_hosts, network["name"], formal=True)
    store_contract.validate_images(
        [parsed_roles[name]["image"] for name in ("graphd", "metad", "storaged")],
        "launch role images",
    )
    data_parents: set[Path] = set()
    data_names = {"metad": "meta", "storaged": "storage", "graphd": "graph"}
    data_destinations = {"metad": "/data/meta", "storaged": "/data/storage", "graphd": "/data/graph"}
    expected_commands = {
        "metad": [
            f"--meta_server_addrs={logical_hosts['metad']}:9559",
            f"--local_ip={logical_hosts['metad']}",
            "--ws_ip=0.0.0.0", "--port=9559", "--ws_http_port=19559",
            "--data_path=/data/meta", "--log_dir=/logs",
        ],
        "storaged": [
            f"--meta_server_addrs={logical_hosts['metad']}:9559",
            f"--local_ip={logical_hosts['storaged']}",
            "--ws_ip=0.0.0.0", "--port=9779", "--ws_http_port=19779",
            "--data_path=/data/storage", "--log_dir=/logs",
        ],
        "graphd": [
            f"--meta_server_addrs={logical_hosts['metad']}:9559",
            f"--local_ip={logical_hosts['graphd']}",
            "--ws_ip=0.0.0.0", "--port=9669", "--ws_http_port=19669", "--log_dir=/logs",
        ],
    }
    for role in ROLE_ORDER:
        item = parsed_roles[role]
        store_contract.require(isinstance(item["mounts"], list) and len(item["mounts"]) == 2,
                               f"launch {role} mounts drift")
        mounts_by_destination = {}
        for mount in item["mounts"]:
            parsed_mount = store_contract.exact_keys(
                mount, ("type", "source", "destination", "read_only"), f"launch {role} mount"
            )
            store_contract.require(parsed_mount["type"] == "bind" and parsed_mount["read_only"] is False,
                                   f"launch {role} mount mode drift")
            source = Path(parsed_mount["source"])
            mounts_by_destination[parsed_mount["destination"]] = parsed_mount
            if parsed_mount["destination"] == "/logs":
                store_contract.require(source.resolve(strict=False) == logs_root / role,
                                       f"launch {role} logs mount root drift")
            else:
                require_real_directory(source, f"launch {role} data mount")
                data_parents.add(source.resolve().parent)
                store_contract.require(source.name == data_names[role], f"launch {role} data source drift")
        store_contract.require(set(mounts_by_destination) == {data_destinations[role], "/logs"},
                               f"launch {role} mount destinations drift")
        expected_ports = (
            {"9669/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port)}]}
            if role == "graphd" else {}
        )
        store_contract.require(item["port_bindings"] == expected_ports,
                               f"launch {role} port binding contract drift")
        store_contract.require(item["command"] == expected_commands[role],
                               f"launch {role} command contract drift")
    store_contract.require(len(data_parents) == 1, "launch role data roots do not share one clone root")
    space = store_contract.nonempty(spec["space"], "launch space")
    store_contract.require(adapter.EDGE_LABEL_RE.fullmatch(space) is not None, "launch space name is unsafe")
    auth = store_contract.exact_keys(
        spec["authentication"], ("user", "password_env", "password_sha256"), "launch authentication"
    )
    store_contract.nonempty(auth["user"], "launch auth user")
    store_contract.nonempty(auth["password_env"], "launch auth password env")
    store_contract.exact_sha(auth["password_sha256"], "launch auth password SHA")
    edges = spec["edge_labels"]
    store_contract.require(isinstance(edges, list) and len(edges) == 34 and len(set(edges)) == 34
                           and all(isinstance(edge, str) and adapter.EDGE_LABEL_RE.fullmatch(edge) for edge in edges),
                           "launch spec must bind exactly 34 edge labels")
    store_contract.require(edges == sorted(edges), "launch edge labels must use canonical sorted order")
    return spec


def validate_repeat_isolation(spec_values: list[dict[str, Any]]) -> dict[str, Any]:
    """Prove that a formal repeat set can coexist without mutable resource reuse."""

    store_contract.require(isinstance(spec_values, list) and bool(spec_values),
                           "repeat isolation requires at least one launch spec")
    specs = sorted(
        (validate_launch_spec(value) for value in spec_values),
        key=lambda value: value["repeat_index"],
    )
    run_ids = {spec["run_id"] for spec in specs}
    store_contract.require(len(run_ids) == 1, "repeat launch specs do not share one run_id")
    repeat_indices = [spec["repeat_index"] for spec in specs]
    store_contract.require(
        sorted(repeat_indices) == list(range(1, len(specs) + 1)),
        "repeat indices must be unique and exactly 1..N",
    )
    first = specs[0]
    immutable_keys = (
        "space", "authentication", "edge_labels", "expected_partition_count", "query_timeout_ms", "client"
    )
    for spec in specs[1:]:
        for key in immutable_keys:
            store_contract.require(spec[key] == first[key], f"repeat immutable launch field drift: {key}")
        for role in ROLE_ORDER:
            store_contract.require(
                spec["roles"][role]["logical_host"] == first["roles"][role]["logical_host"],
                f"repeat frozen logical host drift: {role}",
            )
            store_contract.require(
                spec["roles"][role]["image"] == first["roles"][role]["image"],
                f"repeat image identity drift: {role}",
            )

    data_roots: dict[str, Path] = {}
    log_roots: dict[str, Path] = {}
    networks: list[str] = []
    ports: list[int] = []
    containers: list[str] = []
    root_inodes: set[tuple[int, int]] = set()
    for spec in specs:
        repeat = spec["repeat_index"]
        data_root = Path(spec["roles"]["metad"]["mounts"][0]["source"]).resolve().parent
        logs_root = Path(spec["logs_root"]).resolve()
        data_roots[f"repeat-{repeat:02d}-data"] = data_root
        log_roots[f"repeat-{repeat:02d}-logs"] = logs_root
        stat_result = data_root.stat()
        inode = (stat_result.st_dev, stat_result.st_ino)
        store_contract.require(inode not in root_inodes, "repeat store roots alias the same directory inode")
        root_inodes.add(inode)
        networks.append(spec["network"]["name"])
        ports.append(spec["endpoint"]["port"])
        containers.extend(spec["roles"][role]["container_name"] for role in ROLE_ORDER)
    all_roots = {**data_roots, **log_roots}
    store_contract.assert_nonoverlapping(all_roots)
    store_contract.require(len(networks) == len(set(networks)), "repeat Docker network names are reused")
    store_contract.require(len(ports) == len(set(ports)), "repeat graph host ports are reused")
    store_contract.require(len(containers) == len(set(containers)), "repeat container names are reused")
    return {
        "state": "PASS",
        "run_id": next(iter(run_ids)),
        "repeat_indices": sorted(repeat_indices),
        "store_roots": [str(data_roots[key]) for key in sorted(data_roots)],
        "logs_roots": [str(log_roots[key]) for key in sorted(log_roots)],
        "networks": networks,
        "graph_ports": ports,
        "container_names": containers,
        "spec_sha256": [store_contract.canonical_json_sha(spec) for spec in specs],
    }


def _result_rows(result: Any, statement: str) -> list[dict[str, Any]]:
    if not result.is_succeeded():
        raise ContractError(f"nGQL failed: {statement}: {result.error_msg()}")
    rows = result.as_primitive()
    store_contract.require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows),
                           f"nGQL result rows are malformed: {statement}")
    return rows


def capture_live_gate(
    spec: dict[str, Any],
    *,
    password: str,
    startup_timeout_seconds: int,
    overall_deadline: float | None = None,
) -> dict[str, Any]:
    """Connect with both per-query and overall deadlines and prove exact readiness."""

    store_contract.require(
        not isinstance(startup_timeout_seconds, bool)
        and isinstance(startup_timeout_seconds, int)
        and 1 <= startup_timeout_seconds <= 900,
                           "startup timeout must be in [1, 900] seconds")
    now = time.monotonic()
    requested_deadline = now + startup_timeout_seconds
    deadline = requested_deadline if overall_deadline is None else min(requested_deadline, overall_deadline)
    store_contract.require(deadline > now, "NebulaGraph startup deadline was exhausted before live gate")
    store_contract.require(
        hasattr(signal, "setitimer") and hasattr(signal, "ITIMER_REAL"),
        "formal live gate requires POSIX setitimer for a hard overall deadline",
    )
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    store_contract.require(previous_timer == (0.0, 0.0), "formal live gate refuses to replace an active timer")
    previous_handler = signal.getsignal(signal.SIGALRM)

    def deadline_handler(_signum: int, _frame: Any) -> None:
        raise _OverallDeadlineExceeded("NebulaGraph live-gate hard deadline expired")

    signal.signal(signal.SIGALRM, deadline_handler)
    signal.setitimer(signal.ITIMER_REAL, max(0.001, deadline - time.monotonic()))
    try:
        validate_launch_spec(spec)
        expected_password_sha = spec["authentication"]["password_sha256"]
        store_contract.require(hashlib.sha256(password.encode("utf-8")).hexdigest() == expected_password_sha,
                               "NebulaGraph password does not match launch spec")
        client_root = Path(spec["client"]["path"])
        adapter.import_client(client_root)
        from nebula3.Config import Config
        from nebula3.gclient.net import ConnectionPool

        attempts = 0
        last_error: object = "not attempted"
        while time.monotonic() < deadline:
            attempts += 1
            pool = None
            session = None
            try:
                remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
                config = Config()
                config.max_connection_pool_size = 2
                config.timeout = min(spec["query_timeout_ms"], remaining_ms)
                pool = ConnectionPool()
                if not pool.init([(spec["endpoint"]["host"], spec["endpoint"]["port"])], config):
                    raise ContractError("ConnectionPool.init returned false")
                session = pool.get_session(spec["authentication"]["user"], password)
                results: dict[str, object] = {}
                for statement in ("SHOW HOSTS META", "SHOW HOSTS STORAGE", "SHOW HOSTS GRAPH"):
                    results[statement] = _result_rows(session.execute(statement), statement)
                use = session.execute(f"USE {spec['space']}")
                if not use.is_succeeded():
                    raise ContractError(f"USE failed: {use.error_msg()}")
                for statement in (
                    "SHOW PARTS",
                    f"DESCRIBE SPACE {spec['space']}",
                    "SHOW EDGES",
                    "SHOW TAGS",
                    "SHOW EDGE INDEXES",
                    "SHOW TAG INDEXES",
                ):
                    results[statement] = _result_rows(session.execute(statement), statement)
                snapshot = validate_live_results(spec, results)
                snapshot["attempts"] = attempts
                snapshot["query_timeout_ms"] = spec["query_timeout_ms"]
                snapshot["startup_timeout_seconds"] = startup_timeout_seconds
                snapshot["captured_at_utc"] = utc_now()
                return snapshot
            except _OverallDeadlineExceeded:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
            finally:
                if session is not None:
                    try:
                        session.release()
                    except Exception:  # noqa: BLE001
                        pass
                if pool is not None:
                    try:
                        pool.close()
                    except Exception:  # noqa: BLE001
                        pass
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        raise ContractError(f"NebulaGraph live gate did not pass before deadline: {last_error}")
    except _OverallDeadlineExceeded as exc:
        raise ContractError("NebulaGraph live gate exceeded the hard overall deadline") from exc
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous_handler)


def _common_formal_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--runtime-manifest", required=True, type=Path)
    parser.add_argument("--runtime-manifest-sha256", required=True)
    parser.add_argument("--store-manifest", required=True, type=Path)
    parser.add_argument("--store-manifest-sha256", required=True)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--p02b-result", required=True, type=Path)
    parser.add_argument("--p02b-result-sha256", required=True)
    parser.add_argument("--p02b-validator", required=True, type=Path)
    parser.add_argument("--p02b-validator-sha256", required=True)
    parser.add_argument("--p02b-binary", required=True, type=Path)
    parser.add_argument("--p02b-binary-sha256", required=True)
    parser.add_argument("--p02b-max-age-seconds", type=int, default=21600)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)
    seal_parser = subparsers.add_parser("seal")
    _common_formal_args(seal_parser)
    seal_parser.add_argument("--lock-file", required=True, type=Path)
    seal_parser.add_argument("--lock-owner-pid", required=True, type=int)
    seal_parser.add_argument("--lock-owner-start-ticks", required=True, type=int)
    seal_parser.add_argument("--output", required=True, type=Path)

    preflight_parser = subparsers.add_parser("preflight")
    _common_formal_args(preflight_parser)
    preflight_parser.add_argument("--logs-root", required=True, type=Path)
    preflight_parser.add_argument("--query-timeout-ms", type=int, default=5000)
    preflight_parser.add_argument("--output", required=True, type=Path)

    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--preflight", required=True, type=Path)
    start_parser.add_argument("--preflight-sha256", required=True)
    start_parser.add_argument("--max-preflight-age-seconds", type=int, default=600)
    start_parser.add_argument("--startup-timeout-seconds", type=int, default=300)
    start_parser.add_argument("--partial-output", required=True, type=Path)
    start_parser.add_argument("--cleanup-output", required=True, type=Path)
    start_parser.add_argument("--cleanup-stop-timeout-seconds", type=int, default=30)
    start_parser.add_argument("--output", required=True, type=Path)

    stop_parser = subparsers.add_parser("stop")
    stop_parser.add_argument("--preflight", required=True, type=Path)
    stop_parser.add_argument("--preflight-sha256", required=True)
    stop_parser.add_argument("--start-receipt", required=True, type=Path)
    stop_parser.add_argument("--start-receipt-sha256", required=True)
    stop_parser.add_argument("--stop-timeout-seconds", type=int, default=30)
    stop_parser.add_argument("--output", required=True, type=Path)

    recover_parser = subparsers.add_parser("recover")
    recover_parser.add_argument("--preflight", required=True, type=Path)
    recover_parser.add_argument("--preflight-sha256", required=True)
    recover_parser.add_argument("--start-receipt", required=True, type=Path)
    recover_parser.add_argument("--start-receipt-sha256", required=True)
    recover_parser.add_argument("--stop-timeout-seconds", type=int, default=30)
    recover_parser.add_argument("--output", required=True, type=Path)

    cleanup_parser = subparsers.add_parser("cleanup")
    cleanup_parser.add_argument("--preflight", required=True, type=Path)
    cleanup_parser.add_argument("--preflight-sha256", required=True)
    cleanup_parser.add_argument("--partial-start", required=True, type=Path)
    cleanup_parser.add_argument("--partial-start-sha256", required=True)
    cleanup_parser.add_argument("--max-preflight-age-seconds", type=int, default=604800)
    cleanup_parser.add_argument("--stop-timeout-seconds", type=int, default=30)
    cleanup_parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def _artifact_ref(path: Path, sha256: str, context: str) -> tuple[Path, dict[str, str]]:
    resolved = adapter.checked_file(path, sha256, context)
    return resolved, {"path": str(resolved), "sha256": sha256_file(resolved)}


def _safe_output(path: Path, spec: dict[str, Any], repo: dict[str, Any], context: str) -> Path:
    store_contract.require(path.is_absolute(), f"{context} must be absolute")
    output = path.resolve(strict=False)
    store_contract.require(output.parent.is_dir() and not output.parent.is_symlink(),
                           f"{context} parent must be a real directory")
    store_contract.require(not output.exists() and not output.is_symlink(),
                           f"refusing to overwrite {context}: {output}")
    store_contract.require(not store_contract.is_within(output, Path(repo["root"])),
                           f"{context} must be outside the Git worktree")
    data_root = Path(spec["roles"]["metad"]["mounts"][0]["source"]).parent
    logs_root = Path(spec["logs_root"])
    store_contract.assert_nonoverlapping({"data_root": data_root, "logs_root": logs_root, "output": output})
    return output


def process_start_ticks(pid: int) -> int:
    store_contract.require(not isinstance(pid, bool) and isinstance(pid, int) and pid > 1,
                           "process PID is invalid")
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ContractError(f"cannot read process start identity for PID {pid}: {exc}") from exc
    store_contract.require(")" in raw, "process /proc stat is malformed")
    fields = raw.rsplit(")", 1)[1].split()
    store_contract.require(len(fields) > 19, "process /proc stat is malformed")
    try:
        value = int(fields[19])
    except ValueError as exc:
        raise ContractError("process start ticks are malformed") from exc
    store_contract.require(value > 0, "process start ticks are invalid")
    return value


def stat_seal(path: Path, context: str, *, directory: bool = False) -> dict[str, Any]:
    store_contract.require(path.is_absolute(), f"{context} path must be absolute")
    resolved = path.resolve()
    store_contract.require(not path.is_symlink(), f"{context} must not be a symlink")
    store_contract.require(
        resolved.is_dir() if directory else resolved.is_file(),
        f"{context} has the wrong file type",
    )
    value = resolved.stat()
    return {
        "path": str(resolved),
        "device": value.st_dev,
        "inode": value.st_ino,
        "mode": value.st_mode,
        "uid": value.st_uid,
        "gid": value.st_gid,
        "size_bytes": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns,
    }


def validate_stat_seal(value: object, context: str, *, directory: bool = False) -> Path:
    claim = store_contract.exact_keys(
        value,
        (
            "path", "device", "inode", "mode", "uid", "gid", "size_bytes",
            "mtime_ns", "ctime_ns",
        ),
        context,
    )
    path = Path(store_contract.nonempty(claim["path"], f"{context}.path"))
    current = stat_seal(path, context, directory=directory)
    store_contract.require(current == claim, f"{context} stat seal drift")
    return path.resolve()


def validate_held_lock(
    lock_path: Path,
    *,
    owner_pid: int,
    owner_start_ticks: int,
    expected_stat: object | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    store_contract.require(fcntl is not None, "sealed admission requires POSIX flock")
    lock = stat_seal(lock_path, "sealed admission lock")
    if expected_stat is not None:
        store_contract.require(lock == expected_stat, "sealed admission lock stat drift")
    try:
        owner = store_contract.exact_keys(
            store_contract.read_strict_json(lock_path, "sealed admission lock owner"),
            ("schema_version", "pid", "process_start_ticks", "hostname", "created_at_utc"),
            "sealed admission lock owner",
        )
    except OSError as exc:
        raise ContractError(f"cannot read sealed admission lock owner: {exc}") from exc
    store_contract.require(
        owner["schema_version"] == "cidr-p10-nebulagraph-store-lock-v1",
        "sealed admission lock owner schema drift",
    )
    store_contract.require(
        owner["pid"] == owner_pid
        and owner["process_start_ticks"] == owner_start_ticks
        and owner["hostname"] == socket.gethostname(),
        "sealed admission lock owner identity drift",
    )
    store_contract.parse_timestamp(owner["created_at_utc"], "sealed admission lock created_at")
    store_contract.require(
        process_start_ticks(owner_pid) == owner_start_ticks,
        "sealed admission lock owner process was replaced",
    )
    descriptor = os.open(lock_path, os.O_RDONLY | os.O_CLOEXEC)
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        store_contract.require(not acquired, "sealed admission lock is not held")
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    return lock, owner


def seal(args: argparse.Namespace) -> None:
    """Perform all dense/tree hashing before launch and publish a lock-bound receipt."""

    request_path, request_ref = _artifact_ref(args.request, args.request_sha256, "formal request")
    runtime_path, runtime_ref = _artifact_ref(
        args.runtime_manifest, args.runtime_manifest_sha256, "runtime manifest"
    )
    store_path, store_ref = _artifact_ref(
        args.store_manifest, args.store_manifest_sha256, "store manifest"
    )
    request, _truth_rows, dataset, truth, data_root = adapter.validate_request(
        request_path, "nebulagraph"
    )
    store_contract.require(request["execution_mode"] == "formal", "seal requires formal request")
    repo = adapter.git_state(args.repo_root)
    store_contract.require(repo["clean"] is True, "seal requires a clean Git worktree")
    runtime, _client_root, docker = adapter.validate_runtime_manifest(runtime_path, request)
    p02b = adapter.consume_p02b(args, True, request, repo)
    store_manifest, labels = adapter.validate_store_manifest(
        store_path,
        request,
        dataset,
        truth,
        data_root,
        runtime_path,
        args.runtime_manifest_sha256,
        runtime,
        repo,
        p02b,
    )
    store_contract.assert_offline((data_root,), docker=docker)
    _sealed_store_path, sealed_tree = store_contract.validate_tree_ref(
        store_manifest["data_root"],
        "sealed current store",
        expected_path=data_root,
        recompute=True,
    )
    store_contract.assert_offline((data_root,), docker=docker)
    lock_path = args.lock_file.resolve()
    store_contract.require(
        args.lock_owner_pid > 1 and args.lock_owner_start_ticks > 0,
        "seal lock owner arguments are invalid",
    )
    lock_stat, lock_owner = validate_held_lock(
        lock_path,
        owner_pid=args.lock_owner_pid,
        owner_start_ticks=args.lock_owner_start_ticks,
    )
    output = args.output.resolve(strict=False)
    store_contract.require(output.is_absolute(), "sealed admission output must be absolute")
    store_contract.require(
        output.parent.is_dir() and not output.parent.is_symlink(),
        "sealed admission output parent must be a real directory",
    )
    store_contract.require(
        not output.exists() and not output.is_symlink(),
        "refusing to overwrite sealed admission output",
    )
    store_contract.require(
        not store_contract.is_within(output, Path(repo["root"])),
        "sealed admission output must be outside the Git worktree",
    )
    store_contract.assert_nonoverlapping(
        {"store": data_root, "lock": lock_path, "sealed_admission": output}
    )
    import_ref = store_manifest["import_receipt"]
    clone_ref = store_manifest["clone_receipt"]
    import_value = store_contract.read_strict_json(
        Path(import_ref["path"]), "sealed import receipt"
    )
    clone_value = store_contract.read_strict_json(
        Path(clone_ref["path"]), "sealed clone receipt"
    )
    receipt = {
        "schema_version": SEALED_ADMISSION_SCHEMA,
        "state": "PASS",
        "created_at_utc": utc_now(),
        "producer": store_contract.file_ref(Path(__file__), "sealed admission producer"),
        "repo": repo,
        "request": {**request_ref, "stat": stat_seal(request_path, "sealed request")},
        "artifacts": {
            "runtime_manifest": {**runtime_ref, "stat": stat_seal(runtime_path, "sealed runtime")},
            "store_manifest": {**store_ref, "stat": stat_seal(store_path, "sealed store manifest")},
            "dataset": {
                **request["dataset"],
                "stat": stat_seal(dataset, "sealed dense dataset"),
            },
            "truth": {
                "path": request["truth"]["path"],
                "sha256": request["truth"]["sha256"],
                "stat": stat_seal(truth, "sealed truth"),
            },
        },
        "store": {
            "path": str(data_root),
            "root_stat": stat_seal(data_root, "sealed current store root", directory=True),
            "tree": sealed_tree,
        },
        "lock": {"stat": lock_stat, "owner": lock_owner},
        "lineage": {
            "import_receipt": import_ref,
            "clone_receipt": clone_ref,
            "golden_store": import_value["store"],
            "clone_source": clone_value["source_pre"],
            "clone_target": clone_value["target"],
            "clone_run_id": clone_value["run_id"],
            "clone_repeat_index": clone_value["repeat_index"],
        },
        "validated": {
            "system_version": request["system_version"],
            "run_id": request["run_id"],
            "repeat_index": request["repeat_index"],
            "edge_type_labels": [
                {"edge_type": edge_type, "label": labels[edge_type]}
                for edge_type in sorted(labels)
            ],
            "p02b": p02b,
        },
    }
    store_contract.write_json_exclusive(output, receipt)
    print(output)


def validate_sealed_admission_receipt(
    path: Path,
    sha256: str,
    *,
    request_path: Path,
    request_sha256: str,
    max_age_seconds: int = 900,
) -> tuple[Path, dict[str, Any]]:
    """Validate the small lock/stat surface without rehashing dense data or store files."""

    receipt_path = store_contract.validate_file_ref(
        {"path": str(path), "sha256": sha256},
        "sealed admission receipt",
        recompute=True,
    )
    value = store_contract.exact_keys(
        store_contract.read_strict_json(receipt_path, "sealed admission receipt"),
        (
            "schema_version", "state", "created_at_utc", "producer", "repo", "request",
            "artifacts", "store", "lock", "lineage", "validated",
        ),
        "sealed admission receipt",
    )
    store_contract.require(
        value["schema_version"] == SEALED_ADMISSION_SCHEMA and value["state"] == "PASS",
        "sealed admission receipt schema/state drift",
    )
    _validate_age(value["created_at_utc"], max_age_seconds, "sealed admission receipt")
    validate_current_repo(value["repo"], "sealed admission.repo")
    producer = store_contract.validate_file_ref(
        value["producer"], "sealed admission producer", recompute=True
    )
    store_contract.require(
        producer == Path(__file__).resolve(), "sealed admission producer path drift"
    )
    request = store_contract.exact_keys(
        value["request"], ("path", "sha256", "stat"), "sealed admission request"
    )
    store_contract.validate_file_ref(
        {"path": request["path"], "sha256": request["sha256"]},
        "sealed admission request",
        expected_path=request_path,
        expected_sha256=request_sha256,
        recompute=True,
    )
    validate_stat_seal(request["stat"], "sealed admission request stat")
    artifacts = store_contract.exact_keys(
        value["artifacts"],
        ("runtime_manifest", "store_manifest", "dataset", "truth"),
        "sealed admission artifacts",
    )
    for name, raw in artifacts.items():
        artifact = store_contract.exact_keys(
            raw, ("path", "sha256", "stat"), f"sealed admission artifact {name}"
        )
        store_contract.exact_sha(artifact["sha256"], f"sealed admission artifact {name} SHA")
        artifact_path = validate_stat_seal(
            artifact["stat"], f"sealed admission artifact {name} stat"
        )
        store_contract.require(
            artifact_path == Path(artifact["path"]).resolve(),
            f"sealed admission artifact {name} path/stat drift",
        )
    store = store_contract.exact_keys(
        value["store"], ("path", "root_stat", "tree"), "sealed admission store"
    )
    store_path = validate_stat_seal(
        store["root_stat"], "sealed admission current store stat", directory=True
    )
    store_contract.require(
        store_path == Path(store["path"]).resolve(), "sealed admission store path/stat drift"
    )
    _tree_path, tree = store_contract.validate_tree_ref(
        store["tree"], "sealed admission store tree", recompute=False, allow_empty=False
    )
    store_contract.require(tree["path"] == str(store_path), "sealed admission tree path drift")
    lock = store_contract.exact_keys(value["lock"], ("stat", "owner"), "sealed admission lock")
    owner = store_contract.exact_keys(
        lock["owner"],
        ("schema_version", "pid", "process_start_ticks", "hostname", "created_at_utc"),
        "sealed admission lock owner",
    )
    validate_held_lock(
        Path(lock["stat"]["path"]),
        owner_pid=owner["pid"],
        owner_start_ticks=owner["process_start_ticks"],
        expected_stat=lock["stat"],
    )
    lineage = store_contract.exact_keys(
        value["lineage"],
        (
            "import_receipt", "clone_receipt", "golden_store", "clone_source", "clone_target",
            "clone_run_id", "clone_repeat_index",
        ),
        "sealed admission lineage",
    )
    for name in ("import_receipt", "clone_receipt"):
        store_contract.validate_file_ref(
            lineage[name], f"sealed admission lineage {name}", recompute=True
        )
    for name in ("golden_store", "clone_source", "clone_target"):
        store_contract.validate_tree_ref(
            lineage[name],
            f"sealed admission lineage {name}",
            recompute=False,
            allow_empty=False,
        )
    validated = store_contract.exact_keys(
        value["validated"],
        ("system_version", "run_id", "repeat_index", "edge_type_labels", "p02b"),
        "sealed admission validated contract",
    )
    store_contract.require(
        lineage["clone_run_id"] == validated["run_id"]
        and lineage["clone_repeat_index"] == validated["repeat_index"],
        "sealed admission clone run/repeat lineage drift",
    )
    return receipt_path, value


def preflight(args: argparse.Namespace) -> None:
    request_path, request_ref = _artifact_ref(args.request, args.request_sha256, "formal request")
    runtime_path, runtime_ref = _artifact_ref(
        args.runtime_manifest, args.runtime_manifest_sha256, "runtime manifest"
    )
    store_path, store_ref = _artifact_ref(args.store_manifest, args.store_manifest_sha256, "store manifest")
    request, _truth_rows, dataset, truth, data_root = adapter.validate_request(request_path, "nebulagraph")
    store_contract.require(request["execution_mode"] == "formal", "cluster preflight requires formal request")
    repo = adapter.git_state(args.repo_root)
    store_contract.require(repo["clean"] is True, "cluster preflight requires a clean Git worktree")
    runtime, _client_root, docker = adapter.validate_runtime_manifest(runtime_path, request)
    p02b = adapter.consume_p02b(args, True, request, repo)
    store_manifest, _labels = adapter.validate_store_manifest(
        store_path,
        request,
        dataset,
        truth,
        data_root,
        runtime_path,
        args.runtime_manifest_sha256,
        runtime,
        repo,
        p02b,
    )
    logs_root = require_real_directory(args.logs_root, "formal logs root", empty=True)
    spec = build_launch_spec(
        request,
        runtime,
        store_manifest,
        logs_root,
        query_timeout_ms=args.query_timeout_ms,
    )
    spec = validate_launch_spec(spec)
    output = _safe_output(args.output, spec, repo, "preflight receipt")
    store_contract.assert_offline((data_root, logs_root), docker=docker)
    assert_owned_names_absent(docker, spec)
    assert_port_available(spec["endpoint"]["host"], spec["endpoint"]["port"])
    for role in ROLE_ORDER:
        log_path = Path(spec["roles"][role]["mounts"][1]["source"])
        store_contract.require(not log_path.exists() and not log_path.is_symlink(),
                               f"preflight role log path already exists: {log_path}")
    docker_ref = store_manifest_runtime_docker(runtime)
    p02b_summary = {
        "result": p02b["result"],
        "validator": p02b["validator"],
        "binary": p02b["binary"],
        "pass_marker": p02b["pass_marker"],
        "provenance": p02b["provenance"],
        "canonical_dataset_manifest": p02b["canonical_dataset_manifest"],
        "validator_argv_sha256": p02b["validator_argv_sha256"],
        "current_host": p02b["current_host"],
        "max_age_seconds": p02b["max_age_seconds"],
    }
    receipt = {
        "schema_version": PREFLIGHT_SCHEMA,
        "state": "PASS",
        "created_at_utc": utc_now(),
        "repo": repo,
        "inputs": {
            "request": request_ref,
            "runtime_manifest": runtime_ref,
            "store_manifest": store_ref,
        },
        "docker": docker_ref,
        "spec": spec,
        "spec_sha256": store_contract.canonical_json_sha(spec),
        "p02b": p02b_summary,
        "preconditions": {
            "store_and_logs_offline": True,
            "owned_names_absent": True,
            "graph_port_available": True,
            "role_log_paths_absent": True,
        },
    }
    store_contract.write_json_exclusive(output, receipt)
    print(output)


def store_manifest_runtime_docker(runtime: dict[str, Any]) -> dict[str, str]:
    docker = store_contract.exact_keys(runtime["docker"], ("path", "sha256", "version"), "runtime docker")
    path = store_contract.validate_file_ref(
        {"path": docker["path"], "sha256": docker["sha256"]}, "runtime docker", recompute=True
    )
    store_contract.require(os.access(path, os.X_OK), "runtime Docker client is not executable")
    return {"path": str(path), "sha256": docker["sha256"]}


def validate_current_repo(value: object, context: str) -> dict[str, Any]:
    claim = store_contract.exact_keys(value, ("root", "head", "clean", "status_sha256"), context)
    root = Path(store_contract.nonempty(claim["root"], f"{context}.root"))
    current = store_contract.current_git_state(root)
    return store_contract.validate_repo(claim, current, context)


def _validate_age(created_at: object, max_age_seconds: int, context: str) -> None:
    store_contract.require(max_age_seconds >= 1 and max_age_seconds <= 604_800,
                           f"{context} max age is out of bounds")
    created = store_contract.parse_timestamp(created_at, f"{context}.created_at_utc")
    age = (dt.datetime.now(dt.timezone.utc) - created.astimezone(dt.timezone.utc)).total_seconds()
    store_contract.require(age >= -5 and age <= max_age_seconds, f"{context} is stale or future-dated")


def validate_preflight_receipt(
    path: Path,
    sha256: str,
    *,
    max_age_seconds: int,
) -> tuple[Path, dict[str, Any], Path]:
    reference = {"path": str(path), "sha256": sha256}
    receipt_path = store_contract.validate_file_ref(reference, "preflight receipt", recompute=True)
    value = store_contract.exact_keys(
        store_contract.read_strict_json(receipt_path, "preflight receipt"),
        (
            "schema_version", "state", "created_at_utc", "repo", "inputs", "docker", "spec",
            "spec_sha256", "p02b", "preconditions",
        ),
        "preflight receipt",
    )
    store_contract.require(value["schema_version"] == PREFLIGHT_SCHEMA and value["state"] == "PASS",
                           "preflight receipt schema/state drift")
    _validate_age(value["created_at_utc"], max_age_seconds, "preflight receipt")
    validate_current_repo(value["repo"], "preflight.repo")
    inputs = store_contract.exact_keys(
        value["inputs"], ("request", "runtime_manifest", "store_manifest"), "preflight inputs"
    )
    for name, reference_value in inputs.items():
        store_contract.validate_file_ref(reference_value, f"preflight input {name}", recompute=True)
    docker = store_contract.validate_file_ref(value["docker"], "preflight Docker", recompute=True)
    store_contract.require(os.access(docker, os.X_OK), "preflight Docker client is not executable")
    spec = validate_launch_spec(value["spec"])
    store_contract.require(
        store_contract.exact_sha(value["spec_sha256"], "preflight spec SHA")
        == store_contract.canonical_json_sha(spec),
        "preflight launch spec digest mismatch",
    )
    p02b = store_contract.exact_keys(
        value["p02b"],
        (
            "result", "validator", "binary", "pass_marker", "provenance", "canonical_dataset_manifest",
            "validator_argv_sha256", "current_host", "max_age_seconds",
        ),
        "preflight P02B",
    )
    p02b_paths = {
        name: store_contract.validate_file_ref(p02b[name], f"preflight P02B {name}", recompute=True)
        for name in ("result", "validator", "binary", "pass_marker", "provenance", "canonical_dataset_manifest")
    }
    store_contract.require(os.access(p02b_paths["validator"], os.X_OK),
                           "preflight P02B validator is not executable")
    store_contract.require(os.access(p02b_paths["binary"], os.X_OK),
                           "preflight P02B binary is not executable")
    store_contract.exact_sha(p02b["validator_argv_sha256"], "preflight P02B validator argv SHA")
    host = store_contract.exact_keys(
        p02b["current_host"], ("hostname", "fingerprint_sha256"), "preflight P02B host"
    )
    store_contract.nonempty(host["hostname"], "preflight P02B hostname")
    store_contract.exact_sha(host["fingerprint_sha256"], "preflight P02B host fingerprint")
    actual_host = adapter.p31_host_facts()
    store_contract.require(
        host == {
            "hostname": actual_host["hostname"],
            "fingerprint_sha256": actual_host["fingerprint_sha256"],
        },
        "preflight receipt host differs from current host",
    )
    store_contract.integer(p02b["max_age_seconds"], "preflight P02B max age", 1)
    preconditions = store_contract.exact_keys(
        value["preconditions"],
        ("store_and_logs_offline", "owned_names_absent", "graph_port_available", "role_log_paths_absent"),
        "preflight preconditions",
    )
    store_contract.require(all(item is True for item in preconditions.values()),
                           "preflight receipt contains a failed precondition")
    return receipt_path, value, docker


def validate_network_inspect(
    spec: dict[str, Any],
    value: object,
    *,
    preflight_sha256: str,
    expected_container_ids: dict[str, str],
) -> dict[str, Any]:
    store_contract.exact_sha(preflight_sha256, "network preflight SHA")
    expected_ids = store_contract.exact_keys(expected_container_ids, ROLE_ORDER, "network expected containers")
    for role, container_id in expected_ids.items():
        store_contract.require(
            isinstance(container_id, str) and CONTAINER_ID_RE.fullmatch(container_id) is not None,
            f"network expected container ID is malformed: {role}",
        )
    store_contract.require(isinstance(value, dict), "network inspect is not an object")
    network_id = value.get("Id")
    store_contract.require(isinstance(network_id, str) and CONTAINER_ID_RE.fullmatch(network_id),
                           "network ID is malformed")
    store_contract.require(value.get("Name") == spec["network"]["name"], "network name drift")
    store_contract.require(value.get("Driver") == spec["network"]["driver"], "network driver drift")
    labels = value.get("Labels")
    store_contract.require(isinstance(labels, dict), "network labels are malformed")
    expected_labels = {**spec["labels"], "cidr.p10.preflight_sha256": preflight_sha256}
    store_contract.require(labels == expected_labels, "network labels drift")
    containers = value.get("Containers")
    store_contract.require(isinstance(containers, dict), "network container map is malformed")
    store_contract.require(set(containers) == set(expected_ids.values()),
                           "network container membership drift")
    return {"name": value["Name"], "network_id": network_id, "driver": value["Driver"]}


def _network_inspect_one(
    docker: Path, name_or_id: str, *, timeout: int | None = 60
) -> dict[str, Any]:
    completed = docker_call(
        docker, ["network", "inspect", name_or_id], check=True, timeout=timeout
    )
    try:
        values = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError("Docker network inspect returned malformed JSON") from exc
    store_contract.require(isinstance(values, list) and len(values) == 1 and isinstance(values[0], dict),
                           "Docker network inspect returned unexpected shape")
    return values[0]


def _wait_running(docker: Path, name: str, deadline: float) -> None:
    last_state: object = "not inspected"
    while time.monotonic() < deadline:
        value = docker_inspect_one(
            docker,
            name,
            timeout=_remaining_timeout(deadline, 30, f"{name} running-state inspect"),
        )
        state = value.get("State")
        if isinstance(state, dict):
            last_state = {key: state.get(key) for key in ("Status", "Running", "ExitCode", "Error")}
            if state.get("Running") is True and state.get("Status") == "running":
                return
            if state.get("Status") in ("exited", "dead"):
                raise ContractError(f"container {name} exited during startup: {last_state}")
        time.sleep(0.5)
    raise ContractError(f"container {name} did not become running before deadline: {last_state}")


def _remaining_timeout(deadline: float, cap_seconds: int, context: str) -> int:
    remaining = deadline - time.monotonic()
    store_contract.require(remaining > 0, f"{context}: startup deadline exhausted")
    return max(1, min(cap_seconds, int(math.ceil(remaining))))


def _initial_partial_resources() -> dict[str, Any]:
    return {
        "network": {"state": "NOT_ATTEMPTED", "id": None},
        "containers": {
            role: {"state": "NOT_ATTEMPTED", "id": None} for role in ROLE_ORDER
        },
    }


def _write_partial_start_receipt(
    output: Path,
    *,
    repo: dict[str, Any],
    preflight_path: Path,
    preflight_sha256: str,
    spec_sha256: str,
    stage: str,
    failure: Exception,
    resources: dict[str, Any],
) -> None:
    message = str(failure)
    if len(message) > 2000:
        message = message[:2000] + "..."
    receipt = {
        "schema_version": PARTIAL_START_SCHEMA,
        "state": "FAILED",
        "created_at_utc": utc_now(),
        "repo": repo,
        "preflight": {"path": str(preflight_path), "sha256": preflight_sha256},
        "spec_sha256": spec_sha256,
        "failed_stage": stage,
        "failure": {"error_type": type(failure).__name__, "message": message or "unspecified failure"},
        "resources": resources,
        "timing_scope": "pre-adapter-outside-timed-region-v1",
    }
    store_contract.write_json_exclusive(output, receipt)


def validate_partial_start_receipt(
    path: Path,
    sha256: str,
    *,
    preflight_path: Path,
    preflight_sha256: str,
    preflight_value: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    receipt_path = store_contract.validate_file_ref(
        {"path": str(path), "sha256": sha256}, "partial start receipt", recompute=True
    )
    value = store_contract.exact_keys(
        store_contract.read_strict_json(receipt_path, "partial start receipt"),
        (
            "schema_version", "state", "created_at_utc", "repo", "preflight", "spec_sha256",
            "failed_stage", "failure", "resources", "timing_scope",
        ),
        "partial start receipt",
    )
    store_contract.require(
        value["schema_version"] == PARTIAL_START_SCHEMA and value["state"] == "FAILED",
        "partial start receipt schema/state drift",
    )
    store_contract.parse_timestamp(value["created_at_utc"], "partial start created_at")
    validate_current_repo(value["repo"], "partial start.repo")
    store_contract.validate_file_ref(
        value["preflight"],
        "partial start.preflight",
        expected_path=preflight_path,
        expected_sha256=preflight_sha256,
        recompute=True,
    )
    store_contract.require(
        store_contract.exact_sha(value["spec_sha256"], "partial start spec SHA")
        == preflight_value["spec_sha256"],
        "partial start/preflight spec SHA drift",
    )
    store_contract.nonempty(value["failed_stage"], "partial start failed stage")
    failure = store_contract.exact_keys(
        value["failure"], ("error_type", "message"), "partial start failure"
    )
    store_contract.nonempty(failure["error_type"], "partial start error type")
    store_contract.nonempty(failure["message"], "partial start error message")
    resources = store_contract.exact_keys(
        value["resources"], ("network", "containers"), "partial start resources"
    )
    network = store_contract.exact_keys(resources["network"], ("state", "id"), "partial network")
    store_contract.require(network["state"] in RESOURCE_STATES, "partial network state drift")
    if network["state"] == "KNOWN":
        store_contract.require(
            isinstance(network["id"], str) and CONTAINER_ID_RE.fullmatch(network["id"]) is not None,
            "partial network known ID is malformed",
        )
    else:
        store_contract.require(network["id"] is None, "partial non-known network must not claim an ID")
    containers = store_contract.exact_keys(resources["containers"], ROLE_ORDER, "partial containers")
    normalized_states: list[str] = []
    for role in START_ORDER:
        item = store_contract.exact_keys(containers[role], ("state", "id"), f"partial container {role}")
        store_contract.require(item["state"] in RESOURCE_STATES, f"partial {role} state drift")
        if item["state"] == "KNOWN":
            store_contract.require(
                isinstance(item["id"], str) and CONTAINER_ID_RE.fullmatch(item["id"]) is not None,
                f"partial {role} known ID is malformed",
            )
        else:
            store_contract.require(item["id"] is None, f"partial non-known {role} must not claim an ID")
        normalized_states.append(item["state"])
    seen_non_known = False
    for state in normalized_states:
        if state == "KNOWN":
            store_contract.require(not seen_non_known, "partial known container appears after incomplete role")
        else:
            seen_non_known = True
    store_contract.require(normalized_states.count("UNCERTAIN") <= 1,
                           "partial start may contain at most one uncertain container")
    if network["state"] != "KNOWN":
        store_contract.require(all(state == "NOT_ATTEMPTED" for state in normalized_states),
                               "partial container was attempted without a known network ID")
    store_contract.require(value["timing_scope"] == "pre-adapter-outside-timed-region-v1",
                           "partial start timing scope drift")
    return receipt_path, value


def _inspect_optional_exact_id(docker: Path, kind: str, identifier: str) -> dict[str, Any] | None:
    store_contract.require(kind in ("container", "network"), "unsupported Docker resource kind")
    store_contract.require(CONTAINER_ID_RE.fullmatch(identifier) is not None,
                           f"{kind} cleanup identifier is not a full ID")
    completed = docker_call(docker, [kind, "inspect", identifier], check=False, timeout=30)
    if completed.returncode == 0:
        try:
            values = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ContractError(f"Docker {kind} inspect returned malformed JSON") from exc
        store_contract.require(
            isinstance(values, list) and len(values) == 1 and isinstance(values[0], dict),
            f"Docker {kind} inspect returned unexpected shape",
        )
        return values[0]
    error = (completed.stderr + "\n" + completed.stdout).lower()
    absent_markers = (
        ("no such container", "no such object")
        if kind == "container"
        else ("no such network", "no such object")
    )
    if any(marker in error for marker in absent_markers):
        return None
    raise ContractError(f"cannot verify exact {kind} ID {identifier}: {completed.stderr.strip()}")


def validate_partial_network_inspect(
    spec: dict[str, Any],
    value: object,
    *,
    preflight_sha256: str,
    network_id: str,
    present_container_ids: set[str],
) -> dict[str, Any]:
    store_contract.exact_sha(preflight_sha256, "partial cleanup preflight SHA")
    store_contract.require(CONTAINER_ID_RE.fullmatch(network_id) is not None,
                           "partial cleanup network ID is malformed")
    store_contract.require(isinstance(value, dict), "partial cleanup network inspect is malformed")
    store_contract.require(value.get("Id") == network_id, "partial cleanup network ID drift")
    store_contract.require(value.get("Name") == spec["network"]["name"],
                           "partial cleanup network name drift")
    store_contract.require(value.get("Driver") == spec["network"]["driver"],
                           "partial cleanup network driver drift")
    expected_labels = {**spec["labels"], "cidr.p10.preflight_sha256": preflight_sha256}
    store_contract.require(value.get("Labels") == expected_labels,
                           "partial cleanup network labels drift")
    members = value.get("Containers")
    store_contract.require(isinstance(members, dict) and set(members) == present_container_ids,
                           "partial cleanup network membership drift")
    return {"name": value["Name"], "network_id": network_id, "driver": value["Driver"]}


def _cleanup_receipt(
    *,
    state: str,
    repo: dict[str, Any],
    preflight_path: Path,
    preflight_sha256: str,
    partial_path: Path,
    partial_sha256: str,
    known_container_ids: dict[str, str],
    known_network_id: str | None,
    container_snapshots: dict[str, object],
    network_snapshot: object,
    identities_revalidated: bool,
    commands: list[list[str]],
    blocked_reason: str | None,
    containers_absent: bool | None,
    network_absent: bool | None,
) -> dict[str, Any]:
    return {
        "schema_version": CLEANUP_SCHEMA,
        "state": state,
        "created_at_utc": utc_now(),
        "repo": repo,
        "preflight": {"path": str(preflight_path), "sha256": preflight_sha256},
        "partial_start": {"path": str(partial_path), "sha256": partial_sha256},
        "known_resources": {
            "container_ids": known_container_ids,
            "network_id": known_network_id,
        },
        "verification": {
            "name_lookup_used": False,
            "all_present_identities_revalidated": identities_revalidated,
            "container_snapshots": container_snapshots,
            "network_snapshot": network_snapshot,
        },
        "commands": commands,
        "blocked_reason": blocked_reason,
        "postconditions": {
            "known_container_ids_absent": containers_absent,
            "known_network_id_absent": network_absent,
            "names_not_used": True,
        },
        "timing_scope": "pre-adapter-failure-cleanup-outside-timed-region-v1",
    }


def perform_partial_cleanup(
    *,
    docker: Path,
    preflight_path: Path,
    preflight_value: dict[str, Any],
    partial_path: Path,
    partial_value: dict[str, Any],
    output: Path,
    stop_timeout_seconds: int,
) -> dict[str, Any]:
    """Delete only exact, fully revalidated IDs; otherwise publish BLOCKED evidence."""

    store_contract.require(1 <= stop_timeout_seconds <= 120,
                           "partial cleanup stop timeout must be in [1, 120] seconds")
    spec = preflight_value["spec"]
    output_path = _safe_output(output, spec, preflight_value["repo"], "partial cleanup receipt")
    preflight_sha = sha256_file(preflight_path)
    partial_sha = sha256_file(partial_path)
    resources = partial_value["resources"]
    known_container_ids = {
        role: resources["containers"][role]["id"]
        for role in ROLE_ORDER
        if resources["containers"][role]["state"] == "KNOWN"
    }
    known_network_id = resources["network"]["id"] if resources["network"]["state"] == "KNOWN" else None
    container_snapshots: dict[str, object] = {role: None for role in ROLE_ORDER}
    network_snapshot: object = None
    commands: list[list[str]] = []

    def publish_blocked(reason: str) -> dict[str, Any]:
        receipt = _cleanup_receipt(
            state="BLOCKED",
            repo=preflight_value["repo"],
            preflight_path=preflight_path,
            preflight_sha256=preflight_sha,
            partial_path=partial_path,
            partial_sha256=partial_sha,
            known_container_ids=known_container_ids,
            known_network_id=known_network_id,
            container_snapshots=container_snapshots,
            network_snapshot=network_snapshot,
            identities_revalidated=False,
            commands=commands,
            blocked_reason=reason[:2000],
            containers_absent=None,
            network_absent=None,
        )
        store_contract.write_json_exclusive(output_path, receipt)
        return receipt

    uncertain = []
    if resources["network"]["state"] == "UNCERTAIN":
        uncertain.append("network")
    uncertain.extend(
        role for role in ROLE_ORDER if resources["containers"][role]["state"] == "UNCERTAIN"
    )
    if uncertain:
        return publish_blocked("resource identity is uncertain: " + ",".join(uncertain))

    present_ids: set[str] = set()
    try:
        for role, identifier in known_container_ids.items():
            inspected = _inspect_optional_exact_id(docker, "container", identifier)
            if inspected is None:
                continue
            snapshot = validate_container_inspect(
                spec, role, inspected, preflight_sha256=preflight_sha, require_running=False
            )
            store_contract.require(snapshot["container_id"] == identifier,
                                   f"partial cleanup {role} exact ID drift")
            container_snapshots[role] = snapshot
            present_ids.add(identifier)
        if known_network_id is not None:
            inspected_network = _inspect_optional_exact_id(docker, "network", known_network_id)
            if inspected_network is not None:
                network_snapshot = validate_partial_network_inspect(
                    spec,
                    inspected_network,
                    preflight_sha256=preflight_sha,
                    network_id=known_network_id,
                    present_container_ids=present_ids,
                )
        elif present_ids:
            raise ContractError("known containers exist without a known network ID")
    except Exception as exc:  # noqa: BLE001 - every post-mutation failure needs typed cleanup evidence
        return publish_blocked(f"exact-ID identity revalidation failed: {exc}")

    try:
        for role in STOP_ORDER:
            if container_snapshots[role] is None:
                continue
            identifier = known_container_ids[role]
            command = ["container", "stop", "--time", str(stop_timeout_seconds), identifier]
            commands.append(command)
            docker_call(docker, command, check=True, timeout=stop_timeout_seconds + 30)
        for role in STOP_ORDER:
            if container_snapshots[role] is None:
                continue
            command = ["container", "rm", known_container_ids[role]]
            commands.append(command)
            docker_call(docker, command, check=True, timeout=60)
        if network_snapshot is not None and known_network_id is not None:
            command = ["network", "rm", known_network_id]
            commands.append(command)
            docker_call(docker, command, check=True, timeout=60)
        for identifier in known_container_ids.values():
            store_contract.require(
                _inspect_optional_exact_id(docker, "container", identifier) is None,
                f"known container ID remains after cleanup: {identifier}",
            )
        if known_network_id is not None:
            store_contract.require(
                _inspect_optional_exact_id(docker, "network", known_network_id) is None,
                f"known network ID remains after cleanup: {known_network_id}",
            )
    except (ContractError, OSError, subprocess.SubprocessError, ValueError) as exc:
        return publish_blocked(f"exact-ID cleanup or postcondition failed: {exc}")

    receipt = _cleanup_receipt(
        state="PASS",
        repo=preflight_value["repo"],
        preflight_path=preflight_path,
        preflight_sha256=preflight_sha,
        partial_path=partial_path,
        partial_sha256=partial_sha,
        known_container_ids=known_container_ids,
        known_network_id=known_network_id,
        container_snapshots=container_snapshots,
        network_snapshot=network_snapshot,
        identities_revalidated=True,
        commands=commands,
        blocked_reason=None,
        containers_absent=True,
        network_absent=True,
    )
    store_contract.write_json_exclusive(output_path, receipt)
    return receipt


def validate_cleanup_receipt(
    path: Path,
    sha256: str,
    *,
    preflight_path: Path,
    preflight_sha256: str,
    partial_path: Path,
    partial_sha256: str,
    preflight_value: dict[str, Any],
    partial_value: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    receipt_path = store_contract.validate_file_ref(
        {"path": str(path), "sha256": sha256}, "partial cleanup receipt", recompute=True
    )
    value = store_contract.exact_keys(
        store_contract.read_strict_json(receipt_path, "partial cleanup receipt"),
        (
            "schema_version", "state", "created_at_utc", "repo", "preflight", "partial_start",
            "known_resources", "verification", "commands", "blocked_reason", "postconditions",
            "timing_scope",
        ),
        "partial cleanup receipt",
    )
    store_contract.require(value["schema_version"] == CLEANUP_SCHEMA, "cleanup receipt schema drift")
    store_contract.require(value["state"] in ("PASS", "BLOCKED"), "cleanup receipt state drift")
    store_contract.parse_timestamp(value["created_at_utc"], "cleanup receipt created_at")
    validate_current_repo(value["repo"], "cleanup.repo")
    store_contract.validate_file_ref(
        value["preflight"],
        "cleanup.preflight",
        expected_path=preflight_path,
        expected_sha256=preflight_sha256,
        recompute=True,
    )
    store_contract.validate_file_ref(
        value["partial_start"],
        "cleanup.partial_start",
        expected_path=partial_path,
        expected_sha256=partial_sha256,
        recompute=True,
    )
    known = store_contract.exact_keys(
        value["known_resources"], ("container_ids", "network_id"), "cleanup known resources"
    )
    store_contract.require(isinstance(known["container_ids"], dict),
                           "cleanup known container IDs are malformed")
    expected_ids = {
        role: partial_value["resources"]["containers"][role]["id"]
        for role in ROLE_ORDER
        if partial_value["resources"]["containers"][role]["state"] == "KNOWN"
    }
    store_contract.require(known["container_ids"] == expected_ids,
                           "cleanup known container IDs differ from partial start")
    for role, identifier in known["container_ids"].items():
        store_contract.require(role in ROLE_ORDER and isinstance(identifier, str)
                               and CONTAINER_ID_RE.fullmatch(identifier) is not None,
                               "cleanup known container ID is malformed")
    expected_network_id = (
        partial_value["resources"]["network"]["id"]
        if partial_value["resources"]["network"]["state"] == "KNOWN"
        else None
    )
    store_contract.require(known["network_id"] == expected_network_id,
                           "cleanup known network ID differs from partial start")
    verification = store_contract.exact_keys(
        value["verification"],
        (
            "name_lookup_used", "all_present_identities_revalidated", "container_snapshots",
            "network_snapshot",
        ),
        "cleanup verification",
    )
    store_contract.require(verification["name_lookup_used"] is False,
                           "cleanup evidence used a forbidden name lookup")
    store_contract.require(isinstance(verification["all_present_identities_revalidated"], bool),
                           "cleanup identity verification flag is malformed")
    snapshots = store_contract.exact_keys(
        verification["container_snapshots"], ROLE_ORDER, "cleanup container snapshots"
    )
    present_roles = []
    spec = preflight_value["spec"]
    for role in ROLE_ORDER:
        snapshot = snapshots[role]
        if snapshot is None:
            continue
        item = store_contract.exact_keys(
            snapshot,
            (
                "role", "name", "logical_host", "container_id", "image_id", "config_image", "pid",
                "process_start_ticks", "started_at_utc", "restart_count", "running",
            ),
            f"cleanup container snapshot {role}",
        )
        expected = spec["roles"][role]
        store_contract.require(
            item["role"] == role
            and item["name"] == expected["container_name"]
            and item["logical_host"] == expected["logical_host"]
            and item["container_id"] == expected_ids.get(role)
            and item["image_id"] == expected["image"]["image_id"]
            and item["config_image"] == expected["image"]["repo_digest"],
            f"cleanup container snapshot identity drift: {role}",
        )
        store_contract.require(isinstance(item["pid"], int) and not isinstance(item["pid"], bool)
                               and item["pid"] >= 0, f"cleanup container PID malformed: {role}")
        if item["running"]:
            store_contract.require(
                not isinstance(item["process_start_ticks"], bool)
                and isinstance(item["process_start_ticks"], int)
                and item["process_start_ticks"] > 0,
                f"cleanup running container start ticks malformed: {role}",
            )
        else:
            store_contract.require(
                item["process_start_ticks"] is None,
                f"cleanup stopped container unexpectedly claims live start ticks: {role}",
            )
        store_contract.parse_timestamp(item["started_at_utc"], f"cleanup container StartedAt {role}")
        store_contract.require(item["restart_count"] == 0 and isinstance(item["running"], bool),
                               f"cleanup container lifecycle drift: {role}")
        present_roles.append(role)
    network_snapshot = verification["network_snapshot"]
    if network_snapshot is not None:
        item = store_contract.exact_keys(
            network_snapshot, ("name", "network_id", "driver"), "cleanup network snapshot"
        )
        store_contract.require(
            item
            == {
                "name": spec["network"]["name"],
                "network_id": expected_network_id,
                "driver": spec["network"]["driver"],
            },
            "cleanup network snapshot identity drift",
        )
    commands = value["commands"]
    store_contract.require(
        isinstance(commands, list)
        and all(isinstance(command, list) and all(isinstance(token, str) for token in command)
                for command in commands),
        "cleanup command evidence is malformed",
    )
    ordered_present_roles = [role for role in STOP_ORDER if role in present_roles]
    stop_timeout_text = "1"
    if ordered_present_roles and commands:
        first_command = commands[0]
        store_contract.require(
            len(first_command) == 5
            and first_command[:3] == ["container", "stop", "--time"]
            and first_command[4] == expected_ids[ordered_present_roles[0]],
            "cleanup command sequence does not begin with the first exact-ID stop",
        )
        stop_timeout_text = first_command[3]
        timeout = int(stop_timeout_text) if len(stop_timeout_text) <= 3 and stop_timeout_text.isdigit() else 0
        store_contract.require(1 <= timeout <= 120, "cleanup command stop timeout is invalid")
    expected_commands = [
        ["container", "stop", "--time", stop_timeout_text, expected_ids[role]]
        for role in ordered_present_roles
    ]
    expected_commands.extend(
        [["container", "rm", expected_ids[role]] for role in STOP_ORDER if role in present_roles]
    )
    if network_snapshot is not None and expected_network_id is not None:
        expected_commands.append(["network", "rm", expected_network_id])
    forbidden_names = {
        spec["network"]["name"],
        *(spec["roles"][role]["container_name"] for role in ROLE_ORDER),
    }
    store_contract.require(
        all(forbidden_names.isdisjoint(command) for command in commands),
        "cleanup command evidence contains a forbidden resource name",
    )
    postconditions = store_contract.exact_keys(
        value["postconditions"],
        ("known_container_ids_absent", "known_network_id_absent", "names_not_used"),
        "cleanup postconditions",
    )
    store_contract.require(postconditions["names_not_used"] is True,
                           "cleanup postcondition does not forbid name use")
    if value["state"] == "PASS":
        store_contract.require(value["blocked_reason"] is None, "PASS cleanup claims a blocked reason")
        store_contract.require(verification["all_present_identities_revalidated"] is True,
                               "PASS cleanup lacks identity revalidation")
        store_contract.require(commands == expected_commands, "PASS cleanup command sequence drift")
        store_contract.require(
            postconditions["known_container_ids_absent"] is True
            and postconditions["known_network_id_absent"] is True,
            "PASS cleanup lacks exact-ID absence postconditions",
        )
    else:
        store_contract.nonempty(value["blocked_reason"], "BLOCKED cleanup reason")
        store_contract.require(verification["all_present_identities_revalidated"] is False,
                               "BLOCKED cleanup incorrectly claims complete revalidation")
        store_contract.require(commands == expected_commands[: len(commands)],
                               "BLOCKED cleanup commands are not a safe exact-ID prefix")
        store_contract.require(
            postconditions["known_container_ids_absent"] is None
            and postconditions["known_network_id_absent"] is None,
            "BLOCKED cleanup must not claim absence postconditions",
        )
    store_contract.require(
        value["timing_scope"] == "pre-adapter-failure-cleanup-outside-timed-region-v1",
        "cleanup timing scope drift",
    )
    return receipt_path, value


def start(args: argparse.Namespace) -> None:
    preflight_path, preflight_value, docker = validate_preflight_receipt(
        args.preflight,
        args.preflight_sha256,
        max_age_seconds=args.max_preflight_age_seconds,
    )
    spec = preflight_value["spec"]
    repo = preflight_value["repo"]
    output = _safe_output(args.output, spec, repo, "start receipt")
    partial_output = _safe_output(args.partial_output, spec, repo, "partial start receipt")
    cleanup_output = _safe_output(args.cleanup_output, spec, repo, "automatic cleanup receipt")
    store_contract.require(
        len({output, partial_output, cleanup_output}) == 3,
        "start, partial-start, and cleanup receipt paths must be distinct",
    )
    store_contract.require(args.startup_timeout_seconds >= 1 and args.startup_timeout_seconds <= 900,
                           "startup timeout must be in [1, 900] seconds")
    store_contract.require(
        args.cleanup_stop_timeout_seconds >= 1 and args.cleanup_stop_timeout_seconds <= 120,
        "cleanup stop timeout must be in [1, 120] seconds",
    )
    deadline = time.monotonic() + args.startup_timeout_seconds
    preflight_sha = sha256_file(preflight_path)
    data_root = Path(spec["roles"]["metad"]["mounts"][0]["source"]).parent
    logs_root = Path(spec["logs_root"])
    store_contract.assert_offline((data_root, logs_root), docker=docker)
    assert_owned_names_absent(docker, spec)
    assert_port_available(spec["endpoint"]["host"], spec["endpoint"]["port"])
    resources = _initial_partial_resources()
    stage = "role-log-directory-creation"
    try:
        for role in ROLE_ORDER:
            Path(spec["roles"][role]["mounts"][1]["source"]).mkdir(mode=0o700, exist_ok=False)

        stage = "network-create"
        network_labels = {**spec["labels"], "cidr.p10.preflight_sha256": preflight_sha}
        network_args = ["network", "create", "--driver", spec["network"]["driver"]]
        for key in sorted(network_labels):
            network_args.extend(("--label", f"{key}={network_labels[key]}"))
        network_args.append(spec["network"]["name"])
        resources["network"] = {"state": "UNCERTAIN", "id": None}
        network_created = docker_call(
            docker,
            network_args,
            check=True,
            timeout=_remaining_timeout(deadline, 60, "network create"),
        )
        network_id = network_created.stdout.strip()
        store_contract.require(CONTAINER_ID_RE.fullmatch(network_id) is not None,
                               "Docker network create returned malformed ID")
        resources["network"] = {"state": "KNOWN", "id": network_id}

        returned_ids: dict[str, str] = {}
        for role in START_ORDER:
            stage = f"{role}-docker-run"
            resources["containers"][role] = {"state": "UNCERTAIN", "id": None}
            completed = docker_call(
                docker,
                docker_run_argv(spec, role, preflight_sha),
                check=True,
                timeout=_remaining_timeout(deadline, 120, f"{role} Docker run"),
            )
            container_id = completed.stdout.strip()
            store_contract.require(CONTAINER_ID_RE.fullmatch(container_id) is not None,
                                   f"Docker run returned malformed container ID for {role}")
            returned_ids[role] = container_id
            resources["containers"][role] = {"state": "KNOWN", "id": container_id}
            stage = f"{role}-wait-running"
            _wait_running(docker, spec["roles"][role]["container_name"], deadline)

        stage = "pre-live-identity-validation"
        before_live: dict[str, dict[str, Any]] = {}
        for role in ROLE_ORDER:
            inspected = docker_inspect_one(
                docker,
                spec["roles"][role]["container_name"],
                timeout=_remaining_timeout(deadline, 30, f"{role} pre-live inspect"),
            )
            before_live[role] = validate_container_inspect(
                spec, role, inspected, preflight_sha256=preflight_sha, require_running=True
            )
            store_contract.require(before_live[role]["container_id"] == returned_ids[role],
                                   f"{role} Docker run/inspect ID drift")
        network_snapshot = validate_network_inspect(
            spec,
            _network_inspect_one(
                docker,
                spec["network"]["name"],
                timeout=_remaining_timeout(deadline, 30, "pre-live network inspect"),
            ),
            preflight_sha256=preflight_sha,
            expected_container_ids=returned_ids,
        )
        store_contract.require(network_snapshot["network_id"] == network_id,
                               "network create/inspect ID drift")

        stage = "live-gate"
        password_env = spec["authentication"]["password_env"]
        password = os.environ.get(password_env)
        store_contract.require(isinstance(password, str) and bool(password), f"{password_env} is required")
        live_gate = capture_live_gate(
            spec,
            password=password,
            startup_timeout_seconds=args.startup_timeout_seconds,
            overall_deadline=deadline,
        )
        stage = "post-live-identity-validation"
        after_live: dict[str, dict[str, Any]] = {}
        for role in ROLE_ORDER:
            inspected = docker_inspect_one(
                docker,
                returned_ids[role],
                timeout=_remaining_timeout(deadline, 30, f"{role} post-live inspect"),
            )
            after_live[role] = validate_container_inspect(
                spec, role, inspected, preflight_sha256=preflight_sha, require_running=True
            )
            for key in (
                "container_id", "image_id", "config_image", "pid", "process_start_ticks",
                "started_at_utc", "restart_count",
            ):
                store_contract.require(after_live[role][key] == before_live[role][key],
                                       f"{role} lifecycle changed across live gate: {key}")
        validate_network_inspect(
            spec,
            _network_inspect_one(
                docker,
                network_id,
                timeout=_remaining_timeout(deadline, 30, "post-live network inspect"),
            ),
            preflight_sha256=preflight_sha,
            expected_container_ids=returned_ids,
        )
        stage = "start-receipt-publication"
        receipt = {
            "schema_version": START_SCHEMA,
            "state": "PASS",
            "created_at_utc": utc_now(),
            "repo": repo,
            "preflight": {"path": str(preflight_path), "sha256": preflight_sha},
            "spec_sha256": preflight_value["spec_sha256"],
            "network": network_snapshot,
            "containers": after_live,
            "live_gate": live_gate,
            "lifecycle_stable": True,
            "timing_scope": "pre-adapter-outside-timed-region-v1",
        }
        store_contract.write_json_exclusive(output, receipt)
    except Exception as exc:  # noqa: BLE001 - every post-mutation failure needs typed cleanup evidence
        try:
            _write_partial_start_receipt(
                partial_output,
                repo=repo,
                preflight_path=preflight_path,
                preflight_sha256=preflight_sha,
                spec_sha256=preflight_value["spec_sha256"],
                stage=stage,
                failure=exc,
                resources=resources,
            )
            partial_path, partial_value = validate_partial_start_receipt(
                partial_output,
                sha256_file(partial_output),
                preflight_path=preflight_path,
                preflight_sha256=preflight_sha,
                preflight_value=preflight_value,
            )
            cleanup_receipt = perform_partial_cleanup(
                docker=docker,
                preflight_path=preflight_path,
                preflight_value=preflight_value,
                partial_path=partial_path,
                partial_value=partial_value,
                output=cleanup_output,
                stop_timeout_seconds=args.cleanup_stop_timeout_seconds,
            )
            validate_cleanup_receipt(
                cleanup_output,
                sha256_file(cleanup_output),
                preflight_path=preflight_path,
                preflight_sha256=preflight_sha,
                partial_path=partial_path,
                partial_sha256=sha256_file(partial_path),
                preflight_value=preflight_value,
                partial_value=partial_value,
            )
            print(f"partial_start_receipt={partial_output}", file=sys.stderr)
            print(f"cleanup_receipt={cleanup_output} state={cleanup_receipt['state']}", file=sys.stderr)
        except Exception as cleanup_exc:  # noqa: BLE001 - preserve both the start and evidence failures
            raise ContractError(
                f"start failed at {stage}: {exc}; partial cleanup evidence failed: {cleanup_exc}"
            ) from exc
        raise
    else:
        # Do not remove a service after its PASS receipt is already durable merely
        # because the stdout consumer disappeared.
        print(output)


def validate_start_receipt(
    path: Path,
    sha256: str,
    *,
    preflight_path: Path,
    preflight_sha256: str,
    preflight_value: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    receipt_path = store_contract.validate_file_ref(
        {"path": str(path), "sha256": sha256}, "start receipt", recompute=True
    )
    value = store_contract.exact_keys(
        store_contract.read_strict_json(receipt_path, "start receipt"),
        (
            "schema_version", "state", "created_at_utc", "repo", "preflight", "spec_sha256",
            "network", "containers", "live_gate", "lifecycle_stable", "timing_scope",
        ),
        "start receipt",
    )
    store_contract.require(value["schema_version"] == START_SCHEMA and value["state"] == "PASS",
                           "start receipt schema/state drift")
    store_contract.parse_timestamp(value["created_at_utc"], "start receipt created_at")
    validate_current_repo(value["repo"], "start.repo")
    store_contract.validate_file_ref(
        value["preflight"],
        "start.preflight",
        expected_path=preflight_path,
        expected_sha256=preflight_sha256,
        recompute=True,
    )
    store_contract.require(value["spec_sha256"] == preflight_value["spec_sha256"],
                           "start/preflight spec SHA drift")
    network = store_contract.exact_keys(value["network"], ("name", "network_id", "driver"), "start network")
    store_contract.require(network["name"] == preflight_value["spec"]["network"]["name"],
                           "start network name drift")
    store_contract.require(network["driver"] == "bridge", "start network driver drift")
    store_contract.require(isinstance(network["network_id"], str)
                           and CONTAINER_ID_RE.fullmatch(network["network_id"]), "start network ID malformed")
    containers = store_contract.exact_keys(value["containers"], ROLE_ORDER, "start containers")
    container_ids: set[str] = set()
    for role in ROLE_ORDER:
        snapshot = store_contract.exact_keys(
            containers[role],
            (
                "role", "name", "logical_host", "container_id", "image_id", "config_image", "pid",
                "process_start_ticks", "started_at_utc", "restart_count", "running",
            ),
            f"start container {role}",
        )
        expected = preflight_value["spec"]["roles"][role]
        store_contract.require(snapshot["role"] == role and snapshot["name"] == expected["container_name"],
                               f"start {role} identity drift")
        store_contract.require(snapshot["logical_host"] == expected["logical_host"],
                               f"start {role} logical host drift")
        store_contract.require(isinstance(snapshot["container_id"], str)
                               and CONTAINER_ID_RE.fullmatch(snapshot["container_id"]),
                               f"start {role} container ID malformed")
        container_ids.add(snapshot["container_id"])
        store_contract.require(snapshot["image_id"] == expected["image"]["image_id"],
                               f"start {role} image ID drift")
        store_contract.require(snapshot["config_image"] == expected["image"]["repo_digest"],
                               f"start {role} RepoDigest drift")
        store_contract.require(isinstance(snapshot["pid"], int) and snapshot["pid"] > 0,
                               f"start {role} PID malformed")
        store_contract.require(
            not isinstance(snapshot["process_start_ticks"], bool)
            and isinstance(snapshot["process_start_ticks"], int)
            and snapshot["process_start_ticks"] > 0,
            f"start {role} process start ticks malformed",
        )
        store_contract.parse_timestamp(snapshot["started_at_utc"], f"start {role} StartedAt")
        store_contract.require(snapshot["restart_count"] == 0 and snapshot["running"] is True,
                               f"start {role} lifecycle drift")
    store_contract.require(len(container_ids) == 3, "start container IDs must be unique")
    live = store_contract.exact_keys(
        value["live_gate"],
        (
            "state", "hosts", "partition_count", "partition_ids", "edge_labels", "tag_count",
            "edge_index_count", "tag_index_count", "results_sha256", "attempts", "query_timeout_ms",
            "startup_timeout_seconds", "captured_at_utc",
        ),
        "start live gate",
    )
    store_contract.require(live["state"] == "PASS", "start live gate is not PASS")
    hosts = store_contract.exact_keys(live["hosts"], ROLE_ORDER, "start live hosts")
    expected_ports = {"metad": 9559, "storaged": 9779, "graphd": 9669}
    for role in ROLE_ORDER:
        host = store_contract.exact_keys(hosts[role], ("host", "port", "status"), f"start live host {role}")
        store_contract.require(
            host
            == {
                "host": preflight_value["spec"]["roles"][role]["logical_host"],
                "port": expected_ports[role],
                "status": "ONLINE",
            },
            f"start live host proof drift: {role}",
        )
    store_contract.require(live["partition_count"] == 64
                           and live["partition_ids"] == list(range(1, 65)), "start partition proof drift")
    store_contract.require(live["edge_labels"] == sorted(preflight_value["spec"]["edge_labels"]),
                           "start edge schema proof drift")
    for key in ("tag_count", "edge_index_count", "tag_index_count"):
        store_contract.require(store_contract.integer(live[key], f"start live {key}") == 0,
                               "start no-index/tag proof drift")
    store_contract.exact_sha(live["results_sha256"], "start live results SHA")
    store_contract.integer(live["attempts"], "start live attempts", 1)
    store_contract.require(live["query_timeout_ms"] == preflight_value["spec"]["query_timeout_ms"],
                           "start live query timeout drift")
    startup_timeout = store_contract.integer(
        live["startup_timeout_seconds"], "start live startup timeout", 1
    )
    store_contract.require(startup_timeout <= 900, "start live startup timeout exceeds bound")
    store_contract.parse_timestamp(live["captured_at_utc"], "start live captured_at")
    store_contract.require(value["lifecycle_stable"] is True, "start lifecycle stability proof missing")
    store_contract.require(value["timing_scope"] == "pre-adapter-outside-timed-region-v1",
                           "start timing scope drift")
    return receipt_path, value


def cleanup(args: argparse.Namespace) -> None:
    preflight_path, preflight_value, docker = validate_preflight_receipt(
        args.preflight,
        args.preflight_sha256,
        max_age_seconds=args.max_preflight_age_seconds,
    )
    partial_path, partial_value = validate_partial_start_receipt(
        args.partial_start,
        args.partial_start_sha256,
        preflight_path=preflight_path,
        preflight_sha256=sha256_file(preflight_path),
        preflight_value=preflight_value,
    )
    receipt = perform_partial_cleanup(
        docker=docker,
        preflight_path=preflight_path,
        preflight_value=preflight_value,
        partial_path=partial_path,
        partial_value=partial_value,
        output=args.output,
        stop_timeout_seconds=args.stop_timeout_seconds,
    )
    validate_cleanup_receipt(
        args.output,
        sha256_file(args.output),
        preflight_path=preflight_path,
        preflight_sha256=sha256_file(preflight_path),
        partial_path=partial_path,
        partial_sha256=sha256_file(partial_path),
        preflight_value=preflight_value,
        partial_value=partial_value,
    )
    print(args.output.resolve())
    if receipt["state"] != "PASS":
        raise ContractError(f"partial cleanup is BLOCKED: {receipt['blocked_reason']}")


def _start_cleanup_receipt(
    *,
    state: str,
    repo: dict[str, Any],
    preflight_path: Path,
    preflight_sha256: str,
    start_path: Path,
    start_sha256: str,
    before_cleanup: dict[str, object],
    network_snapshot: object,
    identities_revalidated: bool,
    commands: list[list[str]],
    blocked_reason: str | None,
    containers_absent: bool | None,
    network_absent: bool | None,
) -> dict[str, Any]:
    return {
        "schema_version": START_CLEANUP_SCHEMA,
        "state": state,
        "created_at_utc": utc_now(),
        "repo": repo,
        "preflight": {"path": str(preflight_path), "sha256": preflight_sha256},
        "start_receipt": {"path": str(start_path), "sha256": start_sha256},
        "verification": {
            "name_lookup_used": False,
            "all_present_identities_revalidated": identities_revalidated,
            "before_cleanup": before_cleanup,
            "network_snapshot": network_snapshot,
        },
        "commands": commands,
        "blocked_reason": blocked_reason,
        "postconditions": {
            "container_ids_absent": containers_absent,
            "network_id_absent": network_absent,
            "names_not_used": True,
        },
        "timing_scope": "failure-recovery-outside-timed-region-v1",
    }


def perform_start_bound_cleanup(
    *,
    docker: Path,
    preflight_path: Path,
    preflight_value: dict[str, Any],
    start_path: Path,
    start_value: dict[str, Any],
    output: Path,
    stop_timeout_seconds: int,
) -> dict[str, Any]:
    """Recover a PASS launch by exact IDs, tolerating already-removed exact resources."""

    store_contract.require(1 <= stop_timeout_seconds <= 120,
                           "start cleanup stop timeout must be in [1, 120] seconds")
    spec = preflight_value["spec"]
    output_path = _safe_output(output, spec, preflight_value["repo"], "start cleanup receipt")
    preflight_sha = sha256_file(preflight_path)
    start_sha = sha256_file(start_path)
    expected_ids = {
        role: start_value["containers"][role]["container_id"] for role in ROLE_ORDER
    }
    network_id = start_value["network"]["network_id"]
    before_cleanup: dict[str, object] = {role: None for role in ROLE_ORDER}
    network_snapshot: object = None
    commands: list[list[str]] = []

    def publish_blocked(reason: str) -> dict[str, Any]:
        receipt = _start_cleanup_receipt(
            state="BLOCKED",
            repo=preflight_value["repo"],
            preflight_path=preflight_path,
            preflight_sha256=preflight_sha,
            start_path=start_path,
            start_sha256=start_sha,
            before_cleanup=before_cleanup,
            network_snapshot=network_snapshot,
            identities_revalidated=False,
            commands=commands,
            blocked_reason=reason[:2000],
            containers_absent=None,
            network_absent=None,
        )
        store_contract.write_json_exclusive(output_path, receipt)
        return receipt

    present_ids: set[str] = set()
    try:
        for role in ROLE_ORDER:
            identifier = expected_ids[role]
            inspected = _inspect_optional_exact_id(docker, "container", identifier)
            if inspected is None:
                continue
            snapshot = validate_container_inspect(
                spec, role, inspected, preflight_sha256=preflight_sha, require_running=False
            )
            original = start_value["containers"][role]
            immutable_fields = (
                "role", "name", "logical_host", "container_id", "image_id", "config_image",
                "started_at_utc", "restart_count",
            )
            store_contract.require(
                all(snapshot[field] == original[field] for field in immutable_fields),
                f"start cleanup {role} immutable identity drift",
            )
            if snapshot["running"]:
                store_contract.require(
                    snapshot["pid"] == original["pid"]
                    and snapshot["process_start_ticks"] == original["process_start_ticks"],
                    f"start cleanup {role} live process identity drift",
                )
            else:
                store_contract.require(
                    snapshot["process_start_ticks"] is None,
                    f"start cleanup {role} stopped process identity drift",
                )
            before_cleanup[role] = snapshot
            present_ids.add(identifier)
        inspected_network = _inspect_optional_exact_id(docker, "network", network_id)
        if inspected_network is not None:
            network_snapshot = validate_partial_network_inspect(
                spec,
                inspected_network,
                preflight_sha256=preflight_sha,
                network_id=network_id,
                present_container_ids=present_ids,
            )
        elif present_ids:
            raise ContractError("start-bound containers exist after their exact network disappeared")
    except Exception as exc:  # noqa: BLE001 - publish fail-closed evidence for every identity fault
        return publish_blocked(f"exact-ID identity revalidation failed: {exc}")

    try:
        for role in STOP_ORDER:
            snapshot = before_cleanup[role]
            if snapshot is None:
                continue
            command = [
                "container", "stop", "--time", str(stop_timeout_seconds), expected_ids[role]
            ]
            commands.append(command)
            docker_call(docker, command, check=True, timeout=stop_timeout_seconds + 30)
        for role in STOP_ORDER:
            if before_cleanup[role] is None:
                continue
            command = ["container", "rm", expected_ids[role]]
            commands.append(command)
            docker_call(docker, command, check=True, timeout=60)
        if network_snapshot is not None:
            command = ["network", "rm", network_id]
            commands.append(command)
            docker_call(docker, command, check=True, timeout=60)
        for identifier in expected_ids.values():
            store_contract.require(
                _inspect_optional_exact_id(docker, "container", identifier) is None,
                f"start-bound container ID remains after cleanup: {identifier}",
            )
        store_contract.require(
            _inspect_optional_exact_id(docker, "network", network_id) is None,
            f"start-bound network ID remains after cleanup: {network_id}",
        )
    except (ContractError, OSError, subprocess.SubprocessError, ValueError) as exc:
        return publish_blocked(f"exact-ID cleanup or postcondition failed: {exc}")

    receipt = _start_cleanup_receipt(
        state="PASS",
        repo=preflight_value["repo"],
        preflight_path=preflight_path,
        preflight_sha256=preflight_sha,
        start_path=start_path,
        start_sha256=start_sha,
        before_cleanup=before_cleanup,
        network_snapshot=network_snapshot,
        identities_revalidated=True,
        commands=commands,
        blocked_reason=None,
        containers_absent=True,
        network_absent=True,
    )
    store_contract.write_json_exclusive(output_path, receipt)
    return receipt


def validate_start_cleanup_receipt(
    path: Path,
    sha256: str,
    *,
    preflight_path: Path,
    preflight_sha256: str,
    start_path: Path,
    start_sha256: str,
    preflight_value: dict[str, Any],
    start_value: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    receipt_path = store_contract.validate_file_ref(
        {"path": str(path), "sha256": sha256}, "start cleanup receipt", recompute=True
    )
    value = store_contract.exact_keys(
        store_contract.read_strict_json(receipt_path, "start cleanup receipt"),
        (
            "schema_version", "state", "created_at_utc", "repo", "preflight",
            "start_receipt", "verification", "commands", "blocked_reason",
            "postconditions", "timing_scope",
        ),
        "start cleanup receipt",
    )
    store_contract.require(value["schema_version"] == START_CLEANUP_SCHEMA,
                           "start cleanup receipt schema drift")
    store_contract.require(value["state"] in ("PASS", "BLOCKED"),
                           "start cleanup receipt state drift")
    store_contract.parse_timestamp(value["created_at_utc"], "start cleanup created_at")
    validate_current_repo(value["repo"], "start cleanup.repo")
    store_contract.validate_file_ref(
        value["preflight"], "start cleanup.preflight", expected_path=preflight_path,
        expected_sha256=preflight_sha256, recompute=True,
    )
    store_contract.validate_file_ref(
        value["start_receipt"], "start cleanup.start_receipt", expected_path=start_path,
        expected_sha256=start_sha256, recompute=True,
    )
    verification = store_contract.exact_keys(
        value["verification"],
        (
            "name_lookup_used", "all_present_identities_revalidated", "before_cleanup",
            "network_snapshot",
        ),
        "start cleanup verification",
    )
    store_contract.require(verification["name_lookup_used"] is False,
                           "start cleanup used a forbidden name lookup")
    snapshots = store_contract.exact_keys(
        verification["before_cleanup"], ROLE_ORDER, "start cleanup snapshots"
    )
    expected_ids = {
        role: start_value["containers"][role]["container_id"] for role in ROLE_ORDER
    }
    present_roles: list[str] = []
    for role in ROLE_ORDER:
        snapshot = snapshots[role]
        if snapshot is None:
            continue
        item = store_contract.exact_keys(
            snapshot,
            (
                "role", "name", "logical_host", "container_id", "image_id", "config_image",
                "pid", "process_start_ticks", "started_at_utc", "restart_count", "running",
            ),
            f"start cleanup snapshot {role}",
        )
        original = start_value["containers"][role]
        immutable_fields = (
            "role", "name", "logical_host", "container_id", "image_id", "config_image",
            "started_at_utc", "restart_count",
        )
        store_contract.require(
            all(item[field] == original[field] for field in immutable_fields),
            f"start cleanup receipt identity drift: {role}",
        )
        if item["running"]:
            store_contract.require(
                item["pid"] == original["pid"]
                and item["process_start_ticks"] == original["process_start_ticks"],
                f"start cleanup receipt live process drift: {role}",
            )
        else:
            store_contract.require(
                item["process_start_ticks"] is None,
                f"start cleanup receipt stopped start ticks drift: {role}",
            )
        present_roles.append(role)
    network = verification["network_snapshot"]
    network_present = network is not None
    if network_present:
        store_contract.require(
            network
            == {
                "name": preflight_value["spec"]["network"]["name"],
                "network_id": start_value["network"]["network_id"],
                "driver": preflight_value["spec"]["network"]["driver"],
            },
            "start cleanup network identity drift",
        )
    commands = value["commands"]
    store_contract.require(
        isinstance(commands, list)
        and all(isinstance(command, list) and all(isinstance(token, str) for token in command)
                for command in commands),
        "start cleanup commands are malformed",
    )
    ordered = [role for role in STOP_ORDER if role in present_roles]
    timeout_text = "1"
    if ordered and commands:
        first = commands[0]
        store_contract.require(
            len(first) == 5 and first[:3] == ["container", "stop", "--time"]
            and first[4] == expected_ids[ordered[0]],
            "start cleanup first command drift",
        )
        timeout_text = first[3]
        timeout = int(timeout_text) if timeout_text.isdigit() and len(timeout_text) <= 3 else 0
        store_contract.require(1 <= timeout <= 120, "start cleanup timeout is invalid")
    expected_commands = [
        ["container", "stop", "--time", timeout_text, expected_ids[role]] for role in ordered
    ]
    expected_commands.extend([["container", "rm", expected_ids[role]] for role in ordered])
    if network_present:
        expected_commands.append(["network", "rm", start_value["network"]["network_id"]])
    forbidden_names = {
        preflight_value["spec"]["network"]["name"],
        *(preflight_value["spec"]["roles"][role]["container_name"] for role in ROLE_ORDER),
    }
    store_contract.require(all(forbidden_names.isdisjoint(command) for command in commands),
                           "start cleanup command contains a forbidden resource name")
    post = store_contract.exact_keys(
        value["postconditions"],
        ("container_ids_absent", "network_id_absent", "names_not_used"),
        "start cleanup postconditions",
    )
    store_contract.require(post["names_not_used"] is True,
                           "start cleanup did not prohibit name-based deletion")
    if value["state"] == "PASS":
        store_contract.require(value["blocked_reason"] is None,
                               "PASS start cleanup claims a blocked reason")
        store_contract.require(verification["all_present_identities_revalidated"] is True,
                               "PASS start cleanup lacks identity revalidation")
        store_contract.require(commands == expected_commands, "PASS start cleanup commands drift")
        store_contract.require(
            post["container_ids_absent"] is True and post["network_id_absent"] is True,
            "PASS start cleanup lacks exact-ID absence proof",
        )
    else:
        store_contract.nonempty(value["blocked_reason"], "BLOCKED start cleanup reason")
        store_contract.require(verification["all_present_identities_revalidated"] is False,
                               "BLOCKED start cleanup claims complete revalidation")
        store_contract.require(commands == expected_commands[:len(commands)],
                               "BLOCKED start cleanup commands are not a safe prefix")
        store_contract.require(
            post["container_ids_absent"] is None and post["network_id_absent"] is None,
            "BLOCKED start cleanup claims absence",
        )
    store_contract.require(value["timing_scope"] == "failure-recovery-outside-timed-region-v1",
                           "start cleanup timing scope drift")
    return receipt_path, value


def recover(args: argparse.Namespace) -> None:
    preflight_path, preflight_value, docker = validate_preflight_receipt(
        args.preflight, args.preflight_sha256, max_age_seconds=604_800
    )
    start_path, start_value = validate_start_receipt(
        args.start_receipt,
        args.start_receipt_sha256,
        preflight_path=preflight_path,
        preflight_sha256=sha256_file(preflight_path),
        preflight_value=preflight_value,
    )
    receipt = perform_start_bound_cleanup(
        docker=docker,
        preflight_path=preflight_path,
        preflight_value=preflight_value,
        start_path=start_path,
        start_value=start_value,
        output=args.output,
        stop_timeout_seconds=args.stop_timeout_seconds,
    )
    validate_start_cleanup_receipt(
        args.output,
        sha256_file(args.output),
        preflight_path=preflight_path,
        preflight_sha256=sha256_file(preflight_path),
        start_path=start_path,
        start_sha256=sha256_file(start_path),
        preflight_value=preflight_value,
        start_value=start_value,
    )
    print(args.output.resolve())
    if receipt["state"] != "PASS":
        raise ContractError(f"start-bound cleanup is BLOCKED: {receipt['blocked_reason']}")


def stop(args: argparse.Namespace) -> None:
    preflight_path, preflight_value, docker = validate_preflight_receipt(
        args.preflight,
        args.preflight_sha256,
        max_age_seconds=604_800,
    )
    start_path, start_value = validate_start_receipt(
        args.start_receipt,
        args.start_receipt_sha256,
        preflight_path=preflight_path,
        preflight_sha256=sha256_file(preflight_path),
        preflight_value=preflight_value,
    )
    store_contract.require(args.stop_timeout_seconds >= 1 and args.stop_timeout_seconds <= 120,
                           "stop timeout must be in [1, 120] seconds")
    spec = preflight_value["spec"]
    output = _safe_output(args.output, spec, preflight_value["repo"], "stop receipt")
    preflight_sha = sha256_file(preflight_path)
    expected_ids = {role: start_value["containers"][role]["container_id"] for role in ROLE_ORDER}
    before_stop: dict[str, dict[str, Any]] = {}
    for role in ROLE_ORDER:
        inspected = docker_inspect_one(docker, expected_ids[role])
        snapshot = validate_container_inspect(
            spec, role, inspected, preflight_sha256=preflight_sha, require_running=True
        )
        for key in (
            "container_id", "image_id", "config_image", "pid", "process_start_ticks",
            "started_at_utc", "restart_count",
        ):
            store_contract.require(snapshot[key] == start_value["containers"][role][key],
                                   f"{role} lifecycle changed before stop: {key}")
        before_stop[role] = snapshot
    network = validate_network_inspect(
        spec,
        _network_inspect_one(docker, start_value["network"]["network_id"]),
        preflight_sha256=preflight_sha,
        expected_container_ids=expected_ids,
    )
    store_contract.require(network["network_id"] == start_value["network"]["network_id"],
                           "network identity changed before stop")

    commands: list[list[str]] = []
    for role in STOP_ORDER:
        command = [
            "container",
            "stop",
            "--time",
            str(args.stop_timeout_seconds),
            expected_ids[role],
        ]
        docker_call(docker, command, check=True, timeout=args.stop_timeout_seconds + 30)
        commands.append(command)
    for role in STOP_ORDER:
        command = ["container", "rm", expected_ids[role]]
        docker_call(docker, command, check=True, timeout=60)
        commands.append(command)
    network_command = ["network", "rm", start_value["network"]["network_id"]]
    docker_call(docker, network_command, check=True, timeout=60)
    commands.append(network_command)
    assert_owned_names_absent(docker, spec)
    for role in ROLE_ORDER:
        log_path = Path(spec["roles"][role]["mounts"][1]["source"])
        store_contract.require(log_path.is_dir() and not log_path.is_symlink(),
                               f"{role} logs were not preserved after stop")
    receipt = {
        "schema_version": STOP_SCHEMA,
        "state": "PASS",
        "created_at_utc": utc_now(),
        "repo": preflight_value["repo"],
        "preflight": {"path": str(preflight_path), "sha256": sha256_file(preflight_path)},
        "start_receipt": {"path": str(start_path), "sha256": sha256_file(start_path)},
        "before_stop": before_stop,
        "stop_timeout_seconds": args.stop_timeout_seconds,
        "commands": commands,
        "postconditions": {
            "owned_container_names_absent": True,
            "owned_network_name_absent": True,
            "role_logs_preserved": True,
        },
        "timing_scope": "post-adapter-outside-timed-region-v1",
    }
    store_contract.write_json_exclusive(output, receipt)
    print(output)


def validate_stop_receipt(
    path: Path,
    sha256: str,
    *,
    preflight_path: Path,
    preflight_sha256: str,
    start_path: Path,
    start_sha256: str,
    preflight_value: dict[str, Any],
    start_value: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    """Validate immutable post-run teardown evidence without issuing Docker commands."""

    receipt_path = store_contract.validate_file_ref(
        {"path": str(path), "sha256": sha256}, "stop receipt", recompute=True
    )
    value = store_contract.exact_keys(
        store_contract.read_strict_json(receipt_path, "stop receipt"),
        (
            "schema_version", "state", "created_at_utc", "repo", "preflight", "start_receipt",
            "before_stop", "stop_timeout_seconds", "commands", "postconditions", "timing_scope",
        ),
        "stop receipt",
    )
    store_contract.require(value["schema_version"] == STOP_SCHEMA and value["state"] == "PASS",
                           "stop receipt schema/state drift")
    stopped_at = store_contract.parse_timestamp(value["created_at_utc"], "stop receipt created_at")
    started_at = store_contract.parse_timestamp(start_value["created_at_utc"], "start receipt created_at")
    store_contract.require(stopped_at >= started_at, "stop receipt predates start receipt")
    validate_current_repo(value["repo"], "stop.repo")
    store_contract.validate_file_ref(
        value["preflight"],
        "stop.preflight",
        expected_path=preflight_path,
        expected_sha256=preflight_sha256,
        recompute=True,
    )
    store_contract.validate_file_ref(
        value["start_receipt"],
        "stop.start_receipt",
        expected_path=start_path,
        expected_sha256=start_sha256,
        recompute=True,
    )
    store_contract.require(
        preflight_value["spec_sha256"] == start_value["spec_sha256"],
        "stop input receipts disagree on launch spec",
    )
    before_stop = store_contract.exact_keys(value["before_stop"], ROLE_ORDER, "stop before_stop")
    for role in ROLE_ORDER:
        snapshot = store_contract.exact_keys(
            before_stop[role],
            (
                "role", "name", "logical_host", "container_id", "image_id", "config_image", "pid",
                "process_start_ticks", "started_at_utc", "restart_count", "running",
            ),
            f"stop before_stop {role}",
        )
        store_contract.require(snapshot == start_value["containers"][role],
                               f"stop/start lifecycle snapshot drift: {role}")
    timeout_seconds = store_contract.integer(value["stop_timeout_seconds"], "stop timeout", 1)
    store_contract.require(timeout_seconds <= 120, "stop timeout exceeds bound")
    expected_ids = {role: start_value["containers"][role]["container_id"] for role in ROLE_ORDER}
    expected_commands = [
        ["container", "stop", "--time", str(timeout_seconds), expected_ids[role]] for role in STOP_ORDER
    ]
    expected_commands.extend([["container", "rm", expected_ids[role]] for role in STOP_ORDER])
    expected_commands.append(["network", "rm", start_value["network"]["network_id"]])
    store_contract.require(value["commands"] == expected_commands, "stop command evidence drift")
    postconditions = store_contract.exact_keys(
        value["postconditions"],
        ("owned_container_names_absent", "owned_network_name_absent", "role_logs_preserved"),
        "stop postconditions",
    )
    store_contract.require(all(item is True for item in postconditions.values()),
                           "stop receipt contains a failed postcondition")
    store_contract.require(value["timing_scope"] == "post-adapter-outside-timed-region-v1",
                           "stop timing scope drift")
    return receipt_path, value


def run(args: argparse.Namespace) -> None:
    if args.action == "seal":
        seal(args)
    elif args.action == "preflight":
        preflight(args)
    elif args.action == "start":
        start(args)
    elif args.action == "stop":
        stop(args)
    elif args.action == "cleanup":
        cleanup(args)
    elif args.action == "recover":
        recover(args)
    else:
        raise ContractError(f"unknown formal cluster action: {args.action}")


def main() -> int:
    try:
        run(parse_args())
        return 0
    except (ContractError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"formal_cluster: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - unexpected failures must never look like PASS
        print(f"formal_cluster fatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
