#!/usr/bin/env python3
"""Create a relocatable Memray bundle for an embedding profiler."""

import argparse
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
    # Unlike memray-lite, the full package imports declared dependencies such
    # as rich while loading memray._memray. Keep them in the staged runtime.
    run(
        [
            python,
            "-m",
            "pip",
            "install",
            "--target",
            str(target),
            str(wheel),
        ]
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
        install_wheel(args.python, wheel, bundle_dir / "python")
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
