# Trivial File Transfer Protocol (TFTP)

Bản cài đặt TFTP hoàn chỉnh theo **RFC 1350** kèm **TFTP Option Extension (RFC 2347)**
viết bằng Python 3.10+; **phần lõi chỉ dùng thư viện chuẩn** (`socket`, `struct`,
`threading`, `select`, `argparse`, `logging`). GUI và cổng web là tuỳ chọn.

| Thành phần | File | Nội dung |
|---|---|---|
| Server | `src/tftp_server.py` | Đa luồng, mỗi phiên một TID riêng, hỗ trợ đọc/ghi |
| Client | `src/tftp_client.py` | Tải về (RRQ) và gửi lên (WRQ), có CLI |
| GUI | `src/tftp_gui.py`, `src/gui/` | Giao diện PySide6: `theme` (màu), `components` (vẽ bề mặt), `motion` (hiệu ứng), `main_window` (4 trang) |
| Web gateway | `src/web_upload.py` | Cổng HTTP nhận file từ trình duyệt rồi gửi WRQ (tuỳ chọn, extras `[web]`) |
| Gói tin | `src/tftp_packet.py` | Đóng gói / giải mã RRQ, WRQ, DATA, ACK, ERROR, OACK |
| Mã lỗi | `src/tftp_errors.py` | Bảng mã lỗi RFC 1350 + ngoại lệ |
| Địa chỉ | `src/tftp_net.py` | Phân giải và bind socket cho cả IPv4 lẫn IPv6 |
| Số liệu | `src/tftp_metrics.py` | Counter / gauge / timer, xuất log + JSON |
| Test | `tests/` | 180 test (unit + tích hợp + fuzz + soak + web + hồi quy + GUI), tất cả PASS |

## 1. Cài đặt

Không cần cài thêm gói nào, chỉ cần Python 3.10 trở lên:

```bash
cd nhom-11-main
python --version
```

GUI là **tuỳ chọn**, cần PySide6:

```bash
python -m pip install -e ".[gui]"     # hoặc: python -m pip install PySide6
```

Cổng web upload cũng **tuỳ chọn**, cần Flask + Waitress:

```bash
python -m pip install -e ".[web]"
```

## 1b. Giao diện đồ hoá (GUI)

```bash
python -m src.tftp_gui
```

Bố cục chia **4 trang** để không dồn hết chức năng vào một màn hình:

| Trang | Nội dung |
|---|---|
| **Tổng quan** | Hero đổi theo trạng thái server, lưới số liệu 2×2 cạnh hoạt động gần đây, 2 lối tắt nhanh + liên kết phụ |
| **Truyền file** | Một hàng **"Đích đến"** dùng chung cho cả hai chiều · Tải về \| Gửi lên · bảng File trên server (bấm hàng để điền ô tải về), banner lỗi + toast |
| **Máy chủ** | Cấu hình theo khối dọc (lắng nghe, thư mục, 3 giới hạn đều nhau, tùy chọn 2×2) + bảng file (trạng thái trống có nút hành động), bấm đúp để điền vào ô tải về |
| **Nhật ký** | Luồng gói client trực tiếp (RRQ/WRQ/OACK/DATA/ACK/ERROR), log thời gian thực, lọc mức, sao chép/lưu log |

Chi tiết ngôn ngữ thiết kế xem `GUI_REDESIGN.md`.

Phím tắt: `Ctrl+L` xóa nhật ký · `Ctrl+K` dừng server · `Ctrl+1..4` chuyển trang ·
`F1` mở bảng phím tắt.

Tiện ích: nút **Mở thư mục** cạnh nút Tải về (mở thư mục chứa file vừa tải) và
nút **Sao chép** trên trang Nhật ký (copy toàn bộ log vào clipboard).

Hiệu ứng (tất cả nằm gọn trong `src/gui/motion.py`): chuyển trang trôi nhẹ từ
dưới lên, cửa sổ mờ dần khi mở, chấm trạng thái nhịp thở, thanh tiến trình
shimmer khi chưa rõ tổng, ô số liệu đếm tăng dần, toast góc phải dưới.

> **Về màu sắc:** bề mặt thương hiệu (card, ô, nút, pill, thanh tiến trình) tự
> vẽ bằng `QPainter` chứ không dùng QSS, vì trên Qt 6.12 + Windows
> `setStyleSheet` chỉ áp được cho một phần cây widget. Widget do hệ điều hành vẽ
> lấy màu từ `QPalette` (`theme.build_palette`). QSS chỉ còn dùng cho **màu chữ**
> của một số nhãn; các nhãn đó ghi token vào property `tokenColor` để
> `_restyle_token_labels()` vẽ lại khi đổi theme. Nhờ vậy cả hai theme sáng/tối
> đều hiển thị đúng.

