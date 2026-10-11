"""Soak / stress test (bù mục 2.1 và 2.5 của REVIEW.md).

Các test ở đây nhắm vào ba câu hỏi mà bộ test cũ chưa trả lời:

1. **File thật sự lớn** — truyền vài chục MiB (tham số `--stats` đo được tốc độ,
   retry, độ trễ p95/p99 nên con số này cũng là phép đo hiệu năng).
2. **Block number wrap nhiều vòng** — file vượt 65535 block, thậm chí vượt 2 vòng,
   để chắc chắn `(block + 1) & 0xFFFF` không lệch ở chỗ biên.
3. **Hàng chục client đồng thời + môi trường mất gói** — proxy UDP cố tình bỏ
   một phần gói tin để khuyến khích retransmission, đồng thời kiểm tra server
   không rò rỉ phiên/socket/file tạm sau nhiều chu kỳ.

Kích thước mặc định vừa đủ để `python -m unittest discover` chạy nhanh (~1 phút).
Muốn chạy thật nặng thì đặt biến môi trường:

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `TFTP_SOAK_MB` | `16` | dung lượng file lớn (MiB) |
| `TFTP_SOAK_CLIENTS` | `24` | số client đồng thời |
| `TFTP_SOAK_CYCLES` | `5` | số chu kỳ lặp lại để phát hiện rò rỉ |
| `TFTP_SOAK_BLOCKS` | `70000` | số block trong test wrap |
| `TFTP_SKIP_SOAK` | — | đặt `1` để bỏ qua toàn bộ file này |

Ví dụ chạy soak thật (100 MiB, 40 client, 20 chu kỳ):

```bash
TFTP_SOAK_MB=100 TFTP_SOAK_CLIENTS=40 TFTP_SOAK_CYCLES=20 python -m unittest tests.test_soak -v
```
"""

from __future__ import annotations

import logging
import os
import random
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import enable_utf8_console, tftp_errors
from src.tftp_client import TftpClient
from src.tftp_packet import OP_RRQ, OP_WRQ, RRQ, unpack
from tests.test_e2e import ServerHarness, make_payload

enable_utf8_console()

QUIET = logging.getLogger("tftp.soak")
QUIET.setLevel(logging.CRITICAL)

MB = 1024 * 1024

LARGE_MB = max(1, int(os.environ.get("TFTP_SOAK_MB", "16")))
CLIENTS = max(2, int(os.environ.get("TFTP_SOAK_CLIENTS", "24")))
CYCLES = max(1, int(os.environ.get("TFTP_SOAK_CYCLES", "5")))
WRAP_BLOCKS = max(70000, int(os.environ.get("TFTP_SOAK_BLOCKS", "70000")))
LOSS_PERCENT = int(os.environ.get("TFTP_SOAK_LOSS", "20"))


def _skip_all() -> bool:
    return os.environ.get("TFTP_SKIP_SOAK", "").strip() not in ("", "0", "false", "False")


class LossyProxy(threading.Thread):
    """Proxy UDP theo dõi TID, bỏ bớt gói tin để ép client/server phải gửi lại.

    TFTP đổi TID ngay sau RRQ/WRQ nên proxy phải học địa chỉ nguồn của server và
    địa chỉ client: không làm vậy thì chỉ truyền được gói đầu tiên rồi đứt.
    """

    def __init__(self, server_port: int, drop_percent: int = 25, seed: int = 7) -> None:
        super().__init__(daemon=True)
        self.rng = random.Random(seed)
        self.drop_percent = drop_percent
        self.dropped = 0
        self.forwarded = 0
        self._server_port = server_port
        self._client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._client.bind(("127.0.0.1", 0))
        self._client.settimeout(0.3)
        self.port = self._client.getsockname()[1]
        self._server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._server.bind(("127.0.0.1", 0))
        self._server.settimeout(0.3)
        self._upstream = ("127.0.0.1", server_port)
        self._downstream: tuple[str, int] | None = None
        self._lock = threading.Lock()
        # Đặt tên _stop_event, KHÔNG phải _stop: attribute _stop là method
        # nội bộ của threading.Thread, đè nó khiến join() (Python ≤3.12)
        # raise TypeError: 'Event' object is not callable.
        self._stop_event = threading.Event()

    def _should_drop(self) -> bool:
        return self.rng.randrange(100) < self.drop_percent

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                raw, source = self._client.recvfrom(65535)
            except (TimeoutError, OSError):
                pass
            else:
                with self._lock:
                    self._downstream = source
                    try:
                        opcode = unpack(raw).OPCODE
                    except Exception:  # noqa: BLE001 - gói rác chỉ cần chuyển tiếp
                        opcode = None
                    if opcode in (OP_RRQ, OP_WRQ):
                        # Client gửi lại RRQ/WRQ vì timeout -> quay lại well-known port.
                        self._upstream = ("127.0.0.1", self._server_port)
                if self._should_drop():
                    with self._lock:
                        self.dropped += 1
                    continue
                with self._lock:
                    target = self._upstream
                try:
                    self._server.sendto(raw, target)
                except OSError:
                    pass
                with self._lock:
                    self.forwarded += 1

            try:
                raw, source = self._server.recvfrom(65535)
            except (TimeoutError, OSError):
                continue
            with self._lock:
                # Server trả lời từ TID mới -> mọi gói sau đó đi vào TID đó.
                self._upstream = source
                target = self._downstream
            if target is None or self._should_drop():
                with self._lock:
                    self.dropped += 1
                continue
            try:
                self._client.sendto(raw, target)
            except OSError:
                pass
            with self._lock:
                self.forwarded += 1

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=5)
        for sock in (self._client, self._server):
            try:
                sock.close()
            except OSError:
                pass


