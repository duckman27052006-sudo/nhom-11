# Vận hành, giám sát và xử lý sự cố

## 1. Log

- Server log mỗi transfer: hướng RRQ/WRQ, tên file đã chuẩn hóa, địa chỉ client
  (IPv4/IPv6), block, byte, thời lượng và mã lỗi.
- Không ghi nội dung file.
- `--log-file` dùng `RotatingFileHandler`; `--log-max-bytes` (mặc định 10 MiB)
  và `--log-backups` (mặc định 5) giới hạn dung lượng.
- Nếu ổ log đầy, handler có thể lỗi; tách log sang volume riêng và đặt cảnh báo
  dung lượng. Không đặt log trong thư mục root phục vụ TFTP.

Cấu hình qua JSON:

```json
{
  "log_file": "/var/log/tftp/server.log",
  "log_max_bytes": 10485760,
  "log_backups": 5
}
```

## 2. Metrics

`--stats-interval N` in bảng số liệu mỗi N giây; `--stats-json path` ghi JSON khi
dừng. Các nhóm chính:

| Nhóm | Ví dụ khóa |
|---|---|
| Request | `requests.rate_limited`, `requests.overloaded`, `requests.oversized` |
| Packet | `packets.malformed`, `packets.unknown_tid`, `packets.stale` |
| Transfer | `transfers.completed.RRQ`, `transfers.completed.WRQ`, `payload.sent/received` |
| Lỗi | `errors.sent.<code>`, `errors.unhandled`, `sessions.transfer_timeout` |
| Tài nguyên | `sessions.active`, `writes.active` |

Cảnh báo nên đặt khi:

- `requests.rate_limited`/`requests.overloaded` tăng liên tục.
- `sessions.transfer_timeout` hoặc `timeouts` tăng bất thường.
- `requests.disk_limited` > 0 hoặc dung lượng trống gần `min_free_bytes`.
- `errors.unhandled` > 0 (cần điều tra ngay).
- Listener không còn phản hồi (health check RRQ định kỳ).

## 3. File tạm và dữ liệu

- Upload ghi vào `.<tên>.<random>.part` trong cùng thư mục đích, `fsync`, rồi
  `os.replace` (nếu cho ghi đè) hoặc `os.link` + unlink (mặc định không ghi đè).
- Khi không cho ghi đè, nếu filesystem không hỗ trợ tạo hard link nguyên tử thì
  transfer sẽ thất bại và file đích được giữ nguyên; không fallback sang replace.
- Lỗi/timeout xóa `.part` trong `finally`.
- Khi khởi động, server xóa `.part` cũ hơn `--cleanup-temp-age` giây.
- Backup: sao lưu thư mục root và config theo lịch; kiểm tra restore định kỳ.
- Không coi UDP checksum là xác minh toàn vẹn nghiệp vụ. Nếu cần, dùng manifest
  hoặc chữ ký ở kênh ngoài TFTP.

## 4. Mã lỗi TFTP thường gặp

| Mã | Tên | Nguyên nhân thường gặp |
|---|---|---|
| 1 | File not found | sai tên/root, file chưa có |
| 2 | Access violation | quyền file/thư mục, path bị chặn |
| 3 | Disk full | hết đĩa, vượt `max_file_size`/`max_total_bytes`, `min_free_bytes` |
| 4 | Illegal operation | gói sai opcode/block, vượt blksize |
| 5 | Unknown TID | gói đến sai port TID |
| 6 | File already exists | ghi đè khi `allow_overwrite=false` |
| 7 | No such user | không dùng trong triển khai này |
| 8 | Option negotiation | option không hợp lệ/không hỗ trợ |

## 5. Xử lý sự cố nhanh

| Triệu chứng | Hành động |
|---|---|
| Client timeout ngay | kiểm tra server chạy, đúng IP/port, firewall UDP 6969 |
| Block đầu OK rồi timeout | firewall chặn dải TID; mở `--tid-port-range` |
| `ERROR 6` | client ghi file đã tồn tại; đổi tên hoặc bật `--overwrite` có kiểm soát |
| `ERROR 3` | kiểm tra đĩa, `max_file_size`, quota root |
| Upload xong nhưng thiếu file | kiểm tra log lỗi ghi/fsync, quyền thư mục đích |
| Server không khởi động | chạy `--check-config`; xem lỗi bind/permission/log |
| IPv6 không kết nối | kiểm tra `--family`, route, zone link-local, firewall IPv6 |
| Nghi ngờ lạm dụng | giảm `--max-clients`, `--max-requests-per-second`, siết firewall, dừng dịch vụ |

## 6. Shutdown và restart

- `SIGINT`/`SIGTERM` (Linux) hoặc Ctrl+C: server gửi ERROR cho phiên đang chạy,
  đóng listener, chờ thread tối đa 5 giây.
- Transfer đang chạy bị hủy; file đích không bị thay thế giữa chừng.
- Sau restart, kiểm tra `.part` còn sót và log để biết phiên bị ngắt.
- Với service, đặt restart policy có giới hạn (ví dụ `Restart=on-failure`,
  `RestartSec=5`) để tránh vòng lặp khởi động vô hạn.

## 7. Kiểm tra định kỳ

- [ ] `--check-config` sau mỗi thay đổi cấu hình.
- [ ] Transfer mẫu IPv4 + IPv6 từ subnet được duyệt.
- [ ] Dung lượng trống, log rotation và quyền file.
- [ ] Số phiên/TID bất thường, lỗi `unhandled`, tỷ lệ timeout.
- [ ] Backup/restore thử nghiệm.
- [ ] Rà soát firewall/ACL và tài khoản dịch vụ.
