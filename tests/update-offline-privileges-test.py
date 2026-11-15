#!/usr/bin/env python3
import grp
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys

USER = "armada-offline-smoke"
POLICY = Path("/etc/sudoers.d/armada-offline-u342")
HELPER = Path("/usr/libexec/armada/armada-offline-update")
PYTHON = "/usr/bin/python3"
# Harmless stand-in for the shipped helper: it ignores its arguments, prints a
# constant and exits 0, so a wrongly authorized run is detected by its status.
STUB = "print('armada-offline-stub')\n"


def run(args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True)


def make_stub(path, created):
    """Create an owned stub at a fixed path, recording only what it created."""
    missing = [item for item in [path.parent, *path.parent.parents]
               if not os.path.lexists(item)]
    for parent in reversed(missing):
        parent.mkdir()
        os.chmod(parent, 0o755)
        created.append(parent)
    with path.open("x") as stream:
        stream.write(STUB)
    os.chmod(path, 0o644)
    created.append(path)


def main():
    if not Path("/.dockerenv").exists() or os.geteuid() != 0 or os.environ.get("ARMADA_DISPOSABLE_PRIVILEGE_TEST") != "1":
        raise RuntimeError("refusing to run outside the authorized disposable root container")
    if len(sys.argv) != 3 or sys.argv[2] not in ("deny", "allow"):
        raise RuntimeError("usage: validator.py SOURCE_SUDOERS_SCRIPT deny|allow")
    expected = sys.argv[2]
    tools = {name: shutil.which(name) for name in ("sudo", "visudo", "runuser", "useradd", "userdel", "groupadd", "groupdel")}
    if not all(tools.values()):
        raise RuntimeError("required account and sudo tools are unavailable")
    source = Path(sys.argv[1]).read_text()
    matches = re.findall(r"(?m)^cat > /etc/sudoers\.d/armada-user <<'EOF'\n(.*?)^EOF$", source, re.S)
    if len(matches) != 1:
        raise RuntimeError("could not uniquely extract the armada-user sudoers heredoc")
    policy = matches[0]
    try:
        pwd.getpwnam(USER)
        raise RuntimeError("fixed test user already exists")
    except KeyError:
        pass
    if POLICY.exists():
        raise RuntimeError("owned sudoers test path already exists")
    stubs = [HELPER, Path(str(HELPER) + "-other")]
    if any(os.path.lexists(path) for path in stubs):
        raise RuntimeError("owned stub test path already exists")
    try:
        grp.getgrnam("wheel")
        made_group = False
    except KeyError:
        made_group = True
    made_user = made_policy = False
    created = []
    cases = []
    try:
        if made_group:
            run([tools["groupadd"], "wheel"])
        run([tools["useradd"], "-M", "-g", "wheel", USER])
        made_user = True
        account = pwd.getpwnam(USER)
        wheel = grp.getgrnam("wheel")
        if account.pw_gid != wheel.gr_gid or os.getgrouplist(USER, account.pw_gid) != [wheel.gr_gid]:
            raise RuntimeError("test user does not have exactly wheel as its primary group")
        with POLICY.open("x") as stream:
            stream.write(policy)
        os.chmod(POLICY, 0o440)
        made_policy = True
        run([tools["visudo"], "-cf", str(POLICY)])
        for stub in stubs:
            make_stub(stub, created)

        def query(name, runas, args):
            # sudo -l only reports that every command may be listed: the default
            # Fedora %wheel policy carries another NOPASSWD rule, so a successful
            # listing says nothing about running this command. Execute the real
            # command instead, with -k discarding any cached credential, and take
            # the exit status as the authorization result.
            command = [tools["runuser"], "-u", USER, "--", tools["sudo"],
                       "-n", "-k", "-u", runas, "--"] + args
            result = run(command, check=False)
            authorized = result.returncode == 0
            wanted = expected == "allow" if name == "exact-root" else False
            cases.append({"case": name, "runas": runas, "returncode": result.returncode, "ok": authorized == wanted})

        exact = [PYTHON, "-I", str(HELPER)]
        query("exact-root", "root", exact)
        query("python-c", "root", [PYTHON, "-c", "print(1)"])
        query("without-isolated", "root", [PYTHON, str(HELPER)])
        query("alternative-helper", "root", [PYTHON, "-I", str(HELPER) + "-other"])
        query("worker-execute", "root", exact + ["--worker", "execute", "a" * 32])
        query("exact-nobody", "nobody", exact)
        print(json.dumps({"cases": cases}, separators=(",", ":")))
        if not all(case["ok"] for case in cases):
            raise RuntimeError("sudo authorization did not match expectations")
    finally:
        for path in reversed(created):
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
        if made_policy:
            POLICY.unlink()
        if made_user:
            run([tools["userdel"], USER])
        if made_group:
            run([tools["groupdel"], "wheel"])


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, separators=(",", ":")), file=sys.stderr)
        sys.exit(1)
