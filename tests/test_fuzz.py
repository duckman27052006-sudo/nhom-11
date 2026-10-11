"""Fuzz + malformed packet test (bù mục 2.2 của REVIEW.md).

Ba tầng bảo vệ, từ rẻ nhất đến đắt nhất:

1. **Corpus cố định** — các gói tin hỏng có dạng rõ ràng (thiếu byte 0 kết thúc,
   option lặp, giá trị option không phải số, ACK thừa byte, ...).
2. **Fuzz có seed** — sinh ngẫu nhiên nhưng tái lập được (`random.Random(seed)`),
   gồm byte ngẫu nhiên thuần và biến thể của gói tin hợp lệ (cắt, chèn, đảo byte).
   Bất biến kiểm tra: `unpack` **không bao giờ** ném ngoại lệ Python thô
   (`IndexError`, `struct.error`, `UnicodeDecodeError`...), chỉ `TftpError` hoặc
   `TypeError`; gói nào parse được thì pack lại phải cho cùng ý nghĩa.
3. **Fuzz ở tầng socket** — ném hàng trăm datagram rác vào server thật rồi kiểm tra
   server vẫn sống, không ghi nhận lỗi chưa xử lý và vẫn phục vụ RRQ bình thường.

Fuzz ngẫu nhiên có seed nên CI không bao giờ "xanh rồi đỏ"; khi tìm ra lỗi thật,
chuyển case đó xuống corpus cố định ở trên.
"""

from __future__ import annotations

import io
import logging
import os
import random
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import tftp_errors, tftp_packet
from src.tftp_client import TftpClient
from src.tftp_packet import (
    ACK,
    DATA,
    ERROR,
    MAX_BLKSIZE,
    OACK,
    RRQ,
    WRQ,
    NetasciiDecoder,
    NetasciiReader,
    bytes_to_netascii,
    is_valid,
    netascii_to_bytes,
    unpack,
)
from src.tftp_server import TftpServer
from tests.test_e2e import ServerHarness, make_payload

QUIET = logging.getLogger("tftp.fuzz")
QUIET.setLevel(logging.CRITICAL)

SEEDS = (1337, 20261011, 424242)

#: Corpus gói tin hỏng: (mô tả, byte thô).
MALFORMED_CORPUS: tuple[tuple[str, bytes], ...] = (
    ("rỗng", b""),
    ("chỉ 1 byte", b"\x00"),
    ("opcode lạ 0", b"\x00\x00\x00\x00"),
    ("opcode lạ 7", b"\x00\x07\x00\x00"),
    ("opcode lạ 65535", b"\xff\xff"),
    ("RRQ thiếu byte 0 cuối", b"\x00\x01hello\x00octet"),
    ("RRQ thiếu mode", b"\x00\x01hello\x00"),
    ("RRQ filename rỗng", b"\x00\x01\x00octet\x00"),
    ("RRQ mode rỗng", b"\x00\x01hello\x00\x00"),
    ("RRQ mode lạ", b"\x00\x01hello\x00binary\x00"),
    ("RRQ không phải ASCII", b"\x00\x01he\xffllo\x00octet\x00"),
    ("RRQ option lệ về", b"\x00\x01a\x00octet\x00blksize\x00"),
    ("RRQ option thiếu value", b"\x00\x01a\x00octet\x00blksize\x00\x00"),
    ("RRQ option lặp", b"\x00\x01a\x00octet\x00blksize\x001468\x00blksize\x00999\x00"),
    ("RRQ option lặp khác tên", b"\x00\x01a\x00octet\x00tsize\x001\x00tsize\x002\x00"),
    ("RRQ option không phải số", b"\x00\x01a\x00octet\x00blksize\x00abc\x00"),
    ("RRQ option có dấu +", b"\x00\x01a\x00octet\x00blksize\x00+8\x00"),
    ("RRQ option có khoảng trắng", b"\x00\x01a\x00octet\x00blksize\x00 8 \x00"),
    ("RRQ option âm", b"\x00\x01a\x00octet\x00blksize\x00-8\x00"),
    ("RRQ option số fullwidth", b"\x00\x01a\x00octet\x00blksize\x00\xef\xbc\x98\x00"),
    ("RRQ option rỗng tên", b"\x00\x01a\x00octet\x00\x001468\x00"),
    ("DATA thiếu block", b"\x00\x03\x00"),
    ("ACK thiếu byte", b"\x00\x04\x00"),
    ("ACK thừa byte", b"\x00\x04\x00\x01\x00"),
    ("ACK thừa nhiều byte", b"\x00\x04\x00\x01\xff\xff"),
    ("ERROR thiếu code", b"\x00\x05\x00"),
    ("ERROR thiếu message", b"\x00\x05\x00\x01"),
    ("ERROR thiếu byte 0", b"\x00\x05\x00\x01khong co"),
    ("OACK không option", b"\x00\x06\x00"),
    ("OACK option lệ về", b"\x00\x06blksize\x00"),
    ("OACK option lặp", b"\x00\x06tsize\x001\x00tsize\x002\x00"),
    ("OACK option không phải số", b"\x00\x06blksize\x00xyz\x00"),
    ("filename vượt 255 byte", b"\x00\x01" + b"a" * 300 + b"\x00octet\x00"),
)