@unittest.skipIf(_skip_all(), "TFTP_SKIP_SOAK được đặt")
class TestLargeFile(unittest.TestCase):
    """File lớn: đường đi, thống kê và nội dung phải khớp từng byte."""

    def test_download_large_file(self):
        size = LARGE_MB * MB
        payload = make_payload(size, seed=41)
        with ServerHarness(timeout=5.0, retries=8) as srv, tempfile.TemporaryDirectory() as out:
            (Path(srv.root) / "huge.bin").write_bytes(payload)
            target = str(Path(out) / "huge.bin")
            started = time.monotonic()
            srv.client(blksize=65464, timeout=5.0, retries=8).download("huge.bin", target)
            elapsed = time.monotonic() - started

            self.assertEqual(Path(target).stat().st_size, size)
            self.assertEqual(Path(target).read_bytes(), payload)
            server_metrics = srv.server.metrics
            self.assertGreaterEqual(server_metrics.counter("payload.sent"), size)
            self.assertEqual(server_metrics.counter("sessions.started.RRQ"), 1)
            self.assertEqual(server_metrics.counter("sessions.finished.ok"), 1)
            self.assertEqual(server_metrics.counter("sessions.finished.timeout"), 0)
            self.assertEqual(server_metrics.gauge("sessions.active"), 0)
            self.assertGreater(server_metrics.snapshot()["timers"]["exchange.rtt"]["count"], 0)
            print(
                f"\n  [soak] download {size // MB} MiB blksize=65464: "
                f"{elapsed:.2f}s, {size / elapsed / MB:.1f} MiB/s, "
                f"p95 RTT {server_metrics.snapshot()['timers']['exchange.rtt']['p95']:.2f} ms"
            )

    def test_upload_large_file(self):
        size = LARGE_MB * MB
        payload = make_payload(size, seed=43)
        with ServerHarness(timeout=5.0, retries=8) as srv, tempfile.TemporaryDirectory() as out:
            source = str(Path(out) / "huge-up.bin")
            Path(source).write_bytes(payload)
            started = time.monotonic()
            srv.client(blksize=65464, timeout=5.0, retries=8).upload(source, "huge-up.bin")
            elapsed = time.monotonic() - started

            stored = Path(srv.root, "huge-up.bin")
            self.assertEqual(stored.stat().st_size, size)
            self.assertEqual(stored.read_bytes(), payload)
            self.assertEqual(srv.server.metrics.counter("payload.received"), size)
            print(
                f"\n  [soak] upload {size // MB} MiB blksize=65464: "
                f"{elapsed:.2f}s, {size / elapsed / MB:.1f} MiB/s"
            )


