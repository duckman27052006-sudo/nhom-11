"""Định nghĩa, đóng gói và giải mã gói tin TFTP.

Bố cục gói tin (RFC 1350 mục 4, tất cả số dùng big-endian / network byte order):

    RRQ/WRQ : | opcode (2) | filename (n) | 0 | mode (n) | 0 | options... |
    OACK    : | opcode (2) | option (n) | 0 | value (n) | 0 | ... |
    DATA    : | opcode (2) | block# (2) | data (0..blksize) |
    ACK     : | opcode (2) | block# (2) |
    ERROR   : | opcode (2) | errcode (2) | errmsg (n) | 0 |

Ràng buộc cần nhớ:
  - DATA cuối cùng có data < blksize để đánh dấu kết thúc truyền.
  - File rỗng được truyền bằng đúng một DATA rỗng (block 1, 0 byte).
  - RRQ/WRQ có thể kèm option của RFC 2347, server trả lời bằng OACK nếu đồng ý.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from . import tftp_errors

# ---------------------------------------------------------------- opcode ---
OP_RRQ = 1
OP_WRQ = 2
OP_DATA = 3
OP_ACK = 4
OP_ERROR = 5
OP_OACK = 6

OPCODE_NAMES = {
    OP_RRQ: "RRQ",
    OP_WRQ: "WRQ",
    OP_DATA: "DATA",
    OP_ACK: "ACK",
    OP_ERROR: "ERROR",
    OP_OACK: "OACK",
}

VALID_OPCODES = frozenset(OPCODE_NAMES)

# ------------------------------------------------------------------ hằng số ---
DEFAULT_BLKSIZE = 512
MIN_BLKSIZE = 8
MAX_BLKSIZE = 65464
DEFAULT_TIMEOUT = 5
MIN_TIMEOUT = 1
MAX_TIMEOUT = 255
MAX_DATAGRAM = 65535
MAX_FILENAME = 255
MAX_STALE_PACKETS = 20


def clamp_timeout(value: object, default: int = DEFAULT_TIMEOUT) -> int:
    """Ép giá trị timeout về khoảng hợp lệ của RFC 2347 (1..255 giây).

    `timeout` là số nguyên giây; làm tròn xuống sẽ ra 0 khi người dùng đặt
    `--timeout 0.3`, mà 0 thì `select()` không chờ nữa — server quay vòng gửi
    lại liên tục và nuốt ACK của client.
    """
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return default
    return min(MAX_TIMEOUT, max(MIN_TIMEOUT, seconds))


def next_block(block: int) -> int:
    """Block kế tiếp, wrap về 0 sau 65535 theo RFC 1350 (16 bit)."""
    return (block + 1) & 0xFFFF

# ------------------------------------------------------------ option (RFC 2347) ---
OPTION_BLKSIZE = "blksize"
OPTION_TIMEOUT = "timeout"
OPTION_TSIZE = "tsize"
OPTION_NAMES = (OPTION_BLKSIZE, OPTION_TIMEOUT, OPTION_TSIZE)

TRANSFER_MODES = ("netascii", "octet", "mail")


class Packet:
    """Giao diện chung cho mọi loại gói tin TFTP."""

    OPCODE = 0

    def pack(self) -> bytes:  # pragma: no cover - lớp trừu tượng
        raise NotImplementedError

    def __repr__(self) -> str:
        fields = {k: v for k, v in vars(self).items()}
        return f"{type(self).__name__}({fields})"


@dataclass
class RRQ(Packet):
    """Read Request: yêu cầu đọc file từ server."""

    OPCODE = OP_RRQ
    filename: str
    mode: str = "octet"
    options: dict[str, str] = field(default_factory=dict)

    def pack(self) -> bytes:
        return _pack_request(OP_RRQ, self.filename, self.mode, self.options)


@dataclass
class WRQ(Packet):
    """Write Request: yêu cầu ghi file lên server."""

    OPCODE = OP_WRQ
    filename: str
    mode: str = "octet"
    options: dict[str, str] = field(default_factory=dict)

    def pack(self) -> bytes:
        return _pack_request(OP_WRQ, self.filename, self.mode, self.options)


@dataclass
class DATA(Packet):
    """DATA: một khối dữ liệu, đánh số từ 1 tăng dần, mỗi khối <= blksize byte."""

    OPCODE = OP_DATA
    block: int
    data: bytes = b""

    def __post_init__(self) -> None:
        self.data = bytes(self.data)

    def pack(self) -> bytes:
        if not 0 <= self.block <= 0xFFFF:
            raise tftp_errors.TftpIllegalOperation(f"block number vượt quá 16 bit: {self.block}")
        if len(self.data) > MAX_BLKSIZE:
            raise tftp_errors.TftpIllegalOperation(f"DATA vượt quá {MAX_BLKSIZE} byte")
        return struct.pack("!HH", OP_DATA, self.block) + self.data

    def is_last_for(self, blksize: int) -> bool:
        """True nếu DATA này là block cuối với `blksize` đã thương lượng.

        Không có property `is_last`: nó hard-code 512 nên sai khi blksize đã
        thương lượng (ví dụ 1468) — một block 1000 byte bị coi là chưa xong.
        """
        return len(self.data) < blksize


@dataclass
class ACK(Packet):
    """ACK: xác nhận đã nhận đủ block#."""

    OPCODE = OP_ACK
    block: int = 0

    def pack(self) -> bytes:
        if not 0 <= self.block <= 0xFFFF:
            raise tftp_errors.TftpIllegalOperation(f"block number vượt quá 16 bit: {self.block}")
        return struct.pack("!HH", OP_ACK, self.block)


