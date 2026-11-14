"""Compatibility adapter for the legacy Steam system-update command."""

import re
import sys

_COMMIT = re.compile(r"[0-9a-f]{64}\Z")
_DEPLOYMENT = re.compile(
    r"(?:[A-Za-z0-9][A-Za-z0-9_.-]{0,63})/([0-9a-f]{64})\.(0|[1-9][0-9]{0,9})\Z"
)


def _fail(message):
    text = str(message).replace("\n", " ").replace("\r", " ")[:300]
    print("armada-update: %s" % text, file=sys.stderr)
    return 7


def _awaiting_reboot(result):
    return result.get("state") == "awaiting_reboot" and result.get("requiresReboot") is True


def main(argv=None, client=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if "--supports-duplicate-detection" in args:
        return 0
    args = [arg for arg in args if arg != "--enable-duplicate-detection"]
    if args not in ([], ["check"]):
        return _fail("invalid arguments")
    if client is None:
        from .client import UpdateClient
        client = UpdateClient()
    try:
        if args == ["check"]:
            result = client.run("system", "check", timeout=120)
            state = result.get("state")
            if state == "update_available":
                commit = result.get("targetCommit")
                if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
                    return _fail("update check returned an invalid target commit")
                print(commit)
                return 0
            if _awaiting_reboot(result):
                trial = result.get("trial")
                deployment = trial.get("newDeployment") if isinstance(trial, dict) else None
                match = _DEPLOYMENT.fullmatch(deployment) if isinstance(deployment, str) else None
                if match is None or int(match.group(2)) > 4294967295:
                    return _fail("awaiting reboot without a canonical deployment")
                print(match.group(1))
                return 0
            if state == "up_to_date":
                return 7
            return _fail("check state %s: %s" % (state, result.get("error", "no update available")))

        status = client.run("system", "status", timeout=120)
        if _awaiting_reboot(status):
            print("100%", flush=True)
            return 0
        if status.get("state") not in ("installed", "up_to_date"):
            return _fail("cannot apply from state %s: %s" % (
                status.get("state"), status.get("error", "update is not ready")))
        progress = {"started": False, "phase": None}

        def on_progress(record):
            phase = str(record.get("phase", "unknown"))
            if phase != progress["phase"]:
                sanitized = phase.replace("\n", " ").replace("\r", " ")[:120]
                print("CHANGED %s" % sanitized, file=sys.stderr, flush=True)
                progress["phase"] = phase
            if not progress["started"]:
                print("0%", flush=True)
                progress["started"] = True

        result = client.run("system", "update", on_progress=on_progress)
        complete = result.get("state") == "up_to_date"
        if _awaiting_reboot(result):
            complete = (result.get("bootWriteState") == "written"
                        and result.get("bootPolicy") == "dual-boot-no-slot-control"
                        and result.get("slotControlState") == "not_issued")
        if complete:
            print("100%", flush=True)
            return 0
        return _fail("update ended in state %s phase %s: %s" % (
            result.get("state"), result.get("phase"), result.get("error", "incomplete")))
    except Exception as exc:
        return _fail(exc)


if __name__ == "__main__":
    raise SystemExit(main())
