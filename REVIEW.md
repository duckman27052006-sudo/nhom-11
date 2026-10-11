# Review codebase TFTP — trạng thái hiện tại sau pass tối ưu hóa

> Ngày review: 2026-10-11 (đợt 1) · cập nhật sau pass hardening: 2026-10-11
> Phạm vi: `src/`, `tests/`, `docs/`, `README.md`, `pyproject.toml`, `.github/`
> Kết luận nhanh: sau pass thứ hai, project đã ở mức **production-hardened cho môi
> trường thực tâm** — có fuzz có seed, soak/stress test, metrics vận hành và CI
> chạy riêng bộ soak. Vẫn còn một khoảng trống thuần lý thuyết: chưa có kiểm thử
> chéo với TFTP client/server của bên thứ ba.
> Trạng thái kiểm tra: `python -m unittest discover` → **130 test PASS**,
> `ruff check .` → **sạch**.

---

## 0. Đánh giá tổng thể

Project này đã đi từ trạng thái “có logic nhưng còn nhiều bug RFC” sang một codebase có cấu trúc rõ ràng, có kiểm tra định dạng, có thương lượng option, và có test end-to-end khá đầy đủ.

Điểm mạnh nhất hiện nay là: kiến trúc đã được làm rõ, các bug quan trọng từng báo trước đã được vá, và logic truyền file đã đi sát với giao thức hơn rất nhiều. Về mặt mục tiêu học tập / demo / nội bộ, đây là một project ở mức khá tốt.

**Pass thứ hai** đóng nốt ba khoảng trống mà đợt review đầu nêu ra, và trong lúc làm thì fuzz/soak phát hiện thêm hai lỗi thật:

| # | Việc | Kết quả |
|---|---|---|
| 1 | Soak / stress test | `tests/test_soak.py` — 11 test, file 16 MiB mặc định, 70 000+ block, 24 client song song, proxy mất 20% gói, kiểm tra rò rỉ |
| 2 | Fuzz / malformed packet | `tests/test_fuzz.py` — 21 test: corpus cố định + fuzz có seed (3 tầng) + fuzz tên file + fuzz netascii + fuzz qua socket thật |
| 3 | Metrics / observability | `src/tftp_metrics.py` + gắn vào server & client, `--stats-interval` / `--stats-json`, `docs/observability.md` |

### Bug thật phát hiện được nhờ test mới

1. **`timeout` làm tròn về 0 gây quay vòng (busy-loop).** Client/server đặt
   `--timeout 0.4` → `int(0.4) = 0` → OACK trả `timeout=0` → `select(..., 0)`
   không chờ → server gửi lại DATA liên tục và **nuốt ACK của client**, truyền
   chết. Đã sửa bằng `clamp_timeout()` ép về 1..255 + sàn `MIN_SESSION_TIMEOUT`.
2. **`OACK.pack()` ném `UnicodeEncodeError` với option lạ chứa byte > 127.**
   `unpack` giải mã được nhưng `pack` lại không, phá vỡ bất biến idempotent.
   Đã sửa bằng cách dùng latin-1 cho cả giải mã lẫn đóng gói giá trị option.

Ngoài ra pass này siết thêm parser (xem mục 4).

---

## 1. Những gì đã được optimize rõ rệt


### 1.1 Đã sửa các bug RFC trọng tâm

Các problem lớn trước đây đã được xử lý ở mức code hiện tại:

- block number wrap về 0 sau 65535 bằng `next_block()` với máscara `& 0xFFFF`
- server/session không còn gắn phiên sai khi client reuse ephemeral port; logic match request đã được nâng cấp theo filename + mode + options
- `tsize` đã được đưa vào đường RRQ/WRQ và server/client tính toán chính xác hơn
- `netascii` đã chuyển từ “đọc rồi convert” sai thành “convert trước rồi cắt khối đúng `blksize`”
- `TftpClient` không còn “nhiễm state” giữa các lần truyền; state của mỗi lượt truyền tách riêng bằng dataclass
- `ERROR` message dùng UTF-8 đúng cách; console Windows cũng được set UTF-8 trước khi log

Điều này cho thấy đội ngũ (hoặc bản thân người viết) đã làm đúng một dạng “fix loop”: phát hiện bug, viết test, vá, kiểm chứng lại.

### 1.2 Cấu trúc code đã rõ ràng hơn nhiều

Project hiện có sự chia tầng hợp lý:

- `tftp_packet.py`: định nghĩa packet, parse/unpack, option negotiation, netascii conversion
- `tftp_client.py`: luồng truyền client và CLI
- `tftp_server.py`: server đa luồng, session handling, file resolution, write lock, rate limit
- `tftp_errors.py`: mã lỗi chuẩn RFC
- `tftp_metrics.py`: registry counter/gauge/timer cho số liệu vận hành (mới)
- `tests/`: unit + integration + fuzz + soak (mới)

Việc này giúp project dễ mở rộng, dễ test và dễ debug hơn hẳn so với một codebase “mọi logic lẫn trong một file”.

### 1.3 Tooling / project health đã tốt hơn

Hiện tại project có:

- `pyproject.toml` với dependency và script entry points
- `ruff` + `mypy` config
- optional dependency cho GUI (`PySide6`)
- `pytest` config và chuẩn package discovery
- CI chạy 3 job: test matrix 3.10/3.11/3.12, lint, và **soak riêng** với tham số nặng (64 MiB, 48 client, 20 chu kỳ, 140 000 block)

