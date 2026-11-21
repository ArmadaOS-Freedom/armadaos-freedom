"""TB710FU sensor hub: file staging helper, units, udev rule and packaging."""

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SYSTEM = REPO / 'system_files'
HELPER = SYSTEM / 'usr/libexec/tb710fu/tb710fu-sensors-prepare'
DAEMON_UNIT = SYSTEM / 'usr/lib/systemd/system/tb710fu-hexagonrpcd.service'
GENERATOR = SYSTEM / 'usr/lib/systemd/system-generators/tb710fu-sensors-generator'
SLEEP_HOOK = SYSTEM / 'usr/lib/systemd/system-sleep/45-tb710fu-sensors'
RULE = SYSTEM / 'usr/lib/udev/rules.d/81-armada-tb710fu-sensors.rules'
DTS_PATCH = REPO / 'packages/kernel/dts/sm8650-lenovo-tb710fu.dts.patch'

TB710FU = b'lenovo,tb710fu\0qcom,sm8650\0'
OTHER = b'lenovo,baldur\0qcom,kaanapali\0'
ACTIVE = (1 << 50) | (1 << 54) | (3 << 48)
INACTIVE = (1 << 55)
SECTOR = 512
VENDOR_SECTOR = 2048


def lp_super(path, partitions, vendor=b'', slots=3):
    """Write a super image with liblp metadata and vendor bytes at VENDOR_SECTOR.

    partitions: {name: [(sectors, sector), ...]}
    """
    max_size = 65536
    extents, entries = b'', b''
    index = 0
    for name, exts in partitions.items():
        entries += struct.pack('<36sIIII', name.encode(), 0, index, len(exts), 0)
        for sectors, sector in exts:
            extents += struct.pack('<QIQI', sectors, 0, sector, 0)
        index += len(exts)
    tables = entries + extents
    header = bytearray(256)
    struct.pack_into('<IHHI', header, 0, 0x414C5030, 10, 2, 256)
    struct.pack_into('<I', header, 44, len(tables))
    header[48:80] = hashlib.sha256(tables).digest()
    struct.pack_into('<III', header, 80, 0, len(partitions), 52)
    struct.pack_into('<III', header, 92, len(entries), index, 24)
    header[12:44] = hashlib.sha256(bytes(header)).digest()
    metadata = (bytes(header) + tables).ljust(max_size, b'\0')

    geometry = bytearray(52)
    struct.pack_into('<II', geometry, 0, 0x616C4467, 52)
    struct.pack_into('<III', geometry, 40, max_size, slots, 4096)
    geometry[8:40] = hashlib.sha256(bytes(geometry)).digest()
    geometry = bytes(geometry).ljust(4096, b'\0')

    image = bytearray(VENDOR_SECTOR * SECTOR) + vendor + bytes(8192)
    image[4096:8192] = geometry
    image[8192:12288] = geometry
    # Primary copies of every slot, then the backup copies.
    for slot in range(2 * slots):
        base = 12288 + slot * max_size
        image[base:base + max_size] = metadata
    Path(path).write_bytes(bytes(image))


FAKE_UDEVADM = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    name = [a for a in sys.argv if a.startswith('--name=')][0][7:]
    flags = json.load(open(os.environ['FAKE_FLAGS']))
    print('ID_PART_ENTRY_NAME=' + os.path.basename(name))
    print('ID_PART_ENTRY_FLAGS=0x%x' % flags[os.path.basename(name)])
