#!/usr/bin/env python3
"""Build and freeze the TuGraph 4.5.2 SF10 importer with pinned GCC 8.

This reuses the audited provenance helpers from build_tugraph_worker.py.  The
digest-pinned Ubuntu 18.04 image is an offline compiler environment only; the
resulting importer runs natively on the host.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from build_tugraph_worker import (
    DEFAULT_BUILD_IMAGE,
    BuildError,
    atomic_json,
    checked,
    compiler_provenance,
    existing_directory,
    existing_file,
    image_provenance,
    sha256_file,
    tree_ref,
)


RUNTIME_SCHEMA = "cidr-p10-tugraph-sf10-importer-runtime-v1"
EXECUTION_MODEL = "native-embedded-fresh-store-import-process-v1"


def run(args: argparse.Namespace) -> None:
    adapter_dir = Path(__file__).resolve().parent
    source = existing_file(adapter_dir / "tugraph_sf10_importer.cpp", "importer source")
    compat_include = existing_directory(adapter_dir / "compat", "compat include")
    prefix = existing_directory(args.prefix, "TuGraph prefix")
    include = existing_directory(prefix / "include", "TuGraph include")
    libdir = existing_directory(prefix / "lib64/lgraph", "TuGraph library directory")
    liblgraph = existing_file(libdir / "liblgraph.so", "liblgraph")
    gfortran = existing_directory(args.gfortran_libdir, "gfortran library directory")
    boost_include = existing_directory(args.boost_include, "Boost include")
    boost_headers = existing_directory(boost_include / "boost", "Boost header tree")
    date_include = existing_directory(args.date_include, "date include")
    docker_raw = shutil.which(args.docker)
    if docker_raw is None:
        raise BuildError(f"Docker client not found: {args.docker}")
    docker = existing_file(Path(docker_raw), "Docker client")
    image = image_provenance(docker, args.build_image)

    output = args.output.resolve()
    runtime_manifest = args.runtime_manifest.resolve()
    if output == runtime_manifest:
        raise BuildError("importer and runtime manifest outputs must differ")
    output.parent.mkdir(parents=True, exist_ok=True)
    runtime_manifest.parent.mkdir(parents=True, exist_ok=True)
    if output.parent != output.parent.resolve():
        raise BuildError("importer output parent must be canonical")

    name_seed = hashlib.sha256(
        (str(output) + "\0" + str(os.getpid())).encode("utf-8")
    ).hexdigest()[:12]
    compiler = compiler_provenance(
        docker, args.build_image, f"cidr-p10-tugraph-import-cc-probe-{name_seed}"
    )
    container_source = "/cidr-adapter/tugraph_sf10_importer.cpp"
    container_output = f"/cidr-output/{output.name}"
    flags = [
        "-O3",
        "-DNDEBUG",
        "-std=c++17",
        "-fopenmp",
        "-I",
        "/cidr-adapter/compat",
        "-I",
        str(include),
        "-I",
        str(boost_include),
        "-I",
        str(date_include),
        f"-Wl,-rpath,{libdir}",
        f"-Wl,-rpath,{gfortran}",
        "-Wl,--disable-new-dtags",
        "-Wl,--allow-shlib-undefined",
        "-o",
        container_output,
        container_source,
        str(liblgraph),
        "-lrt",
    ]
    uid = os.getuid()
    gid = os.getgid()
    compile_command = [
        str(docker),
        "run",
        "--rm",
        "--pull=never",
        "--name",
        f"cidr-p10-tugraph-import-build-{name_seed}",
        "--network",
        "none",
        "--read-only",
        "--user",
        f"{uid}:{gid}",
        "--tmpfs",
        f"/tmp:rw,exec,nosuid,size=512m,uid={uid},gid={gid}",
        "-v",
        f"{adapter_dir}:/cidr-adapter:ro",
        "-v",
        f"{prefix}:{prefix}:ro",
        "-v",
        f"{boost_include}:{boost_include}:ro",
        "-v",
        f"{date_include}:{date_include}:ro",
        "-v",
        f"{output.parent}:/cidr-output:rw",
        args.build_image,
        "/usr/local/bin/g++",
        *flags,
    ]
    checked(compile_command, "digest-pinned GCC 8 importer compile", timeout=180)
    if not os.access(output, os.X_OK):
        raise BuildError("container compiler did not produce an executable importer")

    ldd = checked(["ldd", str(output)], "importer ldd", timeout=30)
    expected_binding = f"liblgraph.so => {liblgraph}"
    if expected_binding not in ldd.stdout or "not found" in ldd.stdout:
        raise BuildError(f"importer runtime binding is invalid:\n{ldd.stdout}")
    capability = checked([str(output), "--capabilities"], "importer capability probe", timeout=20)
    try:
        capability_value = json.loads(capability.stdout)
    except json.JSONDecodeError as exc:
        raise BuildError(f"importer capability JSON is malformed: {exc}") from exc
    expected_capability = {
        "schema_version": "cidr-p10-tugraph-sf10-importer-capabilities-v1",
        "credential_source": "CIDR_TUGRAPH_PASSWORD",
        "dense_vid_contract": "internal-vid-equals-dense-id-v1",
        "formal_performance_points": 0,
    }
    if capability_value != expected_capability:
        raise BuildError("importer capability contract drift")

    docker_version = checked(
        [str(docker), "version", "--format", "{{.Client.Version}}"],
        "Docker client version probe",
        timeout=30,
    ).stdout.strip()
    value = {
        "schema_version": RUNTIME_SCHEMA,
        "system_version": args.system_version,
        "execution_model": EXECUTION_MODEL,
        "performance_eligible": False,
        "formal_performance_points": 0,
        "importer": {"path": str(output), "sha256": sha256_file(output)},
        "liblgraph": {"path": str(liblgraph), "sha256": sha256_file(liblgraph)},
        "header_tree": tree_ref(include),
        "compat_header_tree": {
            **tree_ref(compat_include),
            "scope": "unused-spatial-wkb-includes-only-no-wkb-api-v1",
        },
        "boost_header_tree": tree_ref(boost_headers),
        "date_header_tree": tree_ref(date_include),
        "compiler": compiler,
        "build_image": image,
        "docker_client": {
            "path": str(docker),
            "sha256": sha256_file(docker),
            "version": docker_version,
        },
        "source": {"path": str(source), "sha256": sha256_file(source)},
        "build_helper": {
            "path": str(Path(__file__).resolve()),
            "sha256": sha256_file(Path(__file__).resolve()),
        },
        "build_flags": flags,
        "runtime_image_digests": [],
        "runtime_container_names": [],
    }
    atomic_json(runtime_manifest, value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--prefix", type=Path, default=Path("/home/ydl/apps/tugraph-4.5.2/usr/local")
    )
    parser.add_argument(
        "--boost-include", type=Path, default=Path("/home/ydl/apps/boost_1_75_0")
    )
    parser.add_argument(
        "--date-include",
        type=Path,
        default=Path("/data/WorkSpace/tugraph_ldbc_snb/deps/date/include"),
    )
    parser.add_argument(
        "--gfortran-libdir",
        type=Path,
        default=Path("/home/ydl/apps/libs-root/usr/lib/x86_64-linux-gnu"),
    )
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--build-image", default=DEFAULT_BUILD_IMAGE)
    parser.add_argument("--system-version", default="TuGraph-4.5.2-native-embedded")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--runtime-manifest", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    try:
        run(parse_args())
        return 0
    except (BuildError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
