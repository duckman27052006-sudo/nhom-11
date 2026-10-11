"""Unit test cho lớp đóng gói / giải mã gói tin TFTP."""

from __future__ import annotations

import struct
import sys
import unittest
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import tftp_errors, tftp_packet
from src.tftp_packet import (
    ACK,
    DATA,
    ERROR,
    OACK,
    RRQ,
    WRQ,
    bytes_to_netascii,
    is_valid,
    netascii_to_bytes,
    pack,
    unpack,
)


class TestRequestPackets(unittest.TestCase):
    def test_rrq_matches_rfc_layout(self):
        raw = RRQ("hello.txt", "octet").pack()
        self.assertEqual(raw, b"\x00\x01hello.txt\x00octet\x00")

    def test_wrq_matches_rfc_layout(self):
        raw = WRQ("data.bin", "netascii").pack()
        self.assertEqual(raw, b"\x00\x02data.bin\x00netascii\x00")

    def test_request_with_options(self):
        raw = RRQ("big.bin", "octet", {"blksize": "1468", "timeout": "3"}).pack()
        self.assertEqual(raw, b"\x00\x01big.bin\x00octet\x00blksize\x001468\x00timeout\x003\x00")

    def test_unknown_option_is_not_serialized(self):
        raw = RRQ("x", "octet", {"windowsize": "4"}).pack()
        self.assertEqual(raw, b"\x00\x01x\x00octet\x00")

    def test_round_trip(self):
        packet = unpack(RRQ("hello world.bin", "octet", {"blksize": "1024"}).pack())
        self.assertIsInstance(packet, RRQ)
        self.assertEqual(packet.filename, "hello world.bin")
        self.assertEqual(packet.mode, "octet")
        self.assertEqual(packet.options, {"blksize": "1024"})

    def test_large_filename_rejected(self):
        with self.assertRaises(tftp_errors.TftpIllegalOperation):
            RRQ("a" * 300, "octet").pack()

    def test_empty_filename_rejected(self):
        with self.assertRaises(tftp_errors.TftpIllegalOperation):
            RRQ("", "octet").pack()

    def test_non_ascii_filename_rejected(self):
        with self.assertRaises(tftp_errors.TftpIllegalOperation):
            WRQ("tệp.txt", "octet").pack()

    def test_invalid_mode_rejected_on_decode(self):
        raw = struct.pack("!H", tftp_packet.OP_RRQ) + b"a\x00binary\x00"
        with self.assertRaises(tftp_errors.TftpIllegalOperation):
            unpack(raw)


class TestDataAckPackets(unittest.TestCase):
    def test_data_layout(self):
        raw = DATA(1, b"abc").pack()
        self.assertEqual(raw, b"\x00\x03\x00\x01abc")

    def test_data_empty_is_final_block(self):
        packet = unpack(DATA(7, b"").pack())
        self.assertIsInstance(packet, DATA)
        self.assertEqual(packet.block, 7)
        self.assertEqual(packet.data, b"")
        self.assertTrue(packet.is_last_for(512))

    def test_data_full_block_is_not_final(self):
        packet = DATA(3, b"x" * 512)
        self.assertFalse(packet.is_last_for(512))

    def test_is_last_for_uses_negotiated_blksize(self):
        # blksize đã thương lượng: 1000 byte là block CUỐI, không phải block giữa.
        self.assertTrue(DATA(1, b"x" * 1000).is_last_for(1468))
        self.assertFalse(DATA(1, b"x" * 1000).is_last_for(512))

    def test_max_block_number(self):
        self.assertEqual(unpack(DATA(0xFFFF, b"z").pack()).block, 0xFFFF)
        with self.assertRaises(tftp_errors.TftpIllegalOperation):
            DATA(0x10000, b"z").pack()

    def test_oversized_data_rejected(self):
        with self.assertRaises(tftp_errors.TftpIllegalOperation):
            DATA(1, b"x" * (tftp_packet.MAX_BLKSIZE + 1)).pack()

    def test_ack_layout(self):
        self.assertEqual(ACK(0).pack(), b"\x00\x04\x00\x00")
        self.assertEqual(ACK(258).pack(), b"\x00\x04\x01\x02")

    def test_ack_with_extra_bytes_rejected(self):
        with self.assertRaises(tftp_errors.TftpFormatError):
            unpack(b"\x00\x04\x00\x01\x00")


