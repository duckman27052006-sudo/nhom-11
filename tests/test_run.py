"""Unit test cho supervisor `tftp/tftp_run.py` (không mở tiến trình con)."""

from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tftp import tftp_run


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class TestPortChecks(unittest.TestCase):
    def test_tcp_free_detects_listening_socket(self):
        port = free_tcp_port()
        self.assertTrue(tftp_run.tcp_free("127.0.0.1", port))
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
            holder.bind(("127.0.0.1", port))
            holder.listen(1)
            self.assertFalse(tftp_run.tcp_free("127.0.0.1", port))
        self.assertTrue(tftp_run.tcp_free("127.0.0.1", port))

    def test_udp_free_detects_bound_socket(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            self.assertFalse(tftp_run.udp_free(port))
        self.assertTrue(tftp_run.udp_free(port))

    def test_ports_busy_lists_both_ports(self):
        self.assertEqual(tftp_run.ports_busy("127.0.0.1", 1, 2), [])


class TestParser(unittest.TestCase):
    def test_defaults(self):
        args = tftp_run.build_parser().parse_args(["--host", "192.168.1.3",
                                                   "--allowed-network", "192.168.1.0/24"])
        self.assertEqual(args.port, tftp_run.WEB_PORT)
        self.assertEqual(args.tftp_port, tftp_run.TFTP_PORT)
        self.assertEqual(args.root, "files")
        self.assertEqual(args.data_dir, "web-data")
        self.assertEqual(args.allowed_network, ["192.168.1.0/24"])
        self.assertFalse(args.stop)
        self.assertFalse(args.no_gui)

    def test_allowed_network_repeats(self):
        args = tftp_run.build_parser().parse_args([
            "--host", "192.168.1.3",
            "--allowed-network", "192.168.1.0/24",
            "--allowed-network", "192.168.2.0/24",
        ])
        self.assertEqual(args.allowed_network, ["192.168.1.0/24", "192.168.2.0/24"])

    def test_stop_needs_no_host(self):
        args = tftp_run.build_parser().parse_args(["--stop"])
        self.assertTrue(args.stop)

    def test_main_rejects_missing_host(self):
        with self.assertRaises(SystemExit):
            tftp_run.main(["--allowed-network", "192.168.1.0/24"])

    def test_main_rejects_missing_network(self):
        with self.assertRaises(SystemExit):
            tftp_run.main(["--host", "192.168.1.3"])


if __name__ == "__main__":
    unittest.main()
