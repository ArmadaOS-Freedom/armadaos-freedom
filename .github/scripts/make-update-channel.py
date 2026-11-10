#!/usr/bin/env python3
"""Create or verify unsigned Armada update-channel metadata."""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "system_files/usr/lib/armada"))

from updates.catalog import CatalogError, load_channel
from updates.channel_release import bind_package, make_channel
from updates.signature import MAX_DOCUMENT_BYTES, MAX_SIGNATURE_BYTES


def read_bounded(path: Path, limit: int, label: str) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{label} exceeds {limit} bytes")
    return data


def make(args: argparse.Namespace) -> int:
    document = read_bounded(args.descriptor, MAX_DOCUMENT_BYTES, "descriptor")
    signature = read_bounded(
        args.descriptor_signature, MAX_SIGNATURE_BYTES, "descriptor signature"
    )
    channel_document = make_channel(
        document,
        signature,
        trusted_key=args.trusted_key,
        channel=args.channel,
        descriptor_url=args.descriptor_url,
        signature_url=args.signature_url,
        published_at=args.published_at,
        expected_oci_digest=args.expected_oci_digest,
        withdrawn=args.withdrawn,
    )
    payload = (
        json.dumps(channel_document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise ValueError(f"channel metadata exceeds {MAX_DOCUMENT_BYTES} bytes")

    created = False
    try:
        with args.output.open("xb") as stream:
            created = True
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if created:
            try:
                args.output.unlink()
            except OSError:
                pass
        raise

    print(f"Created unsigned channel metadata: {args.output}")
    return 0


def verify(args: argparse.Namespace) -> int:
    document = read_bounded(args.channel_file, MAX_DOCUMENT_BYTES, "channel file")
    signature = read_bounded(
        args.channel_signature, MAX_SIGNATURE_BYTES, "channel signature"
    )
    channel_document = load_channel(
        document, signature, expected_channel=args.expected_channel, trusted_key=args.trusted_key
    )
    descriptor = read_bounded(args.descriptor, MAX_DOCUMENT_BYTES, "descriptor")
    descriptor_signature = read_bounded(
        args.descriptor_signature, MAX_SIGNATURE_BYTES, "descriptor signature"
    )
    package = bind_package(
        channel_document,
        descriptor,
        descriptor_signature,
        trusted_key=args.trusted_key,
    )
    print(package["releaseId"])
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)

    create = commands.add_parser("make", help="create unsigned channel metadata")
    create.add_argument("--descriptor", type=Path, required=True)
    create.add_argument("--descriptor-signature", type=Path, required=True)
    create.add_argument("--trusted-key", type=Path, required=True)
    create.add_argument("--channel", required=True, choices=("stable", "beta", "testing"))
    create.add_argument("--descriptor-url", required=True)
    create.add_argument("--signature-url", required=True)
    create.add_argument("--published-at", required=True)
    create.add_argument("--expected-oci-digest", required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--withdrawn", action="store_true")
    create.set_defaults(handler=make)

    check = commands.add_parser("verify", help="verify signed channel metadata")
    check.add_argument("--channel-file", type=Path, required=True)
    check.add_argument("--channel-signature", type=Path, required=True)
    check.add_argument("--descriptor", type=Path, required=True)
    check.add_argument("--descriptor-signature", type=Path, required=True)
    check.add_argument("--trusted-key", type=Path, required=True)
    check.add_argument("--expected-channel", required=True, choices=("stable", "beta", "testing"))
    check.set_defaults(handler=verify)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        return args.handler(args)
    except (CatalogError, OSError, ValueError) as error:
        print(f"{args.command} failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