def wait_for_idle(server: TftpServer, timeout: float = 15.0) -> None:
    """Chờ mọi phiên của server kết thúc (kiểm tra rò rỉ tài nguyên)."""
    deadline = time.monotonic() + timeout
    while server.active_sessions and time.monotonic() < deadline:
        time.sleep(0.01)


class TestMalformedCorpus(unittest.TestCase):
    """Mọi gói hỏng trong corpus đều phải bị từ chối bằng TftpError chuẩn."""

    def test_corpus_is_rejected(self):
        for name, raw in MALFORMED_CORPUS:
            with self.subTest(gói=name):
                with self.assertRaises(tftp_errors.TftpError):
                    unpack(raw)
                self.assertFalse(is_valid(raw), f"{name} phải bị is_valid từ chối")

    def test_corpus_has_no_duplicate_bytes(self):
        seen = {raw for _, raw in MALFORMED_CORPUS}
        self.assertEqual(len(seen), len(MALFORMED_CORPUS))


class TestFuzzUnpack(unittest.TestCase):
    """Sinh ngẫu nhiên có seed; bất biến là unpack không vỡ, và pack ổn định."""

    @staticmethod
    def _canonical(packet) -> tuple:
        """Hình dạng "ý nghĩa" của gói: bỏ option lạ mà `pack` cố tình không ghi."""
        fields = {
            name: value
            for name, value in vars(packet).items()
            if name != "options"
        }
        options = {
            name: value
            for name, value in getattr(packet, "options", {}).items()
            if name.lower() in tftp_packet.OPTION_NAMES
        }
        return (type(packet).__name__, fields, options)

    def _assert_never_crashes(self, raw: bytes, origin: str) -> None:
        try:
            packet = unpack(raw)
        except (tftp_errors.TftpError, TypeError):
            return
        except Exception as exc:  # noqa: BLE001 - đây chính là lỗi cần bắt
            self.fail(f"{origin}: unpack({raw[:40]!r}...) ném {type(exc).__name__}: {exc}")
        # Parse được thì đóng gói lại không được ném lỗi, và parse lần hai
        # phải cho cùng nội dung (idempotent).
        try:
            again = unpack(packet.pack())
        except Exception as exc:  # noqa: BLE001
            self.fail(f"{origin}: pack lại lỗi {type(exc).__name__}: {exc}")
        self.assertEqual(type(again), type(packet), origin)
        self.assertEqual(self._canonical(again), self._canonical(packet), f"{origin}: lỗi ổn định")

    def test_random_bytes_never_crash_parser(self):
        for seed in SEEDS:
            rng = random.Random(seed)
            for _ in range(2000):
                length = rng.randint(0, 64)
                raw = bytes(rng.randrange(256) for _ in range(length))
                self._assert_never_crashes(raw, f"seed={seed} ngẫu nhiên")

    def test_random_bytes_with_valid_opcode_never_crash(self):
        # Ngẫu nhiên thuần hiếm khi trúng opcode hợp lệ; ép opcode trước để vào
        # sâu nhất của từng nhánh parse.
        for seed in SEEDS:
            rng = random.Random(seed + 1)
            for opcode in (tftp_packet.OP_RRQ, tftp_packet.OP_WRQ, tftp_packet.OP_DATA,
                           tftp_packet.OP_ACK, tftp_packet.OP_ERROR, tftp_packet.OP_OACK):
                for _ in range(400):
                    raw = struct.pack("!H", opcode)
                    raw += bytes(rng.randrange(256) for _ in range(rng.randint(0, 48)))
                    self._assert_never_crashes(raw, f"seed={seed} opcode={opcode}")

    def test_mutated_valid_packets_never_crash_parser(self):
        templates = [
            RRQ("data.bin", "octet", {"blksize": "1468", "timeout": "3", "tsize": "0"}).pack(),
            WRQ("data.bin", "netascii", {"tsize": "12345"}).pack(),
            OACK({"blksize": "1468", "tsize": "0"}).pack(),
            DATA(1, b"payload" * 10).pack(),
            ACK(0xFFFF).pack(),
            ERROR(2, "Access violation").pack(),
        ]
        for seed in SEEDS:
            rng = random.Random(seed + 2)
            for template in templates:
                for _ in range(500):
                    raw = bytearray(template)
                    action = rng.randrange(4)
                    if action == 0 and raw:  # đảo byte ngẫu nhiên
                        raw[rng.randrange(len(raw))] = rng.randrange(256)
                    elif action == 1 and raw:  # cắt bớt
                        del raw[rng.randrange(len(raw)) :]
                    elif action == 2:  # chèn byte rác
                        raw.insert(rng.randrange(len(raw) + 1), rng.randrange(256))
                    else:  # nhân bản toàn bộ
                        raw.extend(template)
                    self._assert_never_crashes(bytes(raw), f"seed={seed} mutate")

    def test_unpack_rejects_non_bytes_input(self):
        for value in (None, 42, "RRQ", [1, 2, 3]):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    unpack(value)

    def test_pack_rejects_non_packet(self):
        with self.assertRaises(TypeError):
            tftp_packet.pack(object())

    def test_oversized_data_is_rejected(self):
        with self.assertRaises(tftp_errors.TftpIllegalOperation):
            DATA(1, b"x" * (MAX_BLKSIZE + 1)).pack()

    def test_block_number_must_fit_16_bits(self):
        for block in (0x10000, 0x1_0000, -1):
            with self.subTest(block=block):
                with self.assertRaises(tftp_errors.TftpIllegalOperation):
                    DATA(block, b"x").pack()
                with self.assertRaises(tftp_errors.TftpIllegalOperation):
                    ACK(block).pack()


