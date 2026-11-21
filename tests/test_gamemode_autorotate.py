"""armada-gamemode-autorotate: sensor orientation to gamescope rotation."""

import importlib.machinery
import os
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SYSTEM = REPO / 'system_files'
SCRIPT = SYSTEM / 'usr/libexec/armada/armada-gamemode-autorotate'
UNIT = SYSTEM / 'usr/lib/systemd/user/armada-gamemode-autorotate.service'
DROPIN = SYSTEM / ('usr/lib/systemd/user/gamescope-session-plus@steam.service.d/'
                   '45-armada-gamemode-autorotate.conf')
PATCH = REPO / 'packages/gamescope/patches/0048-wlserver-add-rotate-display-command.patch'


def load():
    loader = importlib.machinery.SourceFileLoader('gamemode_autorotate', str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


autorotate = load()


class FakeSensor:
    def __init__(self, orientation='normal', has=True):
        self.props = {'HasAccelerometer': has, 'AccelerometerOrientation': orientation,
                      'AccelerometerTilt': 'vertical'}
        self.claims = 0
        self.releases = 0
        self.fail = None

    def properties(self):
        if self.fail:
            raise RuntimeError(self.fail)
        return dict(self.props)

    def claim(self):
        self.claims += 1

    def release(self):
        self.releases += 1


class AutoRotateTest(unittest.TestCase):
    def make(self, sensor, setting='auto', fail_rotate=0, default='normal'):
        if not callable(setting):
            stored = setting
            setting = lambda: stored
        calls = []
        state = {'fail': fail_rotate}

        def rotate(target):
            if state['fail']:
                state['fail'] -= 1
                raise RuntimeError('gamescope not ready')
            calls.append(target)

        rotator = autorotate.AutoRotate(sensor, rotate=rotate, setting=setting,
                                        default=default)
        return rotator, calls

    def test_mapping_follows_kwin(self):
        self.assertEqual(autorotate.ORIENTATIONS, {
            'normal': 'normal', 'left-up': 'left',
            'right-up': 'right', 'bottom-up': 'upsidedown'})

    def test_claims_once_and_rotates_on_change(self):
        sensor = FakeSensor('normal')
        rotator, calls = self.make(sensor)
        rotator.refresh()
        rotator.refresh()
        sensor.props['AccelerometerOrientation'] = 'left-up'
        rotator.refresh()
        sensor.props['AccelerometerOrientation'] = 'bottom-up'
        rotator.refresh()
        sensor.props['AccelerometerOrientation'] = 'right-up'
        rotator.refresh()
        self.assertEqual(calls, ['normal', 'left', 'upsidedown', 'right'])
        self.assertEqual(sensor.claims, 1)

    def test_undefined_and_missing_sensor_do_nothing(self):
        sensor = FakeSensor('undefined')
        rotator, calls = self.make(sensor)
        rotator.refresh()
        self.assertEqual(calls, [])
        absent = FakeSensor('normal', has=False)
        rotator, calls = self.make(absent)
        rotator.refresh()
        self.assertEqual((calls, absent.claims), ([], 0))

    def test_not_selected_never_claims(self):
        for setting in ('', 'normal', 'left', 'right', 'upsidedown'):
            sensor = FakeSensor('left-up')
            rotator, calls = self.make(sensor, setting=setting)
            rotator.refresh()
            self.assertEqual((calls, sensor.claims), ([], 0), setting)

    def test_leaving_auto_restores_choice_or_default(self):
        sensor = FakeSensor('left-up')
        state = {'setting': 'auto'}
        rotator, calls = self.make(sensor, setting=lambda: state['setting'], default='right')
        rotator.refresh()
        self.assertEqual((calls, sensor.claims), (['left'], 1))
        # A fixed choice: release the sensor and show the choice at once.
        state['setting'] = 'upsidedown'
        rotator.refresh()
        self.assertEqual((calls, sensor.releases, rotator.claimed),
                         (['left', 'upsidedown'], 1, False))
        rotator.refresh()
        self.assertEqual(calls, ['left', 'upsidedown'])
        # Back to auto, then no override: the device default.
        state['setting'] = 'auto'
        rotator.refresh()
        self.assertEqual((calls[-1], sensor.claims), ('left', 2))
        state['setting'] = ''
        rotator.refresh()
        self.assertEqual(calls[-1], 'right')

    def test_leaving_auto_already_in_place_or_never_rotated(self):
        sensor = FakeSensor('normal')
        state = {'setting': 'auto'}
        rotator, calls = self.make(sensor, setting=lambda: state['setting'])
        rotator.refresh()
        state['setting'] = ''
        rotator.refresh()
        self.assertEqual(calls, ['normal'])
        # Not selected from the start: the display is left alone.
        sensor = FakeSensor('left-up')
        rotator, calls = self.make(sensor, setting='left')
        rotator.refresh()
        self.assertEqual(calls, [])

    def test_failed_restore_is_retried(self):
        sensor = FakeSensor('left-up')
        state = {'setting': 'auto'}
        rotator, calls = self.make(sensor, setting=lambda: state['setting'])
        rotator.refresh()
        rotator.rotate = lambda target: (_ for _ in ()).throw(RuntimeError('gamescope busy'))
        state['setting'] = ''
        rotator.refresh()
        self.assertEqual(rotator.applied, 'left')
        rotator.rotate = calls.append
        rotator.refresh()
        self.assertEqual((calls, rotator.applied), (['left', 'normal'], None))

    def test_restore_does_not_need_sensorproxy(self):
        sensor = FakeSensor('left-up')
        state = {'setting': 'auto'}
        rotator, calls = self.make(sensor, setting=lambda: state['setting'])
        rotator.refresh()
        rotator.owner_changed(None)
        state['setting'] = 'normal'
        rotator.refresh()
        self.assertEqual(calls, ['left', 'normal'])

    def test_flat_keeps_orientation_until_lifted(self):
        sensor = FakeSensor('left-up')
        sensor.props['AccelerometerTilt'] = 'face-up'
        rotator, calls = self.make(sensor)
        rotator.refresh()
        sensor.props['AccelerometerTilt'] = 'face-down'
        rotator.refresh()
        self.assertEqual(calls, [])
        # Lifted with the same orientation: only the tilt changed.
        sensor.props['AccelerometerTilt'] = 'tilted-up'
        rotator.refresh()
        self.assertEqual(calls, ['left'])

    def test_failed_rotation_is_retried(self):
        sensor = FakeSensor('left-up')
        rotator, calls = self.make(sensor, fail_rotate=1)
        rotator.refresh()
        self.assertEqual(calls, [])
        rotator.refresh()
        self.assertEqual(calls, ['left'])

    def test_sensor_errors_are_tolerated(self):
        sensor = FakeSensor('left-up')
        sensor.fail = 'SensorProxy restarting'
        rotator, calls = self.make(sensor)
        self.assertTrue(rotator.refresh())
        sensor.fail = None
        rotator.refresh()
        self.assertEqual(calls, ['left'])

    def test_no_calls_while_sensorproxy_is_gone(self):
        sensor = FakeSensor('normal')
        rotator, calls = self.make(sensor)
        rotator.owner_changed(None)
        sensor.fail = 'must not be called'
        rotator.refresh()
        self.assertEqual((calls, rotator.last_error), ([], None))
        sensor.fail = None
        sensor.props['AccelerometerOrientation'] = 'right-up'
        rotator.owner_changed(':1.5')
        self.assertEqual(calls, ['right'])

    def test_owner_change_reclaims(self):
        sensor = FakeSensor('normal')
        rotator, _ = self.make(sensor)
        rotator.refresh()
        rotator.owner_changed(None)
        rotator.owner_changed(':1.99')
        self.assertEqual(sensor.claims, 2)
        rotator.close()
        self.assertEqual(sensor.releases, 1)

    def test_setting_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'display-orientation.conf'
            self.assertEqual(autorotate.orientation_setting(path), '')
            for text, expected in (('orientation=left\n', 'left'),
                                   ('orientation=auto\n', 'auto'),
                                   ('orientation=\n', ''),
                                   ('orientation=auto\norientation=bogus\n', ''),
                                   ('orientation=bogus\norientation=normal\n', 'normal')):
                path.write_text(text)
                self.assertEqual(autorotate.orientation_setting(path), expected, text)

    def test_gamescopectl_command(self):
        seen = {}

        def runner(args, **kwargs):
            seen['args'] = args
            seen['env'] = kwargs['env']
            seen['timeout'] = kwargs['timeout']

        autorotate.gamescope_rotate('left', runner=runner)
        self.assertEqual(seen['args'], ['/usr/bin/gamescopectl', 'rotate_display', 'left'])
        self.assertIn('GAMESCOPE_WAYLAND_DISPLAY', seen['env'])
        self.assertEqual(seen['timeout'], 5)

        def failing(args, check=False, **kwargs):
            # Like subprocess.run: a failed command only raises with check=True.
            if check:
                raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 1)

        with self.assertRaises(RuntimeError):
            autorotate.gamescope_rotate('left', runner=failing)

    def test_override_parsing_matches_session_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'display-orientation.conf'
            # A comment with a byte that is not UTF-8 does not hide the choice.
            path.write_bytes(b'# caf\xe9\norientation = auto \n')
            self.assertEqual(autorotate.orientation_setting(path), 'auto')
            path.write_bytes(b'orientation=right\r\n')
            self.assertEqual(autorotate.orientation_setting(path), 'right')
            path.write_bytes(b'orientation=auto\norientation=\xff\n')
            self.assertEqual(autorotate.orientation_setting(path), '')

    def test_device_env_failure_is_not_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / 'device-env'
            helper.write_text('#!/bin/sh\necho broken >&2\nexit 3\n')
            helper.chmod(0o755)
            self.assertIsNone(autorotate.shell_env(str(helper)))
            result = subprocess.run(
                ['python3', str(SCRIPT), '--supported'], capture_output=True, text=True,
                env=dict(os.environ, ARMADA_DEVICE_ENV=str(helper)))
            self.assertEqual(result.returncode, 255, result.stderr)
            helper.write_text('#!/bin/sh\necho ARMADA_GAMEMODE_AUTOROTATE=0\n')
            result = subprocess.run(
                ['python3', str(SCRIPT), '--supported'], capture_output=True, text=True,
                env=dict(os.environ, ARMADA_DEVICE_ENV=str(helper)))
            self.assertEqual(result.returncode, 1, result.stderr)
            helper.write_text('#!/bin/sh\necho ARMADA_GAMEMODE_AUTOROTATE=1\n')
            self.assertEqual(autorotate.shell_env(str(helper)),
                             {'ARMADA_GAMEMODE_AUTOROTATE': '1'})

    def test_supported_needs_profile_flag(self):
        self.assertTrue(autorotate.supported({'ARMADA_GAMEMODE_AUTOROTATE': '1'}))
        self.assertFalse(autorotate.supported({'ARMADA_GAMEMODE_AUTOROTATE': '0'}))
        self.assertFalse(autorotate.supported({}))


