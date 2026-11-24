"""Benchmark TFTP: throughput theo blksize và RFC 7440 windowsize.

Chạy server + client trong cùng process, truyền dữ liệu sinh sẵn để đo
throughput thật của đường truyền loopback (bỏ phần tạo file):

    python tools/benchmark.py
    python tools/benchmark.py --size 16 --repeat 5 --json bench.json

Kết quả là số đo trên máy hiện tại, không phải cam kết hiệu năng tuyệt đối;
dùng để so sánh tương đối giữa các cấu hình (blksize 512 vs 1468 vs 8192,
windowsize 1 vs 4 vs 16) và để đưa vào báo cáo.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tftp import enable_utf8_console
from tftp.tftp_client import TftpClient
from tftp.tftp_server import TftpServer

QUIET = logging.getLogger("tftp.bench")
QUIET.setLevel(logging.CRITICAL)

MiB = 1024 * 1024


def run_once(root: str, size: int, blksize: int, windowsize: int, *, upload: bool) -> float:
    """Trả về thời gian truyền tính bằng giây cho một lượt."""
    server = TftpServer(
        "127.0.0.1",
        0,
        root=root,
        timeout=5.0,
        retries=8,
        blksize=blksize,
        windowsize=windowsize,
        final_ack_dally=0.0,
        logger=QUIET,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for _ in range(500):
        if server.bound_port:
            break
        time.sleep(0.01)
    try:
        client = TftpClient(
            "127.0.0.1",
            server.bound_port,
            timeout=5.0,
            retries=8,
            blksize=blksize,
            windowsize=windowsize,
            logger=QUIET,
        )
        started = time.perf_counter()
        if upload:
            client.upload(os.path.join(root, "payload.bin"), "uploaded.bin")
        else:
            client.download("payload.bin", os.path.join(root, "received.bin"))
        elapsed = time.perf_counter() - started
    finally:
        server.shutdown()
        server.wait_for_idle(timeout=10.0)
        thread.join(timeout=10)
    return max(elapsed, 1e-9)


def benchmark(size: int, repeat: int, upload: bool, configs: list[tuple[int, int]]) -> dict:
    results = []
    with tempfile.TemporaryDirectory(prefix="tftp-bench-") as root:
        payload = os.urandom(size)
        Path(root, "payload.bin").write_bytes(payload)
        for blksize, windowsize in configs:
            if windowsize > 1 and blksize > 1468:
                # Giữ tổng in-flight ≤ ~128 KiB để không phụ thuộc buffer của OS.
                windowsize = max(1, min(windowsize, 128 * 1024 // blksize))
            samples = []
            for _ in range(repeat):
                target = os.path.join(root, "received.bin")
                if os.path.exists(target):
                    os.remove(target)
                if os.path.exists(os.path.join(root, "uploaded.bin")):
                    os.remove(os.path.join(root, "uploaded.bin"))
                samples.append(run_once(root, size, blksize, windowsize, upload=upload))
            best = min(samples)
            median = statistics.median(samples)
            results.append(
                {
                    "blksize": blksize,
                    "windowsize": windowsize,
                    "direction": "upload" if upload else "download",
                    "mib": round(size / MiB, 2),
                    "best_seconds": round(best, 4),
                    "median_seconds": round(median, 4),
                    "best_mib_per_s": round(size / MiB / best, 2),
                    "median_mib_per_s": round(size / MiB / median, 2),
                }
            )
            print(
                f"{'WRQ' if upload else 'RRQ'} blksize={blksize:<6} "
                f"windowsize={windowsize:<3} "
                f"best={size / MiB / best:7.2f} MiB/s "
                f"median={size / MiB / median:7.2f} MiB/s",
                flush=True,
            )
    return {"size_mib": size / MiB, "repeat": repeat, "results": results}


def main(argv: list[str] | None = None) -> int:
    enable_utf8_console()
    parser = argparse.ArgumentParser(description="Benchmark TFTP blksize/windowsize")
    parser.add_argument("--size", type=float, default=8.0, help="kích thước file (MiB)")
    parser.add_argument("--repeat", type=int, default=3, help="số lần lặp mỗi cấu hình")
    parser.add_argument("--upload", action="store_true", help="đo chiều WRQ thay vì RRQ")
    parser.add_argument("--json", default=None, help="ghi kết quả ra file JSON")
    args = parser.parse_args(argv)
    size = max(1, int(args.size * MiB))
    configs = [
        (512, 1),
        (512, 4),
        (512, 16),
        (1468, 1),
        (1468, 4),
        (1468, 16),
        (8192, 1),
        (8192, 4),
    ]
    report = benchmark(size, max(1, args.repeat), args.upload, configs)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"đã ghi {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
