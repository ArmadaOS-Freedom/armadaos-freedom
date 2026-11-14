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
HELPER = "/usr/libexec/armada/armada-offline-update"
PYTHON = "/usr/bin/python3"


def run(args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True)


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
    try:
        grp.getgrnam("wheel")
        made_group = False
    except KeyError:
        made_group = True
    made_user = made_policy = False
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

        def query(name, runas, args):
            command = [tools["runuser"], "-u", USER, "--", tools["sudo"], "-n", "-l", "-u", runas, "--"] + args
            result = run(command, check=False)
            authorized = result.returncode == 0
            wanted = expected == "allow" if name == "exact-root" else False
            cases.append({"case": name, "runas": runas, "returncode": result.returncode, "ok": authorized == wanted})

        exact = [PYTHON, "-I", HELPER]
        query("exact-root", "root", exact)
        query("python-c", "root", [PYTHON, "-c", "print(1)"])
        query("without-isolated", "root", [PYTHON, HELPER])
        query("alternative-helper", "root", [PYTHON, "-I", HELPER + "-other"])
        query("worker-execute", "root", exact + ["--worker", "execute", "a" * 32])
        query("exact-nobody", "nobody", exact)
        print(json.dumps({"cases": cases}, separators=(",", ":")))
        if not all(case["ok"] for case in cases):
            raise RuntimeError("sudo authorization did not match expectations")
    finally:
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
