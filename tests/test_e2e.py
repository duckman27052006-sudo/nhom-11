"""Test tích hợp client <-> server theo mục 11 của kế hoạch.

Các test chạy server thật trên một port UDP ngẫu nhiên (port 0) nên không cần
quyền quản trị và không xung đột với dịch vụ khác trên máy.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import tftp_client, tftp_errors, tftp_packet
from src.tftp_client import TftpClient
from src.tftp_packet import DATA, OP_ACK, OP_RRQ, RRQ, unpack
from src.tftp_server import TftpServer

QUIET = logging.getLogger("tftp.test")
QUIET.setLevel(logging.CRITICAL)


class ServerHarness:
    """Bật/tắt một TftpServer trong thread, phục vụ thư mục tạm."""

    def __init__(self, files: dict[str, bytes] | None = None, **options):
        self.dir = tempfile.TemporaryDirectory(prefix="tftp-test-")
        self.root = self.dir.name
        for name, content in (files or {}).items():
            (Path(self.root) / name).write_bytes(content)
        options.setdefault("host", "127.0.0.1")
        options.setdefault("port", 0)
        options.setdefault("timeout", 1.0)
        options.setdefault("retries", 3)
        options.setdefault("logger", QUIET)
        self.server = TftpServer(root=self.root, **options)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> ServerHarness:
        self.thread.start()
        for _ in range(200):
            if self.server.bound_port:
                break
            threading.Event().wait(0.01)
        return self

    def __exit__(self, *exc_info) -> None:
        self.server.shutdown()
        self.server.wait_for_idle(timeout=5.0)
        self.thread.join(timeout=5)
        self.dir.cleanup()

    @property
    def port(self) -> int:
        return self.server.bound_port

    def client(self, **kwargs) -> TftpClient:
        kwargs.setdefault("logger", QUIET)
        kwargs.setdefault("timeout", 1.0)
        kwargs.setdefault("retries", 3)
        return TftpClient("127.0.0.1", self.port, **kwargs)

    def path(self, name: str) -> str:
        return str(Path(self.root) / name)

    def read(self, name: str) -> bytes:
        return Path(self.root, name).read_bytes()


def make_payload(size: int, seed: int = 1) -> bytes:
    """Sinh dữ liệu nhị phân tất định để so sánh byte-for-byte."""
    return bytes(((index * 31 + seed * 7) % 256) for index in range(size))


class TestDownload(unittest.TestCase):
    def test_empty_file(self):
        with ServerHarness({"empty.bin": b""}) as srv, tempfile.TemporaryDirectory() as out:
            target = str(Path(out) / "empty.bin")
            srv.client().download("empty.bin", target)
            self.assertEqual(Path(target).read_bytes(), b"")

    def test_exactly_one_block(self):
        payload = make_payload(512)
        with ServerHarness({"one.bin": payload}) as srv, tempfile.TemporaryDirectory() as out:
            target = str(Path(out) / "one.bin")
            srv.client().download("one.bin", target)
            self.assertEqual(Path(target).read_bytes(), payload)

    def test_exactly_two_blocks(self):
        payload = make_payload(1024)
        with ServerHarness({"two.bin": payload}) as srv, tempfile.TemporaryDirectory() as out:
            target = str(Path(out) / "two.bin")
            srv.client().download("two.bin", target)
            self.assertEqual(Path(target).read_bytes(), payload)

    def test_large_file_with_blksize_option(self):
        payload = make_payload(3 * 1024 * 1024 + 137, seed=9)
        with ServerHarness({"big.bin": payload}) as srv, tempfile.TemporaryDirectory() as out:
            target = str(Path(out) / "big.bin")
            srv.client(blksize=1468, timeout=2.0).download("big.bin", target)
            self.assertEqual(Path(target).read_bytes(), payload)

    def test_download_without_options(self):
        payload = make_payload(5000)
        with ServerHarness({"plain.bin": payload}) as srv, tempfile.TemporaryDirectory() as out:
            target = str(Path(out) / "plain.bin")
            srv.client(use_options=False).download("plain.bin", target)
            self.assertEqual(Path(target).read_bytes(), payload)

    def test_netascii_mode(self):
        payload = b"dong mot\ndong hai\r\nhet\r"
        with ServerHarness({"text.txt": payload}) as srv, tempfile.TemporaryDirectory() as out:
            target = str(Path(out) / "text.txt")
            srv.client(mode="netascii").download("text.txt", target)
            self.assertEqual(Path(target).read_bytes(), b"dong mot\ndong hai\nhet\n")

    def test_netascii_multi_block_not_truncated(self):
        # Biên block từng rơi đúng giữa cặp CRLF khiến file bị cắt cụt âm thầm.
        payload = b"a" * 255 + b"\n" * 600
        with ServerHarness({"big.txt": payload}) as srv, tempfile.TemporaryDirectory() as out:
            target = str(Path(out) / "big.txt")
            srv.client(mode="netascii", blksize=512).download("big.txt", target)
            self.assertEqual(Path(target).read_bytes(), payload)

    def test_netascii_text_file_large(self):
        payload = b"".join(b"dong thu %d\n" % index for index in range(400))
        with ServerHarness({"lines.txt": payload}) as srv, tempfile.TemporaryDirectory() as out:
            target = str(Path(out) / "lines.txt")
            srv.client(mode="netascii", blksize=1468).download("lines.txt", target)
            self.assertEqual(Path(target).read_bytes(), payload)

    def test_block_number_wraps_after_65535(self):
        # blksize nhỏ nhất (8) để file ~75k block mà vẫn chạy nhanh.
        payload = make_payload(75_000 * 8 + 37, seed=13)
        with ServerHarness({"wrap.bin": payload}) as srv, tempfile.TemporaryDirectory() as out:
            target = str(Path(out) / "wrap.bin")
            srv.client(blksize=8, timeout=5.0, retries=5).download("wrap.bin", target)
            self.assertEqual(Path(target).read_bytes(), payload)

    def test_upload_block_number_wraps_after_65535(self):
        payload = make_payload(65_540 * 8 + 11, seed=17)
        with ServerHarness() as srv, tempfile.TemporaryDirectory() as out:
            source = str(Path(out) / "wrap-up.bin")
            Path(source).write_bytes(payload)
            srv.client(blksize=8, timeout=5.0, retries=5).upload(source, "wrap-up.bin")
            self.assertEqual(srv.read("wrap-up.bin"), payload)

    def test_file_not_found(self):
        with ServerHarness() as srv, tempfile.TemporaryDirectory() as out:
            target = str(Path(out) / "missing.bin")
            with self.assertRaises(tftp_errors.TftpRemoteError) as ctx:
                srv.client().download("khong-ton-tai.bin", target)
            self.assertEqual(ctx.exception.code, tftp_errors.ERR_FILE_NOT_FOUND)

    def test_path_traversal_rejected(self):
        with ServerHarness({"a.txt": b"x"}) as srv, tempfile.TemporaryDirectory() as out:
            target = str(Path(out) / "stolen.txt")
            with self.assertRaises(tftp_errors.TftpRemoteError) as ctx:
                srv.client().download("../secret.txt", target)
            self.assertEqual(ctx.exception.code, tftp_errors.ERR_ACCESS_VIOLATION)

    def test_subdirectory_name_rejected(self):
        with ServerHarness({"a.txt": b"x"}) as srv, tempfile.TemporaryDirectory() as out:
            target = str(Path(out) / "sub.txt")
            with self.assertRaises(tftp_errors.TftpRemoteError) as ctx:
                srv.client().download("sub/../a.txt", target)
            self.assertEqual(ctx.exception.code, tftp_errors.ERR_ACCESS_VIOLATION)


class TestUpload(unittest.TestCase):
    def test_upload_new_file(self):
        payload = make_payload(9000, seed=3)
        with ServerHarness() as srv, tempfile.TemporaryDirectory() as out:
            source = str(Path(out) / "up.bin")
            Path(source).write_bytes(payload)
            srv.client(blksize=1468, timeout=2.0).upload(source, "up.bin")
            self.assertEqual(srv.read("up.bin"), payload)
            self.assertFalse([p for p in os.listdir(srv.root) if p.endswith(".part")])

    def test_upload_empty_file(self):
        with ServerHarness() as srv, tempfile.TemporaryDirectory() as out:
            source = str(Path(out) / "zero.bin")
            Path(source).write_bytes(b"")
            srv.client().upload(source, "zero.bin")
            self.assertEqual(srv.read("zero.bin"), b"")

    def test_upload_overwrite(self):
        with ServerHarness({"dup.bin": b"old"}) as srv, tempfile.TemporaryDirectory() as out:
            source = str(Path(out) / "dup.bin")
            Path(source).write_bytes(b"new-content")
            srv.client().upload(source, "dup.bin")
            self.assertEqual(srv.read("dup.bin"), b"new-content")

    def test_upload_rejected_when_overwrite_disabled(self):
        payload = make_payload(1200)
        with ServerHarness({"keep.bin": b"old"}, allow_overwrite=False) as srv:
            with tempfile.TemporaryDirectory() as out:
                source = str(Path(out) / "keep.bin")
                Path(source).write_bytes(payload)
                with self.assertRaises(tftp_errors.TftpRemoteError) as ctx:
                    srv.client().upload(source, "keep.bin")
                self.assertEqual(ctx.exception.code, tftp_errors.ERR_FILE_EXISTS)
                self.assertEqual(srv.read("keep.bin"), b"old")

    def test_upload_then_download_round_trip(self):
        payload = make_payload(20000, seed=5)
        with ServerHarness() as srv, tempfile.TemporaryDirectory() as out:
            source = str(Path(out) / "rt.bin")
            Path(source).write_bytes(payload)
            srv.client(blksize=1024, timeout=2.0).upload(source, "rt.bin")
            back = str(Path(out) / "rt.back")
            srv.client(blksize=1024, timeout=2.0).download("rt.bin", back)
            self.assertEqual(Path(back).read_bytes(), payload)

    def test_upload_netascii_multi_block(self):
        payload = b"".join(b"dong %d\n" % index for index in range(400))
        with ServerHarness() as srv, tempfile.TemporaryDirectory() as out:
            source = str(Path(out) / "up.txt")
            Path(source).write_bytes(payload)
            srv.client(mode="netascii", blksize=1468).upload(source, "up.txt")
            self.assertEqual(srv.read("up.txt"), payload)


class LossyAckServer(threading.Thread):
    """Server tối giản cố tình bỏ ACK đầu tiên để kiểm tra cơ chế thử lại."""

    def __init__(self, payload: bytes, drop: int = 1):
        super().__init__(daemon=True)
        self.payload = payload
        self.drop = drop
        self.acks: list[int] = []
        self.requests: list[RRQ] = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]

    def run(self) -> None:
        self.sock.settimeout(10)
        blocks = [
            self.payload[index : index + 512] for index in range(0, len(self.payload), 512)
        ] or [b""]
        while True:
            try:
                raw, addr = self.sock.recvfrom(65535)
            except TimeoutError:
                return
            try:
                packet = unpack(raw)
            except tftp_errors.TftpError:
                return
            if packet.OPCODE == OP_RRQ:
                self.requests.append(packet)
                self.sock.sendto(DATA(1, blocks[0]).pack(), addr)
                continue
            if packet.OPCODE == OP_ACK:
                self.acks.append(packet.block)
                if len(self.acks) <= self.drop:
                    continue
                index = packet.block
                if index < len(blocks):
                    self.sock.sendto(DATA(index + 1, blocks[index]).pack(), addr)
                if index >= len(blocks) - 1:
                    return

    def stop(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class TestRetransmission(unittest.TestCase):
    def test_client_resends_ack_when_server_drops_it(self):
        payload = make_payload(1200, seed=11)
        server = LossyAckServer(payload, drop=1)
        server.start()
        try:
            with tempfile.TemporaryDirectory() as out:
                target = str(Path(out) / "lossy.bin")
                client = TftpClient("127.0.0.1", server.port, timeout=1.0, retries=4, logger=QUIET)
                client.download("lossy.bin", target)
                self.assertEqual(Path(target).read_bytes(), payload)
                self.assertGreaterEqual(server.acks.count(1), 2, "client phải gửi lại ACK block 1")
                self.assertEqual(
                    len(server.requests), 1, "client không được gửi lại RRQ khi đã biết TID"
                )
        finally:
            server.stop()
            server.join(timeout=5)

    def test_client_gives_up_after_retries(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        try:
            with tempfile.TemporaryDirectory() as out:
                target = str(Path(out) / "ghost.bin")
                client = TftpClient("127.0.0.1", port, timeout=0.3, retries=2, logger=QUIET)
                with self.assertRaises(tftp_errors.TftpTimeout):
                    client.download("ghost.bin", target)
        finally:
            server.close()

    def test_repeated_request_reuses_same_tid(self):
        payload = make_payload(600)
        with ServerHarness({"dup-req.bin": payload}, timeout=0.5, retries=2) as srv:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.settimeout(3)
            probe.bind(("127.0.0.1", 0))
            try:
                request = RRQ("dup-req.bin", "octet").pack()
                probe.sendto(request, ("127.0.0.1", srv.port))
                first, first_src = probe.recvfrom(65535)
                time.sleep(0.1)
                probe.sendto(request, ("127.0.0.1", srv.port))
                second, second_src = probe.recvfrom(65535)
            finally:
                probe.close()
            first_packet, second_packet = unpack(first), unpack(second)
            self.assertIsInstance(first_packet, DATA)
            self.assertIsInstance(second_packet, DATA)
            self.assertEqual(first_packet.block, 1)
            self.assertEqual(second_packet.block, 1)
            self.assertEqual(first_src, second_src, "phải trả lời lại từ cùng một TID")


class TestOptionInterop(unittest.TestCase):
    """Các ca tương thích đã từng hỏng: giữ regression cho từng bản vá."""

    def test_client_with_options_against_server_without_options(self):
        # Khong co OACK thi ca hai ben phai dung blksize 512 (RFC 1350), ke ca khi
        # client de nghi 1468 va server duoc cau hinh --blksize 1468.
        payload = make_payload(10_000, seed=31)
        with ServerHarness({"noopt.bin": payload}, enable_options=False) as srv:
            with tempfile.TemporaryDirectory() as out:
                target = str(Path(out) / "noopt.bin")
                srv.client(blksize=1468, timeout=2.0).download("noopt.bin", target)
                self.assertEqual(Path(target).read_bytes(), payload)
                self.assertEqual(srv.server.metrics.counter("transfers.completed.RRQ"), 1)

    def test_sub_512_byte_timeout_is_clamped_in_oack(self):
        # timeout < 1s làm tròn xuống sẽ thành 0; 0 khiến select() không chờ,
        # server quay vòng gửi lại và nuốt ACK.
        payload = make_payload(2000, seed=37)
        with ServerHarness({"clamp.bin": payload}, timeout=0.4) as srv:
            with tempfile.TemporaryDirectory() as out:
                target = str(Path(out) / "clamp.bin")
                srv.client(blksize=512, timeout=0.4, retries=5).download("clamp.bin", target)
                self.assertEqual(Path(target).read_bytes(), payload)

            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.settimeout(3)
            probe.bind(("127.0.0.1", 0))
            try:
                probe.sendto(
                    RRQ("clamp.bin", "octet", {"timeout": "0"}).pack(),
                    ("127.0.0.1", srv.port),
                )
                oack = unpack(probe.recvfrom(65535)[0])
            finally:
                probe.close()
            self.assertIsInstance(oack, tftp_packet.OACK)
            timeout = int(oack.options["timeout"])
            self.assertTrue(1 <= timeout <= 255, f"OACK trả timeout={timeout}")

    def test_duplicate_option_in_request_gets_error(self):
        raw = b"\x00\x01clamp.bin\x00octet\x00blksize\x001468\x00blksize\x00999\x00"
        with ServerHarness({"clamp.bin": b"x"}) as srv:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.settimeout(3)
            probe.bind(("127.0.0.1", 0))
            try:
                probe.sendto(raw, ("127.0.0.1", srv.port))
                error = unpack(probe.recvfrom(65535)[0])
            finally:
                probe.close()
            self.assertIsInstance(error, tftp_packet.ERROR)
            self.assertEqual(error.code, tftp_errors.ERR_ILLEGAL_OPERATION)


class TestMalformedRequests(unittest.TestCase):
    def test_bad_opcode_gets_error_4(self):
        with ServerHarness() as srv:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.settimeout(3)
            try:
                probe.sendto(b"\x00\x63abc", ("127.0.0.1", srv.port))
                raw, _ = probe.recvfrom(65535)
            finally:
                probe.close()
            packet = unpack(raw)
            self.assertIsInstance(packet, tftp_packet.ERROR)
            self.assertEqual(packet.code, tftp_errors.ERR_ILLEGAL_OPERATION)

    def test_empty_datagram_is_rejected(self):
        with ServerHarness() as srv:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.settimeout(1)
            try:
                probe.sendto(b"", ("127.0.0.1", srv.port))
                raw, _ = probe.recvfrom(65535)
            finally:
                probe.close()
            packet = unpack(raw)
            self.assertIsInstance(packet, tftp_packet.ERROR)
            self.assertEqual(packet.code, tftp_errors.ERR_ILLEGAL_OPERATION)
            self.assertEqual(srv.server.active_sessions, 0)


class TestConcurrentClients(unittest.TestCase):
    def test_eight_clients_do_not_mix_data(self):
        names = [f"c{i}.bin" for i in range(8)]
        payloads = {name: make_payload(20000 + 137 * i, seed=i + 1) for i, name in enumerate(names)}
        results: dict[str, bytes] = {}
        errors: list[BaseException] = []
        barrier = threading.Barrier(len(names))

        with ServerHarness(payloads, timeout=2.0, retries=5) as srv:
            with tempfile.TemporaryDirectory() as out:

                def worker(name: str) -> None:
                    target = str(Path(out) / f"dl-{name}")
                    try:
                        barrier.wait(timeout=10)
                        srv.client(blksize=1024, timeout=2.0).download(name, target)
                        results[name] = Path(target).read_bytes()
                    except BaseException as exc:  # noqa: BLE001 - gom lỗi về luồng chính
                        errors.append(exc)

                threads = [threading.Thread(target=worker, args=(name,)) for name in names]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=60)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), len(names))
        for name in names:
            self.assertEqual(results[name], payloads[name], f"dữ liệu của {name} bị lẫn")


class TestCommandLine(unittest.TestCase):
    def test_parse_server(self):
        self.assertEqual(tftp_client._parse_server("10.0.0.5"), ("10.0.0.5", 6969))
        self.assertEqual(tftp_client._parse_server("10.0.0.5:7000"), ("10.0.0.5", 7000))
        self.assertEqual(tftp_client._parse_server("[::1]:7000"), ("::1", 7000))

    def test_cli_get_and_put(self):
        payload = make_payload(3000, seed=21)
        with ServerHarness({"cli.bin": payload}) as srv, tempfile.TemporaryDirectory() as out:
            target = str(Path(out) / "cli.bin")
            code = tftp_client.main(
                [
                    "--server",
                    f"127.0.0.1:{srv.port}",
                    "--get",
                    "cli.bin",
                    "--save",
                    target,
                    "--timeout",
                    "1",
                ]
            )
            self.assertEqual(code, 0)
            self.assertEqual(Path(target).read_bytes(), payload)

            source = str(Path(out) / "cli-up.bin")
            Path(source).write_bytes(payload[::-1])
            code = tftp_client.main(
                [
                    "--server",
                    f"127.0.0.1:{srv.port}",
                    "--put",
                    source,
                    "--remote",
                    "cli-up.bin",
                    "--timeout",
                    "1",
                ]
            )
            self.assertEqual(code, 0)
            self.assertEqual(srv.read("cli-up.bin"), payload[::-1])

    def test_cli_reports_error(self):
        with ServerHarness() as srv, tempfile.TemporaryDirectory() as out:
            target = str(Path(out) / "x.bin")
            code = tftp_client.main(
                [
                    "--server",
                    f"127.0.0.1:{srv.port}",
                    "--get",
                    "khong-co.bin",
                    "--save",
                    target,
                    "--timeout",
                    "1",
                ]
            )
            self.assertEqual(code, 1)

    def test_cli_writes_client_stats_json(self):
        import json

        payload = make_payload(2500, seed=33)
        with ServerHarness({"stats.bin": payload}) as srv, tempfile.TemporaryDirectory() as out:
            target = str(Path(out) / "stats.bin")
            stats_path = str(Path(out) / "client-stats.json")
            code = tftp_client.main(
                [
                    "--server",
                    f"127.0.0.1:{srv.port}",
                    "--get",
                    "stats.bin",
                    "--save",
                    target,
                    "--timeout",
                    "1",
                    "--stats",
                    "--stats-json",
                    stats_path,
                ]
            )
            self.assertEqual(code, 0)
            data = json.loads(Path(stats_path).read_text(encoding="utf-8"))
            self.assertEqual(data["name"], "tftp.client")
            self.assertEqual(data["counters"]["download.ok"], 1)
            self.assertEqual(data["counters"]["transfers.completed.download"], 1)
            self.assertIn("transfer.duration.download", data["timers"])

    def test_cli_writes_server_stats_json(self):
        import json
        from unittest import mock

        from src import tftp_server

        with tempfile.TemporaryDirectory() as out:
            stats_path = str(Path(out) / "server-stats.json")
            with mock.patch.object(tftp_server.TftpServer, "serve_forever", lambda self: None):
                code = tftp_server.main(
                    [
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "0",
                        "--root",
                        out,
                        "--stats-interval",
                        "1",
                        "--stats-json",
                        stats_path,
                    ]
                )
            self.assertEqual(code, 0)
            data = json.loads(Path(stats_path).read_text(encoding="utf-8"))
            self.assertEqual(data["name"], "tftp.server")
            self.assertIn("counters", data)
            self.assertIn("timers", data)

    def test_cli_rejects_unwritable_log_path(self):
        from src import tftp_server

        bad_path = Path(tempfile.gettempdir()) / "khong-tao-duoc-nay" / ("x" * 3) / "log.txt"
        code = tftp_server.main(
            [
                "--host",
                "127.0.0.1",
                "--port",
                "0",
                "--root",
                tempfile.gettempdir(),
                "--log-file",
                str(bad_path),
            ]
        )
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