> Server và client chạy ở thread riêng, nên giao diện không bao giờ treo khi đang
> truyền file. Mã lõi (`src/tftp_*.py`) không phụ thuộc PySide6.

## 1c. Cổng web upload (tuỳ chọn)

Người dùng trong LAN có thể gửi file bằng trình duyệt mà không cần cài client
TFTP. Trình duyệt gửi HTTP multipart tới cổng; cổng ghi file tạm rồi gửi WRQ tới
TFTP server, nên file cuối cùng nằm trong thư mục gốc của TFTP server.

```bash
python -m pip install -e ".[web]"
python -m src.web_upload \
  --host 192.168.1.10 --port 8080 \
  --allowed-network 192.168.1.0/24 \
  --tftp-server 127.0.0.1:6969
```

Cổng chỉ bind vào một địa chỉ private/loopback cụ thể (mặc định `127.0.0.1`),
bắt buộc ít nhất một `--allowed-network`, kiểm tra peer IP thật + `Origin` + CSRF
token, giới hạn dung lượng và thêm CSP/security headers. Không có route liệt kê
hay tải file. Trang upload cho chọn/kéo-thả **nhiều file** và gửi tuần tự từng
file. `GET /healthz` trả JSON trạng thái; thêm `?probe=1` để kiểm tra TFTP server
thật (503 nếu không tới được). IPv6 cũng dùng được, ví dụ `--host fd12:3456:789a::10
--allowed-network fd12:3456:789a::/64 --tftp-server "[::1]:6969"`.

Chi tiết và giới hạn bảo mật: [`docs/web-upload.md`](docs/web-upload.md).

## 2. Chạy thử nhanh (2 cửa sổ terminal)

### Cửa sổ 1 — khởi động server

```bash
python -m src.tftp_server --host 0.0.0.0 --port 6969 --root files -v
```

```
14:00:01 INFO    TFTP server sẵn sàng: udp://0.0.0.0:6969, thư mục gốc=...\files, blksize=1468, timeout=5.0s, retries=5
```

### Cửa sổ 2 — tải file về (RRQ)

```bash
python -m src.tftp_client --server 127.0.0.1:6969 --get sample.bin --save sample_copy.bin
python -m src.tftp_client --server 127.0.0.1:6969 --get hello.txt --blocksize 1468
```

### Cửa sổ 2 — gửi file lên (WRQ)

```bash
python -m src.tftp_client --server 127.0.0.1:6969 --put sample_copy.bin --remote uploaded.bin
```

Kiểm tra kết quả: `files/uploaded.bin` sẽ xuất hiện với nội dung giống hệt `files/sample.bin`.

> **Lưu ý về port 69**: RFC 1350 quy định well-known port là UDP 69, nhưng trên
> Windows/Linux cần chạy với quyền quản trị mới bind được port < 1024. Vì vậy mặc
> định dùng **6969**; nếu chạy bằng quyền admin có thể dùng `--port 69` và client
> chỉ cần `--server <ip>:69`.

## 3. Giao diện desktop

Trên Windows, mở `tftp.bat` — script tự kiểm tra `.venv`, cài PySide6 nếu thiếu,
rồi mở thẳng giao diện **TFTP Console** (không còn menu terminal).

Cũng có thể mở trực tiếp từ thư mục dự án:

```bash
python -m src.tftp_gui
```

## 4. Tham số CLI

### Server

| Tham số | Mặc định | Ý nghĩa |
|---|---|---|
| `--host` | `0.0.0.0` | địa chỉ lắng nghe |
| `--port` | `6969` | port UDP (69 nếu có quyền admin) |
| `--root` | `files` | thư mục gốc phục vụ / lưu file |
| `--timeout` | `5` | giây chờ mỗi gói tin |
| `--retries` | `5` | số lần gửi lại tối đa |
| `--blksize` | `1468` | blksize server đề nghị khi client yêu cầu ngoài khoảng 8..65464 |
| `--no-overwrite` | tắt | từ chối ghi đè (trả ERROR 6) |
| `--no-options` | tắt | tắt thương lượng option RFC 2347 |
| `--max-clients` | `32` | số phiên đồng thời tối đa |
| `--max-file-size` | không giới hạn | giới hạn kích thước upload (byte), vượt → ERROR 3 |
| `--allow-subdirs` | tắt | cho phép `incoming/file` trong root (mặc định chặn mọi `/`) |
| `--max-requests-per-second` | `100` | giới hạn request/s chống flood |
| `--stats-interval` | `0` (tắt) | in bảng số liệu vận hành mỗi N giây |
| `--stats-json` | — | ghi số liệu vận hành ra file JSON khi dừng |
| `--log-file` | — | ghi thêm log ra file (vẫn in ra console) |
| `--version` | | in version rồi thoát |
| `-v` | | log từng gói tin |