@dataclass
class ERROR(Packet):
    """ERROR: báo lỗi kèm mã lỗi và thông báo chữ kết thúc bằng byte 0."""

    OPCODE = OP_ERROR
    code: int = tftp_errors.ERR_NOT_DEFINED
    message: str = ""

    def pack(self) -> bytes:
        text = (self.message or tftp_errors.error_message(self.code)).encode("utf-8", "replace")
        return struct.pack("!HH", OP_ERROR, self.code) + text + b"\x00"

    def __str__(self) -> str:
        return f"ERROR {self.code}: {self.message}"


@dataclass
class OACK(Packet):
    """OACK: server xác nhận các option được chấp nhận (RFC 2347)."""

    OPCODE = OP_OACK
    options: dict[str, str] = field(default_factory=dict)

    def pack(self) -> bytes:
        if not self.options:
            raise tftp_errors.TftpIllegalOperation("OACK phải có ít nhất một option")
        out = bytearray(struct.pack("!H", OP_OACK))
        for name, value in self.options.items():
            # latin-1 là bản ánh xạ 1 byte <-> 1 code point nên pack/unpack
            # giữ nguyên byte cho cả option lạ chứa byte > 127.
            out += name.encode("ascii") + b"\x00" + str(value).encode("latin-1") + b"\x00"
        return bytes(out)

    def as_int(self, name: str, default: int | None = None) -> int | None:
        return _option_to_int(name, self.options, default)


AnyPacket = RRQ | WRQ | DATA | ACK | ERROR | OACK


# ------------------------------------------------------------------ helpers ---
def _encode_text(value: str, field_name: str) -> bytes:
    if not isinstance(value, str):
        raise tftp_errors.TftpIllegalOperation(f"{field_name} phải là chuỗi ký tự")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise tftp_errors.TftpIllegalOperation(f"{field_name} chỉ được dùng ký tự ASCII") from exc
    if not encoded:
        raise tftp_errors.TftpIllegalOperation(f"{field_name} không được rỗng")
    if b"\x00" in encoded:
        raise tftp_errors.TftpIllegalOperation(f"{field_name} không được chứa byte 0")
    return encoded


def _encode_options(options: dict[str, str] | None) -> bytes:
    out = bytearray()
    for name, value in (options or {}).items():
        key = str(name).lower()
        if key not in OPTION_NAMES:
            continue
        out += key.encode("ascii") + b"\x00" + str(value).encode("latin-1") + b"\x00"
    return bytes(out)


def _pack_request(opcode: int, filename: str, mode: str, options: dict[str, str] | None) -> bytes:
    if opcode not in (OP_RRQ, OP_WRQ):
        raise tftp_errors.TftpIllegalOperation(f"opcode {opcode} không phải RRQ/WRQ")
    if len(filename.encode("ascii", "replace")) > MAX_FILENAME:
        raise tftp_errors.TftpIllegalOperation("tên file quá dài")
    head = struct.pack("!H", opcode)
    head += _encode_text(filename, "filename") + b"\x00"
    head += _encode_text(mode, "mode") + b"\x00"
    return head + _encode_options(options)


