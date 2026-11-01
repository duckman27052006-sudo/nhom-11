"""Test hồi quy cho các lỗi đã sửa (RFC 1350/2347, netascii, metrics, CLI)."""

from __future__ import annotations

import contextlib
import io
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import tftp_packet
from src.tftp_client import (
    ProgressPrinter,
    TftpClient,
    build_arg_parser,
    format_progress,
    validate_direction_args,
)
from src.tftp_errors import TftpFormatError, TftpIllegalOperation
from src.tftp_metrics import Timer
from src.tftp_packet import (
    ACK,
    DATA,
    ERROR,
    OACK,
    RRQ,
    WRQ,
    NetasciiDecoder,
    bytes_to_netascii,
    unpack,
)
from tests.test_e2e import ServerHarness, make_payload


def _udp(timeout: float = 3.0) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    return sock


class TestPacketEdges(unittest.TestCase):
    def test_option_value_too_long_is_format_error(self):
        raw = b"\x00\x01a\x00octet\x00blksize\x00" + b"1" * 5000 + b"\x00"
        with self.assertRaises(TftpFormatError):
            unpack(raw)
        self.assertFalse(tftp_packet.is_valid(raw))

    def test_mode_is_case_insensitive(self):
        packet = unpack(b"\x00\x01a.bin\x00OCTET\x00")
        self.assertEqual(packet.mode, "octet")
        self.assertEqual(RRQ("a.bin", "NETASCII").pack(), b"\x00\x01a.bin\x00netascii\x00")

    def test_error_code_out_of_range(self):
        with self.assertRaises(TftpIllegalOperation):
            ERROR(70000).pack()

    def test_oack_rejects_nul_in_value(self):
        with self.assertRaises(TftpIllegalOperation):
            OACK({"blksize": "1\x00"}).pack()

    def test_data_payload_above_max_blksize_rejected(self):
        raw = struct.pack("!HH", 3, 1) + b"x" * (tftp_packet.MAX_BLKSIZE + 1)
        with self.assertRaises(TftpFormatError):
            unpack(raw)

    def test_request_over_512_bytes_rejected(self):
        with self.assertRaises(TftpIllegalOperation):
            RRQ("x", "octet", {"windowsize": "1" * 600}).pack()

    def test_decoder_joins_cr_nul_split_across_blocks(self):
        decoder = NetasciiDecoder()
        self.assertEqual(decoder.feed(b"abc\r"), b"abc")
        self.assertEqual(decoder.feed(b"\x00def"), b"\rdef")
        self.assertEqual(decoder.flush(), b"")

    def test_netascii_maps_cr_to_cr_nul(self):
        self.assertEqual(bytes_to_netascii(b"a\rb"), b"a\r\x00b")


class TestMetricsThinning(unittest.TestCase):
    def test_spike_on_odd_index_survives_thinning(self):
        timer = Timer()
        for _ in range(4096):
            timer.observe(0.001)
            timer.observe(9.0)  # spike luôn ở chỉ số lẻ -> [::2] sẽ bỏ hết
        self.assertEqual(timer.percentile(100), 9.0)
        self.assertEqual(timer.snapshot()["max"], 9000.0)