class TestErrorPackets(unittest.TestCase):
    def test_error_layout(self):
        raw = ERROR(tftp_errors.ERR_FILE_NOT_FOUND).pack()
        self.assertEqual(raw, b"\x00\x05\x00\x01File not found\x00")

    def test_error_round_trip(self):
        packet = unpack(ERROR(6, "File already exists").pack())
        self.assertIsInstance(packet, ERROR)
        self.assertEqual(packet.code, 6)
        self.assertEqual(packet.message, "File already exists")

    def test_all_codes_have_messages(self):
        for code in range(8):
            self.assertTrue(tftp_errors.error_message(code))

    def test_custom_message_wins(self):
        raw = ERROR(1, "khong tim thay").pack()
        self.assertEqual(raw, b"\x00\x05\x00\x01khong tim thay\x00")


class TestOackPackets(unittest.TestCase):
    def test_oack_layout(self):
        raw = OACK({"blksize": "1468", "timeout": "3"}).pack()
        self.assertEqual(raw, b"\x00\x06blksize\x001468\x00timeout\x003\x00")

    def test_oack_round_trip(self):
        packet = unpack(OACK({"blksize": "1024", "tsize": "0"}).pack())
        self.assertIsInstance(packet, OACK)
        self.assertEqual(packet.as_int("blksize"), 1024)
        self.assertIsNone(packet.as_int("timeout"))
        self.assertEqual(packet.as_int("timeout", 5), 5)

    def test_oack_requires_option(self):
        with self.assertRaises(tftp_errors.TftpIllegalOperation):
            OACK({}).pack()
        with self.assertRaises(tftp_errors.TftpFormatError):
            unpack(b"\x00\x06\x00")

    def test_non_numeric_option_value(self):
        with self.assertRaises(tftp_errors.TftpFormatError):
            OACK({"blksize": "abc"}).as_int("blksize")


class TestValidation(unittest.TestCase):
    def test_empty_and_short_packets(self):
        self.assertFalse(is_valid(b""))
        self.assertFalse(is_valid(b"\x00"))
        with self.assertRaises(tftp_errors.TftpFormatError):
            unpack(b"")

    def test_unknown_opcode(self):
        self.assertFalse(is_valid(b"\x00\x09\x00\x01"))
        with self.assertRaises(tftp_errors.TftpFormatError):
            unpack(b"\x00\x63abc")

    def test_data_too_short(self):
        self.assertFalse(is_valid(b"\x00\x03\x00"))

    def test_error_too_short(self):
        self.assertFalse(is_valid(b"\x00\x05\x00"))

    def test_request_missing_terminator(self):
        self.assertFalse(is_valid(b"\x00\x01hello.txt\x00octet"))

    def test_valid_packets(self):
        packets = (
            RRQ("a", "octet"),
            WRQ("a", "octet"),
            DATA(1, b"a"),
            ACK(1),
            ERROR(1),
            OACK({"tsize": "0"}),
        )
        for packet in packets:
            self.assertTrue(is_valid(pack(packet)), packet)

    def test_pack_rejects_non_packet(self):
        with self.assertRaises(TypeError):
            pack(b"\x00\x01")


class TestNetascii(unittest.TestCase):
    def test_lf_becomes_crlf(self):
        self.assertEqual(bytes_to_netascii(b"a\nb\n"), b"a\r\nb\r\n")

    def test_existing_crlf_is_not_doubled(self):
        self.assertEqual(bytes_to_netascii(b"a\r\nb"), b"a\r\nb")

    def test_lone_cr_becomes_crlf(self):
        self.assertEqual(bytes_to_netascii(b"a\rb"), b"a\r\nb")

    def test_reverse(self):
        self.assertEqual(netascii_to_bytes(b"a\r\nb\rc"), b"a\nb\nc")

    def test_round_trip_text(self):
        text = b"line one\nline two\r\nend\r"
        self.assertEqual(netascii_to_bytes(bytes_to_netascii(text)), b"line one\nline two\nend\n")