def _split_cstrings(payload: bytes, expected_min: int) -> list[bytes]:
    """Tách chuỗi kết thúc bằng byte 0, có kiểm tra số lượng tối thiểu."""
    parts = payload.split(b"\x00")
    if len(parts) < expected_min:
        raise tftp_errors.TftpFormatError("gói tin bị thiếu trường kết thúc bằng byte 0")
    return parts


def _decode_text(raw: bytes, field_name: str) -> str:
    if not raw:
        raise tftp_errors.TftpFormatError(f"{field_name} rỗng")
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise tftp_errors.TftpFormatError(f"{field_name} không phải ASCII") from exc


def _strip_trailing_empty(tokens: list[bytes]) -> list[bytes]:
    """Bỏ byte 0 thừa ở cuối danh sách token (gói không có option)."""
    while tokens and tokens[-1] == b"":
        tokens = tokens[:-1]
    return tokens


def _parse_options(raw_tokens: list[bytes]) -> dict[str, str]:
    if len(raw_tokens) % 2:
        raise tftp_errors.TftpFormatError("số trường của option không chẵn")
    options: dict[str, str] = {}
    for index in range(0, len(raw_tokens), 2):
        name = _decode_text(raw_tokens[index], "option name").lower()
        # RFC 2347: mỗi option chỉ xuất hiện một lần. Nếu cho phép lặp thì
        # `dict` âm thầm giữ giá trị cuối, hai bên có thể thương lượng khác
        # nhau về blksize mà không ai nhận ra.
        if name in options:
            raise tftp_errors.TftpFormatError(f"option bị lặp: {name!r}")
        # Giá trị option giữ nguyên theo byte (latin-1) để pack lại không đổi.
        # Option đã biết mà không phải số thuần bị chặn ở `parse_options`.
        options[name] = raw_tokens[index + 1].decode("latin-1")
    return options


def _parse_option_int(name: str, value: object) -> int:
    """Chuyển giá trị option thành số nguyên, chỉ nhận chuỗi toàn số ASCII.

    `int()` sẵn có chấp nhận `"+8"`, `" 8"`, `"-8"`, `"_"` hay chữ số fullwidth —
    những thứ không có nghĩa trong RFC 2347 và dễ làm hai bên lệch nhau.
    """
    text = str(value)
    if not text or not text.isascii() or not text.isdigit():
        raise tftp_errors.TftpFormatError(f"option {name} không phải số nguyên: {value!r}")
    return int(text)


def _option_to_int(name: str, options: dict[str, str], default: int | None) -> int | None:
    if name not in options:
        return default
    return _parse_option_int(name, options[name])


def validate_request(packet: RRQ | WRQ) -> None:
    """Kiểm tra tính hợp lệ của RRQ/WRQ trước khi xử lý."""
    if not packet.filename:
        raise tftp_errors.TftpIllegalOperation("tên file rỗng")
    if len(packet.filename) > MAX_FILENAME:
        raise tftp_errors.TftpIllegalOperation("tên file quá dài")
    if packet.mode not in TRANSFER_MODES:
        raise tftp_errors.TftpIllegalOperation(f"mode không hợp lệ: {packet.mode!r}")
    parse_options(packet)


def parse_options(packet: RRQ | WRQ | OACK) -> dict[str, int]:
    """Chuyển option dạng chuỗi sang số nguyên.

    Bỏ qua option không thuộc RFC 2347. Raise TftpFormatError nếu option
    đã biết nhưng giá trị không phải số nguyên thuần.
    """
    result: dict[str, int] = {}
    for name, value in packet.options.items():
        key = name.lower()
        if key not in OPTION_NAMES:
            continue
        result[key] = _parse_option_int(key, value)
    return result


# --------------------------------------------------------------- pack/unpack ---
def pack(packet: AnyPacket) -> bytes:
    """Chuyển một đối tượng gói tin thành byte để gửi đi."""
    if not isinstance(packet, Packet):
        raise TypeError(f"không phải gói tin TFTP: {type(packet).__name__}")
    return packet.pack()


