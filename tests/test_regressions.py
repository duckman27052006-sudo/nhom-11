"""Test hồi quy cho các lỗi đã sửa (RFC 1350/2347, netascii, metrics, CLI)."""

from __future__ import annotations

import contextlib
import errno
import io
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_e2e import ServerHarness, make_payload
from tftp import tftp_errors, tftp_fs, tftp_packet
from tftp.tftp_client import TftpClient, build_arg_parser, validate_direction_args
from tftp.tftp_errors import TftpFormatError, TftpIllegalOperation
from tftp.tftp_metrics import Timer
from tftp.tftp_packet import (
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
from tftp.tftp_server import TftpServer, is_servable_name


def _udp(timeout: float = 3.0) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    return sock


class TestUnavailableTftpPeer(unittest.TestCase):
    def test_udp_rejection_is_a_peer_timeout_not_a_local_file_error(self):
        client = TftpClient("127.0.0.1", 6969)
        for error in (ConnectionResetError(10054), ConnectionRefusedError(errno.ECONNREFUSED)):
            with self.subTest(error=type(error).__name__):
                sock = mock.Mock()
                sock.recvfrom.side_effect = error
                with self.assertRaises(tftp_errors.TftpTimeout) as raised:
                    client._recv_valid(sock, (tftp_packet.OP_ACK,))
                self.assertIn("127.0.0.1:6969", str(raised.exception))
                self.assertIs(raised.exception.__cause__, error)


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


class TestWriteRequestFlow(unittest.TestCase):
    """WRQ + OACK: client gửi thẳng DATA 1 (RFC 2347), server không được bỏ nó."""

    class _Server(threading.Thread):
        def __init__(self) -> None:
            super().__init__(daemon=True)
            self.error: BaseException | None = None
            self.after_oack: object | None = None
            self.sock = _udp(5.0)
            self.sock.bind(("127.0.0.1", 0))
            self.port = self.sock.getsockname()[1]

        def run(self) -> None:
            try:
                raw, addr = self.sock.recvfrom(65535)
                if not isinstance(unpack(raw), WRQ):
                    raise AssertionError("phải nhận WRQ")
                self.sock.sendto(OACK({"blksize": "512", "tsize": "3"}).pack(), addr)
                raw, addr = self.sock.recvfrom(65535)
                self.after_oack = unpack(raw)
                self.sock.sendto(ACK(1).pack(), addr)
            except BaseException as exc:  # pragma: no cover - chỉ để báo lỗi
                self.error = exc
            finally:
                self.sock.close()

    def test_client_sends_data_immediately_after_oack(self):
        server = self._Server()
        server.start()
        with tempfile.TemporaryDirectory() as out:
            source = Path(out) / "a.bin"
            source.write_bytes(b"abc")
            TftpClient(
                "127.0.0.1", server.port, blksize=512, timeout=2.0, retries=1
            ).upload(str(source), "a.bin")
        server.join(timeout=5)
        self.assertIsNone(server.error)
        self.assertEqual(
            server.after_oack, DATA(1, b"abc"), "sau OACK phải là DATA 1, không phải ACK 0"
        )

    def test_upload_with_options_never_retransmits(self):
        # Bug từng gặp: server tiêu thụ DATA 1 trong lần chờ OACK rồi bỏ luôn,
        # khiến client phải chờ hết timeout và gửi lại (file vẫn đúng nhưng
        # mỗi lượt upload chậm thêm đúng một timeout).
        payload = make_payload(4096, seed=11)
        with ServerHarness() as srv, tempfile.TemporaryDirectory() as out:
            source = str(Path(out) / "clean.bin")
            Path(source).write_bytes(payload)
            client = srv.client(blksize=1468)
            client.upload(source, "clean.bin")
            self.assertEqual(srv.read("clean.bin"), payload)
            self.assertEqual(client.metrics.counter("packets.retransmitted"), 0)
            self.assertEqual(srv.server.metrics.counter("packets.retransmitted"), 0)


class TestReadDeadline(unittest.TestCase):
    def test_download_respects_max_transfer_seconds(self):
        # Trước đây `max_transfer_seconds` chỉ được kiểm ở WRQ; RRQ chạy vô hạn.
        payload = make_payload(4096, seed=4)
        with ServerHarness({"slow.bin": payload}, max_transfer_seconds=0.2) as srv:
            with tempfile.TemporaryDirectory() as out:
                client = srv.client(blksize=512)
                with self.assertRaises(tftp_errors.TftpRemoteError) as ctx:
                    client.download(
                        "slow.bin",
                        str(Path(out) / "slow.bin"),
                        progress=lambda done, total: time.sleep(0.25),
                    )
                self.assertIn("thời gian", str(ctx.exception))


class TestExchangeMetrics(unittest.TestCase):
    def test_rtt_never_records_process_uptime(self):
        # Pha chờ client sau OACK từng dùng sent_at = 0.0, khiến
        # `time.monotonic() - 0.0` (uptime process) bị ghi vào exchange.rtt.
        with ServerHarness() as srv, tempfile.TemporaryDirectory() as out:
            source = str(Path(out) / "m.bin")
            Path(source).write_bytes(make_payload(2048, seed=2))
            time.sleep(1.2)  # uptime phải lớn hơn ngưỡng 1s để bug cũ lộ ra
            srv.client(blksize=512).upload(source, "m.bin")
            rtt = srv.server.metrics.snapshot()["timers"].get("exchange.rtt")
            self.assertIsNotNone(rtt)
            self.assertGreater(rtt["count"], 0)
            self.assertLess(rtt["max"], 1000.0, rtt)


class TestCommitWithoutOverwrite(unittest.TestCase):
    def test_returns_false_when_target_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp, "tftp.tmp")
            temp.write_bytes(b"new")
            target = Path(tmp, "dst.bin")
            target.write_bytes(b"old")
            self.assertFalse(tftp_fs.commit_without_overwrite(str(temp), str(target)))
            self.assertEqual(target.read_bytes(), b"old")
            self.assertTrue(temp.exists())

    def test_fallback_copies_when_link_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp, "tftp.tmp")
            temp.write_bytes(b"payload")
            target = Path(tmp, "dst.bin")
            with mock.patch.object(
                tftp_fs, "_link", side_effect=OSError(errno.EPERM, "no hard links")
            ):
                self.assertTrue(tftp_fs.commit_without_overwrite(str(temp), str(target)))
            self.assertEqual(target.read_bytes(), b"payload")
            self.assertFalse(temp.exists())

    def test_upload_and_download_survive_filesystems_without_hardlinks(self):
        # FAT32/exFAT (USB, thẻ nhớ) không hỗ trợ os.link: trước đây mọi lượt
        # upload/tải về trên đó đều thất bại, server còn báo sai là "đĩa đầy".
        payload = make_payload(3000, seed=8)
        unsupported = OSError(errno.EPERM, "hard link not supported")
        with mock.patch.object(tftp_fs, "_link", side_effect=unsupported):
            with ServerHarness(allow_overwrite=False) as srv, tempfile.TemporaryDirectory() as out:
                source = str(Path(out) / "usb.bin")
                Path(source).write_bytes(payload)
                srv.client(blksize=512).upload(source, "usb.bin")
                self.assertEqual(srv.read("usb.bin"), payload)
                back = str(Path(out) / "usb.back")
                srv.client(blksize=512).download("usb.bin", back)
                self.assertEqual(Path(back).read_bytes(), payload)


