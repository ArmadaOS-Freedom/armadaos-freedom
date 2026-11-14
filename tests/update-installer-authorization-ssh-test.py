import grp
import json
import os
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import time

TEST_USER = "armada-auth-smoke"
TEST_HOME = f"/home/{TEST_USER}"
SUDOERS_FILE = f"/etc/sudoers.d/{TEST_USER}"
STUB_PATH = "/usr/libexec/armada/armada-offline-update"
ENTRY_DST = "/usr/libexec/armada/armada-authorize-installer"
MOD_DST = "/usr/lib/armada/updates/installer_authorization.py"
HOST_KEY_PATH = "/etc/ssh/ssh_host_ed25519_key.pub"
HOST_PRIV_KEY = "/etc/ssh/ssh_host_ed25519_key"
MARKER = "/tmp/armada-auth-should-not-exist"
AUTHORIZED_KEYS = f"{TEST_HOME}/.ssh/authorized_keys"
RPC_COMMAND = f"/usr/bin/python3 -I {STUB_PATH}"

STUB_SCRIPT = """#!/usr/bin/python3 -I
import json, os, sys
print(json.dumps({"euid": os.geteuid(), "argc": len(sys.argv), "marker": "offline-auth-sink"}))
"""
EXPECTED_STUB = {"euid": 0, "argc": 1, "marker": "offline-auth-sink"}


def run(*args, **kwargs):
    kwargs.setdefault("timeout", 15)
    return subprocess.run(*args, **kwargs)


