#!/usr/bin/env python3
"""Tests for the USB network link and the SSH socket on it.

The helper runs as a real bash process against a temporary directory that
stands in for a configfs gadget, with a fake device-env; no gadget, network or
systemd state is touched. The unit files are checked for the properties the
design depends on. Standard library only; run with python3 -m unittest.
"""

import configparser
import os
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYSTEM = os.path.join(ROOT, "system_files")
HELPER = os.path.join(SYSTEM, "usr", "libexec", "armada", "usb-network")
UNITS = os.path.join(SYSTEM, "usr", "lib", "systemd", "system")
CONTROL = os.path.join(SYSTEM, "usr", "libexec", "armada", "armada-control")
DEVICES = os.path.join(SYSTEM, "usr", "lib", "armada", "devices")
NM_KEYFILE = os.path.join(
    SYSTEM, "etc", "NetworkManager", "system-connections", "armada-usb.nmconnection"
)


def unit(*parts):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    with open(os.path.join(UNITS, *parts), encoding="utf-8") as handle:
        parser.read_string(handle.read())
    return parser


def raw(*parts):
    with open(os.path.join(UNITS, *parts), encoding="utf-8") as handle:
        return handle.read()


class HelperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gadget = os.path.join(self.tmp.name, "armada-mtp")

        self.config = os.path.join(self.tmp.name, "etc", "usb-network.conf")
        # A fake systemctl on PATH logs every call; is-active answers from MTP_ACTIVE
        # (legacy compatibility) and show -p ActiveState --value answers separately
        # for armada-mtp.service and sshd.service from MTP_STATE / SSHD_STATE. The
        # fake also honours SYSTEM_STATE for is-system-running and MTP_QUERY_FAIL /
        # SSHD_QUERY_FAIL to make show exit nonzero.
        self.bin = os.path.join(self.tmp.name, "bin")
        os.makedirs(self.bin)
        self.calls = os.path.join(self.tmp.name, "systemctl.log")
        systemctl = os.path.join(self.bin, "systemctl")
        with open(systemctl, "w", encoding="utf-8") as handle:
            handle.write(
                "#!/bin/sh\n"
                f'echo "$*" >> "{self.calls}"\n'
                'case "$1" in\n'
                '  is-active) [ "$MTP_ACTIVE" = 1 ] && [ "$3" = armada-mtp.service ] ;;\n'
                '  is-system-running) printf "%s\\n" "${SYSTEM_STATE:-running}" ;;\n'
                '  show)\n'
                '    [ "$2 $3 $4" = "-p ActiveState --value" ] || exit 2\n'
                '    case "$5" in\n'
                '      armada-mtp.service) [ "$MTP_QUERY_FAIL" = 1 ] && exit 1\n'
                '        printf "%s\\n" "$MTP_STATE" ;;\n'
                '      sshd.service) [ "$SSHD_QUERY_FAIL" = 1 ] && exit 1\n'
                '        printf "%s\\n" "$SSHD_STATE" ;;\n'
                '      *) exit 1 ;;\n'
                '    esac ;;\n'
                '  reset-failed) [ "$RESET_FAILED_ERROR" != 1 ] ;;\n'
                'esac\n'
            )
        os.chmod(systemctl, 0o755)

    def run_helper(
        self,
        *args,
        usb_network="1",
        mtp_active=False,
        mtp_state=None,
        mtp_query_fail=False,
        sshd_state="inactive",
        sshd_query_fail=False,
        system_state="running",
        reset_failed_error=False,
    ):
        # mtp_active keeps legacy callers working; it selects the queried state unless
        # an explicit mtp_state is given.
        if mtp_state is None:
            mtp_state = "active" if mtp_active else "inactive"
        env_script = os.path.join(self.tmp.name, "device-env")
        with open(env_script, "w", encoding="utf-8") as handle:
            handle.write(f"#!/bin/sh\necho ARMADA_USB_NETWORK={usb_network}\n")
        os.chmod(env_script, 0o755)
        env = dict(
            os.environ,
            ARMADA_DEVICE_ENV=env_script,
            ARMADA_USB_NETWORK_CONFIG=self.config,
            ARMADA_MTP_GADGET=self.gadget,
            MTP_ACTIVE="1" if mtp_active else "0",
            MTP_STATE=mtp_state,
            MTP_QUERY_FAIL="1" if mtp_query_fail else "0",
            SSHD_STATE=sshd_state,
            SSHD_QUERY_FAIL="1" if sshd_query_fail else "0",
            SYSTEM_STATE=system_state,
            RESET_FAILED_ERROR="1" if reset_failed_error else "0",
            PATH=self.bin + os.pathsep + os.environ["PATH"],
        )
        return subprocess.run(
            ["bash", HELPER, *args], env=env, capture_output=True, text=True, check=False
        )

    def systemctl_calls(self):
        # Mutation calls only; drop read-only probes (is-*, show) so assertions are
        # unaffected by added state queries.
        if not os.path.exists(self.calls):
            return []
        with open(self.calls, encoding="utf-8") as handle:
            return [
                line
                for line in handle.read().splitlines()
                if not line.startswith("is-") and not line.startswith("show ")
            ]

    def write_switch(self, text):
        os.makedirs(os.path.dirname(self.config), exist_ok=True)
        with open(self.config, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_switch_is_on_without_a_config(self):
        self.assertEqual(self.run_helper("enabled").returncode, 0)
        self.assertEqual(self.run_helper("state").stdout, "enabled=1\nsupported=1\npaused=0\n")

    def test_switch_off_disables_the_link(self):
        self.write_switch("enabled=0\n")
        self.assertNotEqual(self.run_helper("enabled").returncode, 0)
        self.assertEqual(self.run_helper("state").stdout, "enabled=0\nsupported=1\npaused=0\n")
        self.write_switch("enabled=1\n")
        self.assertEqual(self.run_helper("enabled").returncode, 0)

    def test_state_reports_an_unsupported_profile(self):
        self.assertEqual(self.run_helper("state", usb_network="0").stdout, "enabled=1\nsupported=0\npaused=0\n")

    def test_set_off_saves_and_stops_the_link(self):
        self.assertEqual(self.run_helper("set", "off").returncode, 0)
        with open(self.config, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "enabled=0\n")
        self.assertEqual(
            self.systemctl_calls(), ["stop armada-usb-ssh.socket armada-usb-network.service"]
        )

    def test_set_on_saves_and_starts_the_link(self):
        self.write_switch("enabled=0\n")
        self.assertEqual(self.run_helper("set", "on").returncode, 0)
        with open(self.config, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "enabled=1\n")
        self.assertEqual(
            self.systemctl_calls(),
            [
                "reset-failed armada-usb-network.service",
                "start armada-usb-network.service",
            ],
        )

    def test_set_is_rejected_while_mtp_runs_or_exists(self):
        for state in ("active", "activating", "reloading", "deactivating"):
            for switch in ("on", "off"):
                with self.subTest(state=state, switch=switch):
                    self.write_switch("enabled=1\n")
                    if os.path.exists(self.calls):
                        os.remove(self.calls)
                    proc = self.run_helper("set", switch, mtp_state=state)
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertEqual(self.systemctl_calls(), [])
                    with open(self.config, encoding="utf-8") as handle:
                        self.assertEqual(handle.read(), "enabled=1\n")
        os.makedirs(os.path.join(self.gadget, "functions"))
        for switch in ("on", "off"):
            with self.subTest(gadget_present=True, switch=switch):
                self.write_switch("enabled=0\n")
                if os.path.exists(self.calls):
                    os.remove(self.calls)
                proc = self.run_helper("set", switch, mtp_state="inactive")
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(self.systemctl_calls(), [])
                with open(self.config, encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), "enabled=0\n")

    def test_set_switch_fails_when_mtp_state_unreadable(self):
        proc = self.run_helper("set", "on", mtp_query_fail=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.systemctl_calls(), [])

    def test_set_switch_rejects_unknown_or_empty_mtp_state(self):
        for state in ("unrecognized", ""):
            with self.subTest(state=state):
                proc = self.run_helper("set", "on", mtp_state=state)
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(self.systemctl_calls(), [])

    def test_resume_resets_failed_then_starts_the_link(self):
        self.assertEqual(self.run_helper("resume").returncode, 0)
        self.assertEqual(
            self.systemctl_calls(),
            [
                "reset-failed armada-usb-network.service",
                "--no-block start armada-usb-network.service",
            ],
        )
        if os.path.exists(self.calls):
            os.remove(self.calls)
        self.write_switch("enabled=0\n")
        self.assertEqual(self.run_helper("resume").returncode, 0)
        self.assertEqual(self.systemctl_calls(), [])

    def test_ssh_resume_only_queues_the_deferred_unit(self):
        self.assertEqual(self.run_helper("ssh-resume").returncode, 0)
        self.assertEqual(
            self.systemctl_calls(), ["--no-block start armada-usb-ssh-resume.service"]
        )
        if os.path.exists(self.calls):
            os.remove(self.calls)
        self.write_switch("enabled=0\n")
        self.assertEqual(self.run_helper("ssh-resume").returncode, 0)
        self.assertEqual(self.systemctl_calls(), [])
        # Unsupported profile: exercise the profile guard independently of the switch.
        self.write_switch("enabled=1\n")
        if os.path.exists(self.calls):
            os.remove(self.calls)
        self.assertEqual(self.run_helper("ssh-resume", usb_network="0").returncode, 0)
        self.assertEqual(self.systemctl_calls(), [])

    def test_unloaded_link_unit_does_not_block_resume_or_manual_start(self):
        for args in (("resume",), ("set", "on")):
            with self.subTest(args=args):
                if os.path.exists(self.calls):
                    os.remove(self.calls)
                result = self.run_helper(*args, reset_failed_error=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.systemctl_calls()[0], "reset-failed armada-usb-network.service")
                self.assertTrue(self.systemctl_calls()[1].endswith("start armada-usb-network.service"))

    def test_ssh_start_starts_socket_only_when_sshd_inactive_or_failed(self):
        for state, starts in (
            ("inactive", True),
            ("failed", True),
            ("active", False),
            ("activating", False),
            ("deactivating", False),
            ("reloading", False),
        ):
            with self.subTest(state):
                if os.path.exists(self.calls):
                    os.remove(self.calls)
                self.assertEqual(
                    self.run_helper("ssh-start", sshd_state=state).returncode, 0
                )
                calls = self.systemctl_calls()
                if starts:
                    self.assertIn("--no-block start armada-usb-ssh.socket", calls)
                else:
                    self.assertNotIn("--no-block start armada-usb-ssh.socket", calls)
                    self.assertEqual(calls, [])

    def test_ssh_start_skips_when_disabled(self):
        self.write_switch("enabled=0\n")
        self.assertEqual(self.run_helper("ssh-start").returncode, 0)
        self.assertEqual(self.systemctl_calls(), [])

    def test_ssh_start_skips_when_unsupported(self):
        # Switch is on: this exercises the profile guard, not the switch guard.
        self.assertEqual(self.run_helper("ssh-start", usb_network="0").returncode, 0)
        self.assertEqual(self.systemctl_calls(), [])

    def test_ssh_start_fails_when_sshd_state_query_fails(self):
        self.assertNotEqual(
            self.run_helper("ssh-start", sshd_query_fail=True).returncode, 0
        )
        self.assertEqual(self.systemctl_calls(), [])

    def test_ssh_start_fails_when_sshd_state_empty(self):
        self.assertNotEqual(self.run_helper("ssh-start", sshd_state="").returncode, 0)
        self.assertEqual(self.systemctl_calls(), [])

    def test_set_on_starts_nothing_where_the_profile_forbids_it(self):
        self.assertEqual(self.run_helper("set", "on", usb_network="0").returncode, 0)
        self.assertEqual(self.systemctl_calls(), [])

    def test_resume_skips_when_shutting_down(self):
        self.assertEqual(self.run_helper("resume", system_state="stopping").returncode, 0)
        self.assertEqual(self.systemctl_calls(), [])

    def test_ssh_resume_skips_when_shutting_down(self):
        self.assertEqual(
            self.run_helper("ssh-resume", system_state="stopping").returncode, 0
        )
        self.assertEqual(self.systemctl_calls(), [])

    def test_ssh_start_skips_when_shutting_down(self):
        self.assertEqual(
            self.run_helper("ssh-start", system_state="stopping").returncode, 0
        )
        self.assertEqual(self.systemctl_calls(), [])

    def test_set_rejects_other_values(self):
        self.assertEqual(self.run_helper("set", "maybe").returncode, 2)
        self.assertFalse(os.path.exists(self.config))

    def test_enabled_follows_the_device_profile(self):
        self.assertEqual(self.run_helper("enabled", usb_network="1").returncode, 0)
        self.assertNotEqual(self.run_helper("enabled", usb_network="").returncode, 0)
        self.assertNotEqual(self.run_helper("enabled", usb_network="0").returncode, 0)

    def test_runtime_pauses_while_the_mtp_gadget_exists(self):
        os.makedirs(os.path.join(self.gadget, "functions"))
        self.assertEqual(
            self.run_helper("state", mtp_active=True).stdout,
            "enabled=0\nsupported=1\npaused=1\n",
        )
        self.assertNotEqual(self.run_helper("enabled", mtp_active=True).returncode, 0)
        for command in ("resume", "ssh-resume", "ssh-start"):
            with self.subTest(command=command):
                if os.path.exists(self.calls):
                    os.remove(self.calls)
                proc = self.run_helper(command, mtp_active=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(self.systemctl_calls(), [])

    def test_resume_after_mtp_uses_the_persisted_preference(self):
        cases = [
            ("explicit enabled=1", "enabled=1\n", False),
            ("explicit enabled=0", "enabled=0\n", True),
            ("missing preference (default on)", None, False),
        ]
        start_calls = [
            "reset-failed armada-usb-network.service",
            "--no-block start armada-usb-network.service",
        ]
        for label, config_text, explicitly_off in cases:
            with self.subTest(label=label):
                if config_text is None:
                    if os.path.exists(self.config):
                        os.remove(self.config)
                else:
                    self.write_switch(config_text)
                os.makedirs(self.gadget, exist_ok=True)
                self.assertEqual(self.run_helper("resume").returncode, 0)
                self.assertEqual(self.systemctl_calls(), [])
                os.rmdir(self.gadget)
                expected_state = (
                    "enabled=0\nsupported=1\npaused=0\n"
                    if explicitly_off
                    else "enabled=1\nsupported=1\npaused=0\n"
                )
                self.assertEqual(self.run_helper("state").stdout, expected_state)
                self.assertEqual(
                    self.run_helper("resume", mtp_state="deactivating").returncode, 0
                )
                self.assertEqual(
                    self.systemctl_calls(), [] if explicitly_off else start_calls
                )
                if os.path.exists(self.calls):
                    os.remove(self.calls)

    def test_unknown_command(self):
        self.assertEqual(self.run_helper("bogus").returncode, 2)


class UnitTest(unittest.TestCase):
    def test_link_runs_only_where_enabled_and_not_beside_mtp(self):
        service = raw("armada-usb-network.service")
        self.assertIn("ExecCondition=/usr/libexec/armada/usb-network enabled", service)
        self.assertIn(
            "ExecCondition=/usr/bin/test ! -e /sys/kernel/config/usb_gadget/armada-mtp",
            service,
        )
        self.assertIn("ExecStopPost=/usr/libexec/armada/usb-network stop", service)
        self.assertNotIn("ExecStop=", service)
        self.assertNotIn("is-active", service)

    def test_gadget_presence_condition_matches_mtp_lifecycle(self):
        service = raw("armada-usb-network.service")
        conditions = [
            line[len("ExecCondition="):]
            for line in service.splitlines()
            if line.startswith("ExecCondition=")
        ]
        self.assertEqual(len(conditions), 2)
        parsed = conditions[1].split()
        self.assertEqual(parsed[0], "/usr/bin/test")
        self.assertEqual(parsed[1], "!")
        self.assertEqual(parsed[2], "-e")
        original_path = parsed[3]
        self.assertEqual(original_path, "/sys/kernel/config/usb_gadget/armada-mtp")

        # Substitute the final path with a temporary gadget path and run the
        # actual parsed command: passes while absent, fails once MTP's setup
        # has created the gadget (i.e. during MTP activation).
        with tempfile.TemporaryDirectory() as tmp:
            gadget = os.path.join(tmp, "armada-mtp")
            command = parsed[:3] + [gadget]
            absent = subprocess.run(command, capture_output=True, check=False)
            self.assertEqual(absent.returncode, 0)
            os.makedirs(gadget)
            present = subprocess.run(command, capture_output=True, check=False)
            self.assertNotEqual(present.returncode, 0)

    def test_ssh_socket_is_usb_only_and_steps_aside_for_sshd(self):
        socket = unit("armada-usb-ssh.socket")
        self.assertEqual(socket["Unit"]["Conflicts"], "sshd.service")
        self.assertEqual(socket["Unit"]["Before"], "sshd.service")
        self.assertEqual(socket["Socket"]["ListenStream"], "172.16.42.1:22")
        self.assertEqual(socket["Socket"]["BindToDevice"], "usb0")
        self.assertEqual(socket["Socket"]["FreeBind"], "yes")
        self.assertEqual(socket["Socket"]["Accept"], "yes")
        self.assertEqual(socket["Socket"]["IPAddressAllow"], "172.16.42.0/24")
        self.assertEqual(socket["Socket"]["IPAddressDeny"], "any")
        # No [Install]: only the link (or sshd stopping) starts it.
        self.assertNotIn("Install", socket)

    def test_ssh_resume_service_waits_for_sshd_and_runs_ssh_start(self):
        resume = unit("armada-usb-ssh-resume.service")
        self.assertEqual(resume["Unit"]["After"], "sshd.service")
        self.assertEqual(resume["Service"]["Type"], "oneshot")
        self.assertNotIn("RemainAfterExit", resume["Service"])
        self.assertEqual(
            resume["Service"]["ExecStart"],
            "/usr/libexec/armada/usb-network ssh-start",
        )
        self.assertNotIn("Install", resume)

    def test_udc_template_retries_resume_and_stops_stale_gadget(self):
        udc = unit("armada-usb-udc@.service")
        self.assertEqual(udc["Unit"]["BindsTo"], "%i.device")
        self.assertEqual(udc["Unit"]["After"], "%i.device")
        self.assertEqual(udc["Service"]["Type"], "oneshot")
        self.assertEqual(udc["Service"]["RemainAfterExit"], "yes")
        self.assertEqual(
            udc["Service"]["ExecStart"], "/usr/libexec/armada/usb-network resume"
        )
        self.assertEqual(
            udc["Service"]["ExecStop"],
            "-/usr/bin/systemctl --no-block stop armada-usb-network.service",
        )
        self.assertNotIn("Install", udc)

    def test_ssh_session_is_key_only(self):
        session = unit("armada-usb-ssh@.service")["Service"]["ExecStart"]
        self.assertIn("sshd -i", session)
        self.assertIn("PasswordAuthentication=no", session)
        self.assertIn("KbdInteractiveAuthentication=no", session)
        self.assertIn("PermitRootLogin=no", session)

    def test_mtp_drop_in_hooks_are_in_the_right_lists(self):
        text = raw("armada-mtp.service.d", "50-usb-network.conf")
        lines = [line for line in text.splitlines() if line and not line.startswith("#")]
        start_pre = [line for line in lines if line.startswith("ExecStartPre=")]
        self.assertEqual(
            start_pre,
            ["ExecStartPre=+/usr/bin/systemctl stop armada-usb-ssh.socket armada-usb-network.service"],
        )
        self.assertEqual([l for l in lines if l.startswith("ExecStop=")], [])
        stop_post = [l[len("ExecStopPost="):] for l in lines if l.startswith("ExecStopPost=")]
        self.assertEqual(
            stop_post,
            [
                "",
                "+/usr/libexec/armada/mtp-gadget unbind",
                "+/usr/libexec/armada/usb-network detach /sys/kernel/config/usb_gadget/armada-mtp",
                "+/usr/libexec/armada/mtp-gadget teardown",
                "+/usr/libexec/armada/usb-network resume",
            ],
        )

    def test_failed_start_detach_removes_function_and_allows_teardown(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        gadget = os.path.join(tmp.name, "armada-mtp")
        config_dir = os.path.join(gadget, "configs", "c.1")
        os.makedirs(config_dir)
        os.makedirs(os.path.join(gadget, "functions"))
        env_script = os.path.join(tmp.name, "device-env")
        with open(env_script, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\necho ARMADA_USB_NETWORK=1\n")
        os.chmod(env_script, 0o755)
        env = dict(os.environ, ARMADA_DEVICE_ENV=env_script, ARMADA_USB_NETWORK_CONFIG=os.path.join(tmp.name, "usb-network.conf"))

        function = os.path.join(gadget, "functions", "ncm.usb0")
        link = os.path.join(config_dir, "ncm.usb0")
        os.makedirs(function)
        os.symlink(function, link)

        text = raw("armada-mtp.service.d", "50-usb-network.conf")
        lines = [line for line in text.splitlines() if line and not line.startswith("#")]
        detach_lines = [
            l[len("ExecStopPost=+"):].split()
            for l in lines
            if l.startswith("ExecStopPost=+") and " detach " in l
        ]
        self.assertEqual(len(detach_lines), 1)
        parsed_args = detach_lines[0]
        self.assertEqual(parsed_args[0], "/usr/libexec/armada/usb-network")
        self.assertEqual(parsed_args[1], "detach")
        detach = subprocess.run(
            ["bash", HELPER, parsed_args[1], gadget],
            env=env, capture_output=True, text=True, check=False
        )
        self.assertEqual(detach.returncode, 0, detach.stderr)
        self.assertFalse(os.path.lexists(link))
        self.assertFalse(os.path.exists(function))
        # Parent directories can now be removed, as the teardown step requires.
        os.rmdir(config_dir)
        os.rmdir(os.path.join(gadget, "configs"))
        os.rmdir(os.path.join(gadget, "functions"))
        os.rmdir(gadget)

    def test_sshd_drop_in_restores_the_socket(self):
        text = raw("sshd.service.d", "50-armada-usb-ssh.conf")
        self.assertIn("ExecStopPost=-/usr/libexec/armada/usb-network ssh-resume", text)

    def test_keyfile_matches_only_gadget_interfaces(self):
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        parser.read(NM_KEYFILE, encoding="utf-8")
        self.assertEqual(parser["connection"]["interface-name"], "usb0")
        self.assertEqual(
            parser["match"]["driver"],
            "configfs-gadget.armada-usb-network;configfs-gadget.armada-mtp;",
        )
        self.assertEqual(parser["ipv4"]["method"], "shared")
        self.assertEqual(parser["ipv4"]["address1"], "172.16.42.1/24")


    def test_udev_rule_hands_only_armada_gadgets_to_networkmanager(self):
        rule = os.path.join(ROOT, "system_files", "usr", "lib", "udev", "rules.d", "86-armada-usb-network.rules")
        with open(rule, encoding="utf-8") as handle:
            lines = [l for l in handle.read().splitlines() if l and not l.startswith("#")]
        self.assertEqual(len(lines), 3)
        nm = [l for l in lines if "NM_UNMANAGED" in l]
        self.assertEqual(len(nm), 1)
        self.assertIn('SUBSYSTEM=="net"', nm[0])
        self.assertIn('ENV{DEVTYPE}=="gadget"', nm[0])
        self.assertIn('DRIVERS=="configfs-gadget.armada-usb-network|configfs-gadget.armada-mtp"', nm[0])
        self.assertIn('ENV{NM_UNMANAGED}="0"', nm[0])
        driver = 'DRIVERS=="configfs-gadget.armada-usb-network|configfs-gadget.armada-mtp"'
        net_add = [
            l for l in lines if 'ENV{SYSTEMD_WANTS}+="armada-usb-ssh-resume.service"' in l
        ]
        self.assertEqual(len(net_add), 1)
        self.assertIn('SUBSYSTEM=="net"', net_add[0])
        self.assertIn('KERNEL=="usb0"', net_add[0])
        self.assertIn('ACTION=="add"', net_add[0])
        self.assertIn('ENV{DEVTYPE}=="gadget"', net_add[0])
        self.assertIn(driver, net_add[0])
        self.assertIn('TAG+="systemd"', net_add[0])
        udc = [l for l in lines if 'ENV{SYSTEMD_WANTS}+="armada-usb-udc@.service"' in l]
        self.assertEqual(len(udc), 1)
        self.assertIn('ACTION=="add"', udc[0])
        self.assertIn('SUBSYSTEM=="udc"', udc[0])
        self.assertIn('TAG+="systemd"', udc[0])


class ProfileTest(unittest.TestCase):
    def test_the_link_is_on_by_default_and_no_profile_turns_it_off(self):
        with open(os.path.join(DEVICES, "defaults.conf"), encoding="utf-8") as handle:
            self.assertIn("\nARMADA_USB_NETWORK=1\n", handle.read())
        for name in sorted(os.listdir(DEVICES)):
            if not name.endswith(".conf") or name == "defaults.conf":
                continue
            with open(os.path.join(DEVICES, name), encoding="utf-8") as handle:
                self.assertNotIn("ARMADA_USB_NETWORK=", handle.read(), name)


class ControlTest(unittest.TestCase):
    def test_freedom_control_can_read_and_set_the_switch(self):
        with open(CONTROL, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn('"get_usb_network_enabled": action_get_usb_network_enabled', text)
        self.assertIn('"set_usb_network_enabled": action_set_usb_network_enabled', text)
        self.assertIn('[USB_NETWORK, "set", "on" if enabled else "off"]', text)


if __name__ == "__main__":
    unittest.main()