class TestNetasciiStream(unittest.TestCase):
    """Bất biến của luồng netascii: chỉ khối CUỐI mới được ngắn hơn blksize.

    Nếu khối giữa file bị ngắn, người nhận coi đó là kết thúc truyền và file bị
    cắt cụt — đây là bug từng xảy ra khi biên khối rơi đúng giữa cặp CRLF.
    """

    def _wire(self, data: bytes, blksize: int) -> list[bytes]:
        reader = tftp_packet.NetasciiReader(BytesIO(data), blksize)
        chunks = []
        while True:
            chunk = reader.read_chunk()
            chunks.append(chunk)
            if len(chunk) < blksize:
                return chunks
            self.assertLess(len(chunks), 100_000, "reader không tiến triển")

    def _decode(self, chunks) -> bytes:
        decoder = tftp_packet.NetasciiDecoder()
        return b"".join(decoder.feed(chunk) for chunk in chunks) + decoder.flush()

    def test_only_last_block_is_short(self):
        payload = b"a" * 255 + b"\n" * 600
        for blksize in (8, 16, 512, 1468):
            with self.subTest(blksize=blksize):
                chunks = self._wire(payload, blksize)
                for index, chunk in enumerate(chunks[:-1]):
                    self.assertEqual(len(chunk), blksize, f"khối {index} bị ngắn giữa file")
                self.assertLess(len(chunks[-1]), blksize)

    def test_wire_bytes_equal_plain_conversion(self):
        payload = b"".join(b"line %d\n" % index for index in range(300))
        for blksize in (8, 512, 1468):
            with self.subTest(blksize=blksize):
                chunks = self._wire(payload, blksize)
                self.assertEqual(b"".join(chunks), bytes_to_netascii(payload))

    def test_round_trip_through_reader_and_decoder(self):
        cases = [
            b"",
            b"a",
            b"abc\r",
            b"\ta\rb\nc\r\n",
            b"a" * 255 + b"\n" * 600,
            b"".join(b"line %d\r\n" % index for index in range(300)),
            bytes((index * 37) % 251 for index in range(9000)),
        ]
        for payload in cases:
            for blksize in (8, 9, 512, 1468):
                with self.subTest(size=len(payload), blksize=blksize):
                    chunks = self._wire(payload, blksize)
                    self.assertEqual(
                        self._decode(chunks),
                        netascii_to_bytes(bytes_to_netascii(payload)),
                    )

    def test_decoder_joins_crlf_split_across_blocks(self):
        decoder = tftp_packet.NetasciiDecoder()
        self.assertEqual(decoder.feed(b"abc\r"), b"abc")
        self.assertEqual(decoder.feed(b"\ndef"), b"\ndef")

    def test_decoder_is_noop_for_senders_that_do_not_split(self):
        decoder = tftp_packet.NetasciiDecoder()
        self.assertEqual(decoder.feed(b"a\r\nb"), b"a\nb")
        self.assertEqual(decoder.flush(), b"")

    def test_decoder_flushes_trailing_cr(self):
        decoder = tftp_packet.NetasciiDecoder()
        self.assertEqual(decoder.feed(b"abc\r"), b"abc")
        self.assertEqual(decoder.flush(), b"\n")


class TestDescribe(unittest.TestCase):
    def test_describe_all_types(self):
        self.assertIn("RRQ a", tftp_packet.describe(RRQ("a", "octet")))
        self.assertIn("WRQ a", tftp_packet.describe(WRQ("a", "octet")))
        self.assertIn("DATA block=2 len=5", tftp_packet.describe(DATA(2, b"12345")))
        self.assertIn("ACK block=9", tftp_packet.describe(ACK(9)))
        self.assertIn("ERROR 1", tftp_packet.describe(ERROR(1)))
        self.assertIn("blksize=8", tftp_packet.describe(OACK({"blksize": "8"})))


if __name__ == "__main__":
    unittest.main(verbosity=2)
