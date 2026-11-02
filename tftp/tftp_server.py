"""TFTP server đa luồng theo RFC 1350 (+ RFC 2347).

Kiến trúc:
  - Một socket "well-known" lắng nghe port cấu hình (mặc định 6969, có thể là 69).
  - Mỗi RRQ/WRQ hợp lệ tạo một thread riêng với một TID mới (port ngẫu nhiên).
  - Phiên đang chạy được đăng ký theo địa chỉ client, nên khi client gửi lại
    RRQ/WRQ (do timeout) server trả lời lại đúng từ TID cũ thay vì tạo phiên mới.

Chạy server::

    python -m tftp.tftp_server --host 0.0.0.0 --port 6969 --root files
"""

from __future__ import annotations

import argparse
import errno
import logging
import os
import random
import select
import shutil
import signal
import socket
import stat
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable

from . import DEFAULT_PORT, enable_utf8_console, tftp_errors, tftp_fs
from .tftp_config import (
    DEFAULT_MAX_TOTAL_BYTES,
    ConfigError,
    ServerConfig,
    load_config,
    validate,
)
from .tftp_logging import DEFAULT_BACKUPS, DEFAULT_MAX_BYTES, setup_logging
from .tftp_metrics import Metrics
from .tftp_net import (
    FamilyMode,
    address_key,
    family_for_host,
    format_endpoint,
    format_sockaddr,
    sockaddr_for,
    split_family_hosts,
)
from .tftp_packet import (
    ACK,
    DATA,
    DEFAULT_BLKSIZE,
    DEFAULT_TIMEOUT,
    ERROR,
    MAX_BLKSIZE,
    MAX_DATAGRAM,
    MAX_FILENAME,
    MAX_STALE_PACKETS,
    MIN_BLKSIZE,
    OACK,
    OP_ACK,
    OP_DATA,
    OPCODE_NAMES,
    OPTION_BLKSIZE,
    OPTION_NAMES,
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

LOG = logging.getLogger("tftp.server")

SESSION_REPLAY = "replay"
SESSION_TIMEOUT = "timeout"

DEFAULT_SERVER_BLKSIZE = 1468
MIN_SESSION_TIMEOUT = 0.05
"""Sàn cho timeout cục bộ: `select(..., 0)` quay vòng không nghỉ và nuốt gói."""

DEFAULT_MAX_FILE_SIZE = 256 * 1024 * 1024
"""Giới hạn upload mặc định (256 MiB) — đủ cho firmware/ảnh, không để đầy đĩa."""

DEFAULT_MIN_FREE_BYTES = 16 * 1024 * 1024
"""Chừa lại tối thiểu 16 MiB trống trước khi nhận thêm dữ liệu."""

DEFAULT_TID_PORT_RANGE: tuple[int, int] | None = None
"""None = để OS cấp port TID tạm; production nên đặt dải cố định cho firewall."""

FINAL_ACK_DALLY = 1.0
"""Cửa sổ giữ TID sau ACK cuối của WRQ để nhận lại DATA cuối bị gửi lặp
(RFC 1350 §5). Bị cắt thêm bởi `timeout` đã thương lượng; đặt 0 để tắt."""

_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul", "clock$"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def is_servable_name(filename: str) -> bool:
    """True nếu `filename` là tên file TFTP có thể phục vụ (không xét đường dẫn).

    Tên phải là ASCII (RFC 1350 chỉ mang ASCII trên dây), không chứa ký tự
    điều khiển / ký tự cấm của Windows, không phải tên thiết bị reserved, không
    phải file ẩn hay file tạm ``.part``.

    Dùng chung cho :meth:`TftpServer.resolve_path` và GUI để bảng file trong GUI
    chỉ hiện những tên thật sự tải được — trước đây GUI liệt kê cả file có dấu
    tiếng Việt hay file bắt đầu bằng ``.``, bấm vào thì nhận lỗi khó hiểu.
    """
    if not filename or len(filename) > MAX_FILENAME:
        return False
    if filename in (".", "..") or os.path.isabs(filename):
        return False
    # RFC 1350 §5: filename là chuỗi ASCII trên dây; `RRQ.pack()` từ chối ký tự
    # ngoài ASCII nên tên không-ASCII chắc chắn không tải được.
    if not filename.isascii():
        return False
    for ch in filename:
        code = ord(ch)
        if code < 32 or code == 127:
            return False
    # 4.2: ký tự cấm của Windows (kèm `:` để chặn alternate data stream).
    if any(ch in filename for ch in '<>:"|?*'):
        return False
    base = filename.replace("\\", "/").split("/")[-1]
    if not base or base in (".", ".."):
        return False
    if base.endswith((" ", ".")):
        return False
    # 3.2: không phục vụ file ẩn/tạm; tránh RRQ đọc .part, log hoặc file cấu
    # hình đặt nhầm trong root.
    if base.startswith(".") or base.lower().endswith(".part"):
        return False
    if base.split(".")[0].lower() in _WINDOWS_RESERVED:
        return False
    return True

# Giữ alias để code cũ vẫn import được.
__all__ = ["TftpServer", "Session", "MAX_STALE_PACKETS"]


def _io_error_code(exc: OSError) -> int:
    """Chọn mã ERROR RFC 1350 theo errno thay vì luôn báo "disk full"."""
    if exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)):
        return tftp_errors.ERR_DISK_FULL
    if exc.errno in (errno.EACCES, errno.EPERM):
        return tftp_errors.ERR_ACCESS_VIOLATION
    return tftp_errors.ERR_NOT_DEFINED


def _hold_port_exclusively(sock: socket.socket) -> None:
    """Giữ cổng UDP độc quyền để bind trùng bị từ chối thay vì chia gói tin.

    Trên Windows, ``SO_REUSEADDR`` (cờ hay được copy từ ví dụ TCP) cho phép một
    process khác bind đúng cổng UDP đang dùng và gói tin rơi không xác định vào
    một trong hai — server không bao giờ báo "cổng đã dùng". ``SO_EXCLUSIVEADDRUSE``
    mới là cờ đúng để giữ cổng. Trên POSIX, UDP không có TIME_WAIT nên không cần
    ``SO_REUSEADDR``; để trống thì bind trùng trả ``EADDRINUSE`` rõ ràng.
    """
    if os.name != "nt":
        return
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if exclusive is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
    except OSError:
        pass


