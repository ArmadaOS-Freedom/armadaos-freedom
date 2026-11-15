#!/usr/bin/python3
"""Real loop-device end-to-end test for kshim-write-boot.py.

Creates its own sparse disk image under --work, one GPT partition named boot_a
with a fixed test PARTUUID, attaches it with losetup --find --show --partscan,
and drives the real writer CLI against that partition. The images here are
synthetic 96 MiB fixtures, not real boot images, and nothing is mounted,
formatted, rebooted, or sent over the network.

This script must run as root with loop devices available (remote Docker). When
/dev/loop-control is absent it prints a skip line and exits 77 before creating
--work or any fixture. It only detaches the loop device it allocated for this
run's disk file. Raw fixtures, transaction directories, and private block nodes
are kept under --work for the manager to remove after logs are collected.
"""
import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from contextlib import redirect_stderr, redirect_stdout

BOOT_BYTES = 100663296
MAGIC = b"ANDROID!"
CHUNK = 1 << 20
DISK_BYTES = 112 * 1024 * 1024
PART_SECTORS = BOOT_BYTES // 512
PART_START = 2048
PARTUUID = "11111111-2222-3333-4444-555555555555"
ROOT_UUID = "aaaaaaaa-1111-2222-3333-444444444444"
BOOT_UUID = "bbbbbbbb-1111-2222-3333-444444444444"
STOCK = "9" * 64
COMMIT = "c" * 64
OLD = MAGIC + b"O" * (BOOT_BYTES - len(MAGIC))
CANDIDATE = MAGIC + b"C" * (BOOT_BYTES - len(MAGIC))
FALLBACK = MAGIC + b"F" * (BOOT_BYTES - len(MAGIC))

COMMANDS = []


def log(argv, proc=None):
    entry = {"argv": [str(item) for item in argv]}
    if proc is not None:
        entry.update({"returncode": proc.returncode,
                      "stdout": proc.stdout, "stderr": proc.stderr})
    COMMANDS.append(entry)
    return entry


def run(argv, **kwargs):
    proc = subprocess.run([str(item) for item in argv], capture_output=True,
                          text=True, check=False, **kwargs)
    log(argv, proc)
    return proc


