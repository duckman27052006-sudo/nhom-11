# Ma trận tương thích và kiểm chứng chuẩn

> Trạng thái hiện tại: **chưa xác minh với implementation TFTP độc lập**. Các
> test nội bộ chỉ chứng minh client và server của dự án nói chuyện được với nhau,
> không chứng minh tương thích rộng.

## 1. Ma trận cần điền

| Hạng mục | Môi trường | Kết quả | Ngày | Ghi chú |
|---|---|---|---|---|
| Client dự án → `tftpd-hpa` (IPv4, octet) | chưa chạy | — | — | |
| Client dự án → `tftpd-hpa` (IPv6, octet) | chưa chạy | — | — | |
| `tftp-hpa` → server dự án (IPv4, WRQ) | chưa chạy | — | — | |
| `tftp-hpa` → server dự án (IPv6, WRQ) | chưa chạy | — | — | |
| netascii hai chiều | chưa chạy | — | — | |
| option on/off, blksize 512/1468/65464 | chưa chạy | — | — | |
| file rỗng / đúng bội block / lớn | chưa chạy | — | — | |
| mất gói 10-20% | chưa chạy | — | — | |

## 2. Lệnh gợi ý

```bash
# tftpd-hpa (Linux)
sudo apt install tftpd-hpa
sudo tftpd-hpa --version
# cấu hình /etc/default/tftpd-hpa: TFTP_DIRECTORY, TFTP_ADDRESS="0.0.0.0:69"
sudo systemctl restart tftpd-hpa

# Client dự án -> tftpd-hpa
python -m src.tftp_client --server 127.0.0.1:69 --get test.bin --save out.bin -v

# tftp-hpa client -> server dự án
python -m src.tftp_server --host 0.0.0.0 --port 6969 --root /tmp/tftp-root --overwrite -v
tftp 127.0.0.1 6969 -c put local.bin remote.bin
tftp 127.0.0.1 6969 -c get remote.bin back.bin
```

IPv6: thay `127.0.0.1` bằng `::1` (hoặc địa chỉ global), dùng `--family ipv6`
với client dự án và cấu hình `TFTP_ADDRESS="[::]:69"` cho `tftpd-hpa` nếu phiên
bản hỗ trợ.

## 3. Bằng chứng cần lưu

- Phiên bản OS, Python, implementation đối chiếu.
- Lệnh chạy đầy đủ.
- Log hai phía và mã thoát.
- Packet capture mẫu (Wireshark/tcpdump) cho RRQ, OACK, DATA, ACK, ERROR.
- Kết quả từng dòng trong ma trận, kèm ngày và người thực hiện.

## 4. Điểm đã đối chiếu trong code

- RRQ/WRQ, DATA, ACK, ERROR, OACK: `src/tftp_packet.py`.
- TID và replay request trùng: `src/tftp_server.py` (`Session`, `_handle_datagram`).
- Kết thúc file theo độ dài trên dây, kể cả file đúng bội blksize.
- Option `blksize` (RFC 2348), `timeout`/`tsize` (RFC 2349), OACK (RFC 2347).
- Ghi chú chi tiết: `docs/RFC-1350-notes.md`.
