# Ghi chú RFC 1350 và những chỗ dễ làm sai

Tài liệu này ghi lại các quy tắc của chuẩn và ánh xạ sang code trong dự án, dùng
khi đọc code hoặc khi tự kiểm tra bằng Wireshark.

## 1. Cấu trúc gói tin (RFC 1350 §4)

Tất cả số nguyên dùng **network byte order (big-endian)**, `struct.pack("!H", ...)`.

| Gói | Bố cục |
|---|---|
| RRQ/WRQ | `opcode(2) filename 0 mode 0 [option 0 value 0 ...]` |
| DATA | `opcode(2) block(2) data(0..blksize)` |
| ACK | `opcode(2) block(2)` — **đúng 4 byte** |
| ERROR | `opcode(2) code(2) msg 0` |

| Opcode | Tên |
|---|---|
| 1 | RRQ |
| 2 | WRQ |
| 3 | DATA |
| 4 | ACK |
| 5 | ERROR |
| 6 | OACK (RFC 2347) |

Code: `src/tftp_packet.py` — các lớp `RRQ`, `WRQ`, `DATA`, `ACK`, `ERROR`, `OACK`.

## 2. Well-known port và TID (RFC 1350 §4.2)

- Request luôn gửi tới **UDP 69**.
- Khi nhận RRQ/WRQ hợp lệ, server phải chọn một **TID mới** (port ngẫu nhiên) và
  **mọi gói tin sau đó đều đi từ/đến TID đó**.
- Gói đến từ một TID lạ phải bị bỏ qua (client) — tránh lẫn dữ liệu khi nhiều
  client cùng chạy (RFC 1123 §4.2).
- Gói DATA/ACK/OACK tới well-known port (TID không tồn tại): server trả
  ERROR 5 `Unknown transfer ID` **từ port 69** (`TftpServer._handle_datagram`).
- Port nguồn UDP không phải định danh duy nhất: OS có thể cấp lại cùng
  ephemeral port cho client mới. Server so khớp
  `(addr, filename, mode, options)` (`Session.matches`); nếu khác nhau thì
  đóng phiên cũ và mở phiên mới, không replay nhầm.

Sau khi client đã biết TID của server, khi timeout client gửi lại **đúng gói ACK/Data
đang chờ** tới TID đó, không gửi lại RRQ. Trước khi biết TID thì gửi lại RRQ tới 69.
Server nhận lại RRQ giống hệt từ một client đang có phiên sẽ **replay** gói đang chờ từ TID cũ
(`Session.replay`), tránh việc sinh phiên mới mỗi lần retry.

## 3. Kết thúc truyền (RFC 1350 §4.2, §4.3)

- DATA cuối cùng **nhỏ hơn** blksize ⇒ đã xong.
- File rỗng vẫn phải gửi **một DATA rỗng** (block 1, 0 byte); client nhận được
  block đó thì coi như xong và vẫn phải ACK.
- File có kích thước đúng bằng `n * blksize` cần thêm một DATA rỗng ở cuối.
- Trong WRQ, server **chỉ gửi ACK**; chỉ DATA mới kết thúc luồng.
- **DATA vượt quá blksize đã thương lượng → ERROR 4.** Nếu chấp nhận, phía nhận
  có thể cắt cụt file: khối đó không phải khối cuối theo cách hiểu của họ, còn
  người gửi lại coi nó là bình thường.
- Gói ERROR phải có `errmsg` kết thúc bằng byte 0; thiếu byte 0 thì đó là gói
  bị cắt cụt và bị từ chối (`unpack`).

## 4. Timeout và retransmission (RFC 1350 §4.2)

- Mỗi gói đều có bộ đếm thời gian chờ (mặc định 5 giây).
- Hết thời gian: gửi lại **gói tin gần nhất đã gửi**, tối đa 5 lần rồi báo lỗi.
- Gói trùng:
  - client nhận DATA cũ → **không ghi lại vào file**, chỉ gửi lại ACK.
  - server nhận DATA cũ → **không ghi lại vào file**, chỉ gửi lại ACK.
  - nhận ACK cũ → gửi lại DATA/ACK hiện tại.

