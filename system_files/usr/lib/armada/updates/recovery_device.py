from contextlib import contextmanager
import os
from pathlib import Path
import stat
import struct
import subprocess

from .recovery_layout import SIZE, RecoveryLayoutError


def _require(condition, message):
    if not condition:
        raise RecoveryLayoutError(message)


def locate_recovery_partition(*, require_edl=False):
    dt = Path('/proc/device-tree')
    compatible = (dt / 'compatible').read_bytes().split(b'\0')
    profiles = [(b'lenovo,elden', (b'qcom,sm8750',), 1),
                (b'lenovo,baldur', (b'qcom,sm8850', b'qcom,kaanapali'), 2)]
    matches = [board for name, socs, board in profiles
               if name in compatible and any(soc in compatible for soc in socs)]
    _require(len(matches) == 1, 'Unsupported board')
    board = matches[0]
    chosen = dt / 'chosen'
    if require_edl:
        _require((chosen / 'kshim,edl-request-version').read_bytes() == struct.pack('>I', 1),
                 'Installed launcher does not support Linux EDL requests')
    report = (chosen / 'kshim,settings-v1').read_bytes()
    _require(len(report) == 64, 'Missing native settings report')
    v = struct.unpack('>16I', report)
    _require(v[0] == 1 and v[1] == board and v[2] == 0 and v[3] == 0 and v[4] == 0 and
             v[5] == 1 and v[12] == 0 and v[13] == 1,
             'Launcher has not verified usable internal settings')
    result = subprocess.run(['blkid', '-t', 'PARTLABEL=ARMADA_BACKUP', '-o', 'device'],
                            capture_output=True, text=True, check=True)
    candidates = []
    for name in result.stdout.splitlines():
        dev = Path(name).resolve()
        info = dev.stat()
        _require(stat.S_ISBLK(info.st_mode), 'Backup path is not a block device')
        node = Path('/sys/dev/block/%d:%d' % (os.major(info.st_rdev), os.minor(info.st_rdev))).resolve()
        if not any(part.endswith('.ufs') for part in node.parts):
            continue
        _require((node / 'partition').is_file() and int((node / 'size').read_text()) * 512 == SIZE,
                 'Internal backup partition is not exactly 2GiB')
        block = int((node.parent / 'queue/logical_block_size').read_text())
        _require(block in (512, 4096), 'Unsupported logical block size')
        part_uuid = subprocess.check_output(['blkid', '-s', 'PARTUUID', '-o', 'value', str(dev)], text=True).strip()
        candidates.append((dev, part_uuid, board, info.st_rdev))
    _require(len(candidates) == 1, 'Expected one internal UFS ARMADA_BACKUP partition')
    return candidates[0]


@contextmanager
def open_recovery(*, writable=False):
    if type(writable) is not bool:
        raise TypeError('writable must be bool')
    dev, part_uuid, board, expected_rdev = locate_recovery_partition(require_edl=False)
    flags = (os.O_RDWR if writable else os.O_RDONLY) | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open(str(dev), flags)
    try:
        info = os.fstat(fd)
        _require(stat.S_ISBLK(info.st_mode) and info.st_rdev == expected_rdev,
                 'Backup path is not a block device')
        yield (fd, part_uuid, board)
    finally:
        os.close(fd)