class TestNegotiationInvariants(unittest.TestCase):
    """Option thương lượng: giá trị trả về luôn nằm trong khoảng hợp lệ."""

    def test_fuzzed_blksize_values_never_leave_range(self):
        server = TftpServer(host="127.0.0.1", port=0, root=tempfile.mkdtemp())
        try:
            rng = random.Random(SEEDS[0])
            for _ in range(500):
                value = rng.randint(0, 100_000)
                accepted = server.negotiate_options({"blksize": str(value)}, None)
                if accepted:
                    got = int(accepted["blksize"])
                    self.assertTrue(8 <= got <= MAX_BLKSIZE, f"{value} -> {got}")
            for _ in range(200):
                value = rng.randint(0, 1000)
                accepted = server.negotiate_options({"timeout": str(value)}, None)
                if accepted:
                    self.assertTrue(1 <= int(accepted["timeout"]) <= 255, str(value))
        finally:
            server.shutdown()

    def test_non_numeric_option_is_rejected_before_negotiation(self):
        server = TftpServer(host="127.0.0.1", port=0, root=tempfile.mkdtemp())
        try:
            for value in ("-1", "+8", " 8", "8.0", "0x10", "1e3", ""):
                with self.subTest(value=value):
                    with self.assertRaises(tftp_errors.TftpFormatError):
                        server.negotiate_options({"blksize": value}, None)
        finally:
            server.shutdown()

    def test_duplicate_options_are_rejected(self):
        raw = b"\x00\x01a\x00octet\x00blksize\x001468\x00blksize\x00999\x00"
        with self.assertRaises(tftp_errors.TftpFormatError):
            unpack(raw)

    def test_option_values_must_be_plain_digits(self):
        for value in (b"abc", b"+8", b" 8 ", b"-8", b"", b"8.0", b"1_000"):
            with self.subTest(value=value):
                raw = b"\x00\x01a\x00octet\x00blksize\x00" + value + b"\x00"
                with self.assertRaises(tftp_errors.TftpFormatError):
                    unpack(raw)

    def test_oack_rejects_non_numeric(self):
        packet = OACK({"blksize": "1024"})
        self.assertEqual(packet.as_int("blksize"), 1024)
        with self.assertRaises(tftp_errors.TftpFormatError):
            OACK({"blksize": "1024 "}).as_int("blksize")