''')

# Device files hold the path of the fixture tree they stand for.
FAKE_DEBUGFS = textwrap.dedent('''\
    #!/usr/bin/env python3
    import os, shutil, sys
    with open(os.environ['FAKE_LOG'], 'a') as log:
        log.write('debugfs ' + ' '.join(sys.argv[1:]) + '\\n')
    if '-w' in sys.argv:
        sys.exit('fake debugfs: refusing write mode')
    # Like the real tool: the banner and errors go to stderr, exit status 0.
    print('debugfs 1.47.3 (8-Jul-2025)', file=sys.stderr)
    command, device = sys.argv[sys.argv.index('-R') + 1], sys.argv[-1]
    words = command.split()
    root = open(device).read().strip()
    if words[:2] == ['ls', '-p']:
        src = os.path.join(root, words[2].lstrip('/'))
        if not os.path.isdir(src):
            print(words[2] + ': File not found by ext2_lookup', file=sys.stderr)
            sys.exit(0)
        print('/2/040755/0/0/.//')
        print('/2/040755/0/0/..//')
        for name in sorted(os.listdir(src)):
            path = os.path.join(src, name)
            if os.path.isdir(path):
                print('/3/040755/0/0/%s//' % name)
            else:
                print('/4/100644/0/0/%s/%d/' % (name, os.path.getsize(path)))
        sys.exit(0)
    verb, source, dest = words
    assert verb == 'rdump'
    src = os.path.join(root, source.lstrip('/'))
    if not os.path.isdir(src):
        print(source + ': File not found by ext2_lookup', file=sys.stderr)
        sys.exit(0)
    target = os.path.join(dest, os.path.basename(source))
    if os.environ.get('FAKE_DEBUGFS_PARTIAL'):
        # One file arrives, then a read error; debugfs still exits 0.
        os.makedirs(target)
        first = sorted(n for n in os.listdir(src) if os.path.isfile(os.path.join(src, n)))[0]
        shutil.copy(os.path.join(src, first), target)
        print('rdump: Attempt to read block from filesystem resulted in short read',
              file=sys.stderr)
        sys.exit(0)
    shutil.copytree(src, target)
''')

FAKE_DUMP_EROFS = textwrap.dedent('''\
    #!/usr/bin/env python3
    import os, sys
    with open(os.environ['FAKE_LOG'], 'a') as log:
        log.write('dump.erofs ' + ' '.join(sys.argv[1:]) + '\\n')
    args = sys.argv[1:]
    offset = [a for a in args if a.startswith('--offset=')][0][9:]
    path = [a for a in args if a.startswith('--path=')][0][7:]
    if offset != os.environ['FAKE_VENDOR_OFFSET']:
        sys.exit('wrong offset ' + offset)
    target = os.path.join(os.environ['FAKE_VENDOR'], path.lstrip('/'))
    if '--cat' in args:
        sys.stdout.buffer.write(open(target, 'rb').read())
    elif '--ls' in args:
        print('Path : ' + path)
        print('       NID TYPE  FILENAME')
        print('       100    2  .')
        print('        99    2  ..')
        for name in sorted(os.listdir(target)):
            kind = 2 if os.path.isdir(os.path.join(target, name)) else 1
            print('%10d %4d  %s' % (1000, kind, name))
