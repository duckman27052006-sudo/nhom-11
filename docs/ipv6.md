# IPv6 end-to-end

## 1. Chế độ family

| Giá trị | Listener | Ghi chú |
|---|---|---|
| `ipv4` | một socket `AF_INET` | mặc định khi `--host` là IPv4 |
| `ipv6` | một socket `AF_INET6` | mặc định khi `--host` chứa `:` |
| `dual` | hai socket riêng, cùng port | IPv4 + IPv6, `IPV6_V6ONLY=1` |
| `auto` | suy ra từ `--host` | `0.0.0.0` → IPv4, `::1` → IPv6 |

`dual` cố ý dùng hai socket thay vì một socket dual-stack, để hành vi giống nhau
trên Windows/Linux và để mỗi phiên chọn đúng family cho TID.

```bash
# IPv4 only
python -m tftp.tftp_server --host 0.0.0.0 --family ipv4 --port 6969

# IPv6 only
python -m tftp.tftp_server --host ::1 --family ipv6 --port 6969

# Dual-stack
python -m tftp.tftp_server --host 0.0.0.0 --host6 :: --family dual --port 6969
```

Server log và GUI hiển thị đúng địa chỉ đã bind, ví dụ `[::1]:6969`; không dùng
`0.0.0.0` để mô tả IPv6.

## 2. Cú pháp địa chỉ

| Dạng | Ví dụ | Ý nghĩa |
|---|---|---|
| IPv4 | `10.0.0.5` | host, dùng port mặc định |
| IPv4:port | `10.0.0.5:7000` | host + port |
| IPv6 có ngoặc | `[2001:db8::10]:6969` | khuyến nghị |
| IPv6 không port | `2001:db8::10` | dùng port mặc định |
| Link-local + zone | `[fe80::1%12]:6969` | zone là interface index |
| Link-local + tên interface | `[fe80::1%Ethernet]:6969` | nếu OS hỗ trợ `if_nametoindex` |

Lấy interface index:

- Windows: `netsh interface ipv6 show interfaces` (cột `Idx`).
- Linux: `ip -6 addr show` (phần `%eth0`), hoặc `cat /sys/class/net/eth0/ifindex`.

## 3. Client

```bash
# DNS có cả A và AAAA: auto thử lần lượt
python -m tftp.tftp_client --server tftp.example.lan:6969 --get image.bin

# Ép IPv6
python -m tftp.tftp_client --server "[2001:db8::10]:6969" --family ipv6 --get image.bin

# Link-local
python -m tftp.tftp_client --server "[fe80::1%12]:6969" --family ipv6 --get image.bin

# Đặt hạn tổng và cho phép ghi đè file đích
python -m tftp.tftp_client --server "[::1]:6969" --get a.bin --total-timeout 120 --overwrite
```

Hành vi:

- Client dùng `getaddrinfo()` và thử từng địa chỉ trước khi transfer bắt đầu.
- Sau khi server trả TID, client khóa sockaddr đó (gồm `scope_id` với IPv6) và
  không đổi địa chỉ giữa chừng.
- Nguồn gói được kiểm tra theo IP đã resolve và TID, không so sánh chuỗi hostname.
- Tải xuống ghi vào `.part` cạnh file đích, `fsync`, rồi `os.replace`; mặc định
  không ghi đè file đích đã tồn tại.

## 4. Server/session

- Mỗi phiên tạo socket TID cùng family với listener nhận request.
- TID bind trên địa chỉ cụ thể của listener (nếu listener bind cụ thể), hoặc
  wildcard cùng family nếu listener dùng `0.0.0.0`/`::`.
- Khóa phiên là `(ip chuẩn hóa, port, scope_id)`; IPv4-mapped IPv6 được chuẩn hóa
  về IPv4 để tránh hai danh tính giả.
- Shutdown đóng toàn bộ listener và phiên của cả hai family.

## 5. Kiểm thử

```bash
python -m unittest tests.test_net -v
```

Test IPv6 tự skip với lý do rõ ràng nếu máy/CI không bind được `::1`. Pipeline
phát hành cần ít nhất một runner thật có IPv6 loopback.

## 6. Sự cố thường gặp

| Triệu chứng | Kiểm tra |
|---|---|
| `Cannot assign requested address` | địa chỉ chưa gán cho interface; `ip -6 addr` / `Get-NetIPAddress` |
| Link-local timeout | thiếu `%zone` hoặc sai interface index |
| Dual-stack chỉ chạy IPv4 | IPv6 bind lỗi; xem log khởi động, kiểm tra `--host6` |
| Client chọn sai family | dùng `--family ipv4/ipv6` để ép |
| Firewall chặn sau block đầu | chưa mở dải TID; xem `docs/deployment.md` |
| `Network is unreachable` | thiếu route IPv6 hoặc router quảng bá sai prefix |