class TestFuzzNetascii(unittest.TestCase):
    """Netascii phải giữ được bất biến: chỉ khối cuối mới ngắn hơn blksize."""

    def _chunks(self, payload: bytes, blksize: int) -> list[bytes]:
        reader = NetasciiReader(io.BytesIO(payload), blksize)
        out = []
        for _ in range(1_000_000):
            chunk = reader.read_chunk()
            out.append(chunk)
            if len(chunk) < blksize:
                return out
        self.fail("NetasciiReader không tiến triển")

    def test_fuzzed_payloads_round_trip(self):
        for seed in SEEDS:
            rng = random.Random(seed + 3)
            for _ in range(120):
                size = rng.randint(0, 4000)
                # Tập trung mật độ byte điều khiển để bắt lỗi biên CRLF.
                alphabet = bytes(range(256)) if rng.random() < 0.3 else b"\r\n\tabc \x00\xff"
                payload = bytes(rng.choice(alphabet) for _ in range(size))
                blksize = rng.choice((8, 9, 16, 511, 512, 513, 1468))
                with self.subTest(seed=seed, size=size, blksize=blksize):
                    chunks = self._chunks(payload, blksize)
                    for chunk in chunks[:-1]:
                        self.assertEqual(len(chunk), blksize, "khối giữa file bị ngắn")
                    self.assertLess(len(chunks[-1]), blksize)
                    decoder = NetasciiDecoder()
                    decoded = b"".join(decoder.feed(c) for c in chunks) + decoder.flush()
                    self.assertEqual(decoded, netascii_to_bytes(bytes_to_netascii(payload)))

    def test_decoder_survives_arbitrary_chunks(self):
        rng = random.Random(SEEDS[1])
        for _ in range(200):
            decoder = NetasciiDecoder()
            produced = b""
            for _ in range(rng.randint(1, 20)):
                chunk = bytes(rng.randrange(256) for _ in range(rng.randint(0, 64)))
                produced += decoder.feed(chunk)
            produced += decoder.flush()
            # Decoder không bao giờ nuốt byte: số byte thô rút ra luôn bằng
            # số byte trả về cộng byte đang treo.
            self.assertLessEqual(len(produced), 64 * 20 + 1)


class TestFuzzResolvePath(unittest.TestCase):
    """Tên file tùy ý không bao giờ đưa được đường dẫn ra ngoài root."""

    @staticmethod
    def _norm(path: Path) -> str:
        # `Path.resolve()` trên Windows trả về tiền tố `\\?\` cho đường dẫn dài;
        # bỏ đi rồi so sánh không phân biệt hoa/thường.
        text = str(path)
        if text.startswith("\\\\?\\"):
            text = text[4:]
        return os.path.normcase(text).rstrip("\\")

    def test_fuzzed_names_never_escape_root(self):
        with tempfile.TemporaryDirectory() as root:
            server = TftpServer(host="127.0.0.1", port=0, root=root, allow_subdirs=True)
            rng = random.Random(SEEDS[2])
            nasty = ["..", "/", "\\", ".", "CON", "aux", "nul", "", " ", "."]
            base = self._norm(Path(root).resolve())
            for _ in range(2000):
                parts = [rng.choice(nasty) for _ in range(rng.randint(1, 4))]
                name = "/".join(parts) if rng.random() < 0.5 else "".join(parts)
                path = server.resolve_path(name)
                if path is None:
                    continue
                resolved = self._norm(Path(path).resolve())
                self.assertTrue(
                    resolved.startswith(base),
                    f"{name!r} -> {resolved} nằm ngoài root {base}",
                )
            server.shutdown()

    def test_known_escape_attempts_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            server = TftpServer(host="127.0.0.1", port=0, root=root)
            for name in (
                "../secret.txt",
                "..\\secret.txt",
                "/etc/passwd",
                "C:\\Windows\\win.ini",
                "sub/../../escape.txt",
                "NUL",
                "con.txt",
                "file\u0000name",
                "file\u007fname",
                "trailing.",
                "trailing ",
                "",
            ):
                with self.subTest(name=name):
                    self.assertIsNone(server.resolve_path(name))
            server.shutdown()


