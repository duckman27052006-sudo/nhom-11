"""TFTP client: tải file (RRQ) và gửi file (WRQ) theo RFC 1350 + RFC 2347.

Cách dùng như thư viện::

    from tftp.tftp_client import TftpClient
    TftpClient("127.0.0.1", 6969).download("hello.txt", "out/hello.txt")
    TftpClient("127.0.0.1", 6969).upload("out/hello.txt", "hello.txt")

Cách dùng trên dòng lệnh::

    python -m tftp.tftp_client --server 127.0.0.1:6969 --get hello.txt --save out.txt
    python -m tftp.tftp_client --server 127.0.0.1:6969 --put out.txt --remote hello.txt
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import DEFAULT_PORT, enable_utf8_console, tftp_errors, tftp_fs
from .tftp_metrics import Metrics
from .tftp_net import (
    FamilyMode,
    ResolvedAddress,
    format_endpoint,
    parse_host_port,
    resolve_addresses,
    same_ip,
)
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

    tid: tuple | None = None
    target: tuple = ("", 0)


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
        family: FamilyMode = "auto",
        overwrite: bool = False,
        total_timeout: float | None = None,
        cancel_event=None,
        metrics: Metrics | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if family not in ("auto", "ipv4", "ipv6"):
            raise ValueError(f"family không hợp lệ: {family!r}")
        if timeout <= 0:
            raise ValueError("timeout phải lớn hơn 0")
        if not 8 <= blksize <= 65464:
            raise ValueError("blksize phải trong khoảng 8..65464")
        if total_timeout is not None and total_timeout <= 0:
            raise ValueError("total_timeout phải lớn hơn 0")
        self.host = host
        self.port = port
        self.timeout = float(timeout)
        self.retries = max(1, retries)
        self.blksize = blksize
        self.mode = mode
        self.use_options = use_options
        self.family = family
        self.overwrite = overwrite
        self.total_timeout = total_timeout
        self.cancel_event = cancel_event
        self.log = logger or LOG
        self.metrics = metrics if metrics is not None else Metrics("tftp.client")
        self._resolved: list[ResolvedAddress] | None = None
        self._state = _ClientState(target=(host, port))
        self._sent_at = 0.0
        self._deadline: float | None = None

    # ------------------------------------------------------------ tiện ích ---
    def _info(self, message: str) -> None:
        self.log.info(message)

    def _debug(self, message: str) -> None:
        self.log.debug(message)

    def _warn(self, message: str) -> None:
        self.log.warning(message)

    def _candidates(self) -> list[ResolvedAddress]:
        if self._resolved is None:
            self._resolved = resolve_addresses(self.host, self.port, self.family)
        return self._resolved

    def _open_socket(
        self, candidate: ResolvedAddress, timeout: float | None = None
    ) -> socket.socket:
        sock = socket.socket(candidate.family, socket.SOCK_DGRAM)
        if candidate.family == socket.AF_INET6:
            try:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            except OSError:
                pass
        sock.settimeout(self.timeout if timeout is None else timeout)
        return sock

    def _check_deadline(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise tftp_errors.TftpTimeout("đã hủy truyền theo yêu cầu")
        if self._deadline is not None and time.monotonic() > self._deadline:
            raise tftp_errors.TftpTimeout("vượt quá thời gian tổng cho phép của transfer")

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
    def _target(self) -> tuple:
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
            except (ConnectionRefusedError, ConnectionResetError) as exc:
                # Windows reports ICMP "UDP port unreachable" as WinError 10054;
                # Unix can report ECONNREFUSED. This is an unavailable peer, not
                # a local staging-file error. Keep the DNS fallback path working.
                endpoint = format_endpoint(self._state.target[0], self._state.target[1])
                raise tftp_errors.TftpTimeout(
                    f"TFTP tại {endpoint} không sẵn sàng hoặc đã ngắt kết nối"
                ) from exc
            if tid is None:
                if not same_ip(source, self._state.target):
                    self.metrics.incr("packets.foreign_host")
                    self._warn(f"bỏ qua gói từ host lạ {source[0]}")
                    continue
                tid = source
                self._state.tid = tid
                self._info(f"server trả lời từ TID mới {format_endpoint(tid[0], tid[1])}")
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
            self._check_deadline()
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

    def _new_state(self, candidate: ResolvedAddress) -> None:
        self._state = _ClientState(tid=None, target=candidate.sockaddr)

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
        self._deadline = (
            time.monotonic() + self.total_timeout if self.total_timeout is not None else None
        )
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
        if not self.overwrite and os.path.exists(local):
            raise FileExistsError(f"file đích đã tồn tại: {local} (dùng overwrite=True để ghi đè)")
        directory = os.path.dirname(os.path.abspath(local)) or "."
        if not os.path.isdir(directory):
            raise FileNotFoundError(f"thư mục đích không tồn tại: {directory}")

        candidates = self._candidates()
        last_timeout: tftp_errors.TftpTimeout | None = None
        for index, candidate in enumerate(candidates):
            self._new_state(candidate)
            sock = self._open_socket(candidate)
            started = time.monotonic()
            self._info(
                f"RRQ {remote} -> {local} qua {format_endpoint(candidate.host, candidate.port)} "
                f"({candidate.family_name}, mode={self.mode}, blksize={self.blksize}, "
                f"timeout={self.timeout}s, retries={self.retries}, "
                f"{'có' if self.use_options else 'không'} option)"
            )
            try:
                return self._download_with_socket(
                    sock, remote, local, progress=progress, started=started
                )
            except tftp_errors.TftpTimeout as exc:
                last_timeout = exc
                if self._state.tid is not None or index + 1 >= len(candidates):
                    raise
                self._warn(
                    f"địa chỉ {format_endpoint(candidate.host, candidate.port)} không phản hồi, "
                    "thử địa chỉ DNS kế tiếp"
                )
            finally:
                sock.close()
        raise last_timeout or tftp_errors.TftpTimeout("không có địa chỉ nào phản hồi")

    def _download_with_socket(
        self,
        sock: socket.socket,
        remote: str,
        local: str,
        *,
        progress: ProgressFn | None,
        started: float,
    ) -> str:
        # 2.2: blksize/timeout thương lượng giữ trong biến local, không đụng self.
        cur_timeout = self.timeout
        negotiated_size: int | None = None
        directory = os.path.dirname(os.path.abspath(local)) or "."
        base = os.path.basename(local)
        fd, temp_path = tempfile.mkstemp(prefix=f".{base}.", suffix=".part", dir=directory)
        handle = os.fdopen(fd, "wb")
        try:
            wire_blksize = DEFAULT_BLKSIZE
            request = RRQ(remote, self.mode, self._requested_options(for_upload=False))
            first = self._exchange(sock, request, (OP_OACK, OP_DATA), None)
            self.metrics.observe("transfer.ttfb", time.monotonic() - started)

            block = 0
            total = 0
            decoder = NetasciiDecoder() if self.mode == "netascii" else None
            with handle:
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
                    self._check_deadline()
                    # Decoder giữ nối cặp CRLF bị tách giữa 2 DATA block.
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
                handle.flush()
                os.fsync(handle.fileno())

            if self.overwrite:
                os.replace(temp_path, local)
            else:
                # Tạo đích mà không ghi đè: hard link khi filesystem hỗ trợ,
                # fallback O_EXCL + copy khi không (exFAT/FAT32 trên USB).
                if not tftp_fs.commit_without_overwrite(temp_path, local):
                    raise FileExistsError(f"file đích đã tồn tại: {local}")
            temp_path = ""
            self.metrics.incr("payload.received", total)
            self._summary("tải về", remote, total, block, started)
            return local
        finally:
            try:
                handle.close()
            except OSError:
                pass
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    # -------------------------------------------------------------- tải lên ---
    def upload(self, local: str, remote: str, *, progress: ProgressFn | None = None) -> str:
        """Gửi file `local` lên server thành `remote` (WRQ)."""
        return self._run_transfer("upload", lambda: self._upload(local, remote, progress=progress))

    def _upload(self, local: str, remote: str, *, progress: ProgressFn | None = None) -> str:
        size = os.path.getsize(local)
        candidates = self._candidates()
        last_timeout: tftp_errors.TftpTimeout | None = None
        for index, candidate in enumerate(candidates):
            self._new_state(candidate)
            sock = self._open_socket(candidate)
            started = time.monotonic()
            self._info(
                f"WRQ {local} -> {remote} qua {format_endpoint(candidate.host, candidate.port)} "
                f"({candidate.family_name}, mode={self.mode}, blksize={self.blksize}, "
                f"timeout={self.timeout}s, retries={self.retries}, {size} byte)"
            )
            try:
                return self._upload_with_socket(
                    sock, local, remote, size=size, progress=progress, started=started
                )
            except tftp_errors.TftpTimeout as exc:
                last_timeout = exc
                if self._state.tid is not None or index + 1 >= len(candidates):
                    raise
                self._warn(
                    f"địa chỉ {format_endpoint(candidate.host, candidate.port)} không phản hồi, "
                    "thử địa chỉ DNS kế tiếp"
                )
            finally:
                sock.close()
        raise last_timeout or tftp_errors.TftpTimeout("không có địa chỉ nào phản hồi")

    def _upload_with_socket(
        self,
        sock: socket.socket,
        local: str,
        remote: str,
        *,
        size: int,
        progress: ProgressFn | None,
        started: float,
    ) -> str:
        cur_timeout = self.timeout
        wire_blksize = DEFAULT_BLKSIZE
        request = WRQ(remote, self.mode, self._requested_options(for_upload=True, size=size))
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
            # RFC 2347: sau OACK của WRQ, client gửi thẳng DATA block 1. OACK đã
            # thay cho ACK 0 của RFC 1350 nên gửi thêm ACK(0) là gói thừa, có
            # thể làm server bên thứ ba bối rối (server của project chấp nhận cả
            # hai nên test nội bộ không phát hiện ra).
        else:
            wire_blksize = DEFAULT_BLKSIZE

        block = 0
        sent_total = 0
        with open(local, "rb") as handle:
            reader = NetasciiReader(handle, wire_blksize) if self.mode == "netascii" else None
            while True:
                self._check_deadline()
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


# --------------------------------------------------------------------- CLI ---
def _parse_server(value: str) -> tuple[str, int]:
    """Parse `host[:port]`, hỗ trợ IPv6 `[::1]:port` và zone `[fe80::1%12]:6969`."""
    return parse_host_port(value, DEFAULT_PORT)


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


def build_arg_parser() -> argparse.ArgumentParser:
    from . import __version__

    parser = argparse.ArgumentParser(
        prog="python -m tftp.tftp_client",
        description="TFTP client theo RFC 1350: tải file (--get) hoặc gửi file (--put).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--server",
        default=f"127.0.0.1:{DEFAULT_PORT}",
        help="địa chỉ server host[:port]; IPv6 dùng [2001:db8::10]:6969",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--get", metavar="REMOTE", help="tải file từ server")
    group.add_argument("--put", metavar="LOCAL", help="gửi file lên server")
    parser.add_argument(
        "--save", help="đường dẫn lưu khi dùng --get (mặc định: tên file gốc)"
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="cho phép ghi đè file đích khi --get (mặc định từ chối)",
    )
    parser.add_argument(
        "--family",
        choices=("auto", "ipv4", "ipv6"),
        default="auto",
        help="họ địa chỉ khi phân giải DNS (mặc định auto: A + AAAA)",
    )
    parser.add_argument(
        "--total-timeout",
        type=float,
        default=None,
        help="thời gian tổng tối đa cho một transfer (giây)",
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
    validate_direction_args(parser, args)
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
    try:
        client = TftpClient(
            host,
            port,
            timeout=args.timeout,
            retries=args.retries,
            blksize=args.blocksize,
            mode=args.mode,
            use_options=not args.no_options,
            family=args.family,
            overwrite=args.overwrite,
            total_timeout=args.total_timeout,
        )
    except ValueError as exc:
        parser.error(str(exc))
        return 2
    try:
        if args.get:
            local = args.save or os.path.basename(args.get) or "download.out"
            client.download(args.get, local)
        else:
            client.upload(args.put, args.remote or os.path.basename(args.put))
    except tftp_errors.TftpError as exc:
        LOG.error("truyền thất bại: %s", exc)
        return 1
    except OSError as exc:
        LOG.error("lỗi hệ thống: %s", exc)
        return 2
    finally:
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
