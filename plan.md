# Kế Hoạch Xây Dựng Trivial File Transfer Protocol (TFTP)

## 1. Mục Tiêu

Xây dựng hệ thống truyền file TFTP đầy đủ theo chuẩn **RFC 1350**, bao gồm:

- **TFTP Client**: gửi/nhận file với server qua giao thức TFTP.
- **TFTP Server**: phục vụ nhiều client đồng thời, hỗ trợ đọc (download) và ghi (upload) file.

Kết quả mong đợi: hai chương trình có thể chạy trên cùng hoặc khác máy, truyền file thành công qua UDP với cơ chế xác nhận từng khối (lock-step), thử lại khi timeout, và báo lỗi đúng chuẩn.

## 2. Phạm Vi

| Hạng mục | Có làm | Không làm |
|---|---|---|
| Truyền file cơ bản (RRQ / WRQ) | ✅ | |
| Cơ chế ACK + retransmission | ✅ | |
| Xử lý ERROR packet | ✅ | |
| Timeout & giới hạn số lần thử | ✅ | |
| TFTP Options (blksize, tsize, timeout, tsize...) | ✅ | Xác thực tài khoản |
| Đa client song song trên server | ✅ | Mã hóa dữ liệu |
| Truyền nhị phân (binary / octet mode) | ✅ | Giao diện đồ họa phức tạp |

## 3. Kiến Thức Nền Tảng Cần Nắm

- **UDP socket**: gửi/nhận datagram, không hướng kết nối.
- **Giao thức lock-step**: gửi 1 khối dữ liệu → chờ ACK → mới gửi khối tiếp theo.
- **Big-endian**: tất cả trường số trong gói tin TFTP dùng network byte order.
- **Magic number**: khối cuối cùng nhỏ hơn 512 byte → kết thúc truyền.

## 4. Định Dạng Gói Tin (RFC 1350)

Mỗi gói TFTP bắt đầu bằng **2 byte opcode** (big-endian):

| Opcode | Tên gói | Mô tả |
|---|---|---|
| 1 | RRQ | Read Request - yêu cầu đọc file từ server |
| 2 | WRQ | Write Request - yêu cầu ghi file lên server |
| 3 | DATA | Khối dữ liệu, tối đa 512 byte |
| 4 | ACK | Xác nhận nhận được khối |
| 5 | ERROR | Báo lỗi, kèm mã lỗi và thông báo |

**Cấu trúc chi tiết:**

```
RRQ/WRQ : | opcode (2) | filename (n) | 0 | mode (n) | 0 |
DATA    : | opcode (2) | block# (2) | data (0..512) |
ACK     : | opcode (2) | block# (2) |
ERROR   : | opcode (2) | errcode (2) | errmsg (n) | 0 |
```

**Bảng mã lỗi (Error codes):**

| Mã | Ý nghĩa |
|---|---|
| 0 | Not defined |
| 1 | File not found |
| 2 | Access violation |
| 3 | Disk full |
| 4 | Illegal TFTP operation |
| 5 | Unknown transfer ID (TID) |
| 6 | File already exists |
| 7 | No such user |

## 5. Kiến Trúc Hệ Thống

```
┌────────────────────┐                              ┌────────────────────┐
│                    │   RRQ/WRQ (port 69)          │                    │
│    TFTP Client     │ ───────────────────────────► │    TFTP Server     │
│                    │                              │                    │
│                    │ ◄─────────────────────────── │  TID mới cho       │
│                    │  DATA/ACK từ port mới        │  mỗi client        │
└────────────────────┘                              └────────────────────┘
```

**Luồng đọc file (Client tải file từ Server):**

1. Client gửi **RRQ** (filename + mode) tới port **69**.
2. Server mở cổng TID mới, trả lời từ TID đó.
3. Server gửi **DATA** block 1.
4. Client nhận và **ACK** block 1.
5. Lặp lại đến khi nhận block cuối có kích thước **< 512 byte** → kết thúc.
6. Nếu timeout, **thử lại** gửi ACK (tối đa N lần).

**Luồng ghi file (Client gửi file lên Server):**