''')


class HelperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for name, body in (('udevadm', FAKE_UDEVADM), ('debugfs', FAKE_DEBUGFS),
                           ('dump.erofs', FAKE_DUMP_EROFS)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.compatible = self.root / 'compatible'
        self.compatible.write_bytes(TB710FU)
        self.parts = self.root / 'by-partlabel'
        self.parts.mkdir()
        self.state = self.root / 'state'
        self.log = self.root / 'log'
        self.log.write_text('')
        self.flags = {'boot_a': ACTIVE, 'boot_b': INACTIVE}
        for label in ('boot_a', 'boot_b'):
            (self.parts / label).write_bytes(b'boot')

        for slot in 'ab':
            tree = self.root / ('dsp-' + slot) / 'adsp'
            tree.mkdir(parents=True)
            (tree / 'fastrpc_shell_0').write_bytes(b'shell-' + slot.encode())
            (tree / 'libsns_skel.so').write_bytes(b'skel')
            (self.parts / ('dsp_' + slot)).write_text(str(tree.parent))
        persist = self.root / 'persist/sensors/registry'
        (persist / 'registry').mkdir(parents=True)
        (persist / 'sns_reg_version').write_text('1')
        (persist / 'registry/lanai_bmi3x0_0.json.cal').write_text('{"bias":1}')
        (self.parts / 'persist').write_text(str(self.root / 'persist'))

        self.vendor = self.root / 'vendor'
        config = self.vendor / 'etc/sensors/config'
        config.mkdir(parents=True)
        (config / 'lanai_bmi3x0_0.json').write_text('{"accel":1}')
        (config / 'json.lst').write_text('lanai_bmi3x0_0.json\n')
        (config / 'subdir').mkdir()
        (self.vendor / 'etc/sensors/sns_reg_config').write_text('reg config')
        self.super = self.parts / 'super'
        self.write_super()

    def tearDown(self):
        self.tmp.cleanup()

    def vendor_image(self, tag):
        """Bytes standing for the vendor erofs image (the fake tool reads self.vendor)."""
        image = bytearray(16 * SECTOR)
        image[1024:1028] = struct.pack('<I', 0xE0F5E1E2)
        image[1028:1028 + len(tag)] = tag
        return bytes(image)

    def write_super(self, slot='a', tag=b'build-1', partitions=None):
        image = self.vendor_image(tag)
        if partitions is None:
            partitions = {'vendor_a': [], 'vendor_b': []}
            partitions['vendor_' + slot] = [(len(image) // SECTOR, VENDOR_SECTOR)]
        lp_super(self.super, partitions, image)

    def run_helper(self, compatible=None, **extra):
        Path(self.root / 'flags.json').write_text(json.dumps(self.flags))
        env = dict(os.environ,
                   TB710FU_DT_COMPATIBLE=str(compatible or self.compatible),
                   TB710FU_SENSORS_STATE=str(self.state),
                   TB710FU_PARTLABEL_DIR=str(self.parts),
                   TB710FU_UDEVADM=str(self.bin / 'udevadm'),
                   TB710FU_DEBUGFS=str(self.bin / 'debugfs'),
                   TB710FU_DUMP_EROFS=str(self.bin / 'dump.erofs'),
                   FAKE_FLAGS=str(self.root / 'flags.json'),
                   FAKE_LOG=str(self.log),
                   FAKE_VENDOR=str(self.vendor),
                   FAKE_VENDOR_OFFSET=str(VENDOR_SECTOR * SECTOR), **extra)
        return subprocess.run(['python3', '-I', str(HELPER)], env=env,
                              capture_output=True, text=True)

    def partition_hashes(self):
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.parts.iterdir()}

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_other_board_untouched(self):
        other = self.root / 'other'
        other.write_bytes(OTHER)
        result = self.run_helper(other)
        self.assert_ok(result)
        self.assertIn('skip', result.stderr)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.log.read_text(), '')

    def test_extracts_files_read_only(self):
        before = self.partition_hashes()
        self.assert_ok(self.run_helper())
        fw = self.state / 'fw'
        self.assertEqual((fw / 'dsp/adsp/fastrpc_shell_0').read_bytes(), b'shell-a')
        self.assertEqual((fw / 'sensors/config/lanai_bmi3x0_0.json').read_text(), '{"accel":1}')
        self.assertEqual((fw / 'sensors/config/json.lst').read_text(), 'lanai_bmi3x0_0.json\n')
        self.assertFalse((fw / 'sensors/config/subdir').exists())
        self.assertEqual((fw / 'sensors/sns_reg.conf').read_text(), 'reg config')
        registry = self.state / 'registry'
        self.assertEqual((registry / 'sns_reg_version').read_text(), '1')
        self.assertTrue((registry / 'registry/lanai_bmi3x0_0.json.cal').is_file())
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        stamp = json.loads((fw / 'stamp.json').read_text())
        self.assertEqual(stamp['slot'], 'a')
        self.assertEqual(stamp['version'], 2)
        self.assertIn('vendor_sensors_sha256', stamp)
        self.assertEqual(self.partition_hashes(), before)
        log = self.log.read_text()
        self.assertNotIn(' -w', log)
        self.assertIn('rdump /adsp', log)
        self.assertIn('ls -p /adsp', log)
        self.assertIn('rdump /sensors/registry', log)
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ['fw', 'registry'])

    def test_second_run_keeps_files_and_registry(self):
        self.assert_ok(self.run_helper())
        written = self.state / 'registry/registry/new-entry'
        written.write_text('from the sensor hub')
        self.log.write_text('')
        result = self.run_helper()
        self.assert_ok(result)
        self.assertIn('current', result.stderr)
        self.assertNotIn('rdump', self.log.read_text())
        self.assertEqual(written.read_text(), 'from the sensor hub')

    def test_changed_dsp_reextracts_but_keeps_registry(self):
        self.assert_ok(self.run_helper())
        written = self.state / 'registry/parsed_file_list.csv'
        written.write_text('hub')
        (self.root / 'dsp-a/adsp/fastrpc_shell_0').write_bytes(b'shell-a2')
        self.parts.joinpath('dsp_a').write_text(str(self.root / 'dsp-a') + '\n')
        self.assert_ok(self.run_helper())
        self.assertEqual((self.state / 'fw/dsp/adsp/fastrpc_shell_0').read_bytes(), b'shell-a2')
        self.assertEqual(written.read_text(), 'hub')

    def test_changed_vendor_reextracts(self):
        self.assert_ok(self.run_helper())
        (self.vendor / 'etc/sensors/sns_reg_config').write_text('new config')
        self.write_super(tag=b'build-2')
        self.assert_ok(self.run_helper())
        self.assertEqual((self.state / 'fw/sensors/sns_reg.conf').read_text(), 'new config')

    def test_changed_sensor_content_reextracts(self):
        self.assert_ok(self.run_helper())
        # Same vendor image identity, different sensor file contents.
        (self.vendor / 'etc/sensors/config/lanai_bmi3x0_0.json').write_text('{"accel":2}')
        self.assert_ok(self.run_helper())
        self.assertEqual(
            (self.state / 'fw/sensors/config/lanai_bmi3x0_0.json').read_text(), '{"accel":2}')

    def test_partial_dump_rejected(self):
        before = self.partition_hashes()
        result = self.run_helper(FAKE_DEBUGFS_PARTIAL='1')
        self.assertEqual(result.returncode, 1)
        self.assertIn('short read', result.stderr)
        self.assertEqual(os.listdir(self.state), [])
        self.assertEqual(self.partition_hashes(), before)

    def test_partial_registry_dump_rejected(self):
        self.assert_ok(self.run_helper())
        shutil.rmtree(self.state / 'registry')
        result = self.run_helper(FAKE_DEBUGFS_PARTIAL='1')
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.state / 'registry').exists())
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ['fw'])

    def test_interrupted_swap_is_recovered(self):
        self.assert_ok(self.run_helper())
        os.rename(self.state / 'fw', self.state / 'fw.old')
        self.log.write_text('')
        result = self.run_helper()
        self.assert_ok(result)
        self.assertIn('current', result.stderr)
        self.assertTrue((self.state / 'fw/sensors/sns_reg.conf').is_file())
        self.assertFalse((self.state / 'fw.old').exists())

    def test_slot_b(self):
        self.flags = {'boot_a': INACTIVE, 'boot_b': ACTIVE}
        self.write_super(slot='b')
        self.assert_ok(self.run_helper())
        self.assertEqual((self.state / 'fw/dsp/adsp/fastrpc_shell_0').read_bytes(), b'shell-b')

    def test_slot_must_be_unique(self):
        for flags in ({'boot_a': ACTIVE, 'boot_b': ACTIVE},
                      {'boot_a': INACTIVE, 'boot_b': INACTIVE},
                      {'boot_a': ACTIVE | INACTIVE, 'boot_b': 0}):
            self.flags = flags
            result = self.run_helper()
            self.assertEqual(result.returncode, 1)
            self.assertIn('exactly one active', result.stderr)
            self.assertEqual(os.listdir(self.state), [])

    def test_vendor_layout_refused(self):
        cases = (
            ({'vendor_a': [(8, VENDOR_SECTOR), (8, VENDOR_SECTOR + 8)]}, 'not one extent'),
            ({'vendor_a': []}, 'not one extent'),
            ({'system_a': [(16, VENDOR_SECTOR)]}, 'no dynamic partition vendor_a'),
            ({'vendor_a': [(1 << 30, VENDOR_SECTOR)]}, 'past super'),
        )
        for partitions, message in cases:
            self.write_super(partitions=partitions)
            result = self.run_helper()
            self.assertEqual(result.returncode, 1, partitions)
            self.assertIn(message, result.stderr)
            self.assertFalse((self.state / 'fw').exists())

    def corrupt_super(self, *offsets):
        data = bytearray(self.super.read_bytes())
        for offset in offsets:
            data[offset] ^= 0xFF
        self.super.write_bytes(bytes(data))

    def test_backup_metadata_used_when_primary_is_corrupt(self):
        # Primary metadata of slot 0, then the primary geometry.
        self.corrupt_super(12288 + 256, 4096 + 20)
        self.assert_ok(self.run_helper())
        self.assertEqual((self.state / 'fw/sensors/sns_reg.conf').read_text(), 'reg config')

    def test_corrupt_metadata_refused(self):
        # Primary and backup copies of slot 0 (3 slots of 64 KiB).
        self.corrupt_super(12288 + 256, 12288 + 3 * 65536 + 256)
        result = self.run_helper()
        self.assertEqual(result.returncode, 1)
        self.assertIn('checksum mismatch', result.stderr)

    def test_vendor_must_be_erofs(self):
        with open(self.super, 'r+b') as f:
            f.seek(VENDOR_SECTOR * SECTOR + 1024)
            f.write(bytes(4))
        result = self.run_helper()
        self.assertEqual(result.returncode, 1)
        self.assertIn('not erofs', result.stderr)

    def test_failed_extraction_keeps_previous_files(self):
        self.assert_ok(self.run_helper())
        self.write_super(tag=b'build-2')
        os.remove(self.vendor / 'etc/sensors/sns_reg_config')
        result = self.run_helper()
        self.assertEqual(result.returncode, 1)
        self.assertEqual((self.state / 'fw/sensors/sns_reg.conf').read_text(), 'reg config')
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ['fw', 'registry'])




def real_tools():
    """e2fsprogs and an erofs-utils whose dump.erofs can --cat (1.8 and later)."""
    if not all(shutil.which(t) for t in ('mke2fs', 'debugfs', 'mkfs.erofs', 'dump.erofs')):
        return False
    usage = subprocess.run(['dump.erofs', '--help'], capture_output=True, text=True)
    return '--cat' in usage.stdout + usage.stderr


REAL_TOOLS = real_tools()

# Logs the call like the fakes, refuses write mode, then runs the real tool.
LOGGING_WRAPPER = textwrap.dedent('''\
    #!/bin/sh
    echo "$(basename "$0") $*" >>"$FAKE_LOG"
    for arg; do [ "$arg" != -w ] || exit 2; done
    exec %s "$@"
