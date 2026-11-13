#!/usr/bin/env python3
"""Create or verify Armada universal update descriptors."""

import argparse
import json
import os
from pathlib import Path

from universal_package import build_descriptor
from updates.package_contract import PackageError, load_package
from updates.signature import MAX_DOCUMENT_BYTES, MAX_SIGNATURE_BYTES


def read_bounded(path: Path, limit: int, label: str) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{label} exceeds {limit} bytes")
    return data


def make(args: argparse.Namespace) -> int:
    descriptor = build_descriptor(
        args.archive,
        release_id=args.release_id,
        minimum_installer_version=args.minimum_installer_version,
        minimum_updater_version=args.minimum_updater_version,
        parts_manifest=args.parts_manifest,
        enable_offline_upgrade=args.offline_upgrade,
        install_models=args.install_model,
    )
    document = (
        json.dumps(descriptor, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    if len(document) > MAX_DOCUMENT_BYTES:
        raise ValueError(f"descriptor exceeds {MAX_DOCUMENT_BYTES} bytes")

    created = False
    try:
        with args.output.open("xb") as stream:
            created = True
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if created:
            try:
                args.output.unlink()
            except OSError:
                pass
        raise

    print(
        f"Created unsigned descriptor (not signed): {args.output}"
    )
    return 0


def verify(args: argparse.Namespace) -> int:
    try:
        document = read_bounded(
            args.descriptor, MAX_DOCUMENT_BYTES, "descriptor"
        )
        signature = read_bounded(
            args.signature, MAX_SIGNATURE_BYTES, "signature"
        )
        package = load_package(document, signature, trusted_key=args.trusted_key)
        print(package["releaseId"])
        return 0
    except (PackageError, OSError, ValueError) as error:
        print(f"verification failed: {error}", file=os.sys.stderr)
        return 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)

    create = commands.add_parser("make", help="create an unsigned descriptor")
    create.add_argument("--archive", type=Path, required=True)
    create.add_argument("--release-id", required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--parts-manifest", type=Path)
    create.add_argument("--minimum-installer-version", default="0.8.0")
    create.add_argument("--minimum-updater-version", default="0.8.0")
    create.add_argument("--offline-upgrade", action="store_true", default=False,
                        help="declare offline-upgrade for normal-Linux installs; requires the "
                             "bundle to record a verified normal updater and every packaged "
                             "model to have the dual-slot update backend; recovery manifests "
                             "optional")
    create.add_argument("--install-model", action="append", default=[],
                        metavar="ID",
                        help="registered non-default model packaged install-only; repeat once "
                             "for every custom model in the archive; never combines with "
                             "--offline-upgrade")
    create.set_defaults(handler=make)

    check = commands.add_parser("verify", help="verify a signed descriptor")
    check.add_argument("--descriptor", type=Path, required=True)
    check.add_argument("--signature", type=Path, required=True)
    check.add_argument("--trusted-key", type=Path, required=True)
    check.set_defaults(handler=verify)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        return args.handler(args)
    except (PackageError, OSError, ValueError) as error:
        print(f"{args.command} failed: {error}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
