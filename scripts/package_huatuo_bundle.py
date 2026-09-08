#!/usr/bin/env python3
"""Package a complete set of Huatuo Memray runtimes."""

import argparse
import gzip
import hashlib
import json
import re
import shutil
import tarfile
import tempfile
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Dict
from typing import Iterable


PYTHON_VERSIONS = [f"3.{minor}" for minor in range(7, 15)]
ELF_MACHINES = {"x86_64": 62, "aarch64": 183}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Package Huatuo's versioned Memray runtime bundle"
    )
    parser.add_argument("--runtimes", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--architecture", choices=sorted(ELF_MACHINES), required=True)
    parser.add_argument("--bundle-version", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--source-date-epoch", required=True, type=int)
    return parser.parse_args()


def read_json(path: Path) -> Dict[str, object]:
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise RuntimeError(f"expected a JSON object in {path}")
    return value


def elf_machine(path: Path) -> int:
    with path.open("rb") as stream:
        header = stream.read(20)
    if len(header) < 20 or header[:4] != b"\x7fELF":
        raise RuntimeError(f"not an ELF shared object: {path}")
    if header[4] != 2 or header[5] != 1:
        raise RuntimeError(f"expected a little-endian 64-bit ELF object: {path}")
    return int.from_bytes(header[18:20], "little")


def validate_runtime(
    runtimes: Path, version: str, architecture: str
) -> Dict[str, object]:
    runtime = runtimes / f"py{version}"
    python_dir = runtime / "python"
    memray_dir = python_dir / "memray"
    required = [
        runtime / "runtime.json",
        memray_dir / "commands" / "_attach.gdb",
        memray_dir / "commands" / "_attach.lldb",
    ]
    for path in required:
        if not path.is_file():
            raise RuntimeError(f"runtime py{version} is missing {path.relative_to(runtime)}")

    extensions = list(memray_dir.glob("_memray*.so"))
    injectors = list(memray_dir.glob("_inject*.so"))
    if len(extensions) != 1 or len(injectors) != 1:
        raise RuntimeError(
            f"runtime py{version} must contain one _memray and one _inject shared object"
        )

    expected_machine = ELF_MACHINES[architecture]
    shared_objects = sorted(
        path
        for path in python_dir.rglob("*")
        if path.is_file() and ".so" in path.name
    )
    if not shared_objects:
        raise RuntimeError(f"runtime py{version} contains no shared objects")
    for path in shared_objects:
        machine = elf_machine(path)
        if machine != expected_machine:
            raise RuntimeError(
                f"{path} has ELF machine {machine}, expected {expected_machine}"
            )

    for wheel_metadata in python_dir.glob("*.dist-info/WHEEL"):
        if "musllinux" in wheel_metadata.read_text(
            encoding="utf-8", errors="replace"
        ):
            raise RuntimeError(f"musllinux package found in glibc runtime: {wheel_metadata}")

    metadata = read_json(runtime / "runtime.json")
    if metadata.get("python_version") != version:
        raise RuntimeError(
            f"runtime py{version} metadata reports Python "
            f"{metadata.get('python_version')!r}"
        )
    wheel = str(metadata.get("source_wheel", ""))
    if "manylinux" not in wheel or "musllinux" in wheel or architecture not in wheel:
        raise RuntimeError(f"runtime py{version} has unexpected source wheel: {wheel}")
    return metadata


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalized_paths(root: Path) -> Iterable[Path]:
    yield root
    yield from sorted(root.rglob("*"), key=lambda path: path.as_posix())


def write_deterministic_tar(source: Path, output: Path, epoch: int) -> None:
    with output.open("wb") as raw_stream:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_stream, mtime=epoch) as gz:
            with tarfile.open(mode="w", fileobj=gz, format=tarfile.PAX_FORMAT) as archive:
                for path in normalized_paths(source):
                    arcname = path.relative_to(source.parent).as_posix()
                    info = archive.gettarinfo(str(path), arcname=arcname)
                    info.uid = 0
                    info.gid = 0
                    info.uname = "root"
                    info.gname = "root"
                    info.mtime = epoch
                    if info.isreg():
                        with path.open("rb") as stream:
                            archive.addfile(info, stream)
                    else:
                        archive.addfile(info)


def safe_version(version: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._+-]", "-", version).strip("-")
    if not value:
        raise RuntimeError("bundle version does not contain a usable character")
    return value


def main() -> None:
    args = parse_args()
    runtimes = args.runtimes.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)

    expected_keys = {f"py{version}" for version in PYTHON_VERSIONS}
    actual_keys = {path.name for path in runtimes.iterdir() if path.is_dir()}
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        unexpected = sorted(actual_keys - expected_keys)
        raise RuntimeError(
            f"runtime set mismatch; missing={missing or 'none'}, "
            f"unexpected={unexpected or 'none'}"
        )

    runtime_metadata = {
        f"py{version}": validate_runtime(runtimes, version, args.architecture)
        for version in PYTHON_VERSIONS
    }
    memray_versions = {
        distribution["version"]
        for metadata in runtime_metadata.values()
        for distribution in metadata.get("distributions", [])
        if distribution.get("name", "").lower() == "memray"
    }
    if len(memray_versions) != 1:
        raise RuntimeError(f"expected one Memray version, found {sorted(memray_versions)}")

    source_time = datetime.fromtimestamp(
        args.source_date_epoch, tz=timezone.utc
    ).isoformat()
    manifest = {
        "schema_version": 1,
        "bundle_version": args.bundle_version,
        "memray_version": next(iter(memray_versions)),
        "source_revision": args.source_revision,
        "source_time": source_time,
        "platform": {
            "architecture": args.architecture,
            "libc": "glibc",
            "os": "linux",
        },
        "python_versions": PYTHON_VERSIONS,
        "runtimes": runtime_metadata,
    }

    archive_stem = (
        f"memray-huatuo-{safe_version(args.bundle_version)}-"
        f"linux-glibc-{args.architecture}"
    )
    archive_path = output / f"{archive_stem}.tar.gz"
    manifest_path = output / f"{archive_stem}.manifest.json"

    with tempfile.TemporaryDirectory(prefix="huatuo-memray-package-") as tmp:
        root = Path(tmp) / "memray"
        shutil.copytree(runtimes, root / "runtimes", symlinks=True)
        (root / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (root / "README.bundle").write_text(
            "Huatuo Memray runtime bundle\n"
            "\n"
            "Linux glibc runtimes for CPython 3.7 through 3.14.\n"
            "See manifest.json for the source revision and packaged files.\n",
            encoding="utf-8",
        )
        write_deterministic_tar(root, archive_path, args.source_date_epoch)

    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Created {archive_path}")
    print(f"SHA256 {sha256(archive_path)}  {archive_path.name}")


if __name__ == "__main__":
    main()