@unittest.skipIf(_skip_all(), "TFTP_SKIP_SOAK được đặt")
class TestBlockWrap(unittest.TestCase):
    """Block 16 bit wrap nhiều vòng mà dữ liệu vẫn khớp byte-for-byte."""

    def test_download_wraps_several_times(self):
        # blksize nhỏ nhất (8) để đạt nhiều vòng wrap với dung lượng vừa phải.
        payload = make_payload(WRAP_BLOCKS * 8 + 13, seed=47)
        expected_wraps = payload.__len__() // 8 // 65535
        with ServerHarness(timeout=5.0, retries=6) as srv, tempfile.TemporaryDirectory() as out:
            (Path(srv.root) / "wrap.bin").write_bytes(payload)
            target = str(Path(out) / "wrap.bin")
            srv.client(blksize=8, timeout=5.0, retries=6).download("wrap.bin", target)
            self.assertEqual(Path(target).read_bytes(), payload)
        self.assertGreaterEqual(expected_wraps, 1)
        print(f"\n  [soak] {len(payload) // 8} block, wrap {expected_wraps} vòng")

    def test_upload_wraps_several_times(self):
        payload = make_payload(WRAP_BLOCKS * 8 + 29, seed=53)
        with ServerHarness(timeout=5.0, retries=6) as srv, tempfile.TemporaryDirectory() as out:
            source = str(Path(out) / "wrap-up.bin")
            Path(source).write_bytes(payload)
            srv.client(blksize=8, timeout=5.0, retries=6).upload(source, "wrap-up.bin")
            self.assertEqual(srv.read("wrap-up.bin"), payload)


@unittest.skipIf(_skip_all(), "TFTP_SKIP_SOAK được đặt")
class TestConcurrentLoad(unittest.TestCase):
    """Hàng chục client cùng lúc: dữ liệu không lẫn, server không quá tải âm thầm."""

    def _harness(self, files=None, **kwargs):
        options = {
            "timeout": 10.0,
            "retries": 8,
            "max_clients": max(32, CLIENTS * 2),
            "max_requests_per_second": max(100, CLIENTS * 4),
        }
        options.update(kwargs)
        return ServerHarness(files=files, **options)

    def test_many_clients_download_in_parallel(self):
        names = [f"dl-{index}.bin" for index in range(CLIENTS)]
        payloads = {
            name: make_payload(40_000 + 977 * index, seed=index + 1)
            for index, name in enumerate(names)
        }
        results: dict[str, bytes] = {}
        errors: list[BaseException] = []
        barrier = threading.Barrier(len(names))

        with self._harness(payloads) as srv, tempfile.TemporaryDirectory() as out:
            def worker(name: str) -> None:
                target = str(Path(out) / name)
                try:
                    barrier.wait(timeout=30)
                    srv.client(blksize=1468, timeout=10.0, retries=8).download(name, target)
                    results[name] = Path(target).read_bytes()
                except BaseException as exc:  # noqa: BLE001 - gom lỗi về luồng chính
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(name,)) for name in names]
            started = time.monotonic()
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=180)
            elapsed = time.monotonic() - started

            self.assertEqual(errors, [])
            for name in names:
                self.assertEqual(results.get(name), payloads[name], f"{name} bị lẫn dữ liệu")

            metrics = srv.server.metrics.snapshot()
            self.assertEqual(metrics["counters"].get("sessions.finished.ok"), CLIENTS)
            self.assertEqual(metrics["counters"].get("packets.malformed", 0), 0)
            peak_sessions = metrics["timers"]["session.duration"]["count"]
            self.assertGreaterEqual(peak_sessions, CLIENTS)
            total_bytes = metrics["counters"].get("payload.sent", 0)
            print(
                f"\n  [soak] {CLIENTS} client tải song song: {elapsed:.2f}s, "
                f"{total_bytes / MB:.1f} MiB, p95 RTT "
                f"{metrics['timers']['exchange.rtt']['p95']:.2f} ms"
            )

    def test_readers_and_writers_mixed(self):
        half = max(2, CLIENTS // 2)
        downloads = {
            f"mix-dl-{i}.bin": make_payload(30_000 + 13 * i, seed=60 + i) for i in range(half)
        }
        uploads = {
            f"mix-up-{i}.bin": make_payload(20_000 + 31 * i, seed=90 + i) for i in range(half)
        }
        errors: list[BaseException] = []

        with self._harness(downloads) as srv, tempfile.TemporaryDirectory() as out:
            barrier = threading.Barrier(len(downloads) + len(uploads))

            def reader(name: str) -> None:
                target = str(Path(out) / name)
                try:
                    barrier.wait(timeout=30)
                    srv.client(blksize=1468, timeout=10.0, retries=8).download(name, target)
                    self.assertEqual(Path(target).read_bytes(), downloads[name])
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            def writer(name: str, payload: bytes) -> None:
                source = str(Path(out) / f"src-{name}")
                Path(source).write_bytes(payload)
                try:
                    barrier.wait(timeout=30)
                    srv.client(blksize=1468, timeout=10.0, retries=8).upload(source, name)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=reader, args=(n,)) for n in downloads]
            threads += [
                threading.Thread(target=writer, args=(n, p)) for n, p in uploads.items()
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=180)

        self.assertEqual(errors, [])

    def test_max_clients_is_enforced_and_reported(self):
        """Vượt `--max-clients` thì phải bị từ chối có kiểm soát, không treo."""
        limit = 4
        with self._harness(
            {"hold.bin": make_payload(200_000, seed=11)}, max_clients=limit, timeout=30.0
        ) as srv, tempfile.TemporaryDirectory() as out:
            results: dict[int, object] = {}

            def hold(index: int) -> None:
                target = str(Path(out) / f"hold-{index}.bin")
                try:
                    # timeout dài + retries cao để giữ phiên sống suốt thời gian chờ.
                    srv.client(blksize=64, timeout=30.0, retries=30).download("hold.bin", target)
                except BaseException as exc:  # noqa: BLE001
                    results[index] = exc

            threads = [threading.Thread(target=hold, args=(i,)) for i in range(limit)]
            for thread in threads:
                thread.start()
            try:
                deadline = time.monotonic() + 20
                while srv.server.active_sessions < limit and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual(srv.server.active_sessions, limit)
                extra = srv.client(blksize=512, timeout=1.0, retries=1)
                with self.assertRaises(tftp_errors.TftpError) as ctx:
                    extra.download("hold.bin", str(Path(out) / "extra.bin"))
                self.assertIn("quá tải", str(ctx.exception))
                self.assertGreaterEqual(srv.server.metrics.counter("requests.overloaded"), 1)
            finally:
                for thread in threads:
                    thread.join(timeout=60)
            self.assertEqual(results, {}, "các client nền không được thất bại")