def checked(argv, **kwargs):
    proc = run(argv, **kwargs)
    if proc.returncode != 0:
        raise RuntimeError("%s failed (%d): %s"
                           % (" ".join(str(item) for item in argv),
                              proc.returncode, proc.stderr.strip()))
    return proc.stdout.strip()


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha_node(path):
    digest = hashlib.sha256()
    total = 0
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        while total < BOOT_BYTES:
            chunk = os.read(fd, min(CHUNK, BOOT_BYTES - total))
            if not chunk:
                raise RuntimeError("short read from %s at %d" % (path, total))
            digest.update(chunk)
            total += len(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


def write_node(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CLOEXEC)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise RuntimeError("short write to %s" % path)
            view = view[written:]
    finally:
        os.close(fd)


def write_file(path, data):
    with open(path, "wb") as handle:
        handle.write(data)


def report_for(image_sha):
    return {
        "layout_mode": "installed",
        "offline_verified": True,
        "checks": [{"check": "installed:result-file", "ok": True},
                   {"check": "installed:result-json", "ok": True}],
        "inputs": {"stock_boot": {"path": "/test-data/stock-boot.img",
                                  "length": BOOT_BYTES, "sha256": STOCK}},
        "outputs": {"final_boot": {"path": "/test-data/boot.img",
                                   "length": BOOT_BYTES, "sha256": image_sha}},
        "installed_metadata": {
            "deployment_checksum": COMMIT,
            "filesystem_uuids": {"root": ROOT_UUID, "boot": BOOT_UUID},
            "options": "root=UUID=%s boot=UUID=%s rw quiet" % (ROOT_UUID, BOOT_UUID),
        },
    }


def write_json(path, document):
    Path(path).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def block_node(work, name, dev_text):
    major, minor = (int(part) for part in dev_text.split(":"))
    path = work / name
    if not path.exists():
        os.mknod(path, stat.S_IFBLK | 0o600, os.makedev(major, minor))
    return path


def read_sysfs(path):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def wait_for(path, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.1)
    return False


def detach_owned(loop, loop_arg, disk, summary):
    """Detach only the loop allocated from this run's disk file.

    Returns True only when the loop is verifiably gone; every refusal or
    losetup error is recorded in the summary and reported as False.
    """
    result = {"loop": loop, "path": loop_arg, "detached": False}
    summary["cleanup"] = result
    if not loop:
        result["reason"] = "no loop was allocated"
        return True
    base = os.path.basename(loop)
    sysfs = "/sys/block/%s/loop/backing_file" % base
    backing = read_sysfs(sysfs)
    if not backing:
        if not os.path.exists("/sys/block/%s" % base):
            result["detached"] = True
            result["reason"] = "loop already absent"
            return True
        result["reason"] = "loop still present but no backing_file in sysfs"
        return False
    result["backing_file"] = backing
    if os.path.realpath(backing) != os.path.realpath(disk):
        result["reason"] = "refused: backing file is %s, owned %s" % (backing, disk)
        return False
    proc = run(["losetup", "-d", loop_arg])
    result.update({"returncode": proc.returncode,
                   "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip()})
    if proc.returncode != 0:
        result["reason"] = "losetup -d failed (%d)" % proc.returncode
        return False
    deadline = time.time() + 2.0
    remaining = read_sysfs(sysfs)
    while remaining and time.time() < deadline:
        time.sleep(0.05)
        remaining = read_sysfs(sysfs)
    if remaining:
        result["reason"] = "loop still attached to %s" % remaining
        return False
    result["detached"] = True
    return True


def writer_argv(script, fixture, transaction, expected, partuuid=PARTUUID):
    """Return an all-string argv; the writer's parser rejects Path objects."""
    return [str(sys.executable), str(script),
            "--image", str(fixture["candidate"]),
            "--report", str(fixture["candidate_report"]),
            "--fallback-image", str(fixture["fallback"]),
            "--fallback-report", str(fixture["fallback_report"]),
            "--target", str(fixture["partition"]),
            "--partuuid", str(partuuid),
            "--expected-current-sha256", str(expected),
            "--transaction-dir", str(transaction)]


def make_fixtures(work):
    fixture = {
        "candidate": work / "test-candidate.img",
        "fallback": work / "test-fallback.img",
        "candidate_report": work / "test-candidate-report.json",
        "fallback_report": work / "test-fallback-report.json",
        "transaction": work / "tx-success",
    }
    write_file(fixture["candidate"], CANDIDATE)
    write_file(fixture["fallback"], FALLBACK)
    write_json(fixture["candidate_report"], report_for(sha_bytes(CANDIDATE)))
    write_json(fixture["fallback_report"], report_for(sha_bytes(FALLBACK)))
    return fixture


def load_module(repo_root):
    script = Path(repo_root) / "system_files/usr/libexec/armada/kshim-write-boot.py"
    spec = importlib.util.spec_from_file_location("kshim_write_boot_loop", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, script


def main():
    parser = argparse.ArgumentParser(description="Loop-device test for kshim-write-boot.py")
    parser.add_argument("--work", required=True, help="empty or absent work directory")
    parser.add_argument("--repo-root", required=True, help="armadaos-freedom checkout")
    args = parser.parse_args()

    work = Path(os.path.abspath(args.work))
    was_absent = not work.exists()
    summary = {
        "work": str(work),
        "test_data": "synthetic 96 MiB fixtures, not real boot images; no mount/format/network",
        "sha256": {"old": sha_bytes(OLD), "candidate": sha_bytes(CANDIDATE),
                   "fallback": sha_bytes(FALLBACK)},
        "results": {},
    }
    loop = None
    loop_arg = None
    cleanup_ok = False
    error = None
    disk = work / "t023-loop-disk.img"
    try:
        if os.geteuid() != 0:
            raise RuntimeError("must run as root")

        # Loop devices are only reachable through the control node, so its
        # absence is a missing precondition, not a failure. Report it before
        # creating the work directory, fixtures, or any writer state.
        if not os.path.exists("/dev/loop-control"):
            print("SKIP: loop devices unavailable: /dev/loop-control is absent",
                  file=sys.stderr)
            return 77

        if work.exists():
            if not work.is_dir() or os.listdir(work) != []:
                raise RuntimeError("work dir must be absent or empty: %s" % work)
        else:
            work.mkdir(parents=True, mode=0o700)
        summary["work_was_absent"] = was_absent

        with open(disk, "wb") as handle:
            handle.truncate(DISK_BYTES)
        summary["disk"] = str(disk)

        sfdisk_script = ('label: gpt\nstart=%d, size=%d, name="boot_a", uuid=%s\n'
                         % (PART_START, PART_SECTORS, PARTUUID))
        checked(["sfdisk", "--quiet", disk], input=sfdisk_script)

        loop = checked(["losetup", "--find", "--show", "--partscan", disk])
        loop_arg = loop
        summary["loop"] = loop
        if not loop.startswith("/dev/loop"):
            raise RuntimeError("unexpected losetup output: %r" % loop)
        base = os.path.basename(loop)
        # Private parent node before any further check, so the finally block
        # can address the loop even when /dev is not populated here.
        loop_dev = read_sysfs("/sys/class/block/%s/dev" % base)
        summary["loop_dev"] = loop_dev
        if loop_dev and not os.path.exists(loop):
            loop_arg = str(block_node(work, "loop-block", loop_dev))
        summary["loop_node"] = loop_arg
        backing = read_sysfs("/sys/block/%s/loop/backing_file" % base)
        summary["loop_backing_file"] = backing
        if os.path.realpath(backing) != os.path.realpath(disk):
            raise RuntimeError("allocated loop is not backed by this disk: %r" % backing)

        part_name = base + "p1"
        if not wait_for("/sys/class/block/%s/dev" % part_name):
            raise RuntimeError("partition %s did not appear" % part_name)
        part_dev = read_sysfs("/sys/class/block/%s/dev" % part_name)
        part_node = block_node(work, "boot_a-part", part_dev)
        summary["partition"] = {"sysfs": part_name, "dev": part_dev, "node": str(part_node)}
        summary["partition_number"] = read_sysfs("/sys/class/block/%s/partition" % part_name)

        fixture = make_fixtures(work)
        fixture["partition"] = part_node

        write_node(part_node, OLD)
        old_sha = sha_node(part_node)
        if old_sha != sha_bytes(OLD):
            raise RuntimeError("seeded partition hash mismatch: %s" % old_sha)

        fixture["transaction"] = work / "tx-success"
        module, script = load_module(args.repo_root)
        proc = run(writer_argv(script, fixture, fixture["transaction"], old_sha))
        backup_path = fixture["transaction"] / "before.img"
        success_record = json.loads((fixture["transaction"] / "transaction.json")
                                    .read_text(encoding="utf-8"))
        summary["results"]["success"] = {
            "returncode": proc.returncode,
            "stderr": proc.stderr.strip(),
            "status": success_record["status"],
            "target_sha256": sha_node(part_node),
            "backup_sha256": sha_node(backup_path) if backup_path.exists() else None,
            "transaction": str(fixture["transaction"] / "transaction.json"),
        }
        if proc.returncode != 0 or sha_node(part_node) != sha_bytes(CANDIDATE):
            raise RuntimeError("success case did not write the candidate")
        if not backup_path.exists() or sha_node(backup_path) != sha_bytes(OLD):
            raise RuntimeError("success case did not retain the old backup")

        unchanged_tx = work / "tx-unchanged"
        proc = run(writer_argv(script, fixture, unchanged_tx, sha_bytes(CANDIDATE)))
        summary["results"]["unchanged"] = {
            "returncode": proc.returncode,
            "target_sha256": sha_node(part_node),
            "before_img_exists": (unchanged_tx / "before.img").exists(),
            "status": json.loads((unchanged_tx / "transaction.json")
                                 .read_text(encoding="utf-8"))["status"],
        }
        if proc.returncode != 0 or (unchanged_tx / "before.img").exists():
            raise RuntimeError("unchanged case wrote or backed up")

        reject_tx = work / "tx-wrong-guid"
        proc = run(writer_argv(script, fixture, reject_tx, sha_bytes(CANDIDATE),
                               partuuid="22222222-2222-3333-4444-555555555555"))
        summary["results"]["wrong_guid"] = {
            "returncode": proc.returncode,
            "stderr": proc.stderr.strip(),
            "target_sha256": sha_node(part_node),
        }
        if proc.returncode != 1 or sha_node(part_node) != sha_bytes(CANDIDATE):
            raise RuntimeError("wrong GUID was not rejected cleanly")

        reject_tx = work / "tx-wrong-current"
        proc = run(writer_argv(script, fixture, reject_tx, "0" * 64))
        summary["results"]["wrong_current"] = {
            "returncode": proc.returncode,
            "stderr": proc.stderr.strip(),
            "target_sha256": sha_node(part_node),
        }
        if proc.returncode != 1 or sha_node(part_node) != sha_bytes(CANDIDATE):
            raise RuntimeError("wrong current hash was not rejected cleanly")

        # Start the fault case from the original bytes so the retained backup
        # is demonstrably the old image, then fail once after a partial write.
        write_node(part_node, OLD)
        fault_tx = work / "tx-midwrite"
        real_write = module.write_image
        state = {"calls": 0}

        def flaky(fd, image_fd, length):
            if state["calls"] == 0:
                state["calls"] += 1
                os.lseek(image_fd, 0, os.SEEK_SET)
                os.lseek(fd, 0, os.SEEK_SET)
                module.write_all(fd, os.read(image_fd, length // 2))
                raise module.WriteError("loop-test deliberate partial write")
            return real_write(fd, image_fd, length)

        module.write_image = flaky
        out = io.StringIO()
        err = io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = module.main(writer_argv(script, fixture, fault_tx,
                                               sha_bytes(OLD))[2:])
        finally:
            module.write_image = real_write
        log(writer_argv(script, fixture, fault_tx, sha_bytes(OLD)))
        COMMANDS[-1].update({"returncode": code, "stdout": out.getvalue(),
                             "stderr": err.getvalue(), "in_process": True})
        fault_record = json.loads((fault_tx / "transaction.json")
                                  .read_text(encoding="utf-8"))
        summary["results"]["midwrite_fault"] = {
            "returncode": code,
            "stdout": out.getvalue().strip(),
            "stderr": err.getvalue().strip(),
            "target_sha256": sha_node(part_node),
            "matches_fallback": sha_node(part_node) == sha_bytes(FALLBACK),
            "backup_sha256": sha_node(fault_tx / "before.img")
            if (fault_tx / "before.img").exists() else None,
            "status": fault_record["status"],
            "error": fault_record["error"],
            "recovery_error": fault_record["recovery_error"],
        }
        if code != 2 or sha_node(part_node) != sha_bytes(FALLBACK):
            raise RuntimeError("mid-write fault did not restore the fallback")
        if not (fault_tx / "before.img").exists() \
                or sha_node(fault_tx / "before.img") != sha_bytes(OLD):
            raise RuntimeError("old backup was not retained")
    except BaseException as failure:
        error = "%s: %s" % (type(failure).__name__, failure)
    finally:
        try:
            cleanup_ok = detach_owned(loop, loop_arg, str(disk), summary)
        except BaseException as failure:
            cleanup_ok = False
            summary["cleanup"] = {
                "loop": loop, "path": loop_arg, "detached": False,
                "reason": "cleanup raised %s: %s" % (type(failure).__name__, failure)}

    summary["cleanup_ok"] = cleanup_ok
    summary["error"] = error
    if not cleanup_ok and error is None:
        summary["error"] = "cleanup failed: %s" % (
            summary.get("cleanup", {}).get("reason", "unknown"))
    summary["commands"] = COMMANDS
    print(json.dumps(summary, indent=2))
    return 0 if (error is None and cleanup_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
