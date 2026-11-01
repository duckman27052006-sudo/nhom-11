"""TFTP client: tải file (RRQ) và gửi file (WRQ) theo RFC 1350 + RFC 2347.

Cách dùng như thư viện::

    from src.tftp_client import TftpClient
    TftpClient("127.0.0.1", 6969).download("hello.txt", "out/hello.txt")
    TftpClient("127.0.0.1", 6969).upload("out/hello.txt", "hello.txt")

Cách dùng trên dòng lệnh::

    python -m src.tftp_client --server 127.0.0.1:6969 --get hello.txt --save out.txt
    python -m src.tftp_client --server 127.0.0.1:6969 --put out.txt --remote hello.txt
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import DEFAULT_PORT, enable_utf8_console, tftp_errors
from .tftp_metrics import Metrics
from .tftp_net import Addr, resolve_remote
from .tftp_packet import (
    ACK,
    DATA,
    DEFAULT_BLKSIZE,
    DEFAULT_TIMEOUT,
    ERROR,
    MAX_BLKSIZE,
    MAX_DATAGRAM,
    MAX_STALE_PACKETS,
    MIN_BLKSIZE,
    OP_ACK,
    OP_DATA,
    OP_OACK,
    OPTION_BLKSIZE,
    OPTION_TIMEOUT,
    OPTION_TSIZE,
    RRQ,
    WRQ,
    NetasciiDecoder,
    NetasciiReader,
    clamp_timeout,
    describe,
    make_error,
    next_block,
    parse_options,
    unpack,
)

LOG = logging.getLogger("tftp.client")

ProgressFn = Callable[[int, int | None], None]
"""Callback tiến trình: ``(so_byte_da_xu_ly, tong_so_byte_hoac_None)``."""


@dataclass
class _ClientState:
    """State của một lượt truyền (2.3): thay Dict[str, object] dễ KeyError."""

    tid: Addr | None = None
    target: Addr = ("", 0)


class TftpClient:
    """Máy khách TFTP. Mỗi lượt truyền dùng một socket UDP riêng (TID của client)."""

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = 5,
        blksize: int = DEFAULT_BLKSIZE,
        mode: str = "octet",
        use_options: bool = True,
        metrics: Metrics | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.host = host
        self.port = port
        if timeout <= 0:
            raise ValueError("timeout phải > 0")
        self.timeout = float(timeout)
        self.retries = max(1, retries)
        self.blksize = min(max(blksize, MIN_BLKSIZE), MAX_BLKSIZE)
        self.mode = mode
        self.use_options = use_options
        self.log = logger or LOG
        self.metrics = metrics if metrics is not None else Metrics("tftp.client")
        self._family: int = socket.AF_INET
        self._state = _ClientState(target=(host, port))
        self._sent_at = 0.0

    # ------------------------------------------------------------ tiện ích ---
    def _info(self, message: str) -> None:
        self.log.info(message)

    def _debug(self, message: str) -> None:
        self.log.debug(message)

    def _warn(self, message: str) -> None:
        self.log.warning(message)

    def _open_socket(self, timeout: float | None = None) -> socket.socket:
        sock = socket.socket(self._family, socket.SOCK_DGRAM)
        sock.settimeout(self.timeout if timeout is None else timeout)
        return sock

    def _requested_options(self, *, for_upload: bool, size: int | None = None) -> dict[str, str]:
        """Option gửi kèm RRQ/WRQ (1.3: tsize luôn được hỏi/gửi)."""
        if not self.use_options:
            return {}
        options = {
            OPTION_BLKSIZE: str(self.blksize),
            OPTION_TIMEOUT: str(clamp_timeout(self.timeout)),
        }
        if for_upload:
            # WRQ gửi kích thước thật để server kiểm tra trước khi nhận.
            if size is not None:
                options[OPTION_TSIZE] = str(size)
            else:
                options[OPTION_TSIZE] = "0"
        else:
            # RRQ gửi tsize=0 để hỏi kích thước file.
            options[OPTION_TSIZE] = "0"
        return options

    def _apply_oack(self, packet, timeout: float) -> tuple[int, float, int | None]:
        """Tính blksize/timeout/tsize đã thương lượng, KHÔNG đụng vào self (2.2).

        Trả về (blksize, timeout, tsize). RFC 2347: option nào không có trong
        OACK thì dùng giá trị mặc định — riêng blksize mặc định là 512, không
        phải giá trị client đề nghị (nếu giữ 1468 thì hai bên lệch blksize và
        truyền dừng sau block đầu).
        """
        values = parse_options(packet)
        new_blksize = DEFAULT_BLKSIZE
        new_timeout = timeout
        tsize: int | None = None
        negotiated = values.get(OPTION_BLKSIZE)
        if negotiated is not None and 8 <= negotiated <= 65464:
            new_blksize = negotiated
        negotiated_timeout = values.get(OPTION_TIMEOUT)
        if negotiated_timeout is not None and 1 <= negotiated_timeout <= 255:
            new_timeout = float(negotiated_timeout)
        if OPTION_TSIZE in values:
            tsize = values.get(OPTION_TSIZE)
        return new_blksize, new_timeout, tsize

    # -------------------------------------------------------- gửi / nhận gói ---
    def _target(self) -> Addr:
        return self._state.target

    def _send(self, sock: socket.socket, packet, *, retransmit: bool = False) -> None:
        self._debug(f"-> {describe(packet)}")
        raw = packet.pack()
        self.metrics.incr("packets.retransmitted" if retransmit else "packets.sent")
        self.metrics.incr("bytes.sent", len(raw))
        if isinstance(packet, DATA):
            self.metrics.incr("payload.sent", len(packet.data))
        self._sent_at = time.monotonic()
        sock.sendto(raw, self._target())

    def _recv_valid(self, sock: socket.socket, accept_ops: tuple[int, ...]):
        """Chờ một gói tin hợp lệ đúng TID của server. Trả None nếu hết thời gian chờ."""
        tid = self._state.tid
        while True:
            try:
                raw, source = sock.recvfrom(MAX_DATAGRAM)
            except TimeoutError:
                return None
            if tid is None:
                if source[0] != self._state.target[0]:
                    self.metrics.incr("packets.foreign_host")
                    self._warn(f"bỏ qua gói từ host lạ {source[0]}")
                    continue
                tid = source
                self._state.tid = tid
                self._info(f"server trả lời từ TID mới {tid[0]}:{tid[1]}")
                self._state.target = tid
            elif source != tid:
                self.metrics.incr("packets.foreign_tid")
                self._warn(f"bỏ qua gói từ TID lạ {source[0]}:{source[1]}")
                continue

            self.metrics.incr("packets.received")
            self.metrics.incr("bytes.received", len(raw))
            try:
                packet = unpack(raw)
            except tftp_errors.TftpError as exc:
                self.metrics.incr("packets.malformed")
                sock.sendto(make_error(exc.code, str(exc)), tid)
                raise

            self._debug(f"<- {describe(packet)}")
            if isinstance(packet, ERROR):
                self.metrics.incr(f"errors.received.{packet.code}")
                raise tftp_errors.TftpRemoteError(packet.code, packet.message)
            if packet.OPCODE not in accept_ops:
                self.metrics.incr("packets.unexpected")
                self._warn(f"bỏ qua {describe(packet)}, đang chờ gói khác")
                continue
            return packet

    def _exchange(self, sock: socket.socket, packet, accept_ops: tuple[int, ...], expected):
        """Gửi `packet` rồi chờ gói khớp `expected` ((opcode, block) hoặc None = bất kỳ).

        Timeout hoặc gói sai block sẽ khiến client gửi lại `packet`, tối đa
        `retries` lần cho mỗi gói tin rồi mới báo lỗi.
        """
        retries = self.retries
        stale = MAX_STALE_PACKETS
        self._send(sock, packet)
        while True:
            reply = self._recv_valid(sock, accept_ops)
            if reply is None:
                retries -= 1
                self.metrics.incr("timeouts")
                if retries < 0:
                    raise tftp_errors.TftpTimeout(
                        f"hết {self.retries} lần thử lại: không nhận được phản hồi hợp lệ "
                        f"sau {describe(packet)}"
                    )
                self._warn(
                    f"timeout ({self.timeout}s): gửi lại {describe(packet)} "
                    f"({retries} lần thử lại còn lại)"
                )
                self._send(sock, packet, retransmit=True)
                continue
            if expected is None or (reply.OPCODE, reply.block) == expected:
                self.metrics.observe("exchange.rtt", time.monotonic() - self._sent_at)
                return reply
            stale -= 1
            self.metrics.incr("packets.stale")
            if stale < 0:
                raise tftp_errors.TftpIllegalOperation(
                    f"nhận {describe(reply)} liên tục trong khi đang chờ {describe(packet)}"
                )
            self._warn(f"{describe(reply)} không mong đợi: gửi lại {describe(packet)}")
            self._send(sock, packet, retransmit=True)

    def _new_state(self) -> None:
        self._family, target = resolve_remote(self.host, self.port)
        self._state = _ClientState(tid=None, target=target)

    def _log_block(self, block: int, size: int, total: int) -> None:
        if block <= 3 or block % 50 == 0:
            self._info(f"block {block}: {size} byte (tổng {total} byte)")
        else:
            self._debug(f"block {block}: {size} byte (tổng {total} byte)")

    def _summary(self, direction: str, name: str, total: int, blocks: int, started: float) -> None:
        elapsed = max(time.monotonic() - started, 1e-6)
        self._info(
            f"hoàn tất {direction} '{name}': {total} byte / {blocks} block trong {elapsed:.2f}s "
            f"({total / elapsed / 1024:.1f} KiB/s)"
        )

    # --------------------------------------------------------------- tải về ---
    def _run_transfer(self, kind: str, action: Callable[[], str]) -> str:
        """Bọc một lượt truyền để đo thời gian, đếm kết quả và phân loại lỗi."""
        self.metrics.incr(f"transfers.started.{kind}")
        try:
            with self.metrics.span(f"transfer.duration.{kind}", fail_counter=kind):
                result = action()
        except tftp_errors.TftpTimeout:
            self.metrics.incr(f"{kind}.timeout")
            raise
        except tftp_errors.TftpRemoteError as exc:
            self.metrics.incr(f"{kind}.remote_error.{exc.code}")
            raise
        except OSError:
            self.metrics.incr(f"{kind}.os_error")
            raise
        self.metrics.incr(f"transfers.completed.{kind}")
        return result

    def download(self, remote: str, local: str, *, progress: ProgressFn | None = None) -> str:
        """Tải `remote` từ server về `local` (RRQ). Trả về đường dẫn đã lưu.

        `progress(so_byte, tong_byte)` được gọi sau mỗi DATA; `tong_byte` là
        `tsize` server báo (None nếu server không hỗ trợ option).
        """
        return self._run_transfer(
            "download", lambda: self._download(remote, local, progress=progress)
        )

    def _download(self, remote: str, local: str, *, progress: ProgressFn | None = None) -> str:
        self._new_state()
        sock = self._open_socket()
        started = time.monotonic()
        # 2.2: blksize/timeout thương lượng giữ trong biến local, không đụng self.
        cur_timeout = self.timeout
        negotiated_size: int | None = None
        self._info(
            f"RRQ {remote} -> {local} (mode={self.mode}, blksize={self.blksize}, "
            f"timeout={self.timeout}s, retries={self.retries}, "
            f"{'có' if self.use_options else 'không'} option)"
        )
        try:
            wire_blksize = DEFAULT_BLKSIZE
            request = RRQ(remote, self.mode, self._requested_options(for_upload=False))
            first = self._exchange(sock, request, (OP_OACK, OP_DATA), None)
            self.metrics.observe("transfer.ttfb", time.monotonic() - started)

            block = 0
            total = 0
            decoder = NetasciiDecoder() if self.mode == "netascii" else None
            with open(local, "wb") as handle:
                if first.OPCODE == OP_OACK:
                    wire_blksize, cur_timeout, negotiated_size = self._apply_oack(
                        first, cur_timeout
                    )
                    if not self.use_options:
                        wire_blksize = DEFAULT_BLKSIZE
                    try:
                        sock.settimeout(cur_timeout)
                    except OSError:
                        pass
                    self._info(
                        f"server chấp nhận option: blksize={wire_blksize}, timeout={cur_timeout}s"
                        + (f", tsize={negotiated_size}" if negotiated_size is not None else "")
                    )
                    packet = self._exchange(sock, ACK(0), (OP_DATA,), (OP_DATA, 1))
                else:
                    packet = first
                    wire_blksize = DEFAULT_BLKSIZE

                while True:
                    # Decoder giữ nối cặp CR LF / CR NUL bị tách giữa 2 DATA block.
                    payload = decoder.feed(packet.data) if decoder is not None else packet.data
                    handle.write(payload)
                    total += len(payload)
                    block = packet.block
                    self._log_block(block, len(payload), total)
                    if progress is not None:
                        progress(total, negotiated_size)
                    if len(packet.data) < wire_blksize:
                        self._send(sock, ACK(block))
                        break
                    packet = self._exchange(
                        sock, ACK(block), (OP_DATA,), (OP_DATA, next_block(block))
                    )
                if decoder is not None:
                    tail = decoder.flush()
                    if tail:
                        handle.write(tail)
                        total += len(tail)
            self.metrics.incr("payload.received", total)
            self._summary("tải về", remote, total, block, started)
            return local
        finally:
            sock.close()

    # -------------------------------------------------------------- tải lên ---
    def upload(self, local: str, remote: str, *, progress: ProgressFn | None = None) -> str:
        """Gửi file `local` lên server thành `remote` (WRQ)."""
        return self._run_transfer("upload", lambda: self._upload(local, remote, progress=progress))

    def _upload(self, local: str, remote: str, *, progress: ProgressFn | None = None) -> str:
        size = os.path.getsize(local)
        self._new_state()
        sock = self._open_socket()
        started = time.monotonic()
        cur_timeout = self.timeout
        self._info(
            f"WRQ {local} -> {remote} (mode={self.mode}, blksize={self.blksize}, "
            f"timeout={self.timeout}s, retries={self.retries}, {size} byte)"
        )
        try:
            wire_blksize = DEFAULT_BLKSIZE
            request = WRQ(
                remote, self.mode, self._requested_options(for_upload=True, size=size)
            )
            first = self._exchange(sock, request, (OP_OACK, OP_ACK), None)
            self.metrics.observe("transfer.ttfb", time.monotonic() - started)
            if first.OPCODE == OP_OACK:
                wire_blksize, cur_timeout, _ = self._apply_oack(first, cur_timeout)
                if not self.use_options:
                    wire_blksize = DEFAULT_BLKSIZE
                try:
                    sock.settimeout(cur_timeout)
                except OSError:
                    pass
                self._info(
                    f"server chấp nhận option: blksize={wire_blksize}, timeout={cur_timeout}s"
                )
                self._send(sock, ACK(0))
            else:
                wire_blksize = DEFAULT_BLKSIZE

            block = 0
            sent_total = 0
            with open(local, "rb") as handle:
                reader = NetasciiReader(handle, wire_blksize) if self.mode == "netascii" else None
                while True:
                    if reader is not None:
                        chunk = reader.read_chunk()
                    else:
                        chunk = handle.read(wire_blksize)
                    block = next_block(block)
                    self._exchange(sock, DATA(block, chunk), (OP_ACK,), (OP_ACK, block))
                    sent_total += len(chunk)
                    self._log_block(block, len(chunk), sent_total)
                    if progress is not None:
                        progress(sent_total, size)
                    if len(chunk) < wire_blksize:
                        break
            self._summary("gửi lên", remote, sent_total, block, started)
            return remote
        finally:
            sock.close()


# --------------------------------------------------------------------- CLI ---
def _parse_server(value: str) -> tuple[str, int]:
    """Parse `host[:port]`, hỗ trợ IPv6 `[::1]:port`. Raise ValueError nếu port xấu."""
    text = value.strip()
    if not text:
        return "127.0.0.1", DEFAULT_PORT
    if text.startswith("["):
        # Dạng [host]:port hoặc [host]
        if "]:" in text:
            host_part, _, port_part = text.rpartition("]:")
            host = host_part.lstrip("[")
            if not port_part:
                return host or "127.0.0.1", DEFAULT_PORT
            try:
                port = int(port_part)
            except ValueError:
                raise ValueError(f"port không hợp lệ: {port_part!r}") from None
            if not 1 <= port <= 65535:
                raise ValueError(f"port ngoài khoảng 1..65535: {port}")
            return host or "127.0.0.1", port
        if text.endswith("]"):
            return text[1:-1] or "127.0.0.1", DEFAULT_PORT
        raise ValueError(f"địa chỉ IPv6 không hợp lệ: {value!r}")
    if ":" in text:
        # IPv6 không ngoặc (chứa nhiều :) thì coi cả chuỗi là host.
        if text.count(":") != 1:
            return text, DEFAULT_PORT
        host, _, port_part = text.rpartition(":")
        if not port_part:
            return host or "127.0.0.1", DEFAULT_PORT
        try:
            port = int(port_part)
        except ValueError:
            raise ValueError(f"port không hợp lệ: {port_part!r}") from None
        if not 1 <= port <= 65535:
            raise ValueError(f"port ngoài khoảng 1..65535: {port}")
        return host or "127.0.0.1", port
    return text, DEFAULT_PORT


def _positive_seconds(value: str) -> float:
    """Kiểm tra --timeout > 0; 0 hoặc âm làm socket non-blocking / settimeout lỗi."""
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"không phải số: {value!r}") from None
    if seconds <= 0:
        raise argparse.ArgumentTypeError("--timeout phải > 0")
    return seconds


def _blksize_value(value: str) -> int:
    try:
        size = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"không phải số nguyên: {value!r}") from None
    if not MIN_BLKSIZE <= size <= MAX_BLKSIZE:
        raise argparse.ArgumentTypeError(
            f"--blocksize phải trong {MIN_BLKSIZE}..{MAX_BLKSIZE}"
        )
    return size


def _human_bytes(size: float) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def _human_eta(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, sec = divmod(rest, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def format_progress(done: int, total: int | None, elapsed: float, width: int = 16) -> str:
    """Một dòng tiến trình cho CLI (hàm thuần, dễ test).

    ``total=None`` (server không trả ``tsize``) thì chỉ hiện byte đã truyền và
    tốc độ, không bịa phần trăm.
    """
    done = max(0, int(done))
    speed = done / elapsed if elapsed > 0 else 0.0
    if total is not None:
        total = max(0, int(total))
        fraction = 1.0 if total == 0 else min(1.0, done / total)
        filled = int(round(fraction * width))
        bar = "█" * filled + "░" * (width - filled)
        percent = int(fraction * 100)
        remaining = max(0, total - done)
        eta = _human_eta(remaining / speed) if speed > 0 else "--:--"
        return f"[{bar}] {percent:3d}%  {_human_bytes(speed)}/s  ETA {eta}"
    return f"{_human_bytes(done)}  {_human_bytes(speed)}/s"


class ProgressPrinter:
    """Callback tiến trình in một dòng cập nhật trên stderr (tối đa ~10 lần/giây)."""

    def __init__(self, stream=None, interval: float = 0.1, width: int = 16) -> None:
        self.stream = stream if stream is not None else sys.stderr
        self.interval = max(0.0, interval)
        self.width = width
        self._started = time.monotonic()
        self._last = -1.0
        self._last_len = 0
        self._finished = False

    def __call__(self, done: int, total: int | None) -> None:
        now = time.monotonic()
        # Luôn in lần cuối (done == total) để không bỏ mất dòng 100%.
        if now - self._last < self.interval and total != done:
            return
        self._last = now
        line = format_progress(done, total, now - self._started, self.width)
        padding = " " * max(0, self._last_len - len(line))
        self.stream.write("\r" + line + padding)
        self.stream.flush()
        self._last_len = len(line)

    def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        self.stream.write("\n")
        self.stream.flush()


def build_arg_parser() -> argparse.ArgumentParser:
    from . import __version__

    parser = argparse.ArgumentParser(
        prog="python -m src.tftp_client",
        description="TFTP client theo RFC 1350: tải file (--get) hoặc gửi file (--put).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--server",
        default=f"127.0.0.1:{DEFAULT_PORT}",
        help="địa chỉ server host[:port]",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--get", metavar="REMOTE", help="tải file từ server")
    group.add_argument("--put", metavar="LOCAL", help="gửi file lên server")
    parser.add_argument(
        "--save", help="đường dẫn lưu khi dùng --get (mặc định: tên file gốc)"
    )
    parser.add_argument(
        "--remote", help="tên file trên server khi dùng --put (mặc định: tên file gốc)"
    )
    parser.add_argument(
        "--mode", default="octet", choices=("octet", "netascii"), help="chế độ truyền"
    )
    parser.add_argument(
        "--blocksize",
        type=_blksize_value,
        default=DEFAULT_BLKSIZE,
        help="kích thước khối mong muốn (byte)",
    )
    parser.add_argument(
        "--timeout", type=_positive_seconds, default=DEFAULT_TIMEOUT, help="thời gian chờ (giây)"
    )
    parser.add_argument("--retries", type=int, default=5, help="số lần thử lại tối đa")
    parser.add_argument(
        "--no-options", action="store_true", help="không thương lượng option RFC 2347"
    )
    parser.add_argument("--log-file", default=None, help="ghi log ra file")
    parser.add_argument(
        "--stats", action="store_true", help="in số liệu vận hành (metrics) khi kết thúc"
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="hiện thanh tiến trình trên stderr trong khi truyền",
    )
    parser.add_argument(
        "--stats-json", default=None, help="ghi số liệu vận hành ra file JSON khi kết thúc"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="ghi log chi tiết từng gói tin"
    )
    return parser


def validate_direction_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Bắt các cờ vô nghĩa theo chiều truyền thay vì lặng lẽ bỏ qua."""
    if args.put and args.save:
        parser.error("--save chỉ dùng với --get")
    if args.get and args.remote:
        parser.error("--remote chỉ dùng với --put")