class TestFuzzAgainstLiveServer(unittest.TestCase):
    """Ném datagram rác vào server thật: phải trả ERROR và vẫn sống."""

    def test_server_survives_garbage_and_keeps_serving(self):
        payload = make_payload(6000, seed=99)
        with ServerHarness({"good.bin": payload}) as srv:
            rng = random.Random(SEEDS[0])
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.settimeout(1.0)
            probe.bind(("127.0.0.1", 0))
            address = ("127.0.0.1", srv.port)
            try:
                for _ in range(300):
                    raw = bytes(rng.randrange(256) for _ in range(rng.randint(0, 80)))
                    probe.sendto(raw, address)
                    try:
                        probe.recvfrom(65535)
                    except TimeoutError:
                        pass
                for _, raw in MALFORMED_CORPUS:
                    probe.sendto(raw, address)
                    try:
                        probe.recvfrom(65535)
                    except TimeoutError:
                        pass
                # DATA/ACK tới well-known port = TID lạ (ERROR 5).
                probe.sendto(ACK(1).pack(), address)
                error = unpack(probe.recvfrom(65535)[0])
                self.assertIsInstance(error, ERROR)
                self.assertEqual(error.code, tftp_errors.ERR_UNKNOWN_TID)
            finally:
                probe.close()

            # Server vẫn phục vụ bình thường sau cả đợt rác.
            with tempfile.TemporaryDirectory() as out:
                target = str(Path(out) / "good.bin")
                srv.client(blksize=1024, timeout=3.0).download("good.bin", target)
                self.assertEqual(Path(target).read_bytes(), payload)

            metrics = srv.server.metrics
            self.assertEqual(metrics.counter("errors.unhandled"), 0)
            self.assertGreater(metrics.counter("packets.malformed"), 0)
            self.assertGreaterEqual(metrics.counter("packets.unknown_tid"), 1)
            self.assertEqual(metrics.gauge("sessions.active"), 0)

    def test_client_rejects_garbage_from_server(self):
        """Client nhận datagram rác từ TID server phải báo lỗi Tftp, không crash."""

        class GarbageServer(threading.Thread):
            def __init__(self) -> None:
                super().__init__(daemon=True)
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.sock.bind(("127.0.0.1", 0))
                self.port = self.sock.getsockname()[1]

            def run(self) -> None:
                self.sock.settimeout(5)
                try:
                    _, addr = self.sock.recvfrom(65535)
                    self.sock.sendto(b"\x00\x63\xff\xff", addr)
                    self.sock.recvfrom(65535)
                except OSError:
                    pass
                finally:
                    self.sock.close()

        server = GarbageServer()
        server.start()
        try:
            with tempfile.TemporaryDirectory() as out:
                target = str(Path(out) / "never.bin")
                client = TftpClient("127.0.0.1", server.port, timeout=1.0, retries=2, logger=QUIET)
                with self.assertRaises(tftp_errors.TftpError):
                    client.download("never.bin", target)
                self.assertEqual(client.metrics.counter("packets.malformed"), 1)
                self.assertEqual(client.metrics.counter("download.fail"), 1)
        finally:
            server.join(timeout=5)


class TestOversizedDataRejected(unittest.TestCase):
    def test_client_sending_oversized_data_gets_error_4(self):
        with ServerHarness() as srv:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.settimeout(3)
            probe.bind(("127.0.0.1", 0))
            try:
                probe.sendto(
                    WRQ("big-blocks.bin", "octet", {"blksize": "512"}).pack(),
                    ("127.0.0.1", srv.port),
                )
                raw, tid = probe.recvfrom(65535)
                oack = unpack(raw)
                self.assertIsInstance(oack, OACK)
                self.assertEqual(oack.as_int("blksize"), 512)
                # Từ đây mọi gói phải tới TID mới, không phải well-known port.
                probe.sendto(ACK(0).pack(), tid)
                probe.sendto(DATA(1, b"x" * 600).pack(), tid)
                error = unpack(probe.recvfrom(65535)[0])
            finally:
                probe.close()
            self.assertIsInstance(error, ERROR)
            self.assertEqual(error.code, tftp_errors.ERR_ILLEGAL_OPERATION)
            self.assertEqual(srv.server.metrics.counter("packets.oversized_data"), 1)
            wait_for_idle(srv.server)
            self.assertFalse([p for p in Path(srv.root).iterdir() if p.suffix == ".part"])
            self.assertFalse((Path(srv.root) / "big-blocks.bin").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
