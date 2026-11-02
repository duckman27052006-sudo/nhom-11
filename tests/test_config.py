"""Test cấu hình JSON và logging rotation."""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.tftp_config import ConfigError, from_mapping, load_config
from src.tftp_logging import setup_logging


class TestConfig(unittest.TestCase):
    def test_defaults_are_safe(self):
        config = from_mapping({})
        self.assertFalse(config.allow_overwrite)
        self.assertGreater(config.max_file_size, 0)
        self.assertGreater(config.min_free_bytes, 0)
        self.assertEqual(config.max_total_bytes, 500 * 1024 * 1024)
        self.assertTrue(config.evict_oldest)
        self.assertEqual(config.family, "auto")

    def test_loads_and_normalizes(self):
        with tempfile.TemporaryDirectory() as out:
            path = Path(out) / "server.json"
            path.write_text(
                json.dumps(
                    {
                        "host": "127.0.0.1",
                        "family": "dual",
                        "port": 0,
                        "tid_port_range": [50000, 50010],
                        "max_file_size": None,
                        "max_total_bytes": 1024,
                        "evict_oldest": False,
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(str(path))
            self.assertEqual(config.host, "127.0.0.1")
            self.assertEqual(config.family, "dual")
            self.assertEqual(config.tid_port_range, (50000, 50010))
            self.assertIsNone(config.max_file_size)
            self.assertEqual(config.max_total_bytes, 1024)
            self.assertFalse(config.evict_oldest)
            self.assertFalse(config.to_server_kwargs()["evict_oldest"])
            self.assertIn("family=dual", config.describe())

    def test_rejects_unknown_key(self):
        with self.assertRaises(ConfigError):
            from_mapping({"max_file_sie": 10})

    def test_rejects_bad_values(self):
        for mapping in (
            {"family": "ipx"},
            {"port": 70000},
            {"blksize": 1},
            {"allow_overwrite": "yes"},
            {"tid_port_range": [50010, 50000]},
            {"max_file_size": 0},
            {"max_total_bytes": -1},
            {"evict_oldest": "true"},
        ):
            with self.subTest(mapping=mapping):
                with self.assertRaises(ConfigError):
                    from_mapping(mapping)

    def test_rejects_invalid_json(self):
        with tempfile.TemporaryDirectory() as out:
            path = Path(out) / "bad.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(str(path))


class TestConfigCli(unittest.TestCase):
    def test_cli_quota_and_eviction_options(self):
        from src import tftp_server

        args = tftp_server.build_arg_parser().parse_args(
            ["--max-total-bytes", "1024", "--no-evict-oldest"]
        )
        config = tftp_server._config_from_args(args)
        self.assertEqual(config.max_total_bytes, 1024)
        self.assertFalse(config.evict_oldest)

    def test_check_config_cli(self):
        from src import tftp_server

        with tempfile.TemporaryDirectory() as out:
            code = tftp_server.main(
                [
                    "--check-config",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "0",
                    "--root",
                    out,
                ]
            )
            self.assertEqual(code, 0)

    def test_check_config_rejects_bad_port(self):
        from src import tftp_server

        with tempfile.TemporaryDirectory() as out:
            code = tftp_server.main(
                ["--check-config", "--port", "70000", "--root", out]
            )
            self.assertEqual(code, 2)

    def test_check_config_file(self):
        from src import tftp_server

        with tempfile.TemporaryDirectory() as out:
            path = Path(out) / "server.json"
            path.write_text(json.dumps({"host": "127.0.0.1", "port": 0}), encoding="utf-8")
            self.assertEqual(tftp_server.main(["--config", str(path), "--check-config"]), 0)


class TestLogging(unittest.TestCase):
    def tearDown(self):
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            try:
                handler.close()
            except OSError:
                pass

    def test_rotating_file_handler(self):
        with tempfile.TemporaryDirectory() as out:
            path = str(Path(out) / "server.log")
            handlers = setup_logging(log_file=path, max_bytes=100, backups=1)
            try:
                self.assertTrue(
                    any(
                        isinstance(h, logging.handlers.RotatingFileHandler)
                        for h in handlers
                    )
                )
                logging.getLogger("rotation.demo").info("x" * 200)
                for handler in handlers:
                    handler.flush()
                logs = [p for p in Path(out).glob("server.log*") if p.is_file()]
                self.assertTrue(logs)
                self.assertTrue(any(p.stat().st_size > 0 for p in logs))
            finally:
                for handler in handlers:
                    handler.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