class TestClientOptionFallback(unittest.TestCase):
    """OACK thiếu blksize => client phải dùng 512, không giữ giá trị đã đề nghị."""

    class _Server(threading.Thread):
        def __init__(self, payload: bytes) -> None:
            super().__init__(daemon=True)
            self.payload = payload
            self.error: BaseException | None = None
            self.sock = _udp(5.0)
            self.sock.bind(("127.0.0.1", 0))
            self.port = self.sock.getsockname()[1]

        def run(self) -> None:
            try:
                raw, addr = self.sock.recvfrom(65535)
                self.assert_request(raw)
                self.sock.sendto(OACK({"tsize": str(len(self.payload))}).pack(), addr)
                raw, addr = self.sock.recvfrom(65535)
                if unpack(raw) != ACK(0):
                    raise AssertionError("client phải ACK 0 sau OACK")
                offset, block = 0, 1
                while True:
                    chunk = self.payload[offset : offset + 512]
                    self.sock.sendto(DATA(block, chunk).pack(), addr)
                    raw, addr = self.sock.recvfrom(65535)
                    if unpack(raw) != ACK(block):
                        raise AssertionError(f"client phải ACK {block}")
                    offset += len(chunk)
                    block += 1
                    if len(chunk) < 512:
                        return
            except BaseException as exc:  # pragma: no cover - chỉ để báo lỗi
                self.error = exc
            finally:
                self.sock.close()

        @staticmethod
        def assert_request(raw: bytes) -> None:
            packet = unpack(raw)
            if not isinstance(packet, RRQ):
                raise AssertionError("phải nhận RRQ")

    def test_oack_without_blksize_uses_512(self):
        payload = make_payload(1024 + 100, seed=7)
        server = self._Server(payload)
        server.start()
        with tempfile.TemporaryDirectory() as out:
            target = str(Path(out) / "fallback.bin")
            client = TftpClient("127.0.0.1", server.port, blksize=1468, timeout=2.0, retries=2)
            client.download("fallback.bin", target)
            self.assertIsNone(server.error)
            self.assertEqual(Path(target).read_bytes(), payload)


class TestServerProtocol(unittest.TestCase):
    def test_netascii_upload_ending_with_cr_is_saved(self):
        # Trước khi sửa: decoder.flush() ghi vào file đã đóng -> ValueError,
        # không có ACK cuối và file không được lưu.
        with ServerHarness() as srv:
            sock = _udp()
            try:
                sock.sendto(WRQ("tail.txt", "netascii").pack(), ("127.0.0.1", srv.port))
                raw, addr = sock.recvfrom(65535)
                self.assertIsInstance(unpack(raw), ACK)
                sock.sendto(DATA(1, b"abc\r").pack(), addr)
                raw, _ = sock.recvfrom(65535)
                self.assertEqual(unpack(raw), ACK(1))
            finally:
                sock.close()
            self.assertEqual(Path(srv.root, "tail.txt").read_bytes(), b"abc\r")

    def test_wrq_oack_echoes_tsize(self):
        with ServerHarness() as srv:
            sock = _udp()
            try:
                sock.sendto(
                    WRQ("echo.bin", "octet", {"tsize": "1234"}).pack(),
                    ("127.0.0.1", srv.port),
                )
                raw, addr = sock.recvfrom(65535)
                packet = unpack(raw)
                self.assertIsInstance(packet, OACK)
                self.assertEqual(packet.options["tsize"], "1234")
                sock.sendto(ERROR(0, "abort").pack(), addr)
            finally:
                sock.close()

    def test_rrq_payload_metric_is_not_double_counted(self):
        payload = make_payload(5000, seed=3)
        with ServerHarness({"metric.bin": payload}) as srv, tempfile.TemporaryDirectory() as out:
            srv.client(blksize=1468).download("metric.bin", str(Path(out) / "metric.bin"))
            self.assertEqual(srv.server.metrics.counter("payload.sent"), len(payload))

    def test_uppercase_mode_is_accepted(self):
        with ServerHarness({"hello.txt": b"hi"}) as srv:
            sock = _udp()
            try:
                sock.sendto(b"\x00\x01hello.txt\x00OCTET\x00", ("127.0.0.1", srv.port))
                raw, addr = sock.recvfrom(65535)
                self.assertIsInstance(unpack(raw), DATA)
                sock.sendto(ERROR(0, "abort").pack(), addr)
            finally:
                sock.close()

    def test_port_reuse_opens_new_session_and_counts_it(self):
        # Cùng (ip, port) nhưng khác file = OS cấp lại ephemeral port: server phải
        # đóng phiên cũ, mở phiên mới và tăng requests.port_reuse (từng là code chết
        # vì thụt lề sai sau `return`).
        with ServerHarness({"a.bin": b"A" * 100, "b.bin": b"B" * 100}) as srv:
            sock = _udp(5.0)
            try:
                sock.sendto(RRQ("a.bin", "octet").pack(), ("127.0.0.1", srv.port))
                raw, old_tid = sock.recvfrom(65535)
                self.assertEqual(unpack(raw).data, b"A" * 100)
                sock.sendto(RRQ("b.bin", "octet").pack(), ("127.0.0.1", srv.port))
                deadline = time.monotonic() + 4.0
                while True:
                    raw, source = sock.recvfrom(65535)
                    if source != old_tid:
                        break
                    if time.monotonic() > deadline:
                        self.fail("không nhận được DATA từ TID mới")
                self.assertEqual(unpack(raw).data, b"B" * 100)
                self.assertEqual(srv.server.metrics.counter("requests.port_reuse"), 1)
            finally:
                sock.close()

    def test_huge_option_value_gets_error_reply_not_crash(self):
        with ServerHarness() as srv:
            sock = _udp()
            try:
                raw = b"\x00\x01a\x00octet\x00blksize\x00" + b"1" * 5000 + b"\x00"
                sock.sendto(raw, ("127.0.0.1", srv.port))
                reply, _ = sock.recvfrom(65535)
                self.assertIsInstance(unpack(reply), ERROR)
            finally:
                sock.close()
            self.assertEqual(srv.server.metrics.counter("errors.unhandled"), 0)