class Session(threading.Thread):
    """Một phiên truyền file: thread riêng + socket riêng (TID riêng)."""

    def __init__(
        self,
        server: TftpServer,
        client_addr: tuple,
        request: RRQ | WRQ,
        *,
        family: int,
        local_addr: tuple,
    ) -> None:
        super().__init__(name=f"tftp-{format_sockaddr(client_addr)}", daemon=True)
        self.server = server
        self.log = server.log
        self.metrics = server.metrics
        self.client_addr = client_addr
        self.client_key = address_key(client_addr)
        self.family = family
        self.local_addr = local_addr
        self.request = request
        self.blksize = DEFAULT_BLKSIZE
        self.timeout = server.timeout
        self.retries = server.retries
        self._closed = threading.Event()
        self._notify_r: socket.socket | None = None
        self._notify_w: socket.socket | None = None
        self.sock: socket.socket | None = None
        self.local_port = 0
        self._opened_at = time.monotonic()
        self._first_packet_at: float | None = None
        try:
            self._notify_r, self._notify_w = socket.socketpair()
            self.sock = socket.socket(family, socket.SOCK_DGRAM)
            if family == socket.AF_INET6:
                try:
                    self.sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                except OSError:
                    pass
            self.local_port = server._bind_session_socket(self.sock, family, local_addr)
        except Exception:
            for sock in (self.sock, self._notify_r, self._notify_w):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            self.sock = None
            raise

    # ------------------------------------------------------------------ vòng đời ---
    def run(self) -> None:
        kind = "RRQ" if isinstance(self.request, RRQ) else "WRQ"
        self.metrics.incr(f"sessions.started.{kind}")
        self.metrics.add_gauge("sessions.active", 1)
        self.log.info(
            "[%s] phiên mới từ %s, TID=%s, file=%s, family=%s",
            kind,
            format_sockaddr(self.client_addr),
            self.local_port,
            self.request.filename,
            "IPv6" if self.family == socket.AF_INET6 else "IPv4",
        )
        outcome = "ok"
        try:
            with self.metrics.span("session.duration", fail_counter="sessions") as span:
                if isinstance(self.request, RRQ):
                    self._serve_read()
                else:
                    self._serve_write()
                span.tags["blksize"] = str(self.blksize)
        except tftp_errors.TftpTimeout as exc:
            outcome = "timeout"
            self.metrics.incr("sessions.timeout")
            self.log.warning("[%s] hết thời gian chờ: %s", self.request.filename, exc)
        except tftp_errors.TftpRemoteError as exc:
            outcome = "remote_error"
            self.log.warning("[%s] client báo lỗi: %s", self.request.filename, exc)
        except tftp_errors.TftpError as exc:
            outcome = "error"
            self.metrics.incr("sessions.error")
            self.log.warning("[%s] kết thúc do lỗi: %s", self.request.filename, exc)
        except OSError as exc:
            outcome = "os_error"
            self.metrics.incr("sessions.os_error")
            self.log.warning("[%s] lỗi socket: %s", self.request.filename, exc)
        finally:
            self.metrics.incr(f"sessions.finished.{outcome}")
            self.metrics.add_gauge("sessions.active", -1)
            self.metrics.observe(
                "session.ttfb", (self._first_packet_at or time.monotonic()) - self._opened_at
            )
            self.close()
            self.server.session_finished(self)
            self.log.info(
                "[%s] đóng phiên %s (TID %s)",
                self.request.filename,
                format_sockaddr(self.client_addr),
                self.local_port,
            )

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        for sock in (self.sock, self._notify_r, self._notify_w):
            if sock is None:
                continue
            try:
                sock.close()
            except OSError:
                pass

    def shutdown_with_error(self, message: str = "server đang dừng") -> None:
        """Gửi ERROR cho client trước khi đóng (graceful shutdown)."""
        try:
            self._send_error(tftp_errors.ERR_NOT_DEFINED, message)
        except tftp_errors.TftpError:
            pass
        except OSError:
            pass
        self.close()

    def matches(self, request: RRQ | WRQ) -> bool:
        """True nếu `request` mới là lần gửi lại của cùng phiên.

        So khớp opcode + filename + mode + options để tránh trộn dữ liệu
        khi OS cấp lại cùng ephemeral port cho client khác (REVIEW 1.2).
        """
        if type(request) is not type(self.request):
            return False
        old = self.request
        return (
            request.filename == old.filename
            and request.mode == old.mode
            and dict(request.options or {}) == dict(old.options or {})
        )

    def replay(self) -> None:
        """Báo phiên gửi lại gói đang chờ khi client lặp lại RRQ/WRQ."""
        if self._notify_w is None:
            return
        try:
            self._notify_w.send(b"\x01")
        except OSError:
            pass

    # ------------------------------------------------------------------- gửi/nhận ---
    def _send(self, packet, *, retransmit: bool = False) -> None:
        if self.sock is None:
            raise tftp_errors.TftpAccessViolation("phiên đã đóng")
        raw = packet.pack()
        self.log.debug("-> %s", describe(packet))
        if self._first_packet_at is None:
            self._first_packet_at = time.monotonic()
        self.metrics.incr("packets.retransmitted" if retransmit else "packets.sent")
        self.metrics.incr("bytes.sent", len(raw))
        if isinstance(packet, DATA):
            self.metrics.incr("payload.sent", len(packet.data))
        try:
            self.sock.sendto(raw, self.client_addr)
        except OSError as exc:
            raise tftp_errors.TftpAccessViolation(f"không gửi được tới client: {exc}") from exc

    def _send_error(self, code: int, message: str) -> None:
        self.metrics.incr(f"errors.sent.{code}")
        if self.sock is None:
            return
        self.log.info("báo lỗi %d '%s' cho %s", code, message, self.client_addr)
        try:
            self.sock.sendto(make_error(code, message), self.client_addr)
        except OSError:
            pass

    def _recv(self, accept_ops: tuple[int, ...]):
        """Chờ một gói tin hợp lệ. Trả về packet, SESSION_REPLAY hoặc SESSION_TIMEOUT."""
        if self.sock is None or self._notify_r is None:
            return SESSION_TIMEOUT
        while True:
            try:
                readable, _, _ = select.select([self.sock, self._notify_r], [], [], self.timeout)
            except (OSError, ValueError):
                return SESSION_TIMEOUT
            if self._notify_r in readable:
                try:
                    self._notify_r.recv(64)
                except OSError:
                    pass
                self.metrics.incr("requests.replayed")
                return SESSION_REPLAY
            if self.sock not in readable:
                return SESSION_TIMEOUT
            try:
                raw, source = self.sock.recvfrom(MAX_DATAGRAM)
            except TimeoutError:
                return SESSION_TIMEOUT
            except OSError:
                return SESSION_TIMEOUT
            if address_key(source) != self.client_key:
                self.metrics.incr("packets.foreign_tid")
                self.log.warning("bỏ qua gói từ TID không xác định %s", format_sockaddr(source))
                continue
            self.metrics.incr("packets.received")
            self.metrics.incr("bytes.received", len(raw))
            try:
                packet = unpack(raw)
            except tftp_errors.TftpError as exc:
                self.metrics.incr("packets.malformed")
                self._send_error(exc.code, str(exc))
                continue
            self.log.debug("<- %s", describe(packet))
            if isinstance(packet, ERROR):
                raise tftp_errors.TftpRemoteError(packet.code, packet.message)
            if packet.OPCODE not in accept_ops:
                waiting = "/".join(describe_of(op) for op in accept_ops)
                self.metrics.incr("packets.unexpected")
                self.log.warning("bỏ qua %s, đang chờ %s", describe(packet), waiting)
                continue
            return packet

    def _exchange(
        self,
        pending,
        pending_block: int,
        accept_ops: tuple[int, ...],
        expected: tuple[tuple[int, int], ...],
        send: bool = True,
        timeout_fatal: bool = True,
    ):
        """Gửi `pending`, chờ gói khớp `expected`; gửi lại `pending` khi timeout.

        `expected` là tập (opcode, block) được coi là tiến triển hợp lệ, ví dụ
        ((OP_ACK, 0),) sau OACK hoặc ((OP_DATA, 5),) ở giữa luồng.
        `send=False` dùng cho pha mà server chỉ chờ client gửi (sau OACK trong
        WRQ), `timeout_fatal=False` để timeout chỉ được tính vào ngân sách chờ.
        """
        retries = self.retries
        stale = MAX_STALE_PACKETS
        # `sent_at` phải luôn là một mốc thật: khi send=False (pha chỉ chờ
        # client gửi, ví dụ sau OACK của WRQ) trước đây nó giữ 0.0 và
        # `time.monotonic() - 0.0` ghi thẳng uptime của process vào timer
        # `exchange.rtt`, làm p95/p99 RTT sai hàng trăm giây.
        if send:
            self._send(pending)
        sent_at = time.monotonic()
        while True:
            packet = self._recv(accept_ops)
            if packet is SESSION_REPLAY:
                if send:
                    self.log.info("client lặp lại request: gửi lại %s", describe(pending))
                    self._send(pending, retransmit=True)
                    sent_at = time.monotonic()
                continue
            if packet is SESSION_TIMEOUT:
                retries -= 1
                stale -= 1
                self.metrics.incr("timeouts")
                if not timeout_fatal:
                    if stale < 0:
                        raise tftp_errors.TftpTimeout("client im lặng quá lâu")
                    continue
                if retries < 0:
                    raise tftp_errors.TftpTimeout(
                        f"hết {self.retries} lần thử lại khi chờ "
                        + " hoặc ".join(describe_of(op) for op, _ in expected)
                    )
                self.log.warning("timeout: gửi lại %s (còn %d lần)", describe(pending), retries)
                self._send(pending, retransmit=True)
                sent_at = time.monotonic()
                continue
            if (packet.OPCODE, packet.block) in expected:
                self.metrics.observe("exchange.rtt", time.monotonic() - sent_at)
                return packet
            stale -= 1
            self.metrics.incr("packets.stale")
            if stale < 0:
                raise tftp_errors.TftpIllegalOperation(
                    f"nhận {describe(packet)} liên tục trong khi đang chờ "
                    + " hoặc ".join(describe_of(op) for op, _ in expected)
                )
            self.log.warning("%s không đúng block, gửi lại %s", describe(packet), describe(pending))
            if send:
                self._send(pending, retransmit=True)
                sent_at = time.monotonic()

    # -------------------------------------------------------------- thương lượng ---
    def _negotiate(self, requested: dict[str, str], size: int | None) -> dict[str, str]:
        accepted = self.server.negotiate_options(requested, size)
        if not accepted:
            # Không gửi OACK nghĩa là cả hai bên dùng blksize mặc định 512 của
            # RFC 1350, bất kể `--blksize` của server và giá trị client đề nghị.
            self.blksize = DEFAULT_BLKSIZE
            return {}
        values = parse_options(OACK(options=accepted))
        if OPTION_BLKSIZE in values:
            self.blksize = values[OPTION_BLKSIZE]
        if OPTION_TIMEOUT in values:
            self.timeout = max(MIN_SESSION_TIMEOUT, float(clamp_timeout(values[OPTION_TIMEOUT])))
        return accepted

    # -------------------------------------------------------------- RRQ: server gửi ---
    def _serve_read(self) -> None:
        assert isinstance(self.request, RRQ)
        request = self.request
        path = self.server.resolve_path(request.filename)
        if path is None:
            self.metrics.incr("requests.bad_filename")
            self._send_error(tftp_errors.ERR_ACCESS_VIOLATION, "tên file không hợp lệ")
            return
        try:
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            if nofollow:
                flags |= nofollow
            fd = os.open(path, flags)
            handle = os.fdopen(fd, "rb")
        except FileNotFoundError:
            self._send_error(
                tftp_errors.ERR_FILE_NOT_FOUND, f"không tìm thấy file '{request.filename}'"
            )
            return
        except PermissionError:
            self._send_error(
                tftp_errors.ERR_ACCESS_VIOLATION, f"không có quyền đọc '{request.filename}'"
            )
            return
        except OSError as exc:
            self._send_error(_io_error_code(exc), str(exc))
            return

        with handle:
            try:
                total = os.fstat(handle.fileno()).st_size
            except OSError as exc:
                self._send_error(_io_error_code(exc), str(exc))
                return
            self.log.info(
                "bắt đầu gửi '%s' (%d byte) cho %s",
                request.filename,
                total,
                format_sockaddr(self.client_addr),
            )
            try:
                accepted = self._negotiate(request.options, total)
            except tftp_errors.TftpError as exc:
                self._send_error(exc.code, str(exc))
                return
            started = time.monotonic()
            deadline = (
                started + self.server.max_transfer_seconds
                if self.server.max_transfer_seconds is not None
                else None
            )
            net_reader = (
                NetasciiReader(handle, self.blksize) if request.mode == "netascii" else None
            )

            def read_fn() -> bytes:
                try:
                    if net_reader is not None:
                        return net_reader.read_chunk()
                    return handle.read(self.blksize)
                except OSError as exc:
                    raise tftp_errors.TftpAccessViolation(f"đọc file thất bại: {exc}") from exc

            try:
                if accepted:
                    pending: object = OACK(accepted)
                    pending_block = 0
                    expect_block = 0
                    data_started = False
                    pending_final = False
                else:
                    chunk = read_fn()
                    pending = DATA(1, chunk)
                    pending_block = 1
                    expect_block = 1
                    data_started = True
                    pending_final = len(chunk) < self.blksize

                while True:
                    if deadline is not None and time.monotonic() > deadline:
                        self.metrics.incr("sessions.transfer_timeout")
                        self._send_error(
                            tftp_errors.ERR_NOT_DEFINED,
                            "vượt quá thời gian tổng cho phép của transfer",
                        )
                        return
                    self._exchange(pending, pending_block, (OP_ACK,), ((OP_ACK, expect_block),))
                    if not data_started:
                        data_started = True
                        chunk = read_fn()
                        pending = DATA(1, chunk)
                        pending_block = 1
                        expect_block = 1
                        pending_final = len(chunk) < self.blksize
                        continue
                    if pending_final:
                        elapsed = max(time.monotonic() - started, 1e-6)
                        self.metrics.incr("transfers.completed.RRQ")
                        self.log.info(
                            "gửi xong '%s' (%d byte) cho %s",
                            request.filename,
                            total,
                            format_sockaddr(self.client_addr),
                        )
                        self.log.debug(
                            "tốc độ %.1f KiB/s", total / elapsed / 1024
                        )
                        return
                    chunk = read_fn()
                    expect_block = next_block(expect_block)
                    pending = DATA(expect_block, chunk)
                    pending_block = expect_block
                    pending_final = len(chunk) < self.blksize
                    if expect_block % 100 == 0:
                        self.log.info("đã gửi %d block", expect_block)
            except tftp_errors.TftpError as exc:
                # _exchange đã timeout / lỗi; run() sẽ log. Nếu là lỗi đọc file,
                # cố gửi ERROR cho client trước khi thoát.
                if isinstance(exc, tftp_errors.TftpAccessViolation) and "đọc file" in str(exc):
                    try:
                        self._send_error(exc.code, str(exc))
                    except OSError:
                        pass
                raise

    def _dally_final_ack(self, block: int) -> None:
        """Giữ TID thêm một nhịp sau khi gửi ACK cuối của WRQ.

        RFC 1350 §5: nếu ACK cuối bị mất, client sẽ gửi lại DATA cuối và bên
        nhận "phải ACK lại". Không có cửa sổ này, TID đóng ngay, gói lặp rơi
        vào cổng không tồn tại và client phải chờ hết `retries` (mặc định
        5 × 5s) rồi báo lỗi — dù file đã lưu xong trên server.

        Cửa sổ bị cắt bởi `timeout` đã thương lượng (client cũng dùng đúng giá
        trị đó để retransmit) và bởi `server.final_ack_dally`.
        """
        grace = min(self.timeout, float(self.server.final_ack_dally))
        if grace <= 0:
            return
        if self.sock is None or self._notify_r is None:
            return
        deadline = time.monotonic() + grace
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                readable, _, _ = select.select(
                    [self.sock, self._notify_r], [], [], remaining
                )
            except (OSError, ValueError):
                return
            if not readable or self._notify_r in readable:
                return
            try:
                raw, source = self.sock.recvfrom(MAX_DATAGRAM)
            except OSError:
                return
            if address_key(source) != self.client_key:
                continue
            try:
                packet = unpack(raw)
            except tftp_errors.TftpError:
                continue
            if isinstance(packet, DATA) and packet.block == block:
                self.log.debug("nhận lại DATA cuối block %d, ACK lại", block)
                try:
                    self._send(ACK(block), retransmit=True)
                except tftp_errors.TftpError:
                    return

    def _read_chunk(self, handle, mode: str) -> bytes:
        """Đọc 1 chunk octet (giữ lại để tương thích). Netascii dùng NetasciiReader."""
        if mode == "netascii":
            return NetasciiReader(handle, self.blksize).read_chunk()
        return handle.read(self.blksize)

    # -------------------------------------------------------------- WRQ: server nhận ---
    def _serve_write(self) -> None:
        assert isinstance(self.request, WRQ)
        request = self.request
        path = self.server.resolve_path(request.filename)
        if path is None:
            self.metrics.incr("requests.bad_filename")
            self._send_error(tftp_errors.ERR_ACCESS_VIOLATION, "tên file không hợp lệ")
            return
        if not self.server.allow_overwrite and os.path.exists(path):
            self.metrics.incr("requests.refused_overwrite")
            self._send_error(tftp_errors.ERR_FILE_EXISTS, f"file '{request.filename}' đã tồn tại")
            return

        # 2.4: chống 2 WRQ đồng thời cùng tên (last-write-wins trong im lặng).
        if not self.server.try_acquire_write(path):
            self.metrics.incr("requests.locked")
            self._send_error(
                tftp_errors.ERR_FILE_EXISTS,
                f"file '{request.filename}' đang được ghi bởi phiên khác",
            )
            return

        self.metrics.add_gauge("writes.active", 1)
        try:
            directory = os.path.dirname(path) or "."
            if not os.path.isdir(directory):
                self._send_error(tftp_errors.ERR_ACCESS_VIOLATION, "thư mục đích không tồn tại")
                return

            # 1.3 + 3.2: client WRQ gửi tsize=<kích thước thật>, dùng để chặn sớm.
            offered_size: int | None = None
            try:
                vals = parse_options(WRQ(request.filename, request.mode, request.options))
                offered_size = vals.get(OPTION_TSIZE)
            except tftp_errors.TftpError:
                offered_size = None
            if (
                offered_size is not None
                and self.server.max_file_size is not None
                and offered_size > self.server.max_file_size
            ):
                self.metrics.incr("requests.oversized")
                self._send_error(
                    tftp_errors.ERR_DISK_FULL,
                    f"file {offered_size} byte vượt giới hạn {self.server.max_file_size} byte",
                )
                return
            ok, reason = self.server._check_disk_budget(
                directory, offered_size or 0, reserve_path=path
            )
            if not ok:
                self.metrics.incr("requests.disk_limited")
                self._send_error(tftp_errors.ERR_DISK_FULL, reason)
                return

            try:
                accepted = self._negotiate(request.options, offered_size)
            except tftp_errors.TftpError as exc:
                self._send_error(exc.code, str(exc))
                return
            try:
                fd, temp_path = tempfile.mkstemp(
                    prefix=f".{os.path.basename(path)}.", suffix=".part", dir=directory
                )
                handle = os.fdopen(fd, "wb")
            except PermissionError:
                self._send_error(tftp_errors.ERR_ACCESS_VIOLATION, "không có quyền ghi")
                return
            except OSError as exc:
                self._send_error(_io_error_code(exc), str(exc))
                return
            received = 0
            started = time.monotonic()
            deadline = (
                started + self.server.max_transfer_seconds
                if self.server.max_transfer_seconds is not None
                else None
            )
            last_budget_check = started
            final_ack = None
            decoder = NetasciiDecoder() if request.mode == "netascii" else None
            try:
                with handle:
                    pending: object = ACK(0)
                    pending_block = 0
                    expect_block = 1
                    send_first = not accepted
                    # RFC 2347: sau OACK của WRQ, client gửi thẳng DATA 1 (OACK
                    # đã thay cho ACK 0). Vì vậy DATA 1 có thể tới ngay trong
                    # lần `_exchange` chờ phản hồi OACK; đó là dữ liệu thật,
                    # phải giữ lại để xử lý chứ không được bỏ — nếu bỏ, client
                    # phải chờ hết `timeout` rồi gửi lại (bug âm thầm: file vẫn
                    # đúng nhưng mỗi lượt upload chậm thêm đúng một timeout).
                    first_data: DATA | None = None
                    if accepted:
                        reply = self._exchange(
                            OACK(accepted),
                            0,
                            (OP_ACK, OP_DATA),
                            ((OP_ACK, 0), (OP_DATA, 1)),
                        )
                        if isinstance(reply, DATA):
                            first_data = reply
                            send_first = True

                    while True:
                        if deadline is not None and time.monotonic() > deadline:
                            self.metrics.incr("sessions.transfer_timeout")
                            self._send_error(
                                tftp_errors.ERR_NOT_DEFINED,
                                "vượt quá thời gian tổng cho phép của transfer",
                            )
                            return
                        if first_data is not None:
                            data_packet = first_data
                            first_data = None
                        else:
                            data_packet = self._exchange(
                                pending,
                                pending_block,
                                (OP_DATA, OP_ACK),
                                ((OP_DATA, expect_block),),
                                send=send_first,
                            )
                            send_first = True
                        assert isinstance(data_packet, DATA)
                        wire_len = len(data_packet.data)
                        if wire_len > self.blksize:
                            # Client vượt blksize đã thương lượng: block sau có
                            # thể bị coi là "khối cuối" và cắt cụt file (xem
                            # docs/RFC-1350-notes.md §3). Chặn ngay.
                            self.metrics.incr("packets.oversized_data")
                            self._send_error(
                                tftp_errors.ERR_ILLEGAL_OPERATION,
                                f"DATA {wire_len} byte vượt quá blksize {self.blksize}",
                            )
                            return
                        # Decoder giữ nối cặp CR LF / CR NUL bị tách giữa 2 DATA block.
                        payload = (
                            decoder.feed(data_packet.data)
                            if decoder is not None
                            else data_packet.data
                        )
                        try:
                            handle.write(payload)
                        except OSError as exc:
                            self._send_error(
                                _io_error_code(exc), f"ghi file thất bại: {exc}"
                            )
                            return
                        received += len(payload)
                        if (
                            self.server.max_file_size is not None
                            and received > self.server.max_file_size
                        ):
                            self.metrics.incr("requests.oversized")
                            self._send_error(
                                tftp_errors.ERR_DISK_FULL,
                                f"file vượt giới hạn {self.server.max_file_size} byte",
                            )
                            return
                        if offered_size is not None:
                            self.server._set_quota_reservation(
                                path, max(0, offered_size - received)
                            )
                        now = time.monotonic()
                        if now - last_budget_check >= 1.0:
                            last_budget_check = now
                            ok, reason = self.server._check_disk_budget(
                                directory, 0, protected_path=path
                            )
                            if not ok:
                                self.metrics.incr("requests.disk_limited")
                                self._send_error(tftp_errors.ERR_DISK_FULL, reason)
                                return
                        if data_packet.block <= 3 or data_packet.block % 100 == 0:
                            self.log.info(
                                "nhận block %d: %d byte (tổng %d)",
                                data_packet.block,
                                len(payload),
                                received,
                            )
                        else:
                            self.log.debug(
                                "nhận block %d: %d byte (tổng %d)",
                                data_packet.block,
                                len(payload),
                                received,
                            )
                        pending = ACK(data_packet.block)
                        pending_block = data_packet.block
                        # 1.4: kết thúc dựa trên độ dài trên dây, không phải sau convert.
                        if wire_len < self.blksize:
                            final_ack = pending
                            break
                        expect_block = next_block(data_packet.block)

                    if decoder is not None:
                        tail = decoder.flush()
                        if tail:
                            handle.write(tail)
                            received += len(tail)
                    self.server._set_quota_reservation(path, 0)
                    handle.flush()
                    os.fsync(handle.fileno())

                ok, reason = self.server._check_disk_budget(
                    directory, 0, protected_path=path
                )
                if not ok:
                    self.metrics.incr("requests.disk_limited")
                    self._send_error(tftp_errors.ERR_DISK_FULL, reason)
                    return

                try:
                    if self.server.allow_overwrite:
                        os.replace(temp_path, path)
                    elif not tftp_fs.commit_without_overwrite(temp_path, path):
                        self.metrics.incr("requests.refused_overwrite")
                        self._send_error(
                            tftp_errors.ERR_FILE_EXISTS,
                            f"file '{request.filename}' đã tồn tại",
                        )
                        return
                except OSError as exc:
                    try:
                        os.remove(temp_path)
                    except FileNotFoundError:
                        pass
                    # Mã lỗi phải theo errno: trước đây mọi lỗi ghi file đều bị
                    # báo là "disk full" (ERROR 3), kể cả lỗi quyền hay
                    # filesystem không hỗ trợ hard link.
                    code = _io_error_code(exc)
                    self._send_error(code, f"không lưu được file: {exc}")
                    raise tftp_errors.TftpError(
                        f"không lưu được file: {exc}", code=code
                    ) from exc
                elapsed = max(time.monotonic() - started, 1e-6)
                self.metrics.incr("payload.received", received)
                self.metrics.incr("transfers.completed.WRQ")
                self.log.info(
                    "lưu xong '%s' (%d byte, %.1f KiB/s) từ %s",
                    request.filename,
                    received,
                    received / elapsed / 1024,
                    format_sockaddr(self.client_addr),
                )
                if final_ack is not None:
                    self._send(final_ack)
                    self._dally_final_ack(final_ack.block)
            finally:
                try:
                    handle.close()
                except OSError:
                    pass
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
        finally:
            self.server.release_write(path)
            self.metrics.add_gauge("writes.active", -1)


