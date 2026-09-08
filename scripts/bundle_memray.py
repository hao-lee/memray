#!/usr/bin/env python3
"""Create a relocatable Memray bundle for an embedding profiler."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Iterable, Optional, Sequence


def shlex_join(parts: Iterable[str]) -> str:
    import shlex

    return " ".join(shlex.quote(str(part)) for part in parts)


def run(command: Sequence[str], **kwargs) -> None:
    print(f"+ {shlex_join(command)}")
    subprocess.run(command, check=True, **kwargs)


def build_wheel(python: str, output: Path) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    run([python, "-m", "build", "--wheel", "--outdir", str(output)])
    wheels = sorted(output.glob("memray-*.whl"))
    if not wheels:
        raise RuntimeError("build did not produce a Memray wheel")
    return wheels[-1]


def install_wheel(python: str, wheel: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    # Keep the declared dependency set so the bundled CLI and reporters remain
    # usable as well as the injected tracker.
    run(
        [
            python,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--only-binary=:all:",
            "--target",
            str(target),
            str(wheel),
        ]
    )


def copy_attach_helpers(source_root: Path, target: Path) -> None:
    source_commands = source_root / "src" / "memray" / "commands"
    target_commands = target / "memray" / "commands"
    target_commands.mkdir(parents=True, exist_ok=True)
    for name in ("_attach.gdb", "_attach.lldb"):
        source = source_commands / name
        if not source.is_file():
            raise RuntimeError(f"attach helper not found: {source}")
        shutil.copy2(source, target_commands / name)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def installed_distributions(target: Path) -> list:
    distributions = []
    for metadata in sorted(target.glob("*.dist-info/METADATA")):
        name = None
        version = None
        with metadata.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if line.startswith("Name: "):
                    name = line[6:].strip()
                elif line.startswith("Version: "):
                    version = line[9:].strip()
                if name is not None and version is not None:
                    break
        if name is not None and version is not None:
            distributions.append({"name": name, "version": version})
    return distributions


def verify_runtime(python: str, target: Path) -> None:
    memray_dir = target / "memray"
    extensions = list(memray_dir.glob("_memray*.so"))
    injectors = list(memray_dir.glob("_inject*.so"))
    if len(extensions) != 1:
        raise RuntimeError(f"expected one _memray extension, found {len(extensions)}")
    if len(injectors) != 1:
        raise RuntimeError(f"expected one _inject extension, found {len(injectors)}")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(target)
    run(
        [
            python,
            "-c",
            (
                "import pathlib, memray; "
                f"root = pathlib.Path({str(target)!r}).resolve(); "
                "loaded = pathlib.Path(memray.__file__).resolve(); "
                "assert str(loaded).startswith(str(root) + '/'), (root, loaded)"
            ),
        ],
        env=env,
    )


def write_runtime_metadata(bundle_dir: Path, wheel: Path) -> None:
    python_dir = bundle_dir / "python"
    metadata = {
        "schema_version": 1,
        "python_version": f"{sys.version_info[0]}.{sys.version_info[1]}",
        "source_wheel": wheel.name,
        "source_wheel_sha256": sha256(wheel),
        "distributions": installed_distributions(python_dir),
    }
    (bundle_dir / "runtime.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_wrapper(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "memray"
    script.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env bash
            set -euo pipefail
            ROOT="$(cd "$(dirname "$0")/.." && pwd)"
            export PYTHONPATH="$ROOT/python${PYTHONPATH:+:$PYTHONPATH}"
            exec "${MEMRAY_PYTHON:-python3}" -m memray "$@"
            """
        ).strip()
        + "\n"
    )
    script.chmod(0o755)


def write_readme(bundle_dir: Path) -> None:
    (bundle_dir / "README.bundle").write_text(
        textwrap.dedent(
            """
            Memray runtime bundle
            =====================

            python/ contains the site-packages tree loaded into the target.
            bin/memray runs the bundled package through MEMRAY_PYTHON or python3.
            """
        ).strip()
        + "\n"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a relocatable Memray runtime bundle"
    )
    parser.add_argument("--output", default="build/memray", type=Path)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument(
        "--source-root",
        default=Path(__file__).resolve().parents[1],
        type=Path,
    )
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--tarball", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    bundle_dir = args.output.resolve()
    if args.clean and bundle_dir.exists():
        shutil.rmtree(bundle_dir)

    temporary_dir = None  # type: Optional[Path]
    try:
        wheel = args.wheel
        if wheel is None:
            temporary_dir = Path(tempfile.mkdtemp(prefix="memray-bundle-wheel"))
            wheel = build_wheel(args.python, temporary_dir)
        wheel = wheel.resolve()
        python_dir = bundle_dir / "python"
        install_wheel(args.python, wheel, python_dir)
        copy_attach_helpers(args.source_root.resolve(), python_dir)
        verify_runtime(args.python, python_dir)
        write_runtime_metadata(bundle_dir, wheel)
        write_wrapper(bundle_dir / "bin")
        write_readme(bundle_dir)
        if args.tarball:
            archive = shutil.make_archive(
                str(bundle_dir.parent / bundle_dir.name),
                "gztar",
                root_dir=bundle_dir.parent,
                base_dir=bundle_dir.name,
            )
            print(f"Created {archive}")
    finally:
        if temporary_dir and temporary_dir.exists():
            shutil.rmtree(temporary_dir)

    print(f"Bundle ready at {bundle_dir}")


if __name__ == "__main__":
    main()
