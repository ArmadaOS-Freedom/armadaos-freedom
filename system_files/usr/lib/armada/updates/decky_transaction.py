"""Durable, Decky-only file transaction with crash recovery."""
import dataclasses
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

from .decky_release import verify_payload
from .decky_source import _validate_release


class DeckyTransaction:
    _PHASES = {"stopping", "backing_up", "applying", "committed"}

    def __init__(self, services_dir: Path, state_dir: Path, lock_path: Path, service):
        self.services_dir = Path(services_dir)
        self.state_dir = Path(state_dir)
        self.lock_path = Path(lock_path)
        self.service = service
        self.txroot = self.state_dir / "transaction"
        self.pending = self.state_dir / "pending.json"
        self.targets = {
            "PluginLoader": self.services_dir / "PluginLoader",
            ".loader.version": self.services_dir / ".loader.version",
            "managed.json": self.state_dir / "managed.json",
        }

    @staticmethod
    def _fsync_dir(path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _sha256(path):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _copy(source, destination, expected=None):
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        fd = os.open(source, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError(f"Unsafe copy source: {source}")
            if expected is not None and (info.st_dev, info.st_ino) != tuple(expected):
                raise RuntimeError(f"Copy source changed: {source}")
            digest = hashlib.sha256()
            with os.fdopen(fd, "rb", closefd=False) as src, destination.open("xb") as dst:
                for block in iter(lambda: src.read(1024 * 1024), b""):
                    digest.update(block)
                    dst.write(block)
                dst.flush()
        finally:
            os.close(fd)
        destination.chmod(0o600)
        out = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(out)
        finally:
            os.close(out)
        DeckyTransaction._fsync_dir(destination.parent)
        return info, digest.hexdigest()

    def _atomic(self, path, data, mode, uid=None, gid=None):
        fd, temporary = tempfile.mkstemp(prefix=".armada-", dir=path.parent)
        tmp = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                if uid is not None:
                    os.fchown(stream.fileno(), uid, gid)
                os.fchmod(stream.fileno(), mode)
                os.fsync(stream.fileno())
            os.replace(tmp, path)
            self._fsync_dir(path.parent)
        finally:
            if tmp.exists():
                tmp.unlink()

    def _json(self, path, value):
        data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
        self._atomic(path, data, 0o600)

    @contextmanager
    def _locked(self):
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.lock_path, flags, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Decky transaction lock is busy") from exc
            yield
        finally:
            os.close(fd)

    def _check_targets(self):
        for path in self.targets.values():
            if os.path.lexists(path) and path.is_symlink():
                raise RuntimeError(f"Refusing symlink target: {path}")

    def _validate_dirs(self):
        for path in (self.state_dir, self.txroot, self.services_dir):
            if os.path.lexists(path):
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise RuntimeError(f"Unsafe transaction directory: {path}")

    def _setup(self):
        for path, mode in ((self.state_dir, 0o700), (self.txroot, 0o700),
                           (self.services_dir, 0o755)):
            if os.path.lexists(path):
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise RuntimeError(f"Unsafe transaction directory: {path}")
            else:
                path.mkdir(mode=mode, parents=True)
                path.chmod(mode)
                self._fsync_dir(path.parent)
        self._check_targets()

    def _journal(self, txid, phase, previous_active):
        self._json(self.pending, {"schema": 1, "txid": txid, "phase": phase,
                                  "previous_active": previous_active})

    def _load_pending(self):
        if self.pending.is_symlink():
            raise RuntimeError("Unsafe pending journal")
        with self.pending.open("rb") as stream:
            value = json.load(stream)
        required = {"schema", "txid", "phase", "previous_active"}
        if not isinstance(value, dict) or set(value) != required:
            raise ValueError("Invalid pending journal keys")
        if type(value["schema"]) is not int or value["schema"] != 1:
            raise ValueError("Unsupported pending journal schema")
        if not isinstance(value["txid"], str) or not re.fullmatch(r"[0-9a-f]{32}", value["txid"]):
            raise ValueError("Invalid transaction id")
        if not isinstance(value["phase"], str) or value["phase"] not in self._PHASES or type(value["previous_active"]) is not bool:
            raise ValueError("Invalid pending journal state")
        return value

    def _cleanup(self, txdir):
        if txdir.parent != self.txroot or not re.fullmatch(r"[0-9a-f]{32}", txdir.name):
            raise RuntimeError("Unsafe transaction cleanup path")
        if txdir.is_symlink():
            raise RuntimeError("Unsafe transaction directory")
        if txdir.exists():
            shutil.rmtree(txdir)
            self._fsync_dir(self.txroot)

    def _remove_pending(self):
        if self.pending.is_symlink():
            raise RuntimeError("Unsafe pending journal")
        self.pending.unlink()
        self._fsync_dir(self.state_dir)

    def _backup(self, txdir):
        entries = {}
        for name, path in self.targets.items():
            if os.path.lexists(path):
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise RuntimeError(f"Unsafe backup source: {path}")
                backup = txdir / (name + ".backup")
                opened, digest = self._copy(path, backup, (info.st_dev, info.st_ino))
                entries[name] = {"exists": True, "mode": stat.S_IMODE(opened.st_mode),
                                 "uid": opened.st_uid, "gid": opened.st_gid,
                                 "sha256": digest}
            else:
                entries[name] = {"exists": False, "mode": None, "uid": None,
                                 "gid": None, "sha256": None}
        self._json(txdir / "backups.json", {"schema": 1, "files": entries})
        return entries

    def _validated_backups(self, txdir):
        if txdir.is_symlink() or not txdir.is_dir():
            raise RuntimeError("Missing or unsafe transaction directory")
        manifest_path = txdir / "backups.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError("Invalid backup manifest file")
        with manifest_path.open("rb") as stream:
            manifest = json.load(stream)
        if not isinstance(manifest, dict) or set(manifest) != {"schema", "files"}:
            raise ValueError("Invalid backup manifest")
        if type(manifest["schema"]) is not int or manifest["schema"] != 1:
            raise ValueError("Unsupported backup manifest")
        entries = manifest["files"]
        if not isinstance(entries, dict) or set(entries) != set(self.targets):
            raise ValueError("Invalid backup file set")
        fields = {"exists", "mode", "uid", "gid", "sha256"}
        for name, entry in entries.items():
            if not isinstance(entry, dict) or set(entry) != fields or type(entry["exists"]) is not bool:
                raise ValueError("Invalid backup entry")
            if not entry["exists"]:
                if any(entry[key] is not None for key in fields - {"exists"}):
                    raise ValueError("Invalid absent backup entry")
                continue
            if (any(type(entry[key]) is not int or entry[key] < 0 for key in ("uid", "gid"))
                    or type(entry["mode"]) is not int or not 0 <= entry["mode"] <= 0o7777):
                raise ValueError("Invalid backup metadata")
            if not isinstance(entry["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
                raise ValueError("Invalid backup digest")
            backup = txdir / (name + ".backup")
            if backup.is_symlink() or not backup.is_file() or self._sha256(backup) != entry["sha256"]:
                raise ValueError(f"Backup integrity failure: {name}")
        return entries

    def _restore(self, txdir, entries):
        self._check_targets()
        for name, path in self.targets.items():
            entry = entries[name]
            if entry["exists"]:
                data = (txdir / (name + ".backup")).read_bytes()
                self._atomic(path, data, entry["mode"], entry["uid"], entry["gid"])
            elif os.path.lexists(path):
                path.unlink()
                self._fsync_dir(path.parent)

    def _recover(self):
        if not os.path.lexists(self.pending):
            return None
        self._validate_dirs()
        self._check_targets()
        journal = self._load_pending()
        txid, phase = journal["txid"], journal["phase"]
        previous = journal["previous_active"]
        txdir = self.txroot / txid
        restored = False
        if phase == "committed":
            result = {"state": "completed", "cleanup": "recovered"}
        else:
            if phase == "stopping":
                if previous and not self.service.is_active():
                    self.service.start()
            elif phase in ("backing_up", "applying"):
                # backing_up never restarts the current on-disk loader: the
                # backup snapshot is not trustworthy yet, so recovery must go
                # through the validated-backup restore path instead.
                self.service.stop()
                entries = self._validated_backups(txdir)
                self._restore(txdir, entries)
                restored = True
                if previous and entries["PluginLoader"]["exists"]:
                    self.service.start()
            healthy = bool(self.service.healthy(None))
            result = {"state": "recovered", "rollback_files_restored": restored,
                      "rollback_service_healthy": healthy}
        self._remove_pending()
        try:
            self._cleanup(txdir)
        except OSError:
            result["cleanup_pending"] = True
        return result

    def install(self, release, payload: Path) -> dict:
        with self._locked():
            self._setup()
            if os.path.lexists(self.pending):
                raise RuntimeError("Recovery required before another install")
            txdir = None
            try:
                release = _validate_release(release)
                txid = uuid.uuid4().hex
                txdir = self.txroot / txid
                txdir.mkdir(mode=0o700)
                self._fsync_dir(self.txroot)
                candidate = txdir / "payload"
                self._copy(Path(payload), candidate)
                verify_payload(release, candidate)
                previous = bool(self.service.is_active())
                self._journal(txid, "stopping", previous)
                self.service.stop()
                self._journal(txid, "backing_up", previous)
                entries = self._backup(txdir)
                self._journal(txid, "applying", previous)
                version = (release.tag + "\n").encode()
                managed = (json.dumps(dataclasses.asdict(release), sort_keys=True,
                                      separators=(",", ":")) + "\n").encode()
                for name, data, mode in (("PluginLoader", candidate.read_bytes(), 0o755),
                                         (".loader.version", version, 0o644),
                                         ("managed.json", managed, 0o600)):
                    entry = entries[name]
                    uid, gid = ((entry["uid"], entry["gid"]) if entry["exists"]
                                else (os.geteuid(), os.getegid()))
                    self._atomic(self.targets[name], data, mode, uid, gid)
                self.service.start()
                if not self.service.healthy(release):
                    raise RuntimeError("Decky service failed post-install health check")
                self._journal(txid, "committed", previous)
                self._remove_pending()
                try:
                    self._cleanup(txdir)
                except OSError:
                    pass
                return {"state": "completed"}
            except Exception as exc:
                result = {"state": "failed", "error": str(exc),
                          "rollback_files_restored": False,
                          "rollback_service_healthy": False}
                if os.path.lexists(self.pending):
                    try:
                        recovery = self._recover()
                        if recovery["state"] == "completed":
                            return recovery
                        result["rollback_files_restored"] = recovery["rollback_files_restored"]
                        result["rollback_service_healthy"] = recovery["rollback_service_healthy"]
                    except Exception as rollback_exc:
                        result["rollback_error"] = str(rollback_exc)
                elif txdir is not None:
                    try:
                        self._cleanup(txdir)
                    except OSError:
                        pass
                return result

    def recover(self):
        with self._locked():
            return self._recover()
