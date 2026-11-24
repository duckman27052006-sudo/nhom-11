"""Test RFC 7440 — TFTP Windowsize Option.

Phía gửi giữ cửa sổ tối đa `windowsize` DATA trước khi chờ ACK; phía nhận của
project ACK từng block (ACK luỹ tiến), nên chỉ cần kiểm tra cửa sổ phía gửi
hoạt động đúng cả hai chiều và negotiation không vượt quá giá trị client xin.
"""

from __future__ import annotations

import logging
import socket
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_e2e import ServerHarness, make_payload
from tests.test_soak import LossyProxy
from tftp.tftp_client import TftpClient
from tftp.tftp_packet import OACK, RRQ, unpack
from tftp.tftp_server import TftpServer

QUIET = logging.getLogger("tftp.test.windowsize")
QUIET.setLevel(logging.CRITICAL)


class TestWindowsizeNegotiation(unittest.TestCase):
    def test_server_caps_client_value(self):
        with tempfile.TemporaryDirectory() as root:
            server = TftpServer("127.0.0.1", 0, root=root, windowsize=8, logger=QUIET)
            try:
                accepted = server.negotiate_options({"windowsize": "4"}, None)
                self.assertEqual(accepted["windowsize"], "4")
                accepted = server.negotiate_options({"windowsize": "32"}, None)
                self.assertEqual(accepted["windowsize"], "8")
            finally:
                server.shutdown()

    def test_server_disabled_does_not_accept(self):
        with tempfile.TemporaryDirectory() as root:
            server = TftpServer("127.0.0.1", 0, root=root, windowsize=1, logger=QUIET)
            try:
                self.assertNotIn("windowsize", server.negotiate_options({"windowsize": "8"}, None))
            finally:
                server.shutdown()

    def test_invalid_windowsize_is_not_echoed(self):
        with tempfile.TemporaryDirectory() as root:
            server = TftpServer("127.0.0.1", 0, root=root, windowsize=8, logger=QUIET)
            try:
                self.assertNotIn("windowsize", server.negotiate_options({"windowsize": "0"}, None))
                self.assertNotIn(
                    "windowsize", server.negotiate_options({"windowsize": "65"}, None)
                )
            finally:
                server.shutdown()

    def test_raw_rrq_oack_contains_windowsize(self):
        with ServerHarness({"a.bin": b"x" * 900}, timeout=1.0, windowsize=8) as srv:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(2.0)
            try:
                options = {"blksize": "512", "windowsize": "4"}
                sock.sendto(RRQ("a.bin", "octet", options).pack(), ("127.0.0.1", srv.port))
                raw, tid = sock.recvfrom(65535)
                packet = unpack(raw)
                self.assertIsInstance(packet, OACK)
                self.assertEqual(packet.options.get("windowsize"), "4")
                self.assertEqual(packet.options.get("blksize"), "512")
            finally:
                sock.close()


class TestWindowsizeTransfer(unittest.TestCase):
    def test_download_with_window(self):
        payload = make_payload(200_000, seed=21)
        with ServerHarness({"big.bin": payload}, windowsize=8) as srv:
            with tempfile.TemporaryDirectory() as out:
                target = str(Path(out) / "big.bin")
                srv.client(blksize=512, windowsize=4, timeout=2.0).download("big.bin", target)
                self.assertEqual(Path(target).read_bytes(), payload)

    def test_upload_with_window(self):
        payload = make_payload(150_000, seed=22)
        with ServerHarness(windowsize=8) as srv:
            with tempfile.TemporaryDirectory() as out:
                source = Path(out) / "up.bin"
                source.write_bytes(payload)
                srv.client(blksize=512, windowsize=4, timeout=2.0).upload(
                    str(source), "up.bin"
                )
                self.assertEqual(srv.read("up.bin"), payload)

    def test_netascii_upload_with_window(self):
        payload = ("line with end\n" * 400).encode("ascii")
        with ServerHarness(windowsize=4) as srv:
            with tempfile.TemporaryDirectory() as out:
                source = Path(out) / "lines.txt"
                source.write_bytes(payload)
                srv.client(
                    mode="netascii", blksize=256, windowsize=4, timeout=2.0
                ).upload(str(source), "lines.txt")
                self.assertEqual(srv.read("lines.txt"), payload)

    def test_windowed_download_survives_packet_loss(self):
        # Mất ACK giữa cửa sổ: server phải go-back-N và truyền lại đủ dữ liệu.
        payload = make_payload(30_000, seed=24)
        with ServerHarness(
            {"lossy.bin": payload}, windowsize=8, timeout=0.5, retries=80
        ) as srv, tempfile.TemporaryDirectory() as out:
            proxy = LossyProxy(srv.port, drop_percent=20, seed=5)
            proxy.start()
            try:
                target = str(Path(out) / "lossy.bin")
                client = TftpClient(
                    "127.0.0.1",
                    proxy.port,
                    timeout=0.5,
                    retries=80,
                    blksize=512,
                    windowsize=8,
                    logger=QUIET,
                )
                client.download("lossy.bin", target)
                self.assertEqual(Path(target).read_bytes(), payload)
                self.assertGreater(proxy.dropped, 0, "proxy phải thực sự bỏ gói")
            finally:
                proxy.stop()

    def test_windowed_upload_survives_packet_loss(self):
        payload = make_payload(20_000, seed=25)
        with ServerHarness(
            windowsize=8, timeout=0.5, retries=80
        ) as srv, tempfile.TemporaryDirectory() as out:
            source = Path(out) / "lossy-up.bin"
            source.write_bytes(payload)
            proxy = LossyProxy(srv.port, drop_percent=20, seed=6)
            proxy.start()
            try:
                client = TftpClient(
                    "127.0.0.1",
                    proxy.port,
                    timeout=0.5,
                    retries=80,
                    blksize=512,
                    windowsize=8,
                    logger=QUIET,
                )
                client.upload(str(source), "lossy-up.bin")
                self.assertTrue(srv.server.wait_for_idle(timeout=10.0))
                self.assertEqual(srv.read("lossy-up.bin"), payload)
                self.assertGreater(proxy.dropped, 0, "proxy phải thực sự bỏ gói")
            finally:
                proxy.stop()


if __name__ == "__main__":
    unittest.main()