class TestListenerPort(unittest.TestCase):
    def test_second_listener_on_same_port_is_rejected(self):
        # SO_REUSEADDR trên UDP (Windows) từng cho phép hai server bind cùng
        # cổng và chia gói tin trong im lặng.
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            with tempfile.TemporaryDirectory() as root:
                server = TftpServer("127.0.0.1", port, root=root, family="ipv4")
                with self.assertRaises(OSError):
                    server._open_listeners()
        finally:
            probe.close()


class TestFinalAckDally(unittest.TestCase):
    def test_server_acks_duplicate_final_data_after_wrq(self):
        # RFC 1350 §5: ACK cuối của WRQ bị mất thì client gửi lại DATA cuối và
        # bên nhận phải ACK lại thay vì đã đóng TID.
        payload = b"x" * 100
        with ServerHarness(final_ack_dally=1.0, timeout=2.0) as srv:
            sock = _udp(2.0)
            try:
                sock.sendto(WRQ("d.bin", "octet").pack(), ("127.0.0.1", srv.port))
                raw, tid = sock.recvfrom(65535)
                self.assertEqual(unpack(raw), ACK(0))
                sock.sendto(DATA(1, payload).pack(), tid)
                raw, _ = sock.recvfrom(65535)
                self.assertEqual(unpack(raw), ACK(1))
                # ACK cuối "bị mất": client gửi lại DATA cuối.
                sock.sendto(DATA(1, payload).pack(), tid)
                raw, _ = sock.recvfrom(65535)
                self.assertEqual(unpack(raw), ACK(1))
            finally:
                sock.close()
            self.assertEqual(Path(srv.root, "d.bin").read_bytes(), payload)


class TestServableNames(unittest.TestCase):
    def test_gui_filter_matches_server_resolution(self):
        with tempfile.TemporaryDirectory() as root:
            server = TftpServer("127.0.0.1", 0, root=root)
            for name in ("ok.txt", "báo-cáo.pdf", ".hidden", "CON", "trailing.", "a:b", "x.part"):
                with self.subTest(name=name):
                    self.assertEqual(
                        is_servable_name(name), server.resolve_path(name) is not None
                    )


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
