#!/usr/bin/env python3
"""Render the strict Neo4j template and pass the full formal suite validator."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


P10_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = P10_DIR.parents[2]
TEMPLATE = P10_DIR / "adapters" / "neo4j" / "formal-system.template.json"
ADAPTER = P10_DIR / "adapters" / "neo4j_adapter.py"
LAUNCHER = P10_DIR / "adapters" / "launch_neo4j_runtime.py"
PYTHON = Path("/usr/bin/python3.8")
ADAPTER_SHA256 = "8b984571c7797f6e3d32db2d819c989b87b95d91ca2479785e523496ee926e09"
LAUNCHER_SHA256 = "ce052ff6a24e8fba565e473692147aa9c49ba50df69898dbbdc9ffd236a6ec59"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def replace_strings(value: object, replacements: dict[str, str]) -> object:
    if isinstance(value, str):
        for source, target in replacements.items():
            value = value.replace(source, target)
        return value
    if isinstance(value, list):
        return [replace_strings(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: replace_strings(item, replacements) for key, item in value.items()}
    return value


def single_file_tree_ref(root: Path) -> dict[str, object]:
    """Build the production v2 tree identity for this fixture's one-file store."""

    marker = root / "nebula-store.marker"
    payload = marker.read_bytes()
    file_sha256 = hashlib.sha256(payload).hexdigest()
    digest = hashlib.sha256()
    digest.update(
        f"file\0{marker.name}\0{len(payload)}\0{file_sha256}\n".encode("utf-8")
    )
    return {
        "path": str(root.resolve()),
        "hash_method": "sha256-tree-v2(type,path,size-or-target,file-sha256)",
        "sha256": digest.hexdigest(),
        "file_count": 1,
        "total_bytes": len(payload),
        "directory_count": 0,
        "symlink_count": 0,
    }


