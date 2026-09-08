Huatuo runtime bundles
======================

The ``Huatuo runtime bundles`` workflow builds the Memray runtimes embedded by
Huatuo. These artifacts are separate from Memray's PyPI wheels.

The supported matrix is CPython 3.7 through 3.14 on Linux glibc, for x86-64
and AArch64. Free-threaded CPython, musl, macOS, and 32-bit targets are not
included. Python 3.7 uses cibuildwheel 2.23.3 because cibuildwheel 3.x no
longer builds that interpreter.

Every wheel is repaired by auditwheel. Its matching Python interpreter then
installs the wheel and dependencies into this layout::

    memray/
      manifest.json
      runtimes/
        py3.7/
          runtime.json
          python/
        ...
        py3.14/
          runtime.json
          python/

The packaging job rejects missing versions, mixed architectures, musllinux
packages, and runtimes without the Memray extension, injector, or debugger
attach scripts. Archive metadata and ordering are normalized after the runtime
dependencies have been resolved.

Run the workflow manually to inspect its artifacts. To publish assets, tag the
fork with ``huatuo-bundle-v<version>`` and push the tag. The workflow creates
or updates the matching GitHub release with one archive and one manifest per
architecture, plus ``SHA256SUMS``.

Huatuo can extract an archive below ``_output/tools``; the archive's top-level
directory is ``memray``. Source builds remain available and are not replaced
by this release workflow.