> **Giới hạn thiết kế**: mặc định server chặn mọi tên chứa `/`, `\`, `:` nên
> không thể chia `root` thành `incoming/`, `public/`... Dùng `--allow-subdirs`
> để mở (vẫn chặn `..`, đường dẫn tuyệt đối, ký tự điều khiển `< > : " | ? *`,
> tên réserved Windows `CON/NUL/...`, và kiểm tra `commonpath` trong root).

### Client

| Tham số | Mặc định | Ý nghĩa |
|---|---|---|
| `--server` | `127.0.0.1:6969` | `host` hoặc `host:port` (hỗ trợ `[::1]:port`) |
| `--get` / `--put` | — | bắt buộc chọn một trong hai |
| `--save` | tên file gốc | nơi lưu khi `--get` |
| `--remote` | tên file gốc | tên file trên server khi `--put` |
| `--mode` | `octet` | `octet` (nhị phân) hoặc `netascii` |
| `--blocksize` | `512` | kích thước khối mong muốn (8..65464) |
| `--timeout` | `5` | giây chờ |
| `--retries` | `5` | số lần thử lại |
| `--no-options` | tắt | giao thức thuần RFC 1350, blksize cố định 512 |
| `--stats` | tắt | in số liệu vận hành (metrics) khi kết thúc |
| `--progress` | tắt | hiện thanh tiến trình (%/tốc độ/ETA) trên stderr |
| `--stats-json` | — | ghi số liệu vận hành ra file JSON |
| `--log-file` | — | ghi thêm log ra file (vẫn in ra console) |
| `--version` | | in version rồi thoát |
| `-v` | | log từng gói tin |

> `blksize` hợp lệ 8..65464. Nếu client xin ngoài khoảng, server trả giá trị
> mặc định của server (`--blksize`, mặc định 1468) theo RFC 2347, client phải
> chấp nhận giá trị đó.

Mã thoát: `0` thành công, `1` lỗi TFTP (ERROR của server, timeout...), `2` lỗi hệ thống.

## 5. Dùng như thư viện

```python
from src.tftp_client import TftpClient
from src.tftp_server import TftpServer
import threading

server = TftpServer("127.0.0.1", 6969, root="files", timeout=5, retries=5)
threading.Thread(target=server.serve_forever, daemon=True).start()

client = TftpClient("127.0.0.1", 6969, blksize=1468, timeout=5, retries=5)
client.download("sample.bin", "sample_copy.bin")   # RRQ
client.upload("sample_copy.bin", "uploaded.bin")   # WRQ

server.shutdown()
```

## 6. Chạy test

```bash
python -m unittest discover -v          # toàn bộ: unit + tích hợp + fuzz + soak + web + hồi quy
python -m unittest tests.test_packet -v  # chỉ unit test gói tin
python -m unittest tests.test_e2e -v     # chỉ test tích hợp
python -m unittest tests.test_fuzz -v    # chỉ malformed packet + fuzz
python -m unittest tests.test_soak -v    # chỉ soak / stress
python -m unittest tests.test_metrics -v # chỉ registry metrics
python -m unittest tests.test_web_upload -v  # cổng web (cần extras [web])
python -m unittest tests.test_regressions -v  # hồi quy RFC/netascii/CLI
python -m unittest tests.test_gui_smoke -v   # smoke GUI (cần extras [gui], offscreen)

ruff check .                             # lint
```

Test tích hợp tự bật server trên một port ngẫu nhiên nên **không cần quyền admin**
và không xung đột với dịch vụ TFTP thật của hệ điều hành. Các test bao phủ:

| # | Test | Nội dung |
|---|---|---|
| 1 | file rỗng | 0 byte, kết thúc đúng sau một DATA rỗng |
| 2 | đúng 512 byte | 2 block (block 2 rỗng), nội dung khớp |
| 3 | file lớn | >3 MiB với `blksize=1468`, so sánh byte-for-byte |
| 4 | upload | file mới xuất hiện đúng nội dung trên đĩa server |
| 5 | file không tồn tại | ERROR code 1 |
| 6 | ghi đè bị chặn | ERROR code 6 khi `--no-overwrite` |
| 7 | mất ACK | client gửi lại đúng ACK cũ, truyền vẫn hoàn tất |
| 8 | nhiều client song song | 8 client cùng lúc, không lẫn dữ liệu |
| 9 | netascii | `LF → CR LF`, `CR → CR NUL`; round trip đúng từng byte, kể cả CR đơn lẻ |
| 10 | path traversal | `../secret.txt` bị từ chối (ERROR 2) |
| 11 | RRQ lặp lại | server trả lời lại từ **cùng TID** |
| 12 | gói tin hỏng | opcode lạ / datagram rỗng → ERROR 4, không tạo phiên |
| 13 | tương thích option | client đề nghị option, server `--no-options` → vẫn truyền được |
| 14 | timeout < 1s | OACK không bao giờ trả `timeout=0` |
| 15 | option lặp | `blksize` gửi hai lần → ERROR 4 |
| 16 | DATA vượt blksize | client gửi khối to hơn thỏa thuận → ERROR 4 |
| 17 | fuzz parser | hàng nghìn gói ngẫu nhiên/biến thể, `unpack` không bao giờ ném lỗi thô |
| 18 | fuzz tên file | 2000 tên ngẫu nhiên không đường dẫn nào thoát khỏi `--root` |
| 19 | fuzz netascii | chỉ khối cuối được ngắn hơn blksize, dữ liệu khớp byte-for-byte |
| 20 | server chịu rác | 300 datagram rác + corpus, server vẫn sống và `errors.unhandled = 0` |
| 21 | soak file lớn | 16 MiB (mặc định, nâng được) tải lên/tải xuống, khớp từng byte |
| 22 | soak block wrap | 70 000+ block, wrap nhiều vòng 16 bit |
| 23 | soak nhiều client | 24 client (mặc định) tải song song + đọc/ghi trộn, không lẫn dữ liệu |
| 24 | soak mất gói | proxy UDP bỏ 20% gói, truyền vẫn hoàn tất nhờ retransmission |
| 25 | soak rò rỉ | nhiều chu kỳ liên tiếp: không sót phiên, file `.part`, thread |
| 26 | metrics | counter/gauge/timer đầy đủ, `render()`/JSON đúng |
| 27 | web gateway | upload qua HTTP multipart → TFTP, ACL mạng, CSRF, Origin, giới hạn dung lượng |
| 28 | hồi quy | option quá dài, OACK thiếu `blksize`, `tsize` echo, netascii CR NUL, CLI validation |
| 29 | GUI smoke | offscreen: đổi theme, lọc log theo mức, bind lỗi vẫn khởi động lại được |
| 30 | CLI progress | `format_progress` 0/50/100%, `total=None`, printer + `--progress` |
| 31 | healthz + nhiều file | probe TFTP sống/chết (200/503), gửi nhiều file tuần tự, markup kéo-thả |

Soak test nặng hơn bằng biến môi trường:

```bash
TFTP_SOAK_MB=100 TFTP_SOAK_CLIENTS=40 TFTP_SOAK_CYCLES=20 python -m unittest tests.test_soak -v
TFTP_SKIP_SOAK=1 python -m unittest discover      # bỏ qua soak khi cần chạy nhanh
```

## 7. Đo đạc vận hành

```bash
python -m src.tftp_server --stats-interval 60        # in số liệu mỗi 60s
python -m src.tftp_server --stats-json metrics.json   # xuất JSON khi dừng
python -m src.tftp_client --get big.bin --stats       # in số liệu client
```

Mỗi bên đo: số phiên (bắt đầu / hoàn tất / timeout), số gói tin gửi-nhận,
**số lần gửi lại**, byte thật, RTT và thời gian phiên với `p50/p95/p99`, số phiên
đang chạy. Danh mục đầy đủ và cách tính tỉ lệ lỗi:
[`docs/observability.md`](docs/observability.md).

Cổng web upload có `GET /healthz` để health check; thêm `?probe=1` để kiểm tra
TFTP server thật (trả 503 nếu không tới được).

## 8. Kiểm tra bằng Wireshark