def main(argv: list | None = None) -> int:
    enable_utf8_console()
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file:
        try:
            handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
        except OSError as exc:
            print(f"không mở được log file: {exc}", file=sys.stderr)
            return 2
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )
    try:
        host, port = _parse_server(args.server)
    except ValueError as exc:
        parser.error(str(exc))
        return 2
    validate_direction_args(parser, args)
    try:
        client = TftpClient(
            host,
            port,
            timeout=args.timeout,
            retries=args.retries,
            blksize=args.blocksize,
            mode=args.mode,
            use_options=not args.no_options,
        )
    except ValueError as exc:
        parser.error(str(exc))
        return 2
    reporter = ProgressPrinter() if args.progress else None
    try:
        if args.get:
            local = args.save or os.path.basename(args.get) or "download.out"
            client.download(args.get, local, progress=reporter)
        else:
            client.upload(args.put, args.remote or os.path.basename(args.put), progress=reporter)
    except tftp_errors.TftpError as exc:
        LOG.error("truyền thất bại: %s", exc)
        return 1
    except OSError as exc:
        LOG.error("lỗi hệ thống: %s", exc)
        return 2
    finally:
        if reporter is not None:
            reporter.finish()
        if args.stats:
            LOG.info("thống kê:\n%s", client.metrics.render())
        if args.stats_json:
            try:
                with open(args.stats_json, "w", encoding="utf-8") as handle:
                    handle.write(client.metrics.to_json(indent=2))
            except OSError as exc:
                LOG.error("không ghi được file số liệu: %s", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