## 5. Sorcerer's Apprentice Syndrome

Nếu mỗi ACK trùng lại làm server gửi lại **hai** bản DATA cũ, số gói tin sẽ bùng
nổ theo cấp số nhân. Cách phòng chống đã dùng:

1. Khi nhận ACK trùng, server gửi lại **gói đang chờ** chứ không phải bản DATA trước đó.
2. Ngân sách gói "rác" (`MAX_STALE_PACKETS = 20`): quá ngân sách thì huỷ phiên với
   ERROR 4 thay vì spam mạng.

## 6. Option RFC 2347

```
RRQ/WRQ kèm option:  | 00 01 file 00 octet 00 blksize 00 1468 00 timeout 00 3 00 |
Server đồng ý:        | 00 06 blksize 00 1468 00 timeout 00 3 00 |                (OACK)
```

- `blksize`: 8..65464, mặc định 512. Nếu client xin ngoài khoảng, server trả
  giá trị mặc định của server (`--blksize`, mặc định 1468).
- `timeout`: 1..255 (giây).
- `tsize`: client RRQ gửi `tsize=0` để hỏi, server RRQ trả về kích thước file
  thật; client WRQ gửi `tsize=<kích thước thật>`, server WRQ trả `tsize=0`
  và dùng giá trị client gửi để kiểm tra `--max-file-size` trước khi nhận.
- Block number chỉ 16 bit: `(block+1) & 0xFFFF`, wrap 65535 → 0 → 1...
- netascii: mỗi DATA trên dây **đúng bằng** blksize đã thương lượng, trừ block cuối
  (`NetasciiReader`). LF -> CRLF làm dữ liệu dài ra nên phải convert *trước* rồi mới cắt
  khối; đọc `blksize` byte thô rồi convert sẽ sinh khối gấp đôi và sai phát hiện
  block cuối. Vì buộc phải đầy khối, biên khối đôi khi rơi đúng giữa cặp CRLF — phía
  nhận phải ghép lại bằng `NetasciiDecoder` (giữ 1 byte `\r` qua block kế tiếp).
- `errmsg` dùng UTF-8 (superset của ASCII) để giữ tiếng Việt.
- Option **không được** server chấp nhận thì bị bỏ qua; nếu không còn option nào
  được chấp nhận thì server **không gửi OACK** mà đi thẳng vào luồng DATA/ACK.
  Khi đó **cả hai bên bắt buộc dùng blksize 512** — kể cả khi client đề nghị
  1468 và server được cấu hình `--blksize 1468` (`Session._negotiate`).
- Client phải chấp nhận giá trị server đồng ý, kể cả khi khác giá trị client đề nghị.

### Cách kiểm tra option (đã siết lại)

| Quy tắc | Lý do | Nơi xử lý |
|---|---|---|
| Option **lặp tên** trong RRQ/WRQ/OACK → ERROR 4 | `dict` âm thầm giữ giá trị cuối, hai bên có thể thương lượng khác nhau về `blksize` mà không ai biết | `_parse_options` |
| Giá trị option phải là **chuỗi toàn số ASCII** | `int()` của Python nhận cả `"+8"`, `" 8 "`, `"-8"`, chữ số fullwidth — vô nghĩa với RFC 2347 | `_parse_option_int` |
| `timeout` luôn ép về **1..255** | `int(0.4)` = 0; `select(..., 0)` không chờ nên server quay vòng gửi lại và nuốt ACK | `clamp_timeout` |
| OACK có option đã biết mà giá trị không phải số → ERROR 4 | OACK rác phải bị chặn ngay, không đẩy lỗi xuống tầng thương lượng | `unpack` |
| Option lạ (không thuộc RFC 2347) vẫn bị bỏ qua, **không** ghi khi `pack` | giữ nguyên hành vi "bỏ qua option không hỗ trợ" | `_encode_options` |