def describe_of(opcode: int) -> str:
    return OPCODE_NAMES.get(opcode, f"opcode {opcode}")


class TftpServer:
    """Server TFTP: lắng nghe request trên well-known port, mở thread cho mỗi phiên."""

    _WINDOWS_RESERVED = _WINDOWS_RESERVED
    """Giữ alias cấp class cho code cũ; nguồn sự thật là module-level."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        *,
        root: str = "files",
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = 5,
        blksize: int = DEFAULT_SERVER_BLKSIZE,
        allow_overwrite: bool = False,
        enable_options: bool = True,
        max_clients: int = 32,
        max_file_size: int | None = DEFAULT_MAX_FILE_SIZE,
        allow_subdirs: bool = False,
        max_requests_per_second: int = 100,
        family: FamilyMode = "auto",
        host6: str | None = None,
        tid_port_range: tuple[int, int] | None = DEFAULT_TID_PORT_RANGE,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
        max_total_bytes: int | None = DEFAULT_MAX_TOTAL_BYTES,
        evict_oldest: bool = False,
        max_transfer_seconds: float | None = 3600.0,
        final_ack_dally: float = FINAL_ACK_DALLY,
        cleanup_temp_age: float = 24 * 3600,
        stats_interval: float = 0.0,
        metrics: Metrics | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if family not in ("auto", "ipv4", "ipv6", "dual"):
            raise ValueError(f"family không hợp lệ: {family!r}")
        if port < 0 or port > 65535:
            raise ValueError("port phải trong khoảng 0..65535")
        if timeout <= 0:
            raise ValueError("timeout phải lớn hơn 0")
        if retries < 1:
            raise ValueError("retries phải >= 1")
        if not MIN_BLKSIZE <= blksize <= MAX_BLKSIZE:
            raise ValueError(f"blksize phải trong khoảng {MIN_BLKSIZE}..{MAX_BLKSIZE}")
        if max_clients < 1:
            raise ValueError("max_clients phải >= 1")
        if max_file_size is not None and max_file_size <= 0:
            raise ValueError("max_file_size phải > 0 hoặc None")
        if max_requests_per_second < 1:
            raise ValueError("max_requests_per_second phải >= 1")
        if min_free_bytes < 0:
            raise ValueError("min_free_bytes phải >= 0")
        if max_total_bytes is not None and max_total_bytes <= 0:
            raise ValueError("max_total_bytes phải > 0 hoặc None")
        if max_transfer_seconds is not None and max_transfer_seconds <= 0:
            raise ValueError("max_transfer_seconds phải > 0 hoặc None")
        if final_ack_dally < 0:
            raise ValueError("final_ack_dally phải >= 0")
        if tid_port_range is not None:
            low, high = tid_port_range
            if not (1 <= low <= high <= 65535):
                raise ValueError("tid_port_range phải là (low, high) trong 1..65535")
        self.host = host
        self.host6 = host6
        self.family = family_for_host(host, family)
        self.port = port
        self.root = os.path.realpath(os.path.abspath(root))
        self.timeout = max(MIN_SESSION_TIMEOUT, float(timeout))
        self.retries = retries
        self.blksize = blksize
        self.allow_overwrite = allow_overwrite
        self.enable_options = enable_options
        self.max_clients = max_clients
        self.max_file_size = max_file_size
        self.allow_subdirs = allow_subdirs
        self.max_requests_per_second = max_requests_per_second
        self.tid_port_range = tid_port_range
        self.min_free_bytes = min_free_bytes
        self.max_total_bytes = max_total_bytes
        self.evict_oldest = evict_oldest
        self.max_transfer_seconds = max_transfer_seconds
        self.final_ack_dally = final_ack_dally
        self.cleanup_temp_age = cleanup_temp_age
        self.log = logger or LOG
        self.metrics = metrics if metrics is not None else Metrics("tftp.server")
        self.stats_interval = max(0.0, stats_interval)
        self._last_stats = time.monotonic()
        self._sessions: dict[tuple[str, int, int], Session] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.sock: socket.socket | None = None
        self._listeners: list[tuple[socket.socket, int, str]] = []
        self.bound_port = port
        self.bound_addresses: list[str] = []
        self._active_writes: set[str] = set()
        self._quota_lock = threading.Lock()
        self._quota_reservations: dict[str, int] = {}
        self._request_times: deque[float] = deque()
        os.makedirs(self.root, exist_ok=True)
        self._cleanup_temp_files()

    # ------------------------------------------------------------------ tiện ích ---
    def session_finished(self, session: Session) -> None:
        with self._lock:
            if self._sessions.get(session.client_key) is session:
                del self._sessions[session.client_key]

    def try_acquire_write(self, path: str) -> bool:
        """Giữ lock theo tên file khi WRQ (2.4). False = đang có phiên khác ghi."""
        key = os.path.normcase(os.path.abspath(path))
        with self._quota_lock:
            with self._lock:
                if key in self._active_writes:
                    return False
                self._active_writes.add(key)
                return True

    def release_write(self, path: str) -> None:
        key = os.path.normcase(os.path.abspath(path))
        with self._quota_lock:
            with self._lock:
                self._active_writes.discard(key)
            self._quota_reservations.pop(key, None)

    def _set_quota_reservation(self, path: str, size: int) -> None:
        key = os.path.normcase(os.path.abspath(path))
        with self._quota_lock:
            self._quota_reservations[key] = size

    def _check_rate_limit(self) -> bool:
        """True nếu cho phép request mới, False nếu vượt giới hạn/s (4.3)."""
        now = time.monotonic()
        with self._lock:
            while self._request_times and now - self._request_times[0] > 1.0:
                self._request_times.popleft()
            if len(self._request_times) >= self.max_requests_per_second:
                return False
            self._request_times.append(now)
            return True

    # -------------------------------------------------------- tài nguyên/đĩa ---
    def _bind_session_socket(self, sock: socket.socket, family: int, local_addr: tuple) -> int:
        """Bind socket TID cùng family và trong dải port cấu hình (nếu có)."""
        bind_host = self._wildcard_for(family, local_addr)
        scope_id = 0
        if family == socket.AF_INET6 and local_addr and len(local_addr) > 3:
            scope_id = int(local_addr[3] or 0)
        base_addr: tuple
        if family == socket.AF_INET6:
            base_addr = (bind_host, 0, 0, scope_id)
        else:
            base_addr = (bind_host, 0)
        if self.tid_port_range is None:
            sock.bind(base_addr)
            return int(sock.getsockname()[1])

        low, high = self.tid_port_range
        ports = list(range(low, high + 1))
        random.shuffle(ports)
        last_error: OSError | None = None
        for candidate_port in ports:
            try:
                if family == socket.AF_INET6:
                    sock.bind((bind_host, candidate_port, 0, scope_id))
                else:
                    sock.bind((bind_host, candidate_port))
                return candidate_port
            except OSError as exc:
                last_error = exc
        raise OSError(f"cạn port TID trong dải {low}..{high}: {last_error}") from last_error

    @staticmethod
    def _wildcard_for(family: int, local_addr: tuple) -> str:
        """Giữ địa chỉ cụ thể nếu listener bind cụ thể; ngược lại dùng wildcard."""
        if not local_addr:
            return "::" if family == socket.AF_INET6 else "0.0.0.0"
        host = str(local_addr[0])
        if host in ("", "0.0.0.0", "::"):
            return "::" if family == socket.AF_INET6 else "0.0.0.0"
        return host

    def _cleanup_temp_files(self) -> int:
        """Xóa file ``.part`` cũ còn sót sau crash; không đụng file đang ghi."""
        if not os.path.isdir(self.root):
            return 0
        now = time.time()
        removed = 0
        for entry in os.scandir(self.root):
            if not entry.is_file(follow_symlinks=False) or not entry.name.endswith(".part"):
                continue
            try:
                if now - entry.stat(follow_symlinks=False).st_mtime < self.cleanup_temp_age:
                    continue
                os.remove(entry.path)
                removed += 1
            except OSError:
                continue
        if removed:
            self.log.info("đã dọn %d file .part cũ trong %s", removed, self.root)
        return removed

    def _quota_files(self) -> list[tuple[str, int, float]]:
        """List regular files without following symlinks or filesystem reparse points."""
        files: list[tuple[str, int, float]] = []
        pending = [self.root]
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        while pending:
            directory = pending.pop()
            try:
                entries = os.scandir(directory)
            except OSError:
                continue
            with entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISLNK(info.st_mode) or (
                        getattr(info, "st_file_attributes", 0) & reparse_flag
                    ):
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(entry.path)
                    elif stat.S_ISREG(info.st_mode):
                        files.append((entry.path, info.st_size, info.st_mtime))
        return files

    def _root_usage(self) -> int:
        return sum(file_entry[1] for file_entry in self._quota_files())

    def _check_disk_budget(
        self,
        directory: str,
        extra: int = 0,
        *,
        reserve_path: str | None = None,
        protected_path: str | None = None,
    ) -> tuple[bool, str]:
        """Enforce free-space and root quota limits, evicting oldest safe files."""
        reservation_key = (
            os.path.normcase(os.path.abspath(reserve_path)) if reserve_path else None
        )
        protected_key = (
            os.path.normcase(os.path.abspath(protected_path)) if protected_path else None
        )
        with self._quota_lock:
            reserved = sum(
                size
                for key, size in self._quota_reservations.items()
                if key != reservation_key
            )
            required = extra + reserved
            if self.min_free_bytes:
                try:
                    free = shutil.disk_usage(directory).free
                except OSError as exc:
                    return False, f"không đọc được dung lượng đĩa: {exc}"
                if free - required < self.min_free_bytes:
                    return False, (
                        f"đĩa còn {free} byte, cần chừa tối thiểu "
                        f"{self.min_free_bytes} byte"
                    )

            if self.max_total_bytes is not None:
                usage = self._root_usage()
                if usage + required > self.max_total_bytes:
                    reason = (
                        f"thư mục gốc cần {usage + required} byte, vượt quota "
                        f"{self.max_total_bytes} byte"
                    )
                    if not self.evict_oldest:
                        return False, reason
                    if required > self.max_total_bytes:
                        return False, reason

                    # Chỉ chụp danh sách dưới lock; `resolve_path()` có I/O
                    # filesystem nên phải chạy ngoài lock để không chặn
                    # `_handle_datagram` khi đang đông phiên.
                    with self._lock:
                        protected = set(self._active_writes)
                        active_names = [
                            session.request.filename
                            for session in self._sessions.values()
                        ]
                    for name in active_names:
                        active_path = self.resolve_path(name)
                        if active_path:
                            protected.add(os.path.normcase(os.path.abspath(active_path)))
                    if protected_key:
                        protected.add(protected_key)
                    candidates = sorted(
                        (
                            item
                            for item in self._quota_files()
                            if not item[0].lower().endswith(".part")
                            and os.path.normcase(os.path.abspath(item[0])) not in protected
                        ),
                        key=lambda item: item[2],
                    )
                    reclaimable = sum(file_entry[1] for file_entry in candidates)
                    if usage + required - reclaimable > self.max_total_bytes:
                        return False, reason + "; không thể giải phóng đủ dung lượng an toàn"

                    for file_entry in candidates:
                        path, size = file_entry[0], file_entry[1]
                        try:
                            os.remove(path)
                        except OSError:
                            self.log.warning("không thể xóa file cũ để giải phóng quota: %s", path)
                            continue
                        usage -= size
                        # Mức WARNING: đây là xoá dữ liệu của người dùng, phải
                        # hiện trong log mặc định chứ không nằm im ở DEBUG/INFO.
                        self.log.warning("đã xóa file cũ nhất để giải phóng quota: %s", path)
                        if usage + required <= self.max_total_bytes:
                            break
                    if usage + required > self.max_total_bytes:
                        return False, reason + "; không thể xóa đủ file cũ"

            if reservation_key is not None:
                self._quota_reservations[reservation_key] = extra
        return True, ""

    @property
    def active_sessions(self) -> int:
        with self._lock:
            return len(self._sessions)

    def wait_for_idle(self, timeout: float = 5.0, poll_interval: float = 0.01) -> bool:
        """Poll cho tới khi tất cả session server đã kết thúc.

        Không assert ngay vì session thread có thể còn ở trạng thái "đang đóng"
        trong vài ms sau khi client kết thúc. Điều này tránh race flaky trong
        test và trong shutdown.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            if self.active_sessions == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(max(0.0, float(poll_interval)))

    def resolve_path(self, filename: str) -> str | None:
        """Chuyển tên file client gửi thành đường dẫn an toàn trong `root`.

        Mặc định chặn mọi thư mục con (giới hạn thiết kế, xem README).
        Dùng ``allow_subdirs=True`` để cho phép ``incoming/``/``public/``...
        nhưng vẫn chặn ``..``, đường dẫn tuyệt đối và kiểm tra commonpath.
        """
        if not is_servable_name(filename):
            return None
        if not self.allow_subdirs:
            if any(sep in filename for sep in ("/", "\\", ":")):
                return None
        else:
            if "\\" in filename or ":" in filename:
                return None
            parts = filename.split("/")
            if any(p in ("..", ".", "") for p in parts):
                return None
        path = os.path.abspath(os.path.join(self.root, filename))
        if self._has_reparse_component(path):
            return None
        try:
            real_path = os.path.realpath(path)
            if os.path.commonpath([real_path, self.root]) != self.root:
                return None
        except (OSError, ValueError):
            return None
        return real_path

    @staticmethod
    def _is_reparse(path: str) -> bool:
        try:
            info = os.lstat(path)
        except OSError:
            return False
        if stat.S_ISLNK(info.st_mode):
            return True
        attributes = getattr(info, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return bool(attributes & reparse_flag)

    def _has_reparse_component(self, path: str) -> bool:
        """True nếu bất kỳ thành phần nào từ root tới đích là symlink/junction."""
        try:
            relative = os.path.relpath(path, self.root)
        except ValueError:
            return True
        if relative == os.pardir or relative.startswith(os.pardir + os.sep):
            return True
        current = self.root
        for part in relative.split(os.sep):
            if part in ("", "."):
                continue
            current = os.path.join(current, part)
            if self._is_reparse(current):
                return True
        return False

    def negotiate_options(self, requested: dict[str, str], size: int | None) -> dict[str, str]:
        """Chọn các option server đồng ý trả về trong OACK (RFC 2347).

        - blksize ngoài 8..65464: trả giá trị mặc định của server (RFC 2347).
        - timeout ngoài 1..255: trả giá trị của server.
        - tsize: RRQ trả kích thước file thật, WRQ trả "0" (client đã gửi size thật).
        """
        if not self.enable_options or not requested:
            return {}
        accepted: dict[str, str] = {}
        values = parse_options(OACK(options=requested)) if requested else {}
        if OPTION_BLKSIZE in values:
            value = values[OPTION_BLKSIZE]
            if MIN_BLKSIZE <= value <= MAX_BLKSIZE:
                accepted[OPTION_BLKSIZE] = str(value)
            else:
                accepted[OPTION_BLKSIZE] = str(self.blksize)
        if OPTION_TIMEOUT in values:
            value = values[OPTION_TIMEOUT]
            if 1 <= value <= 255:
                accepted[OPTION_TIMEOUT] = str(value)
            else:
                # Không được trả 0: `select()` với timeout 0 quay vòng liên tục.
                accepted[OPTION_TIMEOUT] = str(clamp_timeout(self.timeout))
        if OPTION_TSIZE in values:
            accepted[OPTION_TSIZE] = str(size if size is not None else 0)
        unknown = [name for name in requested if name.lower() not in OPTION_NAMES]
        if unknown:
            self.log.info("bỏ qua option không hỗ trợ: %s", ", ".join(sorted(unknown)))
        return accepted

    # -------------------------------------------------------------------- chạy ---
    def _open_listeners(self) -> list[tuple[socket.socket, int, str]]:
        """Mở một listener cho mỗi family được cấu hình.

        Dual-stack dùng hai socket riêng (IPv4 + IPv6) và bật ``IPV6_V6ONLY`` để
        hành vi giống nhau trên Windows/Linux, không phụ thuộc mặc định OS.

        Không bật ``SO_REUSEADDR``: với UDP, trên Windows cờ này cho phép một
        process khác bind trùng đúng cổng và gói tin bị chia không xác định —
        server sẽ không bao giờ báo "cổng đã dùng" và hai instance cùng chạy
        trong im lặng. Trên POSIX, UDP không có TIME_WAIT nên cũng không cần.
        """
        specs = split_family_hosts(self.host, self.host6, self.family)
        listeners: list[tuple[socket.socket, int, str]] = []
        bound_port = self.port
        try:
            for index, (family, bind_host) in enumerate(specs):
                sock = socket.socket(family, socket.SOCK_DGRAM)
                try:
                    _hold_port_exclusively(sock)
                    if family == socket.AF_INET6:
                        try:
                            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                        except OSError:
                            pass
                    sock.settimeout(0.5)
                    sock.bind(sockaddr_for(bind_host, bound_port, family))
                    actual_port = int(sock.getsockname()[1])
                    if index == 0:
                        bound_port = actual_port
                    listeners.append((sock, family, bind_host))
                except Exception:
                    sock.close()
                    raise
        except Exception:
            for sock, _family, _host in listeners:
                try:
                    sock.close()
                except OSError:
                    pass
            raise
        self.bound_port = bound_port
        self._listeners = listeners
        self.bound_addresses = [
            format_endpoint(bind_host, int(sock.getsockname()[1]))
            for sock, _family, bind_host in listeners
        ]
        return listeners

    def serve_forever(self, on_ready: Callable[[int], None] | None = None) -> None:
        """Mở listener rồi phục vụ tới khi `request_stop()`/`shutdown()`.

        `on_ready` được gọi **sau khi** bind xong (kèm port thật), để GUI chỉ
        báo "đang chạy" khi listener đã thực sự mở thay vì trước đó.
        """
        listeners = self._open_listeners()
        self.sock = listeners[0][0]
        self.log.info(
            "TFTP server sẵn sàng: %s, thư mục gốc=%s, blksize=%d, "
            "timeout=%ss, retries=%d, family=%s",
            ", ".join(f"udp://{item}" for item in self.bound_addresses),
            self.root,
            self.blksize,
            self.timeout,
            self.retries,
            self.family,
        )
        self.log.info("nhấn Ctrl+C để dừng")
        if on_ready is not None:
            try:
                on_ready(self.bound_port)
            except Exception:  # callback của caller không được làm sập server
                self.log.exception("lỗi trong callback on_ready")
        try:
            while not self._stop.is_set():
                self.maybe_log_stats()
                try:
                    readable, _, _ = select.select(
                        [sock for sock, _family, _host in listeners], [], [], 0.5
                    )
                except (OSError, ValueError):
                    if self._stop.is_set():
                        break
                    raise
                for sock in readable:
                    listener = next(
                        item for item in listeners if item[0] is sock
                    )
                    try:
                        raw, addr = sock.recvfrom(MAX_DATAGRAM)
                    except TimeoutError:
                        continue
                    except OSError:
                        if self._stop.is_set():
                            break
                        raise
                    try:
                        self._handle_datagram(raw, addr, listener)
                    except Exception as exc:  # giữ server sống sau lỗi lẻ
                        self.metrics.incr("errors.unhandled")
                        self.log.exception("lỗi khi xử lý gói tin từ %s: %s", addr, exc)
        finally:
            self.shutdown()

    def maybe_log_stats(self, *, force: bool = False) -> None:
        """Ghi bảng số liệu định kỳ (mỗi `stats_interval` giây) nếu được bật."""
        if not force and self.stats_interval <= 0:
            return
        now = time.monotonic()
        if not force and now - self._last_stats < self.stats_interval:
            return
        self._last_stats = now
        self.log.info("thống kê:\n%s", self.metrics.render())

    def _handle_datagram(
        self, raw: bytes, addr: tuple, listener: tuple[socket.socket, int, str]
    ) -> None:
        if self._stop.is_set():
            return
        self.metrics.incr("datagrams.received")
        client_key = address_key(addr)
        try:
            packet = unpack(raw)
        except tftp_errors.TftpError as exc:
            self.metrics.incr("packets.malformed")
            self.log.warning("gói tin lỗi từ %s: %s", format_sockaddr(addr), exc)
            self._send_error_from_listener(listener[0], addr, exc.code, str(exc))
            return

        if not isinstance(packet, (RRQ, WRQ)):
            # Gói DATA/ACK/OACK tới well-known port = TID lạ (RFC 1350 §4.2).
            self.metrics.incr("packets.unknown_tid")
            self.log.warning("%s không hợp lệ từ %s", describe(packet), format_sockaddr(addr))
            self._send_error_from_listener(
                listener[0],
                addr,
                tftp_errors.ERR_UNKNOWN_TID,
                f"Unknown transfer ID: {describe(packet)}",
            )
            return

        # 4.3: giới hạn request/s để chống flood thread — nhưng chỉ tính request
        # MỚI. Gói lặp lại của một phiên đang chạy (client retransmit RRQ/WRQ vì
        # mạng chậm) không được tiêu quota, nếu không client yếu có thể tự khoá
        # chính mình khi đang truyền. Xử lý replay trước, rate limit sau.
        replayed = False
        with self._lock:
            session = self._sessions.get(client_key)
            if session is not None and session.is_alive():
                if session.matches(packet):
                    self.log.info(
                        "nhận lại %s từ %s, chuyển phiên %s",
                        describe(packet),
                        format_sockaddr(addr),
                        session.local_port,
                    )
                    session.replay()
                    replayed = True
                else:
                    # 1.2: cùng (ip, port) nhưng filename/mode/options khác nhau
                    # = OS cấp lại ephemeral port cho client mới. Không được
                    # replay phiên cũ (sẽ trộn dữ liệu + rò rỉ .part). Đóng
                    # phiên cũ, mở phiên mới cho request này.
                    self.metrics.incr("requests.port_reuse")
                    self.log.warning(
                        "port %s tái sử dụng cho file khác ('%s' != '%s'): "
                        "đóng phiên cũ TID %s, mở phiên mới",
                        format_sockaddr(addr),
                        packet.filename,
                        session.request.filename,
                        session.local_port,
                    )
                    # Đóng im lặng (không gửi ERROR) vì client_addr trùng với
                    # request mới — gửi ERROR sẽ làm client mới nhận nhầm.
                    # Phiên cũ sẽ timeout, xóa .part trong finally.
                    try:
                        session.close()
                    except OSError:
                        pass
                    # Xóa ngay để phiên cũ kết thúc không xóa nhầm phiên mới
                    # (session_finished kiểm tra `is` nên an toàn).
                    del self._sessions[client_key]
            elif session is not None:
                # Phiên cũ đã chết nhưng entry còn sót: dọn.
                del self._sessions[client_key]
        if replayed:
            return

        if not self._check_rate_limit():
            self.metrics.incr("requests.rate_limited")
            self.log.warning("từ chối %s: vượt giới hạn request/s", format_sockaddr(addr))
            self._send_error_from_listener(
                listener[0], addr, tftp_errors.ERR_NOT_DEFINED, "server bận, thử lại sau"
            )
            return

        with self._lock:
            over_limit = len(self._sessions) >= self.max_clients
        if over_limit:
            self.metrics.incr("requests.overloaded")
            self.log.warning(
                "từ chối %s: đã đạt giới hạn %d client",
                format_sockaddr(addr),
                self.max_clients,
            )
            self._send_error_from_listener(
                listener[0], addr, tftp_errors.ERR_NOT_DEFINED, "server quá tải, thử lại sau"
            )
            return
        # Tạo phiên (bind socket TID) ngoài lock: bind có thể chậm khi dải port
        # TID bị chật, không nên giữ lock của dispatcher trong lúc đó.
        try:
            new_session = Session(
                self,
                addr,
                packet,
                family=listener[1],
                local_addr=listener[0].getsockname(),
            )
        except OSError as exc:
            self.log.warning(
                "không tạo được phiên cho %s: %s", format_sockaddr(addr), exc
            )
            self._send_error_from_listener(
                listener[0],
                addr,
                tftp_errors.ERR_NOT_DEFINED,
                f"không tạo được phiên: {exc}",
            )
            return
        with self._lock:
            self._sessions[client_key] = new_session
        try:
            new_session.start()
        except RuntimeError as exc:
            with self._lock:
                if self._sessions.get(client_key) is new_session:
                    self._sessions.pop(client_key)
            new_session.close()
            self.log.warning("không chạy được thread cho %s: %s", format_sockaddr(addr), exc)
            self._send_error_from_listener(
                listener[0], addr, tftp_errors.ERR_NOT_DEFINED, "không tạo được phiên"
            )

    def _send_error_from_listener(
        self, sock: socket.socket, addr: tuple, code: int, message: str
    ) -> None:
        self.metrics.incr(f"errors.sent.{code}")
        try:
            sock.sendto(make_error(code, message), addr)
        except OSError:
            pass

    def request_stop(self) -> None:
        """Đánh thức vòng lặp để dừng server — an toàn khi gọi từ signal handler.

        Không lấy `self._lock` và không join thread: `shutdown()` làm việc đó,
        và nếu signal handler chen vào đúng lúc main thread đang giữ lock thì
        `threading.Lock` (không reentrant) sẽ deadlock vĩnh viễn.
        """
        self._stop.set()
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def shutdown(self) -> None:
        """Dừng server: gửi ERROR cho client, đóng phiên, đợi thread (3.1)."""
        self._stop.set()
        listeners = list(self._listeners)
        self._listeners = []
        for sock, _family, _host in listeners:
            try:
                sock.close()
            except OSError:
                pass
        if self.sock is not None and self.sock not in [item[0] for item in listeners]:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            try:
                session.shutdown_with_error("server đang dừng")
            except OSError:
                pass
        for session in sessions:
            session.join(timeout=5)
        self.wait_for_idle(timeout=5.0)
        self.log.info("server đã dừng")
        if self.metrics.counter("sessions.started.RRQ") or self.metrics.counter(
            "sessions.started.WRQ"
        ):
            self.maybe_log_stats(force=True)


# --------------------------------------------------------------------- CLI ---
def build_arg_parser() -> argparse.ArgumentParser:
    from . import __version__

    parser = argparse.ArgumentParser(
        prog="python -m tftp.tftp_server",
        description="TFTP server đa luồng theo RFC 1350.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--host", default="0.0.0.0", help="địa chỉ IPv4 lắng nghe (mặc định 0.0.0.0)"
    )
    parser.add_argument(
        "--host6", default=None, help="địa chỉ IPv6 lắng nghe khi family=dual (mặc định ::)"
    )
    parser.add_argument(
        "--family",
        choices=("auto", "ipv4", "ipv6", "dual"),
        default="auto",
        help="họ địa chỉ: auto suy ra từ --host, dual mở cả IPv4 và IPv6",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="port UDP (mặc định 6969, cần quyền admin cho 69)",
    )
    parser.add_argument("--root", default="files", help="thư mục gốc lưu/phục vụ file")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help="thời gian chờ (giây)"
    )
    parser.add_argument("--retries", type=int, default=5, help="số lần thử lại tối đa")
    parser.add_argument(
        "--blksize",
        type=int,
        default=DEFAULT_SERVER_BLKSIZE,
        help="blksize server đề nghị khi client yêu cầu ngoài khoảng 8..65464",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="cho phép ghi đè file đã tồn tại (mặc định từ chối với ERROR 6)",
    )
    parser.add_argument(
        "--no-options", action="store_true", help="tắt thương lượng option RFC 2347"
    )
    parser.add_argument(
        "--max-clients", type=int, default=32, help="số client đồng thời tối đa"
    )
    parser.add_argument(
        "--max-file-size",
        type=int,
        default=DEFAULT_MAX_FILE_SIZE,
        help="giới hạn kích thước mỗi file upload (byte, 0 = không giới hạn)",
    )
    parser.add_argument(
        "--min-free-bytes",
        type=int,
        default=DEFAULT_MIN_FREE_BYTES,
        help="luôn chừa lại ít nhất N byte trống trên đĩa",
    )
    parser.add_argument(
        "--max-total-bytes",
        type=int,
        default=DEFAULT_MAX_TOTAL_BYTES,
        help="quota tổng thư mục gốc (byte, mặc định 500 MiB; 0 = không giới hạn)",
    )
    parser.add_argument(
        "--evict-oldest",
        action="store_true",
        help="khi đầy quota thì xoá file cũ nhất để nhận upload (mặc định từ chối)",
    )
    parser.add_argument(
        "--max-transfer-seconds",
        type=float,
        default=3600.0,
        help="thời gian tối đa cho một transfer (giây, 0 = không giới hạn)",
    )
    parser.add_argument(
        "--tid-port-range",
        default=None,
        metavar="LOW-HIGH",
        help="giới hạn dải port TID, ví dụ 50000-50100 (mặc định ngẫu nhiên)",
    )
    parser.add_argument(
        "--cleanup-temp-age",
        type=float,
        default=24 * 3600,
        help="xóa file .part cũ hơn N giây khi khởi động (0 = xóa ngay)",
    )
    parser.add_argument(
        "--allow-subdirs",
        action="store_true",
        help="cho phép tên file có thư mục con trong root",
    )
    parser.add_argument(
        "--max-requests-per-second",
        type=int,
        default=100,
        help="giới hạn request/s chống flood",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="file cấu hình JSON; khi có --config, các tham số server khác bị bỏ qua",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="chỉ kiểm tra cấu hình và thoát, không mở listener",
    )
    parser.add_argument("--log-file", default=None, help="ghi log ra file ngoài stdout")
    parser.add_argument(
        "--log-max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help="kích thước tối đa mỗi file log trước khi rotate",
    )
    parser.add_argument(
        "--log-backups",
        type=int,
        default=DEFAULT_BACKUPS,
        help="số file log cũ giữ lại",
    )
    parser.add_argument(
        "--stats-interval",
        type=float,
        default=0.0,
        help="in bảng số liệu vận hành mỗi N giây (0 = tắt)",
    )
    parser.add_argument(
        "--stats-json", default=None, help="ghi số liệu vận hành ra file JSON khi dừng"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="ghi log chi tiết từng gói tin"
    )
    return parser


