#!/usr/bin/env python3
"""Stable schemas shared by the CIDR formal resource collector and validator."""

from __future__ import annotations

RESOURCE_SCHEMA_VERSION = "cidr-resource-v1"
MANIFEST_SCHEMA_VERSION = "cidr-run-manifest-v1"
TERMINAL_CENSOR_POLICY_VERSION = "root-only-penultimate-v1"
TERMINAL_CENSOR_POLICY = {
    "version": TERMINAL_CENSOR_POLICY_VERSION,
    "maximum_censored_samples": 1,
    "required_shape": "root-only-penultimate-final-empty",
    "process_cpu_quality": "observed-through-terminal-stat-lower-bound",
    "process_io_quality": "tail-lower-bound",
    "peak_rss_pss_quality": "max-readable-samples",
}
CONTAINER_IDENTITY_SCHEMA_VERSION = "cidr-container-identity-v2"

RESOURCE_COLUMNS = [
    "schema_version",
    "timestamp_utc",
    "monotonic_s",
    "sample_index",
    "root_pid",
    "root_alive",
    "process_count",
    "pids",
    "process_user_cpu_s",
    "process_sys_cpu_s",
    "process_cpu_pct",
    "process_rss_bytes",
    "process_rss_unreadable",
    "process_pss_bytes",
    "process_pss_unreadable",
    "process_io_unreadable",
    "process_read_bytes",
    "process_write_bytes",
    "process_cancelled_write_bytes",
    "process_rchar",
    "process_wchar",
    "process_read_mib_s",
    "process_write_mib_s",
    "host_load1",
    "host_load5",
    "host_load15",
    "host_mem_total_bytes",
    "host_mem_available_bytes",
    "host_swap_total_bytes",
    "host_swap_free_bytes",
    "device",
    "device_read_iops",
    "device_write_iops",
    "device_read_mib_s",
    "device_write_mib_s",
    "device_read_await_ms",
    "device_write_await_ms",
    "device_await_ms",
    "device_util_pct",
    "data_mount",
    "data_free_bytes",
]

DISK_COLUMNS = [
    "schema_version",
    "timestamp_utc",
    "monotonic_s",
    "sample_index",
    "root_role",
    "root_label",
    "root_path",
    "exists",
    "scan_complete",
    "scan_error_count",
    "total_bytes",
    "file_count",
    "payload_bytes",
    "metadata_bytes",
    "catalog_bytes",
    "sidecar_bytes",
    "manifest_bytes",
    "wal_bytes",
    "temp_bytes",
    "other_bytes",
    "scan_elapsed_ms",
]

IOSTAT_COLUMNS = [
    "schema_version",
    "sample_index",
    "timestamp_raw",
    "device",
    "read_iops",
    "write_iops",
    "read_mib_s",
    "write_mib_s",
    "read_await_ms",
    "write_await_ms",
    "await_ms",
    "aqu_sz",
    "util_pct",
]

DISK_CATEGORIES = [
    "payload",
    "metadata",
    "catalog",
    "sidecar",
    "manifest",
    "wal",
    "temp",
    "other",
]

ARTIFACT_FILES = [
    "resource-samples.tsv",
    "disk-samples.tsv",
    "iostat-samples.tsv",
    "pidstat.raw",
    "iostat.raw",
    "collector-status.json",
    "collector-ready.json",
    "collector.stdout.log",
    "collector.stderr.log",
    "command.stdout.log",
    "command.stderr.log",
    "command.txt",
    "execution.json",
]