Giá trị option được giải mã/đóng gói bằng **latin-1** (ánh xạ 1 byte ↔ 1 code
point) nên `unpack` → `pack` → `unpack` giữ nguyên byte kể cả với option lạ.

### Trình tự sau OACK (chỗ dễ làm sai nhất)

| Hướng | Trình tự bắt buộc |
|---|---|
| RRQ (server gửi) | `OACK` → nhận `ACK 0` → `DATA 1` → `ACK 1` → ... |
| WRQ (client gửi) | `OACK` → **client gửi `ACK 0`** → `DATA 1` → `ACK 1` → ... |

Ở hướng WRQ, server **không được gửi thêm gói nào** trước `DATA 1`: client gửi
`ACK 0` rồi ngay lập tức gửi `DATA 1` mà không chờ phản hồi. Vì vậy trong
`TftpClient.upload` lệnh `_send(sock, ACK(0))` là gửi một chiều, còn phía server
dùng `_exchange(..., send=False)` để chờ DATA 1.

## 7. Mã lỗi (RFC 1350 §3)

| Mã | Thông điệp chuẩn | Dùng khi |
|---|---|---|
| 0 | Not defined | lỗi chung, hết số lần retry |
| 1 | File not found | RRQ tới file không tồn tại |
| 2 | Access violation | tên file không hợp lệ / path traversal / không có quyền |
| 3 | Disk full | không ghi được file |
| 4 | Illegal TFTP operation | opcode lạ, gói hỏng, ACK loạn |
| 5 | Unknown transfer ID | gói tới TID không tồn tại |
| 6 | File already exists | ghi đè khi server cấu hình `--no-overwrite` |
| 7 | No such user | (không dùng, RFC để dành cho xác thực) |

## 8. Bảo mật tối thiểu

- Tên file chỉ nhận tên đơn giản: chặn `/`, `\`, `:`, `..`, đường dẫn tuyệt đối
  (`TftpServer.resolve_path`) rồi kiểm tra `commonpath` còn nằm trong `--root`.
- Ghi file: ghi vào `<tên>.tftp-<pid>-<tid>.part` rồi `os.replace` khi nhận đủ;
  hủy phiên thì xóa file tạm.
- Server giới hạn `--max-clients`, đồng thời đều có `--timeout`/`--retries` nên
  thread treo sẽ tự kết thúc.
- Mọi gói không giải mã được đều bị từ chối bằng `TftpError` — không đường nào để
  `struct.error` / `IndexError` / `UnicodeDecodeError` thoát ra ngoài
  (`tests/test_fuzz.py` kiểm bất biến này bằng fuzz có seed).

## 9. Kiểm chứng nhanh bằng Wireshark

```
tshark -i any -f "udp port 6969" -Y tftp -V
```

Các mẫu cần thấy:

| Tình huống | Chuỗi gói |
|---|---|
| RRQ + option | RRQ → OACK → ACK 0 → DATA 1 → ACK 1 → DATA 2 (< blksize) |
| RRQ thuần | RRQ → DATA 1 → ACK 1 → ... |
| WRQ + option | WRQ → OACK → ACK 0 → DATA 1 → ACK 1 → ... |
| RRQ thất bại | RRQ → ERROR 1 |
| Mất gói | `DATA 1` lặp lại, chỉ có một `ACK 1` được gửi lại |

## 10. Đo đạc

Server và client đều tích hợt sẵn registry metrics (`src/tftp_metrics.py`):
đếm phiên, gói tin, retry, byte; đo RTT, thời gian phiên, TTFB với p50/p95/p99.

```bash
python -m src.tftp_server --stats-interval 60 --stats-json metrics.json
```

Danh mục chỉ số và công thức tính: [`docs/observability.md`](observability.md).
