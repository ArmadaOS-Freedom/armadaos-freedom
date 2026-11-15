import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ARMADA_LIB = Path(__file__).resolve().parents[1] / "system_files" / "usr" / "lib" / "armada"
if str(ARMADA_LIB) not in sys.path:
    sys.path.insert(0, str(ARMADA_LIB))

from updates import _io


class StrictLoadsTest(unittest.TestCase):
    def test_accepts_valid_str_and_bytes_with_unicode_and_finite_float(self):
        document = '{"text":"héllo 世界","value":1.25,"items":[true,null,{"k":"v"}]}'
        expected = {"text": "héllo 世界", "value": 1.25,
                    "items": [True, None, {"k": "v"}]}
        self.assertEqual(_io.strict_loads(document), expected)
        self.assertEqual(_io.strict_loads(document.encode("utf-8")), expected)

    def test_rejects_duplicate_keys_at_any_depth(self):
        for raw in ('{"a":1,"a":2}',
                    '{"a":{"b":1,"b":2}}',
                    '[{"b":1,"b":2}]',
                    '{"a":[{"b":1,"b":2}]}'):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as caught:
                    _io.strict_loads(raw)
                self.assertNotIsInstance(caught.exception, json.JSONDecodeError)

    def test_rejects_nonfinite_constants(self):
        for raw in ('{"v":NaN}', '{"v":Infinity}', '{"v":-Infinity}',
                    '{"a":[{"v":NaN}]}'):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as caught:
                    _io.strict_loads(raw)
                self.assertNotIsInstance(caught.exception, json.JSONDecodeError)

    def test_malformed_json_stays_json_decode_error(self):
        for raw in ('{"a":', '{"a" 1}', 'not json'):
            with self.subTest(raw=raw):
                with self.assertRaises(json.JSONDecodeError):
                    _io.strict_loads(raw)

    def test_object_pairs_and_reject_constant_share_strict_checks(self):
        self.assertEqual(_io.object_pairs([("a", 1), ("b", [2])]),
                         {"a": 1, "b": [2]})
        with self.assertRaises(ValueError):
            _io.object_pairs([("a", 1), ("a", 2)])
        with self.assertRaises(ValueError):
            _io.reject_constant("NaN")


class FsyncWriteTest(unittest.TestCase):
    def test_fsync_dir_closes_fd_when_fsync_fails(self):
        with mock.patch.object(_io.os, "open", return_value=41) as opened, \
                mock.patch.object(_io.os, "fsync", side_effect=OSError("boom")), \
                mock.patch.object(_io.os, "close") as closed:
            with self.assertRaises(OSError):
                _io.fsync_dir("/some/dir")
        opened.assert_called_once()
        closed.assert_called_once_with(41)

    def test_fsync_dir_does_not_create_missing_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing"
            with self.assertRaises(OSError):
                _io.fsync_dir(missing)
            self.assertFalse(missing.exists())

    def test_fsync_dir_rejects_a_regular_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            regular = Path(tmp) / "regular"
            regular.write_bytes(b"data")
            with self.assertRaises(OSError):
                _io.fsync_dir(regular)

    def test_write_all_handles_partial_writes(self):
        with mock.patch.object(_io.os, "write", side_effect=[2, 3]) as write:
            _io.write_all(7, b"hello")
        self.assertEqual([bytes(call.args[1]) for call in write.call_args_list],
                         [b"hello", b"llo"])

    def test_write_all_rejects_zero_and_negative_writes(self):
        for result in (0, -1):
            with self.subTest(result=result):
                with mock.patch.object(_io.os, "write", return_value=result):
                    with self.assertRaises(OSError):
                        _io.write_all(7, b"hello")


class HashTest(unittest.TestCase):
    def test_sha256_hex_is_lowercase_sha256(self):
        self.assertEqual(_io.sha256_hex(b"abc"),
                         hashlib.sha256(b"abc").hexdigest())
        self.assertEqual(_io.sha256_hex(b"abc"),
                         "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")

    def test_sha256_stream_uses_position_keeps_open_and_reads_bounded_chunks(self):
        class RecordingStream(io.BytesIO):
            def __init__(self, data):
                super().__init__(data)
                self.sizes = []

            def read(self, size=-1):
                if (not isinstance(size, int) or isinstance(size, bool) or
                        size <= 0 or size > 1024 * 1024):
                    raise AssertionError("unbounded stream read: %r" % (size,))
                self.sizes.append(size)
                return super().read(size)

        data = b"skip" + b"x" * (2 * 1024 * 1024 + 17)
        stream = RecordingStream(data)
        stream.seek(4)
        self.assertEqual(_io.sha256_stream(stream),
                         hashlib.sha256(data[4:]).hexdigest())
        self.assertFalse(stream.closed)
        self.assertEqual(stream.tell(), len(data))
        self.assertGreaterEqual(len(stream.sizes), 3)
        self.assertLessEqual(max(stream.sizes), 1024 * 1024)


class BoundedReadTest(unittest.TestCase):
    def test_read_fd_bounded_reads_pipe_without_closing_or_seeking(self):
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b"abcdef")
            os.close(write_fd)
            write_fd = -1
            self.assertEqual(_io.read_fd_bounded(read_fd, 6), b"abcdef")
            os.fstat(read_fd)  # still open
            for limit in (True, -1):
                with self.subTest(limit=limit):
                    with self.assertRaises(ValueError):
                        _io.read_fd_bounded(read_fd, limit)
            os.fstat(read_fd)
        finally:
            if write_fd >= 0:
                os.close(write_fd)
            os.close(read_fd)

    def test_read_fd_bounded_rejects_oversized_payload(self):
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b"abcdef")
            os.close(write_fd)
            write_fd = -1
            with self.assertRaises(ValueError):
                _io.read_fd_bounded(read_fd, 5)
            os.fstat(read_fd)
        finally:
            if write_fd >= 0:
                os.close(write_fd)
            os.close(read_fd)

    def test_read_bounded_regular_oversized_symlink_and_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "payload"
            target.write_bytes(b"abcdef")
            link = Path(tmp) / "link"
            link.symlink_to(target)

            self.assertEqual(_io.read_bounded(target, 6), b"abcdef")
            with self.assertRaises(ValueError):
                _io.read_bounded(target, 5)
            with self.assertRaises(OSError):
                _io.read_bounded(link, 10)

            with mock.patch.object(_io.os, "close", wraps=os.close) as closed:
                with self.assertRaises(ValueError):
                    _io.read_bounded(target, 5)
            closed.assert_called_once()


if __name__ == "__main__":
    unittest.main()