class TestCliProgress(unittest.TestCase):
    def test_format_progress_with_total(self):
        line = format_progress(500, 1000, 2.0)
        self.assertIn("50%", line)
        self.assertIn("ETA", line)
        self.assertIn("█", line)

    def test_format_progress_complete_and_zero_total(self):
        self.assertIn("100%", format_progress(10, 10, 1.0))
        self.assertIn("100%", format_progress(0, 0, 1.0))

    def test_format_progress_without_total(self):
        line = format_progress(2048, None, 1.0)
        self.assertNotIn("%", line)
        self.assertIn("KiB/s", line)

    def test_printer_writes_line_and_finish(self):
        stream = io.StringIO()
        printer = ProgressPrinter(stream=stream, interval=0.0)
        printer(512, 1024)
        printer(1024, 1024)
        printer.finish()
        text = stream.getvalue()
        self.assertIn("\r", text)
        self.assertIn("100%", text)
        self.assertTrue(text.endswith("\n"))

    def test_cli_accepts_progress_flag(self):
        with contextlib.redirect_stderr(io.StringIO()):
            args = build_arg_parser().parse_args(["--get", "a", "--progress"])
        self.assertTrue(args.progress)


class TestClientCliValidation(unittest.TestCase):
    def _parse(self, argv: list[str]):
        with contextlib.redirect_stderr(io.StringIO()):
            return build_arg_parser().parse_args(argv)

    def test_rejects_non_positive_timeout(self):
        for value in ("0", "-1"):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                self._parse(["--get", "a", "--timeout", value])

    def test_rejects_blocksize_out_of_range(self):
        for value in ("4", "70000"):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                self._parse(["--get", "a", "--blocksize", value])

    def test_rejects_save_with_put_and_remote_with_get(self):
        parser = build_arg_parser()
        cases = (
            ["--put", "a", "--save", "b"],
            ["--get", "a", "--remote", "b"],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                args = self._parse(argv)
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    validate_direction_args(parser, args)

    def test_accepts_valid_invocation(self):
        args = self._parse(["--get", "a", "--timeout", "2.5", "--blocksize", "1468"])
        self.assertEqual(args.timeout, 2.5)
        self.assertEqual(args.blocksize, 1468)


if __name__ == "__main__":
    unittest.main()
