#!/usr/bin/env python3
"""Refresh the pinned Decky Loader ARM64 preview tag from GitHub releases JSON.

Reads the current pin, selects the newest eligible preview release, compares it
against the currently pinned release, and rewrites the pin only when the new
release advances (newer published_at, or equal date with larger release id).
Prints the resulting tag on stdout only when the pin advances; otherwise the
script is a strict no-op that prints nothing.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

from updates.decky_release import DeckyRelease, DeckyReleaseError, select_preview

REPO_ROOT = Path(__file__).resolve().parents[2]
PIN_PATH = REPO_ROOT / "build_files" / "decky-preview-version"


def _read_pin() -> str:
    raw = PIN_PATH.read_text(encoding="utf-8")
    tag = raw.rstrip("\n")
    if not tag or "\n" in tag or "\r" in tag or tag != raw.split("\n", 1)[0]:
        raise SystemExit("Pin file must contain exactly one non-empty line")
    return tag


def _compare(a: DeckyRelease, b: DeckyRelease) -> int:
    ta = datetime.fromisoformat(a.published_at.replace("Z", "+00:00"))
    tb = datetime.fromisoformat(b.published_at.replace("Z", "+00:00"))
    if ta != tb:
        return -1 if ta < tb else 1
    if a.release_id != b.release_id:
        return -1 if a.release_id < b.release_id else 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("releases_json", type=Path, help="GitHub releases JSON list")
    args = parser.parse_args()

    try:
        data = json.loads(args.releases_json.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"error: cannot read releases JSON: {exc}", file=sys.stderr)
        return 1

    try:
        pin = _read_pin()
    except OSError as exc:
        print(f"error: cannot read pin: {exc}", file=sys.stderr)
        return 1

    try:
        latest = select_preview(data, prerelease_only=True)
        current = select_preview(data, prerelease_only=True, pinned_tag=pin)
    except DeckyReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if current.tag != pin:
        print(
            f"error: pinned tag {pin!r} resolved to {current.tag!r}",
            file=sys.stderr,
        )
        return 1

    if latest.tag == pin:
        # Identical release: strict no-op, no rewrite, no stdout.
        return 0

    cmp = _compare(latest, current)
    if cmp > 0:
        PIN_PATH.write_text(latest.tag + "\n", encoding="utf-8")
        print(latest.tag)
        return 0
    if cmp == 0:
        print(
            f"error: candidate {latest.tag!r} equal to pin {pin!r}",
            file=sys.stderr,
        )
        return 1

    print(
        f"error: refusing downgrade from {pin!r} to {latest.tag!r}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
