"""Chạy GUI và web gateway như một khối, để tắt là nhả hết cổng.

Cách cũ trong ``tftp.bat`` mở hai cửa sổ ``start`` riêng. Đóng cửa sổ GUI thì
TIFF server dừng nhưng web gateway vẫn chạy và giữ cổng 8080, nên lần mở sau
báo ``[WinError 10048] Only one usage of each socket address``.

Module này gom về một console duy nhất:

* GUI (kèm TFTP server nội bộ) và web gateway là hai tiến trình con của tiến
  trình này, dùng chung console.
* Đóng console, nhấn Ctrl+C, hoặc đóng cửa sổ GUI đều dừng cả hai và nhả
  cổng TCP 8080 + UDP 6969.
* ``--stop`` dừng mọi bản đang chạy (kể cả tiến trình mồ côi) và chờ cổng trống.

    python -m tftp.tftp_run --host 192.168.1.3 --allowed-network 192.168.1.0/24
    python -m tftp.tftp_run --stop
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Sequence

WEB_PORT = 8080
TFTP_PORT = 6969
STOP_TIMEOUT = 12.0


def _port_free(host: str, port: int, *, udp: bool) -> bool:
    family = socket.SOCK_DGRAM if udp else socket.SOCK_STREAM
    with socket.socket(socket.AF_INET, family) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def tcp_free(host: str, port: int) -> bool:
    """True nếu bind được TCP ``host:port`` (tức là cổng đang trống)."""
    return _port_free(host, port, udp=False)


def udp_free(port: int) -> bool:
    """True nếu bind được UDP ``127.0.0.1:port`` (tức là cổng đang trống)."""
    return _port_free("127.0.0.1", port, udp=True)


def _wait_until(condition: Callable[[], bool], timeout: float = STOP_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.25)
    return condition()


def _run_quiet(command: Sequence[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout or ""


def pids_on_port(port: int) -> set[int]:
    """PID đang giữ ``port`` (TCP đang LISTENING hoặc UDP đang bind)."""
    pids: set[int] = set()
    suffix = f":{port}"
    for protocol in ("tcp", "udp"):
        output = _run_quiet(["netstat", "-ano", "-p", protocol])
        for line in output.splitlines():
            parts = line.split()
            if len(parts) < 4 or parts[0].upper() != protocol.upper():
                continue
            if not parts[1].endswith(suffix):
                continue
            if protocol == "tcp" and "LISTENING" not in line.upper():
                continue
            pid = parts[-1]
            if pid.isdigit() and int(pid) > 0:
                pids.add(int(pid))
    return pids


def _is_python(pid: int) -> bool:
    output = _run_quiet(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"])
    return "python" in output.lower()


def ports_busy(host: str, web_port: int = WEB_PORT, tftp_port: int = TFTP_PORT) -> list[str]:
    """Danh sách mô tả các cổng đang bị chiếm."""
    busy = []
    if not tcp_free(host, web_port):
        busy.append(f"TCP {host}:{web_port}")
    if not udp_free(tftp_port):
        busy.append(f"UDP 127.0.0.1:{tftp_port}")
    return busy


def stop_running() -> int:
    """Dừng mọi tiến trình Python đang giữ cổng của app; trả về số đã dừng."""
    pids: set[int] = set()
    for port in (WEB_PORT, TFTP_PORT):
        pids |= pids_on_port(port)
    stopped = 0
    for pid in sorted(pids):
        if not _is_python(pid):
            print(f"[stop] bỏ qua pid {pid} (không phải Python)")
            continue
        print(f"[stop] dừng pid {pid}")
        _run_quiet(["taskkill", "/PID", str(pid), "/T", "/F"])
        stopped += 1
    freed = _wait_until(
        lambda: not pids_on_port(WEB_PORT) and not pids_on_port(TFTP_PORT)
    )
    if stopped == 0:
        print("[stop] không có tiến trình nào đang giữ cổng.")
    print("[stop] cổng 8080 và 6969 đã trống." if freed else
          "[stop] cổng vẫn còn bận — kiểm tra lại bằng netstat.")
    return 0 if freed else 1


def _terminate(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=6)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=6)


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def serve(args: argparse.Namespace) -> int:
    busy = ports_busy(args.host, args.port, args.tftp_port)
    if busy:
        print("[run] Cổng đang bận: " + ", ".join(busy))
        print("[run] Chạy 'stop.bat' (hoặc `python -m tftp.tftp_run --stop`) rồi thử lại.")
        return 2

    env = _child_env()
    gui: subprocess.Popen[bytes] | None = None
    web: subprocess.Popen[bytes] | None = None
    try:
        if not args.no_gui:
            gui = subprocess.Popen(
                [sys.executable, "-m", "tftp.tftp_gui", "--start-server"], env=env
            )
        command = [
            sys.executable, "-m", "tftp.web_upload",
            "--host", args.host,
            "--port", str(args.port),
            "--root", str(args.root),
            "--data-dir", str(args.data_dir),
            "--tftp-server", f"127.0.0.1:{args.tftp_port}",
        ]
        for network in args.allowed_network:
            command += ["--allowed-network", network]
        web = subprocess.Popen(command, env=env)

        print()
        print(f"  Web portal   : http://{args.host}:{args.port}")
        print(f"  TFTP server  : udp://127.0.0.1:{args.tftp_port}  (root: {args.root})")
        print("  Tắt app      : đóng cửa sổ GUI, hoặc Ctrl+C tại cửa sổ này.")
        print("                 Cả hai cổng 8080 và 6969 sẽ được nhả.")
        print(flush=True)

        while True:
            if web.poll() is not None:
                print(f"[run] web gateway đã thoát (mã {web.returncode}) — dừng GUI.")
                break
            if gui is not None and gui.poll() is not None:
                print("[run] GUI đã đóng — dừng web gateway.")
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[run] nhận Ctrl+C — dừng tất cả.")
    finally:
        _terminate(web)
        _terminate(gui)

    free = _wait_until(
        lambda: tcp_free(args.host, args.port) and udp_free(args.tftp_port)
    )
    print("[run] đã dừng; cổng 8080 và 6969 đã trống." if free
          else "[run] cảnh báo: cổng chưa được nhả hoàn toàn.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tftp-run", description="Chạy GUI + web gateway và dọn cổng khi tắt."
    )
    parser.add_argument("--host", help="IP LAN của máy chủ (ví dụ 192.168.1.3)")
    parser.add_argument("--port", type=int, default=WEB_PORT, help="cổng web (mặc định 8080)")
    parser.add_argument("--tftp-port", type=int, default=TFTP_PORT,
                        help="cổng UDP của TFTP server (mặc định 6969)")
    parser.add_argument("--root", default="files", help="thư mục gốc chia sẻ file")
    parser.add_argument("--data-dir", default="web-data", help="thư mục dữ liệu web")
    parser.add_argument("--allowed-network", action="append", default=[],
                        metavar="CIDR", help="subnet được phép; lặp lại được")
    parser.add_argument("--no-gui", action="store_true", help="chỉ chạy web gateway")
    parser.add_argument("--stop", action="store_true", help="dừng app và nhả cổng")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.stop:
        return stop_running()
    if not args.host:
        parser.error("cần --host (hoặc dùng --stop)")
    if not args.allowed_network:
        parser.error("cần ít nhất một --allowed-network")
    return serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
