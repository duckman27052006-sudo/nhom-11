# Ma trận tương thích và kiểm chứng chuẩn

> Trạng thái: **đã tự động hoá** bằng `scripts/interop.sh` và job `interop`
> trong CI (Ubuntu, `tftpd-hpa` + `atftp`). Kết quả từng lần chạy nằm ở log job
> GitHub Actions; tài liệu này ghi phạm vi và cách đọc, không thay cho kết quả
> của lần chạy cụ thể.

## 1. Vì sao cần

Test nội bộ chỉ chứng minh client và server của dự án nói chuyện được với nhau —
cả hai đầu cùng một cách hiểu sai sẽ vẫn "pass". Các điểm chỉ implementation
độc lập mới bắt được:

- OACK có option lạ/giá trị biên, thứ tự option, `tsize` của WRQ.
- `blksize` > 512 khi không thương lượng (RFC 1350 yêu cầu 512).
- Source TID khác port 69 và cách firewall/NAT xử lý.
- netascii thật (CR LF / CR NUL) do bên thứ ba tạo.
- Kết thúc file rỗng và file đúng bội `blksize`.

## 2. Ma trận tự động (`scripts/interop.sh`)

| # | Hạng mục | Đầu gửi → đầu nhận | Kiểm chứng |
|---|---|---|---|
| 1 | RRQ + OACK `blksize=1468` | project client → `tftpd-hpa` | nội dung khớp từng byte |
| 2 | WRQ + OACK | project client → `tftpd-hpa` | file xuất hiện đúng nội dung |
| 3 | RRQ không option (512) | project client → `tftpd-hpa` | đường RFC 1350 thuần |
| 4 | RRQ + OACK | `atftp` → project server | nội dung khớp |
| 5 | WRQ + `blksize=512` | `atftp` → project server | nội dung khớp |
| 6 | file rỗng | `atftp` → project server | 0 byte, đúng một DATA rỗng |

Chạy cục bộ trên Linux/CI:

```bash
sudo apt-get install -y tftpd-hpa atftp
bash scripts/interop.sh
```

Script tự dựng hai thư mục tạm, chạy lần lượt `in.tftpd` và server của dự án,
so sánh bằng `cmp`, dọn process bằng `trap EXIT`.

## 3. Những gì **chưa** tự động hoá

| Hạng mục | Cách làm thủ công |
|---|---|
| IPv6 hai chiều với bên thứ ba | `--family ipv6`, `atftp`/`tftp-hpa` với `::1`; một số bản `tftpd-hpa` cần `TFTP_ADDRESS="[::]:69"` |
| netascii hai chiều | `atftp -m netascii`; so sánh sau khi chuẩn hoá CRLF |
| mất gói 10–20 % | proxy UDP (`tests.test_soak.LossyProxy`) hoặc `tc netem` |
| wifi thật / RTT cao | chạy trên hai máy cùng LAN, ghi `--windowsize` 1 vs 8 |
| `blksize=65464` | ghi chú MTU/fragment; loopback không đại diện |

Các mục này nên chạy trước khi tuyên bố "tương thích rộng" trong báo cáo.

## 4. Bằng chứng cần lưu cho báo cáo

- Phiên bản OS, Python, `tftpd-hpa`/`atftp`, ngày chạy.
- Lệnh đầy đủ + mã thoát (script in ra ở đầu/cuối).
- Log hai phía (server dự án dùng `-v` hoặc `--log-file`).
- Packet capture mẫu: `tcpdump -i lo udp and port 6969` cho RRQ/OACK/DATA/ACK.
- Kết quả từng dòng mục 2, đính kèm link job CI.

## 5. Điểm đã đối chiếu trong code

- RRQ/WRQ, DATA, ACK, ERROR, OACK: `tftp/tftp_packet.py`.
- TID, replay request trùng: `tftp/tftp_server.py` (`Session`, `_handle_datagram`).
- Kết thúc file theo độ dài trên dây, kể cả file đúng bội `blksize`.
- Option `blksize` (RFC 2348), `timeout`/`tsize` (RFC 2349), OACK (RFC 2347),
  `windowsize` (RFC 7440).
- Ghi chú chi tiết: `docs/RFC-1350-notes.md`.