Trang **Nhật ký** trong GUI hiển thị log trực tiếp của client/server (kể cả từng
gói tin ở mức DEBUG). Wireshark cho thấy datagram thật trên dây — bắt gói **trước**
khi truyền file rồi đối chiếu hai bên.

Trên Windows, dùng adapter **Npcap Loopback Adapter** khi client/server cùng máy.
Không đặt capture filter `udp port 6969`: sau RRQ/WRQ, TFTP chuyển sang TID dùng
port UDP tạm. Dùng capture filter `udp and host 127.0.0.1` và display filter
`udp && ip.addr == 127.0.0.1`; không dùng display filter `tftp` đơn lẻ vì một số
Wireshark không giải mã DATA/ACK trên TID tạm. Nếu cần, chọn gói đầu tiên đến
port 6969 → chuột phải → **Decode As…** → TFTP. Một số gói có thể mang nhãn UDP;
nhấp phải gói rồi chọn **Follow → UDP Stream** để xem trao đổi.

```bash
tshark -i any -f "udp and host 127.0.0.1" -Y "udp && ip.addr == 127.0.0.1" -V
```

Bộ lọc hiển thị chuỗi: `RRQ → OACK → ACK 0 → DATA 1 → ACK 1 → ...`.

## 9. Đặc điểm đã triển khai

- Lock-step: mỗi khối chờ ACK trước khi gửi khối kế tiếp.
- Timeout + retransmission với giới hạn số lần thử.
- Hỗ trợ **IPv4 và IPv6** cho cả server lẫn client (`src/tftp_net.py`); `--host ::1`,
  `--server [::1]:6969` đều dùng được.
- Cổng **web upload** tuỳ chọn: HTTP multipart → TFTP WRQ, ACL theo peer IP,
  CSRF + Origin, CSP, giới hạn dung lượng (`src/web_upload.py`). Chọn/kéo-thả
  nhiều file, gửi tuần tự; `GET /healthz[?probe=1]` cho giám sát.
- **`--progress`** cho client: thanh tiến trình %, tốc độ, ETA trên stderr.
- GUI: **Mở thư mục** sau khi tải, **Sao chép** nhật ký, `F1` bảng phím tắt,
  `Ctrl+1..4` chuyển trang.
- `--log-file` ghi thêm ra file chứ không thay log console.
- TID riêng cho từng phiên, kiểm tra nguồn gói để chống lẫn dữ liệu giữa các client.
- So khớp `(addr, filename, mode, options)` để phát hiện OS cấp lại cùng port
  cho client mới (không replay nhầm phiên cũ).
- Block number 16 bit wrap về 0 sau 65535 (`(block+1) & 0xFFFF`), truyền được
  file >32 MB với blksize 512.
- `blksize` (8..65464), `timeout` (1..255), `tsize` + gói OACK (RFC 2347).
  RRQ gửi `tsize=0` để hỏi kích thước, WRQ gửi `tsize=<size thật>` (server echo
  lại trong OACK) để kiểm tra `--max-file-size` trước khi nhận.
- Chống "Sorcerer's Apprentice Syndrome": chỉ gửi lại đúng gói đang chờ, có ngân sách gói rác.
- Chặn path traversal, ký tự điều khiển, `<>:"|?*`, tên réserved Windows;
  ghi file tạm rồi `os.replace` để không để lại file dở dang.
- Lock theo tên file khi WRQ: 2 upload cùng tên thì 1 bên nhận ERROR 6.
- Chế độ `octet` và `netascii` (netascii luôn chia chunk **đúng blksize** trừ block cuối,
  decoder tự nối cặp `CR LF`/`CR NUL` bị tách qua 2 block — không bao giờ cắt cụt file).
- File rỗng, file đúng bằng bội số blksize, file nhiều MB.
- Siết parser: option lặp, giá trị option không phải số thuần, OACK rác,
  ERROR thiếu byte 0, DATA vượt blksize — đều bị từ chối bằng ERROR chuẩn.
- Metrics counter/gauge/timer với p50/p95/p99, xuất log định kỳ hoặc JSON.
- Graceful shutdown (SIGTERM/SIGINT): gửi ERROR, đóng phiên, `join` thread.
- `--version`, `--log-file`, `pyproject.toml`, `.gitignore`, CI (test + lint + soak riêng).

Chi tiết ghi chú chuẩn và các điểm dễ sai: [`docs/RFC-1350-notes.md`](docs/RFC-1350-notes.md).
Số liệu vận hành: [`docs/observability.md`](docs/observability.md).
