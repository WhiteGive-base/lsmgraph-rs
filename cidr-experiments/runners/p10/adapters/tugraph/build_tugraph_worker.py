#!/usr/bin/env python3
"""Build and freeze the native TuGraph P10 worker runtime.

The installed TuGraph 4.5.2 C++ ABI requires GCC 8.  The host compiler is GCC
9 and produces a binary which enters liblgraph but crashes in Galaxy's
constructor.  This builder therefore uses a locally present, digest-pinned
Ubuntu 18.04 image only as an offline compiler environment.  The resulting
worker runs natively on the host; no container participates in a P10 run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


RUNTIME_SCHEMA = "cidr-p10-tugraph-runtime-v1"
EXECUTION_MODEL = "native-embedded-single-worker-process-v1"
DEFAULT_BUILD_IMAGE = (
    "tugraph/tugraph-runtime-ubuntu18.04@"
    "sha256:b492ccc83f866170b1fb43da5fd89e0ea43199a3144e9faee5b0c02b167bcee5"
)
SHA_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class BuildError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_hash(root: Path) -> tuple[str, int, int]:
    files: list[Path] = []
    for directory, dirnames, filenames in os.walk(str(root), followlinks=False):
        base = Path(directory)
        for name in sorted(dirnames):
            if (base / name).is_symlink():
                raise BuildError(f"header tree rejects symlink directory: {base / name}")
        for name in sorted(filenames):
            path = base / name
            if path.is_symlink() or not path.is_file():
                raise BuildError(f"header tree rejects non-regular file: {path}")
            files.append(path)
    files.sort(key=lambda path: path.relative_to(root).as_posix().encode("utf-8"))
    if not files:
        raise BuildError(f"header tree is empty: {root}")
    digest = hashlib.sha256()
    total_bytes = 0
    for path in files:
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        total_bytes += size
        digest.update(
            f"file\0{relative}\0{size}\0{sha256_file(path)}\n".encode("utf-8")
        )
    return digest.hexdigest(), len(files), total_bytes


def tree_ref(path: Path) -> dict[str, object]:
    digest, count, total_bytes = tree_hash(path)
    return {
        "path": str(path),
        "sha256": digest,
        "hash_method": "sha256-tree-v1(relative-path,size,file-sha256)",
        "file_count": count,
        "total_bytes": total_bytes,
    }


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def existing_directory(path: Path, context: str) -> Path:
    result = path.resolve()
    if not result.is_dir():
        raise BuildError(f"{context} is not a directory: {result}")
    return result


def existing_file(path: Path, context: str) -> Path:
    result = path.resolve()
    if not result.is_file():
        raise BuildError(f"{context} is not a file: {result}")
    return result


def checked(command: list[str], context: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise BuildError(
            f"{context} failed ({completed.returncode}):\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
    return completed


def image_provenance(docker: Path, reference: str) -> dict[str, str]:
    if "@sha256:" not in reference:
        raise BuildError("--build-image must be pinned by repo digest")
    raw = checked(
        [str(docker), "image", "inspect", reference],
        "local build-image inspection",
        timeout=30,
    )
    try:
        values = json.loads(raw.stdout)
    except json.JSONDecodeError as exc:
        raise BuildError(f"Docker image inspection returned malformed JSON: {exc}") from exc
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
        raise BuildError("Docker image inspection did not identify exactly one local image")
    value = values[0]
    image_id = value.get("Id")
    repo_digests = value.get("RepoDigests")
    if not isinstance(image_id, str) or not SHA_RE.fullmatch(image_id):
        raise BuildError("local build image has an invalid image ID")
    if not isinstance(repo_digests, list) or reference not in repo_digests:
        raise BuildError("local image RepoDigests does not contain the pinned reference")
    return {"reference": reference, "repo_digest": reference, "image_id": image_id}


def compiler_provenance(
    docker: Path,
    image: str,
    container_name: str,
) -> dict[str, str]:
    probe = (
        "import hashlib,json,os,subprocess;"
        "p=os.path.realpath('/usr/local/bin/g++');"
        "h=hashlib.sha256(open(p,'rb').read()).hexdigest();"
        "v=subprocess.check_output(['/usr/local/bin/g++','--version']).decode().splitlines()[0];"
        "print(json.dumps({'path':p,'sha256':h,'version':v},sort_keys=True))"
    )
    completed = checked(
        [
            str(docker),
            "run",
            "--rm",
            "--pull=never",
            "--name",
            container_name,
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m",
            image,
            "python3",
            "-c",
            probe,
        ],
        "container compiler probe",
        timeout=30,
    )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise BuildError(f"compiler probe returned malformed JSON: {exc}") from exc
    if set(value) != {"path", "sha256", "version"}:
        raise BuildError("compiler probe schema drift")
    if not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
        raise BuildError("compiler probe SHA-256 is invalid")
    return value


def run(args: argparse.Namespace) -> None:
    adapter_dir = Path(__file__).resolve().parent
    source = existing_file(adapter_dir / "tugraph_p10_worker.cpp", "worker source")
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
        raise BuildError("worker and runtime manifest outputs must differ")
    output.parent.mkdir(parents=True, exist_ok=True)
    runtime_manifest.parent.mkdir(parents=True, exist_ok=True)
    if output.parent != output.parent.resolve():
        raise BuildError("worker output parent must be canonical")

    name_seed = hashlib.sha256(
        (str(output) + "\0" + str(os.getpid())).encode("utf-8")
    ).hexdigest()[:12]
    compiler = compiler_provenance(
        docker, args.build_image, f"cidr-p10-tugraph-cc-probe-{name_seed}"
    )
    container_source = "/cidr-adapter/tugraph_p10_worker.cpp"
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
        f"cidr-p10-tugraph-build-{name_seed}",
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
    checked(compile_command, "digest-pinned GCC 8 worker compile", timeout=180)
    if not os.access(output, os.X_OK):
        raise BuildError("container compiler did not produce an executable worker")

    ldd = checked(["ldd", str(output)], "worker ldd", timeout=30)
    expected_binding = f"liblgraph.so => {liblgraph}"
    if expected_binding not in ldd.stdout:
        raise BuildError(f"worker is not bound to requested liblgraph:\n{ldd.stdout}")
    if "not found" in ldd.stdout:
        raise BuildError(f"worker has unresolved shared libraries:\n{ldd.stdout}")
    capability = checked([str(output), "--capabilities"], "worker capability probe", timeout=20)
    try:
        capability_value = json.loads(capability.stdout)
    except json.JSONDecodeError as exc:
        raise BuildError(f"worker capability JSON is malformed: {exc}") from exc
    if capability_value.get("fixture_only") is not False or capability_value.get(
        "formal_query_count"
    ) != 1700:
        raise BuildError("worker capability does not advertise frozen formal contract")

    docker_version = checked(
        [str(docker), "version", "--format", "{{.Client.Version}}"],
        "Docker client version probe",
        timeout=30,
    ).stdout.strip()
    value = {
        "schema_version": RUNTIME_SCHEMA,
        "system_version": args.system_version,
        "execution_model": EXECUTION_MODEL,
        "worker": {"path": str(output), "sha256": sha256_file(output)},
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