''')


@unittest.skipUnless(REAL_TOOLS, 'needs e2fsprogs and erofs-utils')
class RealToolsTest(HelperTest):
    """The same flows on real ext4 (dsp, persist) and erofs (vendor) images."""

    def setUp(self):
        super().setUp()
        for tool in ('debugfs', 'dump.erofs'):
            (self.bin / tool).write_text(LOGGING_WRAPPER % shutil.which(tool))
        for label, tree in (('dsp_a', 'dsp-a'), ('dsp_b', 'dsp-b'), ('persist', 'persist')):
            self.make_ext4(label, tree)

    def make_ext4(self, label, tree):
        image = self.parts / label
        image.unlink()
        subprocess.run(['mke2fs', '-q', '-F', '-t', 'ext4', '-d', str(self.root / tree),
                        str(image), '4M'], check=True)

    def vendor_image(self, tag):
        image = self.root / 'vendor.erofs'
        image.unlink(missing_ok=True)
        # The build time stands for a new vendor build.
        stamp = str(1700000000 + int(hashlib.sha256(tag).hexdigest()[:6], 16))
        subprocess.run(['mkfs.erofs', '--quiet', '-T', stamp, str(image), str(self.vendor)],
                       check=True)
        data = image.read_bytes()
        return data + bytes(-len(data) % SECTOR)

    def test_changed_dsp_reextracts_but_keeps_registry(self):
        self.assert_ok(self.run_helper())
        written = self.state / 'registry/parsed_file_list.csv'
        written.write_text('hub')
        (self.root / 'dsp-a/adsp/fastrpc_shell_0').write_bytes(b'shell-a2')
        self.make_ext4('dsp_a', 'dsp-a')
        self.assert_ok(self.run_helper())
        self.assertEqual((self.state / 'fw/dsp/adsp/fastrpc_shell_0').read_bytes(), b'shell-a2')
        self.assertEqual(written.read_text(), 'hub')

    def test_changed_sensor_content_reextracts(self):
        self.assert_ok(self.run_helper())
        (self.vendor / 'etc/sensors/config/lanai_bmi3x0_0.json').write_text('{"accel":2}')
        self.write_super(tag=b'build-1')
        self.assert_ok(self.run_helper())
        self.assertEqual(
            (self.state / 'fw/sensors/config/lanai_bmi3x0_0.json').read_text(), '{"accel":2}')

    @unittest.skip('needs the fake debugfs that fails part-way')
    def test_partial_dump_rejected(self):
        pass

    @unittest.skip('needs the fake debugfs that fails part-way')
    def test_partial_registry_dump_rejected(self):
        pass

    def test_failed_extraction_keeps_previous_files(self):
        self.assert_ok(self.run_helper())
        os.remove(self.vendor / 'etc/sensors/sns_reg_config')
        self.write_super(tag=b'build-2')
        result = self.run_helper()
        self.assertEqual(result.returncode, 1)
        self.assertEqual((self.state / 'fw/sensors/sns_reg.conf').read_text(), 'reg config')
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ['fw', 'registry'])


def unit_values(path, key):
    return [line.split('=', 1)[1] for line in path.read_text().splitlines()
            if line.startswith(key + '=')]


class IntegrationTest(unittest.TestCase):
    def test_units(self):
        self.assertFalse((SYSTEM / 'usr/lib/systemd/system/tb710fu-sensors-prepare.service').exists())
        self.assertEqual(unit_values(DAEMON_UNIT, 'ConditionFirmware'),
                         ['device-tree-compatible(lenovo,tb710fu)'])
        # Staging runs before every start, outside the sandbox, so Restart=
        # retries a failed staging together with the daemon.
        self.assertEqual(unit_values(DAEMON_UNIT, 'ExecStartPre'),
                         ['+/usr/libexec/tb710fu/tb710fu-sensors-prepare'])
        self.assertEqual(unit_values(DAEMON_UNIT, 'Requires'), [])
        self.assertEqual(unit_values(DAEMON_UNIT, 'Restart'), ['always'])
        self.assertEqual(unit_values(DAEMON_UNIT, 'RestartMaxDelaySec'), ['60'])
        self.assertEqual(unit_values(DAEMON_UNIT, 'StartLimitIntervalSec'), ['0'])
        self.assertEqual(unit_values(DAEMON_UNIT, 'ExecStart'),
                         ['/usr/bin/hexagonrpcd -f /dev/fastrpc-adsp-secure -d adsp -s '
                          '-R /var/lib/tb710fu-sensors/fw'])
        self.assertEqual(unit_values(DAEMON_UNIT, 'Environment'),
                         ['HEXAGONRPCD_WRITABLE_REGISTRY=/var/lib/tb710fu-sensors/registry'])
        self.assertEqual(unit_values(DAEMON_UNIT, 'ReadWritePaths'),
                         ['/var/lib/tb710fu-sensors/registry'])
        self.assertIn('dev-fastrpc\\x2dadsp\\x2dsecure.device',
                      unit_values(DAEMON_UNIT, 'BindsTo'))
        wants = ' '.join(unit_values(DAEMON_UNIT, 'Wants'))
        for label in ('boot_a', 'boot_b', 'dsp_a', 'dsp_b', 'super', 'persist'):
            self.assertIn('dev-disk-by\\x2dpartlabel-%s.device' % label, wants)
        # Started by udev on the board only, not enabled on every device.
        self.assertNotIn('[Install]', DAEMON_UNIT.read_text())
        for path in (HELPER, GENERATOR, SLEEP_HOOK):
            self.assertTrue(os.access(path, os.X_OK), path)

    def test_udev_rule(self):
        text = RULE.read_text()
        rule = [line for line in text.splitlines() if line.startswith('PROGRAM')]
        self.assertEqual(len(rule), 1)
        rule = rule[0]
        self.assertIn('grep -azFxq lenovo,tb710fu /sys/firmware/devicetree/base/compatible', rule)
        self.assertIn('ENV{IIO_SENSOR_PROXY_TYPE}="ssc-accel ssc-light ssc-proximity"', rule)
        self.assertIn('ENV{ACCEL_MOUNT_MATRIX}="0, 1, 0; -1, 0, 0; 0, 0, 1"', rule)
        self.assertIn('TAG+="systemd"', rule)
        self.assertIn('tb710fu-hexagonrpcd.service iio-sensor-proxy.service', rule)
        self.assertIn('KERNEL!="fastrpc-adsp-secure"', text)
        # Runs after iio-sensor-proxy's own rule, so nothing there overrides it.
        self.assertGreater(RULE.name, '80-iio-sensor-proxy.rules')
        # The packaged rule no longer names SSC types on every FastRPC node.
        spec = (REPO / 'packages/iio-sensor-proxy/iio-sensor-proxy.spec').read_text()
        self.assertIn('sed -i \'/^SUBSYSTEM=="misc", KERNEL=="fastrpc-/d\' "$rules"', spec)

    def run_generator(self, compatible):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'compatible'
            path.write_bytes(compatible)
            out = Path(tmp) / 'normal'
            out.mkdir()
            subprocess.run([str(GENERATOR), str(out), str(out), str(out)], check=True,
                           env=dict(os.environ, TB710FU_DT_COMPATIBLE=str(path)))
            return {str(p.relative_to(out)): p.read_text()
                    for p in out.rglob('*') if p.is_file()}

    def test_generator(self):
        self.assertEqual(self.run_generator(OTHER), {})
        files = self.run_generator(TB710FU)
        self.assertEqual(list(files), ['iio-sensor-proxy.service.d/50-tb710fu-ssc.conf'])
        dropin = files['iio-sensor-proxy.service.d/50-tb710fu-ssc.conf']
        for line in ('Restart=always', 'RestartSec=5', 'StartLimitIntervalSec=0',
                     'After=tb710fu-hexagonrpcd.service', 'TimeoutStopSec=3'):
            self.assertIn(line + '\n', dropin)

    def run_sleep_hook(self, compatible, phases, state, start_ok=True, run_dir=None):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / 'compatible').write_bytes(compatible)
            log = tmp / 'systemctl.log'
            fake = tmp / 'bin/systemctl'
            fake.parent.mkdir()
            fake.write_text(textwrap.dedent('''\
                #!/bin/sh
                echo "$*" >>"%s"
                case "$1" in
                show) echo %s ;;
                start) [ %s = yes ] ;;
                esac
            ''') % (log, state, 'yes' if start_ok else 'no'))
            fake.chmod(0o755)
            run = run_dir(tmp) if run_dir else tmp / 'run'
            env = dict(os.environ, TB710FU_DT_COMPATIBLE=str(tmp / 'compatible'),
                       TB710FU_SENSORS_RUN=str(run),
                       PATH=str(fake.parent) + os.pathsep + os.environ['PATH'])
            for phase in phases:
                subprocess.run([str(SLEEP_HOOK), phase, 'suspend'], check=True, env=env,
                               capture_output=True)
            calls = log.read_text().splitlines() if log.exists() else []
            marker = (run / 'proxy-stopped').exists()
            return calls, marker

    def test_sleep_hook(self):
        show = 'show -P ActiveState iio-sensor-proxy.service'
        stop = 'stop iio-sensor-proxy.service'
        start = 'start --no-block iio-sensor-proxy.service'
        self.assertEqual(self.run_sleep_hook(OTHER, ['pre', 'post'], 'active'), ([], False))
        self.assertEqual(self.run_sleep_hook(TB710FU, ['pre', 'post'], 'active'),
                         ([show, stop, start], False))
        # A pending Restart= shows as activating: stop it and resume it.
        self.assertEqual(self.run_sleep_hook(TB710FU, ['pre', 'post'], 'activating'),
                         ([show, stop, start], False))
        self.assertEqual(self.run_sleep_hook(TB710FU, ['pre', 'post'], 'inactive'),
                         ([show], False))
        # Rejected start: the marker stays for the next resume.
        self.assertEqual(self.run_sleep_hook(TB710FU, ['pre', 'post'], 'active', start_ok=False),
                         ([show, stop, start], True))

    def test_sleep_hook_keeps_proxy_without_marker(self):
        def unwritable(tmp):
            (tmp / 'file').write_text('')
            return tmp / 'file' / 'run'
        calls, _ = self.run_sleep_hook(TB710FU, ['pre'], 'active', run_dir=unwritable)
        self.assertEqual(calls, ['show -P ActiveState iio-sensor-proxy.service'])

    def test_device_tree_secure_fastrpc(self):
        patch = DTS_PATCH.read_text()
        block = re.search(r'^\+&remoteproc_adsp \{\n(.*?)^\+\};$', patch, re.S | re.M)
        self.assertIsNotNone(block)
        self.assertRegex(block.group(1),
                         r'glink-edge \{\n\+\t\tfastrpc \{\n'
                         r'\+\t\t\t/delete-property/ qcom,non-secure-domain;')
        cdsp = re.search(r'^\+&remoteproc_cdsp \{\n(.*?)^\+\};$', patch, re.S | re.M)
        self.assertNotIn('non-secure-domain', cdsp.group(1))

    def test_packages_registered(self):
        containerfile = (REPO / 'Containerfile').read_text()
        stages = (REPO / 'packages/Containerfile').read_text()
        base = (REPO / 'build_files/10-base-packages.sh').read_text()
        cleanup = (REPO / 'build_files/70-cleanup.sh').read_text()
        for name, var in (('hexagonrpc', 'HEXAGONRPC_REF'),
                          ('iio-sensor-proxy', 'IIO_SENSOR_PROXY_REF')):
            self.assertIn('ARG %s\n' % var, containerfile)
            self.assertIn('FROM ${%s} AS %s\n' % (var, name), containerfile)
            self.assertIn('from=%s,source=/rpms,target=/packages/%s ' % (name, name),
                          containerfile)
            self.assertIn('FROM scratch AS out-%s\n' % name, stages)
        for rpm in ('/packages/iio-sensor-proxy/libssc-[0-9]*.aarch64.rpm',
                    '/packages/iio-sensor-proxy/iio-sensor-proxy-[0-9]*.aarch64.rpm',
                    '/packages/hexagonrpc/hexagonrpc-[0-9]*.aarch64.rpm'):
            self.assertIn(rpm, base)
        for name in ('hexagonrpc', 'iio-sensor-proxy', 'libssc'):
            self.assertRegex(cleanup, r'\n    %s \\\n' % re.escape(name))
        spec = (REPO / 'packages/iio-sensor-proxy/iio-sensor-proxy.spec').read_text()
        self.assertIn('-Dssc-support=enabled', spec)
        hexagonrpc = (REPO / 'packages/hexagonrpc/hexagonrpc.spec').read_text()
        for patch in sorted((REPO / 'packages/hexagonrpc/patches').glob('*.patch')):
            self.assertIn(patch.name, hexagonrpc)


if __name__ == '__main__':
    unittest.main()