def _config_from_args(args: argparse.Namespace) -> ServerConfig:
    tid_port_range = None
    if args.tid_port_range:
        low_text, high_text = args.tid_port_range.split("-", 1)
        tid_port_range = (int(low_text), int(high_text))
    config = ServerConfig(
        host=args.host,
        host6=args.host6,
        port=args.port,
        root=args.root,
        family=args.family,
        timeout=args.timeout,
        retries=args.retries,
        blksize=args.blksize,
        allow_overwrite=args.overwrite,
        enable_options=not args.no_options,
        max_clients=args.max_clients,
        max_file_size=args.max_file_size or None,
        allow_subdirs=args.allow_subdirs,
        max_requests_per_second=args.max_requests_per_second,
        tid_port_range=tid_port_range,
        min_free_bytes=args.min_free_bytes,
        max_total_bytes=args.max_total_bytes or None,
        evict_oldest=args.evict_oldest,
        max_transfer_seconds=args.max_transfer_seconds or None,
        cleanup_temp_age=args.cleanup_temp_age,
        stats_interval=args.stats_interval,
        log_file=args.log_file,
        log_max_bytes=args.log_max_bytes,
        log_backups=args.log_backups,
    )
    validate(config)
    return config


def main(argv: list[str] | None = None) -> int:
    enable_utf8_console()
    args = build_arg_parser().parse_args(argv)
    config: ServerConfig | None = None
    if args.config:
        try:
            config = load_config(args.config)
        except ConfigError as exc:
            print(f"cấu hình không hợp lệ: {exc}", file=sys.stderr)
            return 2
    else:
        try:
            config = _config_from_args(args)
        except (ConfigError, ValueError) as exc:
            print(f"cấu hình không hợp lệ: {exc}", file=sys.stderr)
            return 2
    if args.check_config:
        print("cấu hình hợp lệ:\n" + config.describe())
        return 0

    log_file = args.log_file or config.log_file
    try:
        setup_logging(
            log_file=log_file,
            verbose=args.verbose,
            max_bytes=config.log_max_bytes if args.config else args.log_max_bytes,
            backups=config.log_backups if args.config else args.log_backups,
        )
    except OSError as exc:
        print(f"không mở được log file: {exc}", file=sys.stderr)
        return 2
    server = TftpServer(
        config.host,
        config.port,
        **config.to_server_kwargs(),
    )

    def _on_term(signum, _frame) -> None:
        LOG.info("đã nhận tín hiệu %s, đang dừng...", signum)
        # Chỉ set cờ + đóng socket; shutdown() (lấy lock, join thread) chạy
        # trong finally của serve_forever để tránh deadlock trong signal handler.
        server.request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _on_term)
        except (ValueError, OSError, RuntimeError):
            # Chạy trong thread test: không cài signal được, bỏ qua.
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("đã nhận Ctrl+C")
    except OSError as exc:
        LOG.error("không khởi động được server: %s", exc)
        return 2
    finally:
        if args.stats_json:
            try:
                with open(args.stats_json, "w", encoding="utf-8") as handle:
                    handle.write(server.metrics.to_json(indent=2))
                LOG.info("đã ghi số liệu vận hành ra %s", args.stats_json)
            except OSError as exc:
                LOG.error("không ghi được file số liệu: %s", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