class Neo4jStrictTemplateValidateTest(unittest.TestCase):
    def test_rendered_template_passes_full_formal_validate(self) -> None:
        self.assertEqual(sha256_file(ADAPTER), ADAPTER_SHA256)
        self.assertEqual(sha256_file(LAUNCHER), LAUNCHER_SHA256)
        self.assertTrue(PYTHON.is_file())
        with tempfile.TemporaryDirectory(prefix="neo4j-strict-template-") as raw:
            root = Path(raw).resolve()
            run_root = root / "validate-run"
            neo4j_root = root / "p10-neo4j"
            source_store = neo4j_root / "pristine-source-store"
            source_store.mkdir(parents=True)
            p02b = root / "p02b"
            p02b.mkdir()
            dataset_manifest = p02b / "sf10-dataset-manifest.json"
            sentinel_result = p02b / "sentinel-result.json"
            write_json(dataset_manifest, {"fixture": "strict-template-validate"})
            write_json(sentinel_result, {"fixture": "strict-template-validate"})

            replacements = {
                "${REPO_ROOT}": str(REPO_ROOT),
                "/ABS/P10-NEO4J": str(neo4j_root),
                "/ABS/P02B": str(p02b),
                "REPLACE_ADAPTER_SHA256": ADAPTER_SHA256,
                "REPLACE_LAUNCHER_SHA256": LAUNCHER_SHA256,
                "REPLACE_DATASET_MANIFEST_SHA256": sha256_file(dataset_manifest),
                "REPLACE_P02B_RESULT_SHA256": sha256_file(sentinel_result),
                "REPLACE_P02B_VALIDATOR_SHA256": sha256_file(
                    REPO_ROOT / "cidr-experiments" / "runners" / "p02b" / "validate_sentinel_result.py"
                ),
                "REPLACE_P02B_BINARY_SHA256": "9" * 64,
            }
            for repeat in range(1, 4):
                repeat_root = neo4j_root / f"repeat-{repeat:02d}"
                (repeat_root / "neo4j-runtime-copy").mkdir(parents=True)
                (repeat_root / "logs").mkdir()
                store_manifest = repeat_root / "store-manifest-v3.json"
                import_receipt = repeat_root / "controlled-import-receipt.json"
                write_json(store_manifest, {"repeat_index": repeat, "fixture": "strict-validate"})
                write_json(import_receipt, {"repeat_index": repeat, "fixture": "strict-validate"})
                prefix = f"REPLACE_R{repeat:02d}_"
                replacements.update(
                    {
                        prefix + "BOLT_PORT": str(17686 + repeat),
                        prefix + "STORE_MANIFEST_SHA256": sha256_file(store_manifest),
                        prefix + "IMPORT_RECEIPT_SHA256": sha256_file(import_receipt),
                        prefix + "OFFLINE_STORE_TREE_SHA256": str(repeat) * 64,
                    }
                )

            template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
            neo4j = replace_strings(template, replacements)
            rendered_text = json.dumps(neo4j, sort_keys=True)
            self.assertNotIn("REPLACE_", rendered_text)
            self.assertNotIn("/ABS/", rendered_text)
            self.assertNotIn("${", rendered_text)
            self.assertEqual(neo4j["adapter"]["sha256"], ADAPTER_SHA256)
            adapter_args = neo4j["adapter"]["args"]
            self.assertEqual(
                adapter_args[adapter_args.index("--expected-entrypoint-json") + 1],
                '["tini","-g","--","/startup/docker-entrypoint.sh"]',
            )
            self.assertEqual(
                adapter_args[adapter_args.index("--expected-command-json") + 1],
                '["neo4j"]',
            )
            self.assertTrue(
                all(
                    binding["launcher"]["sha256"] == LAUNCHER_SHA256
                    for binding in neo4j["repeat_bindings"]
                )
            )

            dataset = REPO_ROOT / "baseline" / "shared-truth" / "fixtures" / "dense-edges.txt"
            truth = REPO_ROOT / "baseline" / "shared-truth" / "fixtures" / "truth.tsv"
            adapter_ref = {"path": str(ADAPTER), "sha256": ADAPTER_SHA256, "args": []}
            binary_ref = {"path": str(PYTHON), "sha256": sha256_file(PYTHON)}

            def generic_system(system_id: str, group: str) -> dict[str, object]:
                store = root / "generic-stores" / system_id
                store.mkdir(parents=True)
                if system_id == "livegraph":
                    process_lifetime = "fresh-import-and-query-process-lifetime-v1"
                elif group == "client-server":
                    process_lifetime = "external-prestarted-query-process-lifetime-v1"
                else:
                    process_lifetime = "prebuilt-store-query-process-lifetime-v1"
                return {
                    "id": system_id,
                    "display_name": f"strict fixture {system_id}",
                    "group": group,
                    "interface_scope": "typed-neighbor-dense-id-v1",
                    "system_version": "strict-template-validate-v1",
                    "fixture_only": False,
                    "service_lifecycle": "in-process" if group == "embedded" else "external-prestarted",
                    "process_lifetime": process_lifetime,
                    "adapter": dict(adapter_ref),
                    "binary": dict(binary_ref),
                    "runtime_libraries": [dict(binary_ref)] if system_id == "livegraph" else [],
                    "store_roots": [
                        {"label": "graph", "path": str(store), "sha256": "8" * 64}
                    ],
                    "temp_roots": [],
                    "containers": [] if group == "embedded" else [f"strict-{system_id}"],
                    "extra_pids": [],
                    "image_digests": [] if group == "embedded" else ["sha256:" + "7" * 64],
                }

            def nebulagraph_system() -> dict[str, object]:
                nebula_root = root / "p10-nebulagraph"
                source_store = nebula_root / "golden-store"
                source_store.mkdir(parents=True)
                (source_store / "nebula-store.marker").write_bytes(
                    b"strict NebulaGraph repeat-binding fixture\n"
                )
                source_ref = single_file_tree_ref(source_store)
                bindings = []
                for repeat_index in range(1, 4):
                    repeat_root = nebula_root / f"repeat-{repeat_index:02d}"
                    repeat_root.mkdir()
                    store = repeat_root / "store-copy"
                    shutil.copytree(source_store, store)
                    logs = repeat_root / "logs"
                    logs.mkdir()
                    target_ref = single_file_tree_ref(store)
                    self.assertEqual(
                        {key: value for key, value in source_ref.items() if key != "path"},
                        {key: value for key, value in target_ref.items() if key != "path"},
                    )
                    containers = {
                        "metad": f"strict-nebula-meta-r{repeat_index:02d}",
                        "storaged": f"strict-nebula-storage-r{repeat_index:02d}",
                        "graphd": f"strict-nebula-graph-r{repeat_index:02d}",
                    }
                    clone_receipt = repeat_root / "clone-receipt.json"
                    write_json(
                        clone_receipt,
                        {
                            "schema_version": "cidr-p10-nebulagraph-clone-receipt-v1",
                            "state": "PASS",
                            "run_id": run_root.name,
                            "repeat_index": repeat_index,
                            "source_pre": source_ref,
                            "source_post": source_ref,
                            "target": target_ref,
                        },
                    )
                    clone_ref = {
                        "path": str(clone_receipt.resolve()),
                        "sha256": sha256_file(clone_receipt),
                    }
                    network = f"strict-nebula-r{repeat_index:02d}-net"
                    store_manifest = repeat_root / "store-manifest.json"
                    write_json(
                        store_manifest,
                        {
                            "schema_version": "cidr-p10-nebulagraph-store-v2",
                            "formal_eligible": True,
                            "performance_eligible": True,
                            "system_version": "NebulaGraph 3.8.0",
                            "dataset": {
                                "path": str(dataset.resolve()),
                                "sha256": sha256_file(dataset),
                            },
                            "truth": {
                                "path": str(truth.resolve()),
                                "sha256": sha256_file(truth),
                            },
                            "logical_hosts": {
                                "metad": "seml0-nebula-meta-sf10",
                                "storaged": "seml0-nebula-storage-sf10",
                                "graphd": "seml0-nebula-graph-sf10",
                            },
                            "data_root": target_ref,
                            "containers": containers,
                            "network": network,
                            "graph_endpoint": {
                                "host": "127.0.0.1",
                                "port": 29668 + repeat_index,
                            },
                            "clone_receipt": clone_ref,
                        },
                    )
                    manifest_ref = {
                        "path": str(store_manifest.resolve()),
                        "sha256": sha256_file(store_manifest),
                    }
                    bindings.append(
                        {
                            "repeat_index": repeat_index,
                            "repeat_root": str(repeat_root.resolve()),
                            "source_store_root": str(source_store.resolve()),
                            "store_root": {
                                "label": "nebulagraph",
                                "path": str(store.resolve()),
                                "sha256": target_ref["sha256"],
                            },
                            "logs_root": {
                                "label": "nebulagraph-logs",
                                "path": str(logs.resolve()),
                            },
                            "containers": containers,
                            "network": network,
                            "graph_port": 29668 + repeat_index,
                            "store_manifest": manifest_ref,
                            "clone_receipt": clone_ref,
                        }
                    )

                first = bindings[0]
                nebula_adapter = P10_DIR / "adapters" / "nebulagraph" / "nebula_adapter.py"
                first_manifest = first["store_manifest"]
                first_containers = first["containers"]
                return {
                    "id": "nebulagraph",
                    "display_name": "strict fixture nebulagraph",
                    "group": "client-server",
                    "interface_scope": "typed-neighbor-dense-id-v1",
                    "system_version": "NebulaGraph 3.8.0",
                    "fixture_only": False,
                    "service_lifecycle": "external-prestarted",
                    "process_lifetime": "external-prestarted-query-process-lifetime-v1",
                    "adapter": {
                        "path": str(nebula_adapter.resolve()),
                        "sha256": sha256_file(nebula_adapter),
                        "args": [
                            "--store-manifest",
                            first_manifest["path"],
                            "--store-manifest-sha256",
                            first_manifest["sha256"],
                        ],
                    },
                    "binary": dict(binary_ref),
                    "runtime_libraries": [],
                    "store_roots": [dict(first["store_root"])],
                    "temp_roots": [dict(first["logs_root"])],
                    "containers": [
                        first_containers[role]
                        for role in ("graphd", "metad", "storaged")
                    ],
                    "extra_pids": [],
                    "image_digests": [
                        "sha256:1040573cc684ea6cc5e673b667422e9abab607c9d553c2c17c7bac6ad8a74e05",
                        "sha256:ab687bd32d3e441d436842b41427ba0961a5e169c46997ef03fa4dcfd5388b35",
                        "sha256:7142642ee69001a5b5c50520196538d58bb2ec3930707c067cb4c4e2be3890f9",
                    ],
                    "repeat_bindings": bindings,
                }

            manifest = {
                "$schema": str(P10_DIR / "schemas" / "suite-manifest.schema.json"),
                "schema_version": "cidr-p10-suite-v1",
                "suite_id": "neo4j-strict-template-validate",
                "task_ids": ["P10", "P11"],
                "fixture_only": False,
                "dataset": {"path": str(dataset), "sha256": sha256_file(dataset)},
                "truth": {
                    "path": str(truth),
                    "sha256": sha256_file(truth),
                    "query_count": 2,
                    "digest_algorithm": "mix64-dense-dst-count-sum-xor-v1",
                },
                "protocol": {
                    "group_policy": "report-separately-no-cross-group-speedups",
                    "interface_scope": "typed-neighbor-dense-id-v1",
                    "timing_boundary": "typed-neighbor-call-plus-result-materialization-and-digest-v1",
                    "clock": "CLOCK_MONOTONIC",
                    "cache_policy": "full-shared-trace-before-each-measured-repeat-v1",
                    "process_reuse_between_phases": True,
                    "warmup_passes": 1,
                    "measured_passes": 1,
                    "repeats": 3,
                    "per_query_timeout_ms": 100,
                    "adapter_process_timeout_s": 10,
                    "concurrency": 1,
                    "max_timeouts": 0,
                },
                "resources": {
                    "device": "strict-template-fixture-device",
                    "data_mount": str(root),
                    "interval_s": 1,
                    "disk_interval_s": 1,
                    "min_samples": 2,
                },
                "systems": [
                    generic_system("seml0", "embedded"),
                    generic_system("livegraph", "embedded"),
                    generic_system("aster", "embedded"),
                    generic_system("tugraph", "embedded"),
                    neo4j,
                    nebulagraph_system(),
                ],
            }
            manifest_path = root / "strict-suite.json"
            write_json(manifest_path, manifest)
            command = [
                sys.executable,
                str(P10_DIR / "run_suite.py"),
                "validate",
                "--manifest",
                str(manifest_path),
                "--run-root",
                str(run_root),
                "--mode",
                "formal",
            ]
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["state"], "VALID")
            self.assertEqual(result["mode"], "formal")
            self.assertEqual(result["systems"], [
                "seml0", "livegraph", "aster", "tugraph", "neo4j", "nebulagraph"
            ])


if __name__ == "__main__":
    unittest.main()