class IntegrationTest(unittest.TestCase):
    def test_unit_and_dropin(self):
        unit = UNIT.read_text()
        self.assertIn('BindsTo=gamescope-session-plus@steam.service\n', unit)
        self.assertIn('ExecCondition=/usr/bin/python3 /usr/libexec/armada/'
                      'armada-gamemode-autorotate --supported\n', unit)
        self.assertEqual(DROPIN.read_text(),
                         '[Unit]\nWants=armada-gamemode-autorotate.service\n')

    def test_gamescope_command_patch(self):
        patch = PATCH.read_text()
        self.assertIn('ConCommand cc_rotate_display( "rotate_display"', patch)
        for name in ('"normal"', '"left"', '"right"', '"upsidedown"'):
            self.assertIn(name, patch)
        self.assertIn('GAMESCOPE_CONTROL_DISPLAY_TARGET_TYPE_INTERNAL', patch)

    def test_profiles(self):
        defaults = (SYSTEM / 'usr/lib/armada/devices/defaults.conf').read_text()
        self.assertIn('\nARMADA_GAMEMODE_AUTOROTATE=0\n', defaults)
        tb710fu = (SYSTEM / 'usr/lib/armada/devices/lenovo-tb710fu.conf').read_text()
        self.assertIn('\nARMADA_GAMEMODE_AUTOROTATE=1\n', tb710fu)
        # 90/270 degrees need composition rotation on this panel.
        self.assertIn('\nARMADA_GAMESCOPE_USE_ROTATION_SHADER=1\n', tb710fu)
        self.assertIn('    ARMADA_GAMEMODE_AUTOROTATE\n',
                      (SYSTEM / 'usr/libexec/armada/device-env').read_text())


if __name__ == '__main__':
    unittest.main()