def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def main():
    if not os.path.exists("/.dockerenv") or os.geteuid() != 0 or os.environ.get("ARMADA_DISPOSABLE_PRIVILEGE_TEST") != "1":
        sys.stderr.write("Refusing to run: requires /.dockerenv, euid 0, ARMADA_DISPOSABLE_PRIVILEGE_TEST=1\n")
        return 2

    try:
        pwd.getpwnam(TEST_USER)
    except KeyError:
        pass
    else:
        sys.stderr.write(f"Refusing to run: test user {TEST_USER} already exists\n")
        return 2

    for p in [TEST_HOME, SUDOERS_FILE, STUB_PATH, ENTRY_DST, MOD_DST, MARKER]:
        if os.path.lexists(p):
            sys.stderr.write(f"Refusing to run: test path {p} already exists\n")
            return 2

    host_priv_exists = os.path.lexists(HOST_PRIV_KEY)
    host_pub_exists = os.path.lexists(HOST_KEY_PATH)
    if host_priv_exists != host_pub_exists:
        sys.stderr.write("Refusing to run: incomplete SSH host key pair\n")
        return 2

    temp_dir = tempfile.mkdtemp(prefix="armada-ssh-test-")
    account_created = False
    group_created = False
    created_paths = []
    sshd_proc = None
    sshd_err = None
    results = {}

    try:
        try:
            grp.getgrnam("wheel")
        except KeyError:
            run(["groupadd", "wheel"], check=True)
            group_created = True

        run(["useradd", "-m", "-s", "/bin/sh", "-G", "wheel", "-p", "x", TEST_USER], check=True)
        account_created = True
        os.chmod(TEST_HOME, 0o700)

        os.makedirs(os.path.dirname(STUB_PATH), exist_ok=True)
        os.makedirs(os.path.dirname(MOD_DST), exist_ok=True)
        shutil.copy2("system_files/usr/libexec/armada/armada-authorize-installer", ENTRY_DST)
        created_paths.append(ENTRY_DST)
        shutil.copy2("system_files/usr/lib/armada/updates/installer_authorization.py", MOD_DST)
        created_paths.append(MOD_DST)
        os.chmod(ENTRY_DST, 0o755)
        os.chmod(MOD_DST, 0o644)

        with open(STUB_PATH, "w") as f:
            f.write(STUB_SCRIPT)
        created_paths.append(STUB_PATH)
        os.chmod(STUB_PATH, 0o755)

        sudo_rule = f"%wheel ALL=(root) NOPASSWD: /usr/bin/python3 -I {STUB_PATH}\n"
        with open(SUDOERS_FILE, "w") as f:
            f.write(sudo_rule)
        created_paths.append(SUDOERS_FILE)
        os.chmod(SUDOERS_FILE, 0o440)
        run(["visudo", "-c", "-f", SUDOERS_FILE], check=True)

        if not host_priv_exists:
            run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", HOST_PRIV_KEY], check=True)
            created_paths.extend([HOST_KEY_PATH, HOST_PRIV_KEY])

        client_key = os.path.join(temp_dir, "client_key")
        unauth_key = os.path.join(temp_dir, "unauth_key")
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", client_key], check=True, stdout=subprocess.DEVNULL)
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", unauth_key], check=True, stdout=subprocess.DEVNULL)

        with open(f"{client_key}.pub") as f:
            client_pub = f.read().strip().split()[:2]
        client_pub_line = f"{client_pub[0]} {client_pub[1]}"

        enr = run(["runuser", "-u", TEST_USER, "--", "/usr/bin/python3", "-I", ENTRY_DST, client_pub_line],
                  capture_output=True, text=True, check=True)
        enr_data = json.loads(enr.stdout)
        fp_out = run(["ssh-keygen", "-lf", HOST_KEY_PATH, "-E", "sha256"], capture_output=True, text=True, check=True)
        expected_host_fp = fp_out.stdout.strip().split()[1]
        assert enr_data["schema"] == 1
        assert enr_data["hostKeyFingerprint"] == expected_host_fp
        assert enr_data["userName"] == TEST_USER

        with open(AUTHORIZED_KEYS, "rb") as f:
            authorized_keys_before = f.read()
        enr_repeat = run(["runuser", "-u", TEST_USER, "--", "/usr/bin/python3", "-I", ENTRY_DST, client_pub_line],
                         capture_output=True, text=True, check=True)
        assert json.loads(enr_repeat.stdout) == enr_data
        with open(AUTHORIZED_KEYS, "rb") as f:
            authorized_keys_after = f.read()
        assert authorized_keys_after == authorized_keys_before
        results["enrollment_fingerprint_and_idempotence"] = True

        port = get_free_port()
        os.makedirs("/run/sshd", exist_ok=True)
        sshd_conf = os.path.join(temp_dir, "sshd_config")
        with open(sshd_conf, "w") as f:
            f.write(f"""Port {port}
ListenAddress 127.0.0.1
HostKey {HOST_PRIV_KEY}
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
UsePAM no
AuthorizedKeysFile .ssh/authorized_keys
StrictModes yes
PrintMotd no
""")

        sshd_err = open(os.path.join(temp_dir, "sshd.err"), "w+")
        sshd_proc = subprocess.Popen(
            ["/usr/sbin/sshd", "-D", "-e", "-f", sshd_conf],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=sshd_err,
        )
        if not wait_port(port, timeout=10.0):
            raise RuntimeError("sshd failed to bind port")

        known_hosts = os.path.join(temp_dir, "known_hosts")
        with open(HOST_KEY_PATH) as f:
            host_pub_body = f.read().strip()
        with open(known_hosts, "w") as f:
            f.write(f"[127.0.0.1]:{port} {host_pub_body}\n")

        base_ssh = [
            "ssh", "-F", "none", "-T", "-a", "-x",
            "-oBatchMode=yes", "-oIdentitiesOnly=yes", "-oIdentityAgent=none",
            "-oStrictHostKeyChecking=yes", f"-oUserKnownHostsFile={known_hosts}",
            "-oGlobalKnownHostsFile=none", "-oConnectTimeout=5",
            "-p", str(port),
        ]
        destination = f"{TEST_USER}@127.0.0.1"

        c1 = run(base_ssh + ["-i", client_key, destination, RPC_COMMAND], capture_output=True, text=True)
        c1_json = json.loads(c1.stdout.strip())
        results["normal_rpc"] = (c1.returncode == 0 and c1_json == EXPECTED_STUB)

        c2 = run(base_ssh + ["-i", client_key, destination, f"touch {MARKER}"], capture_output=True, text=True)
        c2_json = json.loads(c2.stdout.strip())
        results["forced_command_overrides"] = (
            c2.returncode == 0 and c2_json == EXPECTED_STUB and not os.path.lexists(MARKER)
        )

        c3 = run(base_ssh + ["-i", unauth_key, destination, RPC_COMMAND], capture_output=True, text=True)
        results["unauthorized_refused"] = (c3.returncode == 255)

        c4 = run(base_ssh + ["-i", client_key, "-W", f"127.0.0.1:{port}", destination], capture_output=True, text=True)
        err_low = c4.stderr.lower()
        results["port_forwarding_refused"] = (c4.returncode != 0 and ("prohibited" in err_low or "open failed" in err_low))

        all_pass = all(results.values())
        print(json.dumps({"results": results, "success": all_pass}, indent=2))
        return 0 if all_pass else 1
    finally:
        if sshd_proc is not None:
            try:
                sshd_proc.terminate()
                try:
                    sshd_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    sshd_proc.kill()
                    sshd_proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired) as exc:
                sys.stderr.write(f"Cleanup warning: failed to stop sshd: {exc}\n")
        if sshd_err is not None:
            try:
                sshd_err.close()
            except OSError as exc:
                sys.stderr.write(f"Cleanup warning: failed to close sshd error log: {exc}\n")
        try:
            shutil.rmtree(temp_dir)
        except OSError as exc:
            sys.stderr.write(f"Cleanup warning: failed to remove {temp_dir}: {exc}\n")
        for p in reversed(created_paths):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
            except OSError as exc:
                sys.stderr.write(f"Cleanup warning: failed to remove {p}: {exc}\n")
        if account_created:
            try:
                run(["userdel", "-r", TEST_USER], check=True)
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                sys.stderr.write(f"Cleanup warning: failed to delete {TEST_USER}: {exc}\n")
        if group_created:
            try:
                run(["groupdel", "wheel"], check=True)
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                sys.stderr.write(f"Cleanup warning: failed to delete wheel group: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
