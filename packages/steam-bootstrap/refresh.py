#!/usr/bin/env python3
import pathlib
import re
import sys

import fetch

PACKAGE_DIR = pathlib.Path(__file__).resolve().parent
BASE = PACKAGE_DIR / "BASE.env"
MANIFESTS = PACKAGE_DIR / "manifests"


def parse_version(contents):
    if re.match(r'\A\s*"linuxarm64"\s*\{', contents) is None:
        raise ValueError('Missing "linuxarm64" root at start')
    versions = re.findall(r'^\s*"version"\s+"([0-9]+)"\s*$', contents, re.MULTILINE)
    if len(versions) != 1:
        raise ValueError("Expected exactly one ASCII numeric version line")
    return versions[0]


def read_pinned(contents):
    match = re.search(r'^STEAM_CLIENT_VERSION=([0-9]+)$', contents, re.MULTILINE)
    if match is None:
        raise ValueError("BASE.env has no numeric STEAM_CLIENT_VERSION")
    return match.group(1)


def render_base(contents, version):
    pattern = re.compile(r'^STEAM_CLIENT_VERSION=[0-9]+$', re.MULTILINE)
    matches = list(pattern.finditer(contents))
    if len(matches) != 1:
        raise ValueError("BASE.env must contain exactly one version line")
    match = matches[0]
    return contents[:match.start()] + f"STEAM_CLIENT_VERSION={version}" + contents[match.end():]


def atomic_write(path, data):
    temporary = path.with_name(path.name + ".partial")
    temporary.write_bytes(data)
    temporary.replace(path)


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: refresh.py <manifest>")
    source = pathlib.Path(sys.argv[1])
    data = source.read_bytes()
    contents = data.decode("utf-8")
    version = parse_version(contents)
    fetch.packages(contents, version)
    env = BASE.read_text()
    pinned = read_pinned(env)
    if int(version) <= int(pinned):
        return
    target = MANIFESTS / version
    if target.exists() and target.read_bytes() != data:
        raise ValueError(f"manifests/{version} already exists with different contents")
    updated = render_base(env, version)
    MANIFESTS.mkdir(exist_ok=True)
    if not target.exists():
        atomic_write(target, data)
    if updated != env:
        atomic_write(BASE, updated.encode("utf-8"))
    print(version)


if __name__ == "__main__":
    main()
