#!/usr/bin/env python3
"""Freeze the local NebulaGraph v3.8.0 Docker/client runtime identity."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from nebula_adapter import (
    EXPECTED_IMAGES,
    RUNTIME_SCHEMA,
    atomic_json,
    sha256_file,
    tree_identity,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--client-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    client = args.client_root.resolve()
    if not client.is_dir():
        parser.error("--client-root must be an existing directory")
    docker_raw = shutil.which("docker")
    if docker_raw is None:
        parser.error("docker is not on PATH")
    docker = Path(docker_raw).resolve()
    metadata = sorted(client.glob("nebula3_python-*.dist-info/METADATA"))
    if len(metadata) != 1:
        parser.error("client root must contain exactly one nebula3_python dist-info METADATA")
    version = ""
    for line in metadata[0].read_text(encoding="utf-8").splitlines():
        if line.startswith("Version: "):
            version = line.split(": ", 1)[1]
            break
    if version != "3.8.3":
        parser.error(f"expected nebula3-python 3.8.3, found {version!r}")
    images = []
    for role in ("graphd", "metad", "storaged"):
        expected = EXPECTED_IMAGES[role]
        completed = subprocess.run(
            [str(docker), "image", "inspect", expected["tag"]],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            parser.error(f"missing exact image {expected['tag']}: {completed.stderr.strip()}")
        value = json.loads(completed.stdout)[0]
        repo_digest = f"{expected['tag'].split(':', 1)[0]}@{expected['digest']}"
        if repo_digest not in value.get("RepoDigests", []):
            parser.error(f"{expected['tag']} does not resolve to {repo_digest}")
        images.append(
            {
                "role": role,
                "tag": expected["tag"],
                "repo_digest": repo_digest,
                "image_id": value["Id"],
            }
        )
    version_result = subprocess.run(
        [str(docker), "version", "--format", "{{.Client.Version}}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if version_result.returncode != 0:
        parser.error("cannot query Docker client version")
    manifest = {
        "schema_version": RUNTIME_SCHEMA,
        "system_version": "NebulaGraph 3.8.0",
        "client": {
            "version": version,
            "tree": {"path": str(client), **tree_identity(client)},
            "metadata": {"path": str(metadata[0]), "sha256": sha256_file(metadata[0])},
        },
        "docker": {
            "path": str(docker),
            "sha256": sha256_file(docker),
            "version": version_result.stdout.strip(),
        },
        "images": images,
    }
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(output, manifest)
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