1. Client gửi **WRQ** tới port **69**.
2. Server trả lời **ACK block 0**.
3. Client gửi **DATA** block 1.
4. Lặp lại lock-step; block cuối < 512 byte → server hoàn tất.

**Quy tắc timeout & thử lại:**

- Mỗi bên có hẹn giờ riêng (mặc định ~5 giây).
- Hết thời gian mà không nhận được ACK/DATA phù hợp → **gửi lại gói gần nhất**.
- Thử lại tối đa **5 lần** sau đó báo lỗi.

## 6. Môi Trường & Công Nghệ

| Thành phần | Lựa chọn đề xuất |
|---|---|
| Ngôn ngữ | Python 3.10+ |
| Thư viện | Chỉ dùng thư viện chuẩn: `socket`, `struct`, `os`, `threading`, `queue`, `argparse`, `logging` |
| Giao thức truyền | UDP |
| Kiểm thử | Script tự động bằng Python + Wireshark để đối chiếu gói tin |

Lý do chọn Python: socket API đơn giản, đóng gói/giải mã gói tin nhanh bằng `struct`, phù hợp minh họa cơ chế giao thức rõ ràng.

## 7. Cấu Trúc Thư Mục Dự Án

```
Trivial File Transfer Protocol/
├── plan.md
├── README.md
├── src/
│   ├── __init__.py
│   ├── tftp_packet.py      # Định nghĩa + encode/decode gói tin TFTP
│   ├── tftp_errors.py      # Bảng mã lỗi chuẩn RFC 1350
│   ├── tftp_client.py      # Logic TFTP client
│   └── tftp_server.py      # Logic TFTP server (đa luồng)
├── tests/
│   ├── test_packet.py      # Unit test cho encode/decode gói
│   └── test_e2e.py         # Test end-to-end client <-> server
├── files/                 # Thư mục lưu file đã truyền (test)
└── docs/
    └── RFC-1350-notes.md   # Ghi chú trích dẫn chuẩn
```

## 8. Chi Tiết Các Module

### 8.1 `tftp_packet.py`

- Class/lớp đại diện cho từng loại gói: `RRQ`, `WRQ`, `DATA`, `ACK`, `ERROR`.
- Hàm `pack()` → chuyển gói thành bytes.
- Hàm `unpack()` → parse bytes thành gói, kiểm tra opcode hợp lệ.
- Hàm `is_valid()` → kiểm tra kích thước gói tối thiểu (header).

### 8.2 `tftp_errors.py`

- Danh sách hằng số mã lỗi.
- Hàm tạo gói ERROR.
- Ánh xạ mã lỗi → chuỗi mô tả để ghi log.

### 8.3 `tftp_client.py`

- Giao diện dòng lệnh (CLI):
  ```
  python -m src.tftp_client --server 127.0.0.1:69 \
      --get remote.txt --save local.txt \
      --blocksize 512 --timeout 5 --retries 5
  ```
- Hàm chính:
  - `send_rrq(filename, mode)` → nhận file.
  - `send_wrq(filename, mode)` → gửi file.
  - `wait_for(expected_opcode, expected_block)` → chờ gói đúng, timeout → thử lại.
- Trạng thái ghi log hiển thị (received block n, size m byte).

### 8.4 `tftp_server.py`

- Lắng nghe UDP port 69 (hoặc port cấu hình được).
- Với mỗi request tạo một **thread riêng** + cổng TID mới.
- Hàm chính:
  - `handle_rrq()` → mở file, gửi DATA lần lượt đến khi ACK đủ.
  - `handle_wrq()` → nhận DATA, ghi file, gửi ACK.
  - `handle_error()` → gửi ERROR cho các trường hợp ngoại lệ.
- Hạn chế tần suất gửi lại, ngăn chặn "Sorcerer's Apprentice Syndrome".

## 9. Các Tính Năng Mở Rộng (nâng cao)

| Tính năng | Mô tả | Tham khảo |
|---|---|---|
| | TFTP Option Negotiation: `blksize`, `timeout`, `tsize` | RFC 2347 |
| | Gói OACK xác nhận option | RFC 2347 |
| | Hỗ trợ `netascii` mode ngoài `octet` | RFC 1350 |
| | Kiểm soát quyền ghi file trên server | — |
| | Giới hạn kích thước file, số client đồng thời | — |

