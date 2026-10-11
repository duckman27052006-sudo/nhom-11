"""TFTP server đa luồng theo RFC 1350 (+ RFC 2347).

Kiến trúc:
  - Một socket "well-known" lắng nghe port cấu hình (mặc định 6969, có thể là 69).
  - Mỗi RRQ/WRQ hợp lệ tạo một thread riêng với một TID mới (port ngẫu nhiên).
  - Phiên đang chạy được đăng ký theo địa chỉ client, nên khi client gửi lại
    RRQ/WRQ (do timeout) server trả lời lại đúng từ TID cũ thay vì tạo phiên mới.

Chạy server::

    python -m src.tftp_server --host 0.0.0.0 --port 6969 --root files
"""

from __future__ import annotations

import argparse
import logging
import os
import select
import signal
import socket
import sys
import threading
import time
from collections import deque

from . import DEFAULT_PORT, enable_utf8_console, tftp_errors
from .tftp_metrics import Metrics
from .tftp_packet import (
    ACK,
    DATA,
    DEFAULT_BLKSIZE,
    DEFAULT_TIMEOUT,
    MAX_BLKSIZE,
    MAX_DATAGRAM,
    MAX_STALE_PACKETS,
    MIN_BLKSIZE,
    OACK,
    OP_ACK,
    OP_DATA,
    OP_ERROR,
    OP_RRQ,
    OP_WRQ,
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

# Giữ alias để code cũ vẫn import được.
__all__ = ["TftpServer", "Session", "MAX_STALE_PACKETS"]


class Session(threading.Thread):
    """Một phiên truyền file: thread riêng + socket riêng (TID riêng)."""

    def __init__(
        self, server: TftpServer, client_addr: tuple[str, int], request: RRQ | WRQ
    ) -> None:
        super().__init__(name=f"tftp-{client_addr[0]}:{client_addr[1]}", daemon=True)
        self.server = server
        self.log = server.log
        self.metrics = server.metrics
        self.client_addr = client_addr
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
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind((server.host if server.host != "0.0.0.0" else "", 0))
            self.local_port = self.sock.getsockname()[1]
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
            "[%s] phiên mới từ %s:%s, TID=%s, file=%s",
            kind,
            self.client_addr[0],
            self.client_addr[1],
            self.local_port,
            self.request.filename,
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
                "[%s] đóng phiên %s:%s (TID %s)",
                self.request.filename,
                self.client_addr[0],
                self.client_addr[1],
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
            if source != self.client_addr:
                self.metrics.incr("packets.foreign_tid")
                self.log.warning("bỏ qua gói từ TID không xác định %s:%s", *source)
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
            if packet.OPCODE == OP_ERROR:
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
        sent_at = 0.0
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
        request: RRQ = self.request
        path = self.server.resolve_path(request.filename)
        if path is None:
            self.metrics.incr("requests.bad_filename")
            self._send_error(tftp_errors.ERR_ACCESS_VIOLATION, "tên file không hợp lệ")
            return
        try:
            handle = open(path, "rb")
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
            self._send_error(tftp_errors.ERR_NOT_DEFINED, str(exc))
            return

        with handle:
            try:
                total = os.fstat(handle.fileno()).st_size
            except OSError as exc:
                self._send_error(tftp_errors.ERR_NOT_DEFINED, str(exc))
                return
            self.log.info(
                "bắt đầu gửi '%s' (%d byte) cho %s:%s",
                request.filename,
                total,
                self.client_addr[0],
                self.client_addr[1],
            )
            try:
                accepted = self._negotiate(request.options, total)
            except tftp_errors.TftpError as exc:
                self._send_error(exc.code, str(exc))
                return
            started = time.monotonic()
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
                        self.metrics.incr("payload.sent", total)
                        self.metrics.incr("transfers.completed.RRQ")
                        self.log.info(
                            "gửi xong '%s' (%d byte) cho %s:%s",
                            request.filename,
                            total,
                            self.client_addr[0],
                            self.client_addr[1],
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

    def _read_chunk(self, handle, mode: str) -> bytes:
        """Đọc 1 chunk octet (giữ lại để tương thích). Netascii dùng NetasciiReader."""
        if mode == "netascii":
            return NetasciiReader(handle, self.blksize).read_chunk()
        return handle.read(self.blksize)

    # -------------------------------------------------------------- WRQ: server nhận ---
    def _serve_write(self) -> None:
        request: WRQ = self.request
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
            self.metrics.add_gauge("writes.active", -1)
            self._send_error(
                tftp_errors.ERR_FILE_EXISTS,
                f"file '{request.filename}' đang được ghi bởi phiên khác",
            )
            return

        self.metrics.add_gauge("writes.active", 1)
        try:
            directory = os.path.dirname(path) or "."
            if not os.path.isdir(directory):
                self.metrics.add_gauge("writes.active", -1)
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
                self._send_error(
                    tftp_errors.ERR_DISK_FULL,
                    f"file {offered_size} byte vượt giới hạn {self.server.max_file_size} byte",
                )
                return

            self.metrics.incr("requests.oversized")
            temp_path = f"{path}.tftp-{os.getpid()}-{self.local_port}.part"
            try:
                accepted = self._negotiate(request.options, None)
            except tftp_errors.TftpError as exc:
                self._send_error(exc.code, str(exc))
                return
            try:
                handle = open(temp_path, "wb")
            except PermissionError:
                self._send_error(tftp_errors.ERR_ACCESS_VIOLATION, "không có quyền ghi")
                return
            except OSError as exc:
                self._send_error(tftp_errors.ERR_DISK_FULL, str(exc))
                return

            received = 0
            started = time.monotonic()
            final_ack = None
            decoder = NetasciiDecoder() if request.mode == "netascii" else None
            try:
                with handle:
                    if accepted:
                        self._exchange(
                            OACK(accepted),
                            0,
                            (OP_ACK, OP_DATA),
                            ((OP_ACK, 0), (OP_DATA, 1)),
                        )

                    pending: object = ACK(0)
                    pending_block = 0
                    expect_block = 1
                    send_first = not accepted

                    while True:
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
                        # Decoder giữ nối cặp CRLF bị tách giữa 2 DATA block.
                        payload = (
                            decoder.feed(data_packet.data)
                            if decoder is not None
                            else data_packet.data
                        )
                        try:
                            handle.write(payload)
                        except OSError as exc:
                            self._send_error(tftp_errors.ERR_DISK_FULL, f"ghi file thất bại: {exc}")
                            return
                        received += len(payload)
                        if (
                            self.server.max_file_size is not None
                            and received > self.server.max_file_size
                        ):
                            self._send_error(
                                tftp_errors.ERR_DISK_FULL,
                                f"file vượt giới hạn {self.server.max_file_size} byte",
                            )
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

                try:
                    os.replace(temp_path, path)
                except OSError as exc:
                    self._send_error(tftp_errors.ERR_DISK_FULL, f"không lưu được file: {exc}")
                    raise tftp_errors.TftpDiskFull(f"không lưu được file: {exc}") from exc
                elapsed = max(time.monotonic() - started, 1e-6)
                self.metrics.incr("payload.received", received)
                self.metrics.incr("transfers.completed.WRQ")
                self.log.info(
                    "lưu xong '%s' (%d byte, %.1f KiB/s) từ %s:%s",
                    request.filename,
                    received,
                    received / elapsed / 1024,
                    self.client_addr[0],
                    self.client_addr[1],
                )
                if final_ack is not None:
                    self._send(final_ack)
            finally:
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

    _WINDOWS_RESERVED = frozenset(
        {"con", "prn", "aux", "nul"}
        | {f"com{i}" for i in range(1, 10)}
        | {f"lpt{i}" for i in range(1, 10)}
    )

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        *,
        root: str = "files",
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = 5,
        blksize: int = DEFAULT_SERVER_BLKSIZE,
        allow_overwrite: bool = True,
        enable_options: bool = True,
        max_clients: int = 32,
        max_file_size: int | None = None,
        allow_subdirs: bool = False,
        max_requests_per_second: int = 100,
        stats_interval: float = 0.0,
        metrics: Metrics | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.root = os.path.abspath(root)
        self.timeout = max(MIN_SESSION_TIMEOUT, float(timeout))
        self.retries = max(1, retries)
        self.blksize = min(max(blksize, MIN_BLKSIZE), MAX_BLKSIZE)
        self.allow_overwrite = allow_overwrite
        self.enable_options = enable_options
        self.max_clients = max_clients
        self.max_file_size = max_file_size
        self.allow_subdirs = allow_subdirs
        self.max_requests_per_second = max(1, max_requests_per_second)
        self.log = logger or LOG
        self.metrics = metrics if metrics is not None else Metrics("tftp.server")
        self.stats_interval = max(0.0, stats_interval)
        self._last_stats = time.monotonic()
        self._sessions: dict[tuple[str, int], Session] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.sock: socket.socket | None = None
        self.bound_port = port
        self._active_writes: set[str] = set()
        self._request_times: deque[float] = deque()
        os.makedirs(self.root, exist_ok=True)

    # ------------------------------------------------------------------ tiện ích ---
    def session_finished(self, session: Session) -> None:
        with self._lock:
            if self._sessions.get(session.client_addr) is session:
                del self._sessions[session.client_addr]

    def try_acquire_write(self, path: str) -> bool:
        """Giữ lock theo tên file khi WRQ (2.4). False = đang có phiên khác ghi."""
        with self._lock:
            if path in self._active_writes:
                return False
            self._active_writes.add(path)
            return True

    def release_write(self, path: str) -> None:
        with self._lock:
            self._active_writes.discard(path)

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
        if not filename or len(filename) > 255:
            return None
        if filename in (".", "..") or os.path.isabs(filename):
            return None
        # 4.2: chặn ký tự điều khiển và ký tự cấm của Windows.
        for ch in filename:
            o = ord(ch)
            if o < 32 or o == 127:
                return None
        if any(c in filename for c in '<>:"|?*'):
            return None
        base = filename.replace("\\", "/").split("/")[-1]
        if not base or base in (".", ".."):
            return None
        if base.endswith((" ", ".")):
            return None
        stem = base.split(".")[0].lower()
        if stem in self._WINDOWS_RESERVED:
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
        try:
            if os.path.commonpath([path, self.root]) != self.root:
                return None
        except ValueError:
            return None
        return path

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
    def serve_forever(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock = sock
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(0.5)
        sock.bind((self.host, self.port))
        self.bound_port = sock.getsockname()[1]
        self.log.info(
            "TFTP server sẵn sàng: udp://%s:%d, thư mục gốc=%s, blksize=%d, "
            "timeout=%ss, retries=%d",
            self.host or "0.0.0.0",
            self.bound_port,
            self.root,
            self.blksize,
            self.timeout,
            self.retries,
        )
        self.log.info("nhấn Ctrl+C để dừng")
        try:
            # Giữ socket cục bộ: shutdown() từ thread/signal khác sẽ set
            # self.sock = None, nếu đọc self.sock trong vòng lặp sẽ AttributeError.
            while not self._stop.is_set():
                self.maybe_log_stats()
                try:
                    raw, addr = sock.recvfrom(MAX_DATAGRAM)
                except TimeoutError:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    raise
                try:
                    self._handle_datagram(raw, addr)
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

    def _handle_datagram(self, raw: bytes, addr: tuple[str, int]) -> None:
        if self._stop.is_set():
            return
        self.metrics.incr("datagrams.received")
        try:
            packet = unpack(raw)
        except tftp_errors.TftpError as exc:
            self.metrics.incr("packets.malformed")
            self.log.warning("gói tin lỗi từ %s: %s", addr, exc)
            self._send_error_from_well_known(addr, exc.code, str(exc))
            return

        if packet.OPCODE not in (OP_RRQ, OP_WRQ):
            # Gói DATA/ACK/OACK tới well-known port = TID lạ (RFC 1350 §4.2).
            self.metrics.incr("packets.unknown_tid")
            self.log.warning("%s không hợp lệ từ %s", describe(packet), addr)
            self._send_error_from_well_known(
                addr, tftp_errors.ERR_UNKNOWN_TID, f"Unknown transfer ID: {describe(packet)}"
            )
            return

        # 4.3: giới hạn số request/s để chống flood thread.
        if not self._check_rate_limit():
            self.metrics.incr("requests.rate_limited")
            self.log.warning("từ chối %s: vượt giới hạn request/s", addr)
            self._send_error_from_well_known(
                addr, tftp_errors.ERR_NOT_DEFINED, "server bận, thử lại sau"
            )
            return

        with self._lock:
            session = self._sessions.get(addr)
            if session is not None and session.is_alive():
                if session.matches(packet):
                    self.log.info(
                        "nhận lại %s từ %s, chuyển phiên %s",
                        describe(packet),
                        addr,
                        session.local_port,
                    )
                    session.replay()
                    return
                # 1.2: cùng (ip, port) nhưng filename/mode/options khác nhau =
                # OS cấp lại ephemeral port cho client mới. Không được replay
                # phiên cũ (sẽ trộn dữ liệu + rò rỉ .part). Đóng phiên cũ,
                # mở phiên mới cho request này.
                    self.metrics.incr("requests.port_reuse")
                    self.log.warning(
                        "port %s tái sử dụng cho file khác ('%s' != '%s'): "

                    "đóng phiên cũ TID %s, mở phiên mới",
                    addr,
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
                del self._sessions[addr]
            elif session is not None:
                # Phiên cũ đã chết nhưng entry còn sót: dọn.
                del self._sessions[addr]
            if len(self._sessions) >= self.max_clients:
                self.metrics.incr("requests.overloaded")
                self.log.warning("từ chối %s: đã đạt giới hạn %d client", addr, self.max_clients)
                self._send_error_from_well_known(
                    addr, tftp_errors.ERR_NOT_DEFINED, "server quá tải, thử lại sau"
                )
                return
            try:
                new_session = Session(self, addr, packet)
            except OSError as exc:
                self.log.warning("không tạo được phiên cho %s: %s", addr, exc)
                self._send_error_from_well_known(
                    addr, tftp_errors.ERR_NOT_DEFINED, f"không tạo được phiên: {exc}"
                )
                return
            self._sessions[addr] = new_session
        new_session.start()

    def _send_error_from_well_known(self, addr: tuple[str, int], code: int, message: str) -> None:
        self.metrics.incr(f"errors.sent.{code}")
        if self.sock is None:
            return
        try:
            self.sock.sendto(make_error(code, message), addr)
        except OSError:
            pass

    def shutdown(self) -> None:
        """Dừng server: gửi ERROR cho client, đóng phiên, đợi thread (3.1)."""
        self._stop.set()
        if self.sock is not None:
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
        prog="python -m src.tftp_server",
        description="TFTP server đa luồng theo RFC 1350.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--host", default="0.0.0.0", help="địa chỉ lắng nghe (mặc định 0.0.0.0)"
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
        help="kích thước khối mặc định",
    )
    parser.add_argument(
        "--no-overwrite",
        action="store_true",
        help="từ chối ghi đè file đã tồn tại (ERROR 6)",
    )
    parser.add_argument(
        "--no-options", action="store_true", help="tắt thương lượng option RFC 2347"
    )
    parser.add_argument(
        "--max-clients", type=int, default=32, help="số client đồng thời tối đa"
    )
    parser.add_argument(
        "--max-file-size", type=int, default=None, help="giới hạn kích thước upload (byte)"
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
    parser.add_argument("--log-file", default=None, help="ghi log ra file ngoài stdout")
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


def main(argv: list[str] | None = None) -> int:
    enable_utf8_console()
    args = build_arg_parser().parse_args(argv)
    handlers: list[logging.Handler] = []
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
        handlers=handlers or None,
    )
    server = TftpServer(
        args.host,
        args.port,
        root=args.root,
        timeout=args.timeout,
        retries=args.retries,
        blksize=args.blksize,
        allow_overwrite=not args.no_overwrite,
        enable_options=not args.no_options,
        max_clients=args.max_clients,
        max_file_size=args.max_file_size,
        allow_subdirs=args.allow_subdirs,
        max_requests_per_second=args.max_requests_per_second,
        stats_interval=args.stats_interval,
    )

    def _on_term(signum, _frame) -> None:
        LOG.info("đã nhận tín hiệu %s, đang dừng...", signum)
        server.shutdown()

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