@unittest.skipIf(_skip_all(), "TFTP_SKIP_SOAK được đặt")
class TestPacketLoss(unittest.TestCase):
    """Môi trường mất gói: phải hoàn tất nhờ retransmission, không mất dữ liệu."""

    def test_transfer_completes_with_heavy_packet_loss(self):
        payload = make_payload(20_000, seed=67)
        # Timeout ngắn + nhiều lần thử lại: với mất gói, mỗi lần timeout chỉ tốn
        # 0.5s thay vì 3s nên toàn bộ pha "môi trường xấu" còn vài giây.
        with ServerHarness(timeout=0.5, retries=60) as srv, tempfile.TemporaryDirectory() as out:
            (Path(srv.root) / "lossy.bin").write_bytes(payload)
            proxy = LossyProxy(srv.port, drop_percent=LOSS_PERCENT, seed=3)
            proxy.start()
            try:
                target = str(Path(out) / "lossy.bin")
                # Client nói chuyện với PROXY, không nói trực tiếp với server.
                client = TftpClient(
                    "127.0.0.1", proxy.port, timeout=0.5, retries=60, logger=QUIET
                )
                client.download("lossy.bin", target)
                self.assertEqual(Path(target).read_bytes(), payload)
                self.assertGreater(proxy.dropped, 0, "proxy phải thực sự bỏ gói")
                self.assertGreater(client.metrics.counter("packets.retransmitted"), 0)
                server_metrics = srv.server.metrics
                self.assertGreater(server_metrics.counter("packets.retransmitted"), 0)
                self.assertEqual(server_metrics.counter("sessions.finished.ok"), 1)
                print(
                    f"\n  [soak] mất {LOSS_PERCENT}% gói: server gửi lại "
                    f"{int(server_metrics.counter('packets.retransmitted'))} lần, "
                    f"{int(server_metrics.counter('timeouts'))} lần timeout"
                )
            finally:
                proxy.stop()