Lưu ý: mức tối thiểu cần đạt là truyền thành công file nhỏ & file lớn (>512 byte, nhiều block) ở chế độ `octet`, không cần option.

## 10. Lộ Trình Thực Hiện

| Giai đoạn | Nội dung | Kết quả kiểm chứng |
|---|---|---|
| **Tuần 1** | Nghiên cứu RFC 1350, thiết kế gói tin | `tftp_packet.py` pass unit test |
| **Tuần 2** | Xây dựng server đọc file (RRQ) | Client tải được 1 file từ server trên localhost |
| **Tuần 3** | Xây dựng client đọc file (RRQ) | Truyền được file >1 block, lock-step đúng |
| **Tuần 4** | Xây dựng WRQ (client → server) | Upload file mới lên server thành công |
| **Tuần 5** | Timeout, retransmission, xử lý lỗi | Mô phỏng mất gói (netem/drop packet), hệ thống vẫn truyền xong |
| **Tuần 6** | Đa client, logging, dọn dẹp, tài liệu | 3+ client song song, log rõ ràng |

## 11. Kế Hoạch Kiểm Thử

### Test đơn vị (`test_packet.py`)

- Encode/decode 5 loại gói ra giống byte chuẩn RFC 1350.
- Parse gói lỗi (gói rỗng, sai opcode) → phải ném lỗi rõ ràng.
- RRQ/WRQ với filename thoả thuận, mode `octet`.

### Test tích hợp (`test_e2e.py`)

1. Truyền file **rỗng (0 byte)** — client nhận 0 block, kết thúc đúng.
2. Truyền file **đúng 512 byte** — nhận 2 block, sau đó block kết thúc rỗng (0 byte).
3. Truyền file **lớn (vài MB)** — nhiều block, kiểm tra nội dung nhận được trùng khớp.
4. Upload file mới lên server — file xuất hiện đúng trên đĩa server.
5. Yêu cầu file không tồn tại → nhận ERROR code 1.
6. Ghi đè file trên server → ERROR code 6 (nếu cấu hình nghiêm ngặt).
7. Mô phỏng mất ACK — client gửi lại đúng gói trước đó.
8. Chạy nhiều client đồng thời — không có block bị lẫn client khác.

## 12. Rủi Ro & Biện Pháp

| Rủi ro | Ảnh hưởng | Xử lý |
|---|---|---|
| Mất gói do UDP | Truyền bị kẹt | Hẹn giờ + gửi lại, tối đa 5 lần |
| Block bị lẫn giữa các client | Dữ liệu sai | Dùng TID riêng, kiểm tra nguồn gói khi nhận |
| Sorcerer's Apprentice | Tắc nghẽn mạng | Kiểm tra block number trước khi xử lý ACK |
| File nhị phân bị lỗi | File hỏng | Dùng mode `octet`, mở file ở chế độ `"rb"`/`"wb"` |
| Server treo khi client rớt | Thread kẹt | Hẹn giờ toàn bộ phiên, dọn thread sau khi xong |

## 13. Checklist Hoàn Thành

- [ ] Sinh/parse đủ 5 loại gói đúng RFC 1350.
- [ ] Client RRQ tải file về đúng nguyên văn.
- [ ] Client WRQ gửi file lên server thành công.
- [ ] Truyền file > 512 byte (nhiều block) hoạt động ổn định.
- [ ] Timeout & thử lại hoạt động khi mô phỏng mất gói.
- [ ] Phản hồi ERROR đúng cho file không tồn tại / quyền truy cập.
- [ ] Server xử lý nhiều client song song không lẫn dữ liệu.
- [ ] Unit test + test tích hợp đều pass.
- [ ] Có README hướng dẫn cách chạy client và server.

## 14. Tài Liệu Tham Khảo

- RFC 1350 — *The TFTP Protocol (Revision 2)*
- RFC 2347 — *TFTP Option Extension*
- RFC 1350 mẫu gói tin trong RFC 1123
