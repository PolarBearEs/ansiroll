"""Build the deterministic pure-Python YAML bundle shipped by Ansiroll.

The upstream archive is verified before any member is read.  PyYAML's package
is stored as ``ansiroll_yaml`` so generated agents never depend on, or collide
with, a target's system ``yaml`` package.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import tarfile
import urllib.request
import zipfile


VERSION = "5.3.1"
SOURCE_URL = (
    "https://files.pythonhosted.org/packages/64/c2/"
    "b80047c7ac2478f9501676c988a5411ed5572f35d1beff9cae07d321512c/"
    "PyYAML-5.3.1.tar.gz"
)
SOURCE_SHA256 = "b8eac752c5e14d3eca0e6dd9199cd627518cb5ec06add0de9d32baeee6fe645d"
ARCHIVE_ROOT = "PyYAML-%s" % VERSION


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (0o100644 & 0xFFFF) << 16
    return info


def build(output: str) -> str:
    with urllib.request.urlopen(SOURCE_URL) as response:
        source = response.read()
    actual = hashlib.sha256(source).hexdigest()
    if actual != SOURCE_SHA256:
        raise RuntimeError(
            "PyYAML source hash mismatch: expected %s, got %s"
            % (SOURCE_SHA256, actual)
        )

    entries = {}
    with tarfile.open(fileobj=io.BytesIO(source), mode="r:gz") as archive:
        prefix = ARCHIVE_ROOT + "/lib3/yaml/"
        for member in archive.getmembers():
            if not member.isfile() or not member.name.startswith(prefix):
                continue
            relative = member.name[len(prefix):]
            if "/" in relative or not relative.endswith(".py"):
                continue
            # Do not ship the optional wrapper for the external ``_yaml`` C
            # extension.  Its presence would make behavior depend on a target
            # package with a compatible ABI instead of the bundled sources.
            if relative == "cyaml.py":
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                raise RuntimeError("cannot read %s" % member.name)
            entries["ansiroll_yaml/" + relative] = extracted.read()

        license_member = archive.getmember(ARCHIVE_ROOT + "/LICENSE")
        extracted = archive.extractfile(license_member)
        if extracted is None:
            raise RuntimeError("cannot read PyYAML license")
        entries["licenses/PyYAML-%s.txt" % VERSION] = extracted.read()

    if "ansiroll_yaml/__init__.py" not in entries:
        raise RuntimeError("PyYAML package was not found in the source archive")

    parent = os.path.dirname(os.path.abspath(output))
    os.makedirs(parent, exist_ok=True)
    with zipfile.ZipFile(output, "w") as bundle:
        for name in sorted(entries):
            bundle.writestr(_zip_info(name), entries[name])
    with open(output, "rb") as built:
        return hashlib.sha256(built.read()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "output",
        nargs="?",
        default=os.path.join("ansiroll", "vendor", "pyyaml-py35.zip"),
    )
    args = parser.parse_args()
    print(build(args.output))


if __name__ == "__main__":
    main()