@unittest.skipIf(_skip_all(), "TFTP_SKIP_SOAK được đặt")
class TestNoResourceLeak(unittest.TestCase):
    """Nhiều chu kỳ liên tiếp không được để lại phiên, socket hay file tạm."""

    def test_repeated_cycles_keep_server_clean(self):
        payload = make_payload(24_000, seed=71)
        threads_before = len(threading.enumerate())
        with ServerHarness(timeout=5.0, retries=5, max_clients=64) as srv:
            with tempfile.TemporaryDirectory() as out:
                source = str(Path(out) / "cycle.bin")
                Path(source).write_bytes(payload)
                (Path(srv.root) / "cycle.bin").write_bytes(payload)

                durations: list[float] = []
                for cycle in range(CYCLES):
                    started = time.monotonic()
                    client = srv.client(blksize=1024, timeout=5.0, retries=5)
                    client.upload(source, "cycle.bin")
                    client.download("cycle.bin", str(Path(out) / f"back-{cycle}.bin"))
                    durations.append(time.monotonic() - started)
                    self.assertEqual(Path(out, f"back-{cycle}.bin").read_bytes(), payload)
                    # Client trả về trước khi thread phiên server chạy hết finally
                    # (gỡ khỏi _sessions) — chờ có giới hạn thay vì assert ngay,
                    # tránh flaky trên runner chậm.
                    deadline = time.monotonic() + 5.0
                    while (
                        srv.server.active_sessions != 0
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.01)
                    self.assertEqual(srv.server.active_sessions, 0)

            leftovers = [p.name for p in Path(srv.root).iterdir() if p.suffix == ".part"]
            self.assertEqual(leftovers, [], f"còn file tạm sót lại: {leftovers}")

            metrics = srv.server.metrics
            self.assertEqual(metrics.gauge("sessions.active"), 0)
            self.assertEqual(metrics.gauge("writes.active"), 0)
            self.assertEqual(metrics.counter("sessions.finished.ok"), CYCLES * 2)
            self.assertEqual(metrics.counter("sessions.finished.timeout"), 0)
            self.assertEqual(metrics.counter("packets.malformed"), 0)

            # Throughput không được tụt dốt sau nhiều chu kỳ (nghẽn tích tụ).
            fastest = min(durations)
            slowest = max(durations)
            self.assertLess(
                slowest,
                fastest * 6 + 1.0,
                f"thời gian tăng vọt: {durations}",
            )
            print(
                f"\n  [soak] {CYCLES} chu kỳ upload+download: "
                f"nhanh nhất {fastest * 1000:.0f} ms, chậm nhất {slowest * 1000:.0f} ms, "
                f"gửi lại {int(metrics.counter('packets.retransmitted'))} lần"
            )

        time.sleep(0.2)
        self.assertLessEqual(
            len(threading.enumerate()),
            threads_before + 2,
            "thread không được dọn sau khi server dừng",
        )

    def test_abandoned_sessions_are_reaped(self):
        """Client biến mất giữa chừng: server phải tự dọn sau khi hết timeout."""
        payload = make_payload(4_000_000, seed=73)
        threads_before = len(threading.enumerate())
        with ServerHarness(timeout=1.0, retries=1) as srv:
            (Path(srv.root) / "abandon.bin").write_bytes(payload)
            raw_client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            raw_client.bind(("127.0.0.1", 0))
            raw_client.settimeout(2.0)
            try:
                raw_client.sendto(
                    RRQ("abandon.bin", "octet", {"blksize": "8"}).pack(),
                    ("127.0.0.1", srv.port),
                )
                raw_client.recvfrom(65535)  # nhận OACK
            finally:
                raw_client.close()
            # Không gửi ACK nữa: phiên phải tự timeout rồi bị dọn khỏi bảng.
            deadline = time.monotonic() + 30
            while srv.server.active_sessions and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(srv.server.active_sessions, 0)
            self.assertEqual(srv.server.metrics.counter("sessions.finished.timeout"), 1)
        self.assertLessEqual(len(threading.enumerate()), threads_before + 2)


@unittest.skipIf(_skip_all(), "TFTP_SKIP_SOAK được đặt")
class TestMetricsReport(unittest.TestCase):
    def test_metrics_cover_a_transfer(self):
        payload = make_payload(30_000, seed=79)
        with ServerHarness(timeout=5.0, retries=5) as srv, tempfile.TemporaryDirectory() as out:
            (Path(srv.root) / "m.bin").write_bytes(payload)
            client = srv.client(blksize=1024, timeout=5.0, retries=5)
            client.download("m.bin", str(Path(out) / "m.bin"))

            server_snapshot = srv.server.metrics.snapshot()
            for key in ("packets.sent", "packets.received", "payload.sent", "sessions.started.RRQ"):
                self.assertIn(key, server_snapshot["counters"], key)
            self.assertIn("exchange.rtt", server_snapshot["timers"])
            self.assertGreater(server_snapshot["timers"]["exchange.rtt"]["count"], 0)
            self.assertIn("sessions.active", server_snapshot["gauges"])

            client_snapshot = client.metrics.snapshot()
            self.assertEqual(client_snapshot["counters"].get("download.ok"), 1)
            self.assertEqual(client_snapshot["counters"].get("transfers.completed.download"), 1)
            self.assertIn("transfer.duration.download", client_snapshot["timers"])

            rendered = srv.server.metrics.render()
            self.assertIn("tftp.server", rendered)
            self.assertIn("độ trễ (ms)", rendered)
            self.assertIn('"name": "tftp.server"', srv.server.metrics.to_json(indent=2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
