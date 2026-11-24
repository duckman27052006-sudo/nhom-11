# Ghi chú RFC 7440 — TFTP Windowsize Option

> Tài liệu này ghi cách dự án hiện thực RFC 7440, các giới hạn có chủ ý và những
> điểm dễ sai khi đọc chuẩn. Xem `docs/RFC-1350-notes.md` cho phần giao thức nền.

## 1. Option

- Tên: `windowsize`, giá trị ASCII toàn chữ số, 1..65535 theo RFC.
- Thương lượng qua OACK như các option RFC 2347 khác. Không có OACK ⇒ cửa sổ = 1
  (hành vi RFC 1350).
- Server chỉ trả giá trị **≤ giá trị client xin** và ≤ cấu hình `--windowsize`
  (mặc định 1, trần dự án 64 block).

Vì sao trần 64: 64 × 65464 byte ≈ 4 MiB in-flight; lớn hơn không cần cho LAN và
dễ tràn buffer socket nhận của bên thứ ba. Client xin 65535 vẫn nhận OACK 64 và
phải dùng 64 — hợp lệ vì server được phép giảm.

## 2. Phía gửi

Cửa sổ là một `deque[(block, payload)]`:

1. Đọc và gửi cho tới khi đủ `windowsize` block hoặc gặp block cuối.
2. Chờ ACK. ACK cho block k giải phóng mọi block ≤ k trong deque (ACK luỹ tiến).
3. Timeout ⇒ **go-back-N**: gửi lại toàn bộ block chưa ACK, giảm `retries`.
4. ACK lạ (không có trong deque) bị bỏ qua và tăng `packets.stale`.

Không dùng SACK/cửa sổ nhận: RFC 7440 chỉ định nghĩa ACK luỹ tiến, và go-back-N
là hành vi được phép khi mất gói.

## 3. Phía nhận của dự án

Nhận **ACK từng block** thay vì gom đủ cửa sổ. Lý do:

- ACK sớm vẫn là ACK hợp lệ và giữ cho sender không bị treo nếu nó gửi ít hơn
  cửa sổ (ví dụ block cuối).
- Không phải buffer gói đến muộn/out-of-order — đơn giản hơn nhiều so với
  reassembly, vốn là nguồn lỗi memory/DoS.
- Với sender của dự án, ACK từng block vẫn cho phép pipeline đầy đủ: sender chỉ
  chờ khi đã gửi hết cửa sổ, và mỗi ACK giải phóng một slot.

Đánh đổi: ACK nhiều hơn RFC tối thiểu (1 ACK/block thay vì 1 ACK/N block), tốn
băng thông nhỏ trên LAN. Trên link RTT cao, lợi ích cửa sổ vẫn giữ vì sender
không chờ từng block.

## 4. Tương tác với các phần khác

| Thành phần | Ảnh hưởng |
|---|---|
| `blksize` | Cửa sổ tính bằng **block**, không phải byte; đổi blksize đổi in-flight bytes |
| block wrap 16 bit | So khớp ACK theo giá trị block chính xác trong deque; cửa sổ ≤ 64 nên không mơ hồ qua mốc 65535 |
| netascii | Vẫn chia chunk đúng `blksize` trên dây; cửa sổ chỉ đổi cách gửi, không đổi nội dung |
| timeout/retries | Một timeout phát lại **cả cửa sổ**; `retries` đếm theo lần timeout, không theo block |
| `max_transfer_seconds` | Kiểm ở mỗi vòng lặp gửi/nhận và trong `_recv` |
| TID | Không đổi: vẫn một TID/phiên, TID đóng sau dally |

## 5. Kiểm thử

- `tests/test_windowsize.py`: negotiation (cap, disabled, invalid), e2e RRQ/WRQ,
  netascii, và mất gói 20 % qua `LossyProxy` cả hai chiều.
- Benchmark: `tools/benchmark.py` + `docs/benchmark.md`.

## 6. Điều **không** làm

- Không thêm option ngoài RFC (ví dụ `sack`, `rollover`) vì client bên thứ ba
  không hiểu và sẽ bỏ qua, tạo cảm giác an toàn giả.
- Không bật windowsize mặc định: client cũ/ bootloader chỉ hiểu RFC 1350 phải
  chạy được mà không cần cấu hình.
