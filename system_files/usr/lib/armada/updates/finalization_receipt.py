# SPDX-License-Identifier: GPL-3.0-only
"""Durable evidence of completed Linux trial finalization."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import uuid
from pathlib import Path

from . import boot_context
from .trial_record import BOOT_POLICY, CONFIRMED, encode_record

STATE_PATH = Path("/var/lib/armada/updates/system/finalized.json")
_MAX_RECEIPT_BYTES = 8192
_MAX_ERROR_CHARS = 200


def _document(record) -> dict:
    encoded = encode_record(record)
    if getattr(record, "state", None) != CONFIRMED:
        raise ValueError("trial record state is not CONFIRMED")
    deployment = f"{record.os_name}/{record.new_commit.hex()}.{record.new_serial}"
    return {
        "schemaVersion": 1,
        "recordSha256": hashlib.sha256(encoded).hexdigest(),
        "transactionId": str(uuid.UUID(bytes=bytes(record.transaction_uuid))),
        "recordSequence": record.sequence,
        "deployment": deployment,
        "defaultDeployment": deployment,
        "bootPolicy": BOOT_POLICY,
        "firmwareSlotControl": "not_issued",
    }


def _trusted_parent(path: Path, *, create: bool) -> Path:
    if not isinstance(path, Path):
        path = Path(path)
    if not path.is_absolute():
        raise ValueError("finalization receipt path must be absolute")
    if path.name in ("", ".", ".."):
        raise ValueError("finalization receipt path has no file name")
    parent = path.parent
    if parent != parent.resolve(strict=False):
        raise ValueError("finalization receipt parent must be canonical")
    if create:
        parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if parent != parent.resolve(strict=True):
        raise ValueError("finalization receipt parent resolves through symlink")
    st = os.lstat(parent)
    if not stat.S_ISDIR(st.st_mode):
        raise ValueError("finalization receipt parent is not a directory")
    if st.st_uid != 0:
        raise ValueError("finalization receipt parent must be owned by root")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError("finalization receipt parent is group or world writable")
    return parent / path.name


def _no_duplicate_keys(pairs) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _pending(error: object) -> dict:
    message = str(error) or type(error).__name__
    return {"state": "pending", "error": message[:_MAX_ERROR_CHARS]}


def _discard_installed(target: Path, identity: tuple[int, int]) -> None:
    """Remove the receipt file this call installed at ``target``.

    Used only when the rename landed but the containing directory could not be
    flushed: the new receipt is then readable without being provably durable,
    and a later ``read`` would report ``complete`` from state that may vanish
    on power loss.  Only the exact inode written by this call is removed, so a
    different file (for example one installed by another writer) is never
    touched.  Cleanup failures are ignored: the caller still propagates the
    original flush error, so the receipt is never reported as durable.
    """
    try:
        st = os.lstat(target)
    except OSError:
        return
    if (st.st_dev, st.st_ino) != identity or not stat.S_ISREG(st.st_mode):
        return
    try:
        os.unlink(target)
    except OSError:
        pass


def write(record) -> dict:
    if os.geteuid() != 0:
        raise PermissionError("finalization receipt write requires root")
    doc = _document(record)
    target = _trusted_parent(STATE_PATH, create=True)
    payload = (json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", dir=os.fspath(target.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        st = os.fstat(fd)
        installed = (st.st_dev, st.st_ino)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # Open the containing directory before the rename so that a failure to
        # open it leaves the previous receipt in place instead of replacing
        # evidence this call could never make durable.
        dir_fd = os.open(
            target.parent,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.replace(tmp_path, target)
            try:
                os.fsync(dir_fd)
            except OSError:
                # The receipt is visible but its directory entry is not durable,
                # so it must not stay readable as complete evidence.
                _discard_installed(target, installed)
                raise
        finally:
            os.close(dir_fd)
    except BaseException:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    return doc


def read(record) -> dict:
    try:
        expected = _document(record)
        target = _trusted_parent(STATE_PATH, create=False)
        st = os.lstat(target)
        if not stat.S_ISREG(st.st_mode):
            return _pending("finalization receipt is not a regular file")
        if st.st_uid != 0:
            return _pending("finalization receipt is not owned by root")
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return _pending("finalization receipt is group or world writable")
        raw = boot_context._read_regular(target, _MAX_RECEIPT_BYTES)
        loaded = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys
        )
        if not isinstance(loaded, dict):
            return _pending("finalization receipt is not a JSON object")
        if (
            type(loaded.get("schemaVersion")) is not int
            or type(loaded.get("recordSequence")) is not int
            or loaded.get("bootPolicy") != BOOT_POLICY
            or loaded.get("firmwareSlotControl") != "not_issued"
            or loaded != expected
        ):
            return _pending("finalization receipt does not match confirmed record")
        return {"state": "complete"}
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _pending(exc)
