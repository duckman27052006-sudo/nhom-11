"""Test địa chỉ IPv4/IPv6 và listener theo family."""

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
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tftp import tftp_errors
from tftp.tftp_client import TftpClient
from tftp.tftp_net import (
    address_key,
    format_endpoint,
    format_sockaddr,
    normalize_ip,
    parse_host_port,
    resolve_addresses,
    split_family_hosts,
)
from tftp.tftp_server import TftpServer

QUIET = logging.getLogger("tftp.net.test")
QUIET.setLevel(logging.CRITICAL)


def ipv6_loopback_available() -> bool:
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        try:
            sock.bind(("::1", 0))
            return True
        finally:
            sock.close()
    except OSError:
        return False


class TestAddressParsing(unittest.TestCase):
    def test_parse_ipv4_and_ipv6(self):
        self.assertEqual(parse_host_port("10.0.0.5", 6969), ("10.0.0.5", 6969))
        self.assertEqual(parse_host_port("10.0.0.5:7000", 6969), ("10.0.0.5", 7000))
        self.assertEqual(parse_host_port("[::1]:7000", 6969), ("::1", 7000))
        self.assertEqual(parse_host_port("2001:db8::1", 6969), ("2001:db8::1", 6969))
        self.assertEqual(parse_host_port("[fe80::1%12]:6969", 6969), ("fe80::1%12", 6969))

    def test_parse_rejects_bad_port(self):
        with self.assertRaises(ValueError):
            parse_host_port("[::1]:0", 6969)
        with self.assertRaises(ValueError):
            parse_host_port("[::1]:abc", 6969)

    def test_normalize_ipv4_mapped(self):
        self.assertEqual(normalize_ip("::ffff:127.0.0.1"), "127.0.0.1")
        self.assertEqual(normalize_ip("[::1]"), "::1")

    def test_address_key_includes_scope(self):
        self.assertEqual(address_key(("fe80::1", 69, 0, 12)), ("fe80::1", 69, 12))
        self.assertEqual(address_key(("127.0.0.1", 69)), ("127.0.0.1", 69, 0))

    def test_format_sockaddr_brackets_ipv6(self):
        self.assertEqual(format_sockaddr(("::1", 6969, 0, 0)), "[::1]:6969")
        self.assertEqual(format_endpoint("::1", 6969), "[::1]:6969")

    def test_split_family_hosts(self):
        self.assertEqual(split_family_hosts("0.0.0.0", None, "ipv4"), [(socket.AF_INET, "0.0.0.0")])
        self.assertEqual(split_family_hosts("0.0.0.0", None, "ipv6"), [(socket.AF_INET6, "::")])
        self.assertEqual(
            split_family_hosts("0.0.0.0", None, "dual"),
            [(socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")],
        )

    def test_resolve_localhost(self):
        addresses = resolve_addresses("localhost", 6969, "auto")
        self.assertTrue(addresses)
        self.assertTrue(all(item.port == 6969 for item in addresses))


class TestIpv6EndToEnd(unittest.TestCase):
    @unittest.skipUnless(ipv6_loopback_available(), "máy/CI không có IPv6 loopback ::1")
    def test_download_and_upload_ipv6(self):
        root = tempfile.mkdtemp(prefix="tftp-ipv6-")
        payload = b"ipv6 payload\x00" * 300
        Path(root, "v6.bin").write_bytes(payload)
        server = TftpServer(
            host="::1",
            port=0,
            root=root,
            timeout=1.0,
            retries=3,
            allow_overwrite=True,
            logger=QUIET,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for _ in range(200):
                if server.bound_port:
                    break
                time.sleep(0.01)
            self.assertTrue(server.bound_port)
            with tempfile.TemporaryDirectory() as out:
                target = str(Path(out) / "v6.bin")
                TftpClient("::1", server.bound_port, timeout=1.0, retries=3, logger=QUIET).download(
                    "v6.bin", target
                )
                self.assertEqual(Path(target).read_bytes(), payload)

                source = str(Path(out) / "up-v6.bin")
                Path(source).write_bytes(payload[::-1])
                TftpClient("::1", server.bound_port, timeout=1.0, retries=3, logger=QUIET).upload(
                    source, "up-v6.bin"
                )
                self.assertEqual(Path(root, "up-v6.bin").read_bytes(), payload[::-1])
        finally:
            server.shutdown()
            server.wait_for_idle(timeout=5)
            thread.join(timeout=5)

    @unittest.skipUnless(ipv6_loopback_available(), "máy/CI không có IPv6 loopback ::1")
    def test_dual_stack_listeners_share_port(self):
        root = tempfile.mkdtemp(prefix="tftp-dual-")
        server = TftpServer(
            host="0.0.0.0",
            port=0,
            root=root,
            family="dual",
            timeout=1.0,
            retries=3,
            logger=QUIET,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for _ in range(200):
                if server.bound_port:
                    break
                time.sleep(0.01)
            self.assertEqual(len(server.bound_addresses), 2)
            self.assertEqual(
                {item.rsplit(":", 1)[1] for item in server.bound_addresses},
                {str(server.bound_port)},
            )
        finally:
            server.shutdown()
            server.wait_for_idle(timeout=5)
            thread.join(timeout=5)


class TestPathPolicy(unittest.TestCase):
    def test_symlink_outside_root_rejected(self):
        root = tempfile.mkdtemp(prefix="tftp-root-")
        outside = tempfile.mkdtemp(prefix="tftp-outside-")
        secret = Path(outside) / "secret.txt"
        secret.write_text("do not serve", encoding="utf-8")
        link = Path(root) / "link.txt"
        try:
            os.symlink(secret, link)
        except (OSError, NotImplementedError):
            self.skipTest("môi trường không tạo được symlink")
        server = TftpServer(host="127.0.0.1", port=0, root=root, logger=QUIET)
        self.assertIsNone(server.resolve_path("link.txt"))


class TestWritePolicy(unittest.TestCase):
    def test_default_rejects_overwrite(self):
        root = tempfile.mkdtemp(prefix="tftp-policy-")
        Path(root, "keep.bin").write_bytes(b"old")
        server = TftpServer(
            host="127.0.0.1", port=0, root=root, timeout=1.0, retries=2, logger=QUIET
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for _ in range(200):
                if server.bound_port:
                    break
                time.sleep(0.01)
            with tempfile.TemporaryDirectory() as out:
                source = str(Path(out) / "keep.bin")
                Path(source).write_bytes(b"new")
                with self.assertRaises(tftp_errors.TftpRemoteError) as ctx:
                    TftpClient(
                        "127.0.0.1", server.bound_port, timeout=1.0, retries=2, logger=QUIET
                    ).upload(source, "keep.bin")
                self.assertEqual(ctx.exception.code, tftp_errors.ERR_FILE_EXISTS)
                self.assertEqual(Path(root, "keep.bin").read_bytes(), b"old")
                self.assertFalse([p for p in os.listdir(root) if p.endswith(".part")])
        finally:
            server.shutdown()
            server.wait_for_idle(timeout=5)
            thread.join(timeout=5)

    def test_unsupported_atomic_no_overwrite_does_not_replace_server_file(self):
        root = tempfile.mkdtemp(prefix="tftp-no-overwrite-")
        server = TftpServer(
            host="127.0.0.1", port=0, root=root, timeout=1.0, retries=2, logger=QUIET
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for _ in range(200):
                if server.bound_port:
                    break
                time.sleep(0.01)
            with tempfile.TemporaryDirectory() as out:
                source = str(Path(out) / "new.bin")
                Path(source).write_bytes(b"payload")
                with mock.patch(
                    "tftp.tftp_server.os.link", side_effect=OSError("hard links unsupported")
                ):
                    with self.assertRaises(tftp_errors.TftpRemoteError):
                        TftpClient(
                            "127.0.0.1",
                            server.bound_port,
                            timeout=1.0,
                            retries=2,
                            logger=QUIET,
                        ).upload(source, "new.bin")
                self.assertFalse(Path(root, "new.bin").exists())
                self.assertFalse([p for p in os.listdir(root) if p.endswith(".part")])
        finally:
            server.shutdown()
            server.wait_for_idle(timeout=5)
            thread.join(timeout=5)

    def test_unsupported_atomic_no_overwrite_does_not_replace_local_file(self):
        root = tempfile.mkdtemp(prefix="tftp-download-")
        Path(root, "remote.bin").write_bytes(b"payload")
        server = TftpServer(
            host="127.0.0.1",
            port=0,
            root=root,
            timeout=1.0,
            retries=2,
            allow_overwrite=True,
            logger=QUIET,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for _ in range(200):
                if server.bound_port:
                    break
                time.sleep(0.01)
            with tempfile.TemporaryDirectory() as out:
                target = Path(out) / "download.bin"
                with mock.patch(
                    "tftp.tftp_client.os.link", side_effect=OSError("hard links unsupported")
                ):
                    with self.assertRaises(OSError):
                        TftpClient(
                            "127.0.0.1",
                            server.bound_port,
                            timeout=1.0,
                            retries=2,
                            logger=QUIET,
                        ).download("remote.bin", str(target))
                self.assertFalse(target.exists())
                self.assertFalse(list(Path(out).glob("*.part")))
        finally:
            server.shutdown()
            server.wait_for_idle(timeout=5)
            thread.join(timeout=5)

class TestQuotaPolicy(unittest.TestCase):
    def _start_server(self, root: str, **kwargs):
        kwargs.setdefault("final_ack_dally", 0.0)
        server = TftpServer(
            host="127.0.0.1",
            port=0,
            root=root,
            timeout=1.0,
            retries=2,
            logger=QUIET,
            **kwargs,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        for _ in range(200):
            if server.bound_port:
                break
            time.sleep(0.01)
        self.assertGreater(server.bound_port, 0)
        return server, thread

    def _stop_server(self, server: TftpServer, thread: threading.Thread) -> None:
        server.shutdown()
        server.wait_for_idle(timeout=5)
        thread.join(timeout=5)

    def _upload(self, port: int, filename: str, payload: bytes) -> None:
        with tempfile.TemporaryDirectory() as source_dir:
            source = Path(source_dir) / filename
            source.write_bytes(payload)
            TftpClient(
                "127.0.0.1",
                port,
                timeout=1.0,
                retries=2,
                logger=QUIET,
            ).upload(str(source), filename)

    def test_quota_evicts_oldest_file_before_accepting_upload(self):
        with tempfile.TemporaryDirectory() as root:
            oldest = Path(root) / "oldest.bin"
            oldest.write_bytes(b"old!")
            os.utime(oldest, (1, 1))
            newer = Path(root) / "newer.bin"
            newer.write_bytes(b"next")
            os.utime(newer, (2, 2))
            server, thread = self._start_server(
                root, max_total_bytes=10, min_free_bytes=0, evict_oldest=True
            )
            try:
                self._upload(server.bound_port, "new.bin", b"abcdef")
                self.assertFalse(oldest.exists())
                self.assertTrue(newer.exists())
                self.assertEqual((Path(root) / "new.bin").read_bytes(), b"abcdef")
            finally:
                self._stop_server(server, thread)

    def test_upload_larger_than_quota_does_not_delete_existing_files(self):
        with tempfile.TemporaryDirectory() as root:
            oldest = Path(root) / "keep.bin"
            oldest.write_bytes(b"keep")
            server, thread = self._start_server(
                root, max_total_bytes=10, min_free_bytes=0
            )
            try:
                with self.assertRaises(tftp_errors.TftpRemoteError) as ctx:
                    self._upload(server.bound_port, "oversized.bin", b"x" * 11)
                self.assertEqual(ctx.exception.code, tftp_errors.ERR_DISK_FULL)
                self.assertEqual(oldest.read_bytes(), b"keep")
            finally:
                self._stop_server(server, thread)

    def test_quota_protects_active_files_tempfiles_and_symlinks(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            active = Path(root) / "active.bin"
            active.write_bytes(b"live")
            candidate = Path(root) / "old.bin"
            candidate.write_bytes(b"old!")
            temporary = Path(root) / ".active.part"
            temporary.write_bytes(b"temp")
            external = Path(outside) / "external.bin"
            external.write_bytes(b"safe")
            link = Path(root) / "linked.bin"
            try:
                os.symlink(external, link)
            except (OSError, NotImplementedError):
                self.skipTest("môi trường không tạo được symlink")

            server = TftpServer(
                host="127.0.0.1",
                port=0,
                root=root,
                max_total_bytes=8,
                min_free_bytes=0,
                evict_oldest=True,
                logger=QUIET,
            )
            self.assertTrue(server.try_acquire_write(str(active)))
            try:
                ok, reason = server._check_disk_budget(root)
                self.assertTrue(ok, reason)
                self.assertFalse(candidate.exists())
                self.assertTrue(active.exists())
                self.assertTrue(temporary.exists())
                self.assertTrue(link.exists())
                self.assertEqual(external.read_bytes(), b"safe")
            finally:
                server.release_write(str(active))

    def test_quota_can_reject_without_evicting(self):
        with tempfile.TemporaryDirectory() as root:
            existing = Path(root) / "keep.bin"
            existing.write_bytes(b"123456")
            server = TftpServer(
                host="127.0.0.1",
                port=0,
                root=root,
                max_total_bytes=10,
                min_free_bytes=0,
                evict_oldest=False,
                logger=QUIET,
            )
            ok, _reason = server._check_disk_budget(root, extra=6)
            self.assertFalse(ok)
            self.assertEqual(existing.read_bytes(), b"123456")


if __name__ == "__main__":
    unittest.main(verbosity=2)