Đây là một dấu hiệu rất tốt: project không chỉ chạy mà còn có “tầng tooling” để maintain lâu dài hơn.

---

## 2. Đánh giá chất lượng hiện tại

### Điểm mạnh

- TFTP core gần đúng chuẩn: RRQ/WRQ, DATA, ACK, OACK, ERROR
- Hỗ trợ RFC 2347 option negotiation (`blksize`, `timeout`, `tsize`)
- Có xử lý path traversal / access control cơ bản
- Có write lock chống overwrite race khi upload cùng tên
- Có rate limiting và giới hạn client đồng thời
- Có logic `NetasciiReader` để giữ block size đúng chuẩn
- Có `enable_utf8_console()` để log tiếng Việt trên Windows không lỗi encoding
- Có số liệu vận hành đầy đủ: phiên, gói tin, retry, byte, RTT/thời gian phiên với p50/p95/p99

### Các khoảng trống đã đóng trong pass này

| Mục 2.5 của đợt review đầu | Trạng thái |
|---|---|
| 1. Soak / stress test file rất lớn | ✅ `tests/test_soak.py` — 16 MiB mặc định (nâng được lên 100 MiB+ qua `TFTP_SOAK_MB`), 70 000+ block wrap nhiều vòng |
| 2. Fuzzing / malformed packet | ✅ `tests/test_fuzz.py` — corpus 35 gói hỏng + fuzz có seed (byte ngẫu nhiên, ép opcode, biến thể gói hợp lệ) + fuzz tên file + fuzz netascii + ném rác vào server thật |
| 3. Monitoring / observability | ✅ `src/tftp_metrics.py`, CLI `--stats-interval` / `--stats` / `--stats-json`, `docs/observability.md` |
| 5. Tải cao + nhiều client đồng thời | ✅ 24 client song song mặc định, đọc/ghi trộn, kiểm tra `--max-clients` có thực sự từ chối, proxy mất 20% gói |

### Vùng còn lại

1. **Chưa kiểm thử chéo với TFTP của bên thứ ba** — đây là khoảng trống lớn nhất
   còn lại. Khuyến nghị: chạy `atftp`/`tftp-hpa` làm client và server để đối chiếu
   (đặc biệt là đường không có OACK, blksize 512, và `netascii`).
2. GUI là tốt nhưng chưa phải “full-fledged product UI” — chưa có UX/accessibility
   testing ở mức sản phẩm.
3. Chưa có tracing phân tán (chỉ có metrics), và chưa xuất metrics theo chuẩn
   Prometheus/OpenTelemetry — hiện là JSON + log, đủ dùng nhưng chưa plug-and-play.

---

## 3. Kết luận: đã optimize chưa?

### Kết luận ngắn gọn

- Đối với mục tiêu học tập / validate RFC / demo công nghệ: đã optimize tốt.
- Đối với mục tiêu “sẵn sàng production”: **đã đạt sau pass này** — có kiểm thử
  chịu tải, fuzz, số liệu vận hành và CI. Phần còn thiếu là tương thích bên thứ ba
  và (nếu cần) chuẩn hoá metrics.

### Chấm mức hiện tại

| Tiêu chí | Đợt 1 | Hiện tại |
|---|---|---|
| Architecture & code quality | 8.5 | 9 |
| RFC correctness | 8.5 | 9 |
| Test coverage | 8 | 9 |
| Production hardening | 6.5 | 8.5 |
| **Overall** | **8** | **9** |

---

## 4. Những gì đã siết thêm trong pass hardening

Không phải “vá bug” mà là làm rõ hợp đồng với đối tác, đều có test:

| Quy tắc | Mã lỗi | Nơi kiểm |
|---|---|---|
| Option lặp tên trong RRQ/WRQ/OACK | ERROR 4 | `_parse_options` |
| Giá trị option phải là chuỗi toàn số ASCII (`"+8"`, `" 8 "`, `"-8"`, chữ số fullwidth đều bị từ chối) | ERROR 4 | `_parse_option_int` |
| OACK có option đã biết mà giá trị không phải số | ERROR 4 | `unpack` |
| `timeout` luôn nằm trong 1..255 (kể cả khi cấu hình < 1s) | — | `clamp_timeout` |
| ERROR thiếu `errmsg` hoặc thiếu byte 0 kết thúc | ERROR 4 | `unpack` |
| DATA vượt blksize đã thương lượng | ERROR 4 | `Session._serve_write` |
| Không có OACK ⇒ cả hai bên dùng blksize 512 | — | `Session._negotiate` |
| `unpack` → `pack` → `unpack` phải ổn định với mọi byte | — | `tests/test_fuzz.py` |

---

## 5. Recommendation cuối cùng

Ba khuyến nghị của đợt review đầu **đã được thực hiện xong**:

1. ✅ soak test file lớn + hàng chục client đồng thời → `tests/test_soak.py`
2. ✅ malformed packet test / fuzz coverage → `tests/test_fuzz.py`
3. ✅ metrics và log phân tích latency / retry / fail rate → `src/tftp_metrics.py` + `docs/observability.md`

Nếu muốn đi tiếp một bước nữa, thứ tự đáng làm là:

1. **kiểm thử chéo với `atftp` / `tftp-hpa`** (rẻ nhất, giá trị cao nhất)
2. xuất metrics theo chuẩn Prometheus nếu sẽ đưa vào hệ thống có sẵn
3. thêm chế độ `--tls`/xác thực nếu TFTP chạy trên mạng không tin cậy (hiện tại
   bảo mật ở mức *access control*, chưa có *authentication*)