def unpack(data: bytes) -> AnyPacket:
    """Giải mã byte nhận được thành đối tượng gói tin, kiểm tra định dạng RFC 1350."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("dữ liệu phải là bytes-like")
    data = bytes(data)
    if len(data) < 2:
        raise tftp_errors.TftpFormatError(f"gói tin quá ngắn ({len(data)} byte)")

    opcode = struct.unpack("!H", data[:2])[0]
    if opcode not in VALID_OPCODES:
        raise tftp_errors.TftpFormatError(f"opcode không hợp lệ: {opcode}")

    if opcode in (OP_RRQ, OP_WRQ):
        tokens = _split_cstrings(data[2:], 3)
        filename = _decode_text(tokens[0], "filename")
        mode = _decode_text(tokens[1], "mode")
        options = _parse_options(_strip_trailing_empty(tokens[2:]))
        cls = RRQ if opcode == OP_RRQ else WRQ
        packet: AnyPacket = cls(filename=filename, mode=mode, options=options)
        validate_request(packet)
        return packet

    if opcode == OP_DATA:
        if len(data) < 4:
            raise tftp_errors.TftpFormatError("DATA tối thiểu phải có 4 byte")
        block = struct.unpack("!H", data[2:4])[0]
        return DATA(block=block, data=data[4:])

    if opcode == OP_ACK:
        if len(data) != 4:
            raise tftp_errors.TftpFormatError(f"ACK phải đúng 4 byte, nhận {len(data)} byte")
        block = struct.unpack("!H", data[2:4])[0]
        return ACK(block=block)

    if opcode == OP_ERROR:
        if len(data) < 4:
            raise tftp_errors.TftpFormatError("ERROR tối thiểu phải có 4 byte")
        code = struct.unpack("!H", data[2:4])[0]
        body = data[4:]
        # RFC 1350 §4: errmsg bắt buộc kết thúc bằng byte 0. Thiếu byte 0 thì
        # `ERROR(code, "")` sẽ đóng gói lại thành thông điệp mặc định, khiến
        # pack->unpack không ổn định và che mất gói tin bị cắt cụt.
        if not body:
            raise tftp_errors.TftpFormatError("ERROR thiếu thông điệp")
        if not body.endswith(b"\x00"):
            raise tftp_errors.TftpFormatError("ERROR thiếu byte 0 kết thúc thông điệp")
        message = body[:-1].decode("utf-8", "replace")
        return ERROR(code=code, message=message)

    tokens = _split_cstrings(data[2:], 2)
    options = _parse_options(_strip_trailing_empty(tokens))
    if not options:
        raise tftp_errors.TftpFormatError("OACK phải có ít nhất một option")
    packet = OACK(options=options)
    # Option đã biết mà giá trị không phải số thuần => OACK rác, từ chối ngay
    # thay vì đẩy lỗi xuống tầng thương lượng.
    parse_options(packet)
    return packet


def is_valid(data: bytes) -> bool:
    """True nếu `data` là một gói tin TFTP hợp lệ về mặt định dạng."""
    try:
        unpack(data)
    except (tftp_errors.TftpError, TypeError):
        return False
    return True


def describe(packet: AnyPacket) -> str:
    """Mô tả ngắn gói tin để ghi log."""
    if isinstance(packet, (RRQ, WRQ)):
        opts = ", ".join(f"{k}={v}" for k, v in packet.options.items()) or "-"
        return f"{OPCODE_NAMES[packet.OPCODE]} {packet.filename} mode={packet.mode} opts=[{opts}]"
    if isinstance(packet, DATA):
        return f"DATA block={packet.block} len={len(packet.data)}"
    if isinstance(packet, ACK):
        return f"ACK block={packet.block}"
    if isinstance(packet, OACK):
        opts = ", ".join(f"{k}={v}" for k, v in packet.options.items())
        return f"OACK [{opts}]"
    if isinstance(packet, ERROR):
        return f"ERROR {packet.code} '{packet.message}'"
    return repr(packet)


def make_error(code: int, message: str = "") -> bytes:
    """Tạo nhanh byte gói ERROR."""
    return ERROR(code=code, message=message).pack()


# ------------------------------------------------------------------- netascii ---
def bytes_to_netascii(data: bytes) -> bytes:
    """Chuyển dữ liệu 8-bit sang netascii (LF -> CRLF, CR -> CRLF)."""
    data = data.replace(b"\r\n", b"\n")
    return data.replace(b"\r", b"\n").replace(b"\n", b"\r\n")


def netascii_to_bytes(data: bytes) -> bytes:
    """Chuyển dữ liệu netascii về 8-bit (CRLF -> LF, CR -> LF)."""
    data = data.replace(b"\r\n", b"\n")
    return data.replace(b"\r", b"\n")


class NetasciiReader:
    """Đọc file và trả từng khối netascii, **luôn đúng `blksize` byte** trừ khối cuối.

    LF -> CRLF làm dữ liệu dài ra, nên phải convert *trước* rồi mới cắt khối:
    đọc `blksize` byte thô rồi convert có thể sinh khối gấp đôi blksize
    (vi phạm RFC 2347) và làm sai phát hiện block cuối.

    Bất biến quan trọng nhất: **chỉ khối cuối mới được ngắn hơn blksize**, vì
    người nhận coi DATA ngắn là kết thúc truyền (RFC 1350 §4.2). Nếu khối ở
    giữa file bị ngắn, file nhận được sẽ bị cắt cụt.

    Vì buộc phải đầy khối, biên khối đôi khi rơi đúng giữa cặp CRLF. Phía nhận
    phải ghép lại bằng :class:`NetasciiDecoder` (xem `docs/RFC-1350-notes.md`).
    """

    def __init__(self, handle, blksize: int) -> None:
        self.handle = handle
        self.blksize = blksize
        self._buf = bytearray()
        self._eof = False

    def _fill(self) -> None:
        """Đọc + convert cho tới khi đủ `blksize` byte trên dây hoặc hết file."""
        while len(self._buf) < self.blksize and not self._eof:
            raw = self.handle.read(self.blksize - len(self._buf))
            if not raw:
                self._eof = True
                break
            if raw.endswith(b"\r"):
                # Không để cặp CRLF bị tách ở phía *thô*: nếu không, byte \n đứng
                # đầu lần đọc sau sẽ thành một kết thúc dòng thừa.
                try:
                    nxt = self.handle.read(1)
                except OSError:
                    nxt = b""
                if nxt == b"\n":
                    raw = raw + nxt
                elif nxt:
                    try:
                        self.handle.seek(-1, 1)
                    except OSError:
                        pass
            self._buf += bytes_to_netascii(raw)

    def read_chunk(self) -> bytes:
        self._fill()
        chunk = bytes(self._buf[: self.blksize])
        del self._buf[: self.blksize]
        return chunk


class NetasciiDecoder:
    """Giải mã netascii theo luồng, tự nối cặp CRLF bị tách qua 2 DATA block.

    Mỗi block đều có thể được convert độc lập **trừ** khi block kết thúc bằng
    ``\\r`` — dấu hiệu nửa đầu của một cặp CRLF nằm sang block kế tiếp. Decoder
    giữ lại byte đó và ghép vào đầu block sau, nên dữ liệu luôn khớp với gốc.

    Với sender không cắt CRLF (đa số cài đặt khác), ``_pending`` luôn rỗng và
    decoder hành xử y hệt ``netascii_to_bytes``.
    """

    def __init__(self) -> None:
        self._pending = b""

    def feed(self, data: bytes) -> bytes:
        """Nhận payload của một DATA và trả về phần đã giải mã (có thể rỗng)."""
        out = b""
        if self._pending:
            if data[:1] == b"\n":
                # Nửa "\n" của cặp CRLF bị tách: cả cặp chỉ là MỘT kết thúc dòng.
                out = b"\n"
                data = data[1:]
            else:
                out = netascii_to_bytes(self._pending)
            self._pending = b""
        if data.endswith(b"\r"):
            self._pending = data[-1:]
            data = data[:-1]
        return out + netascii_to_bytes(data)

    def flush(self) -> bytes:
        """Trả nốt byte treo dở (chỉ xảy ra nếu luồng kết thúc giữa cặp CRLF)."""
        pending, self._pending = self._pending, b""
        return netascii_to_bytes(pending)
