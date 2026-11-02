# Triển khai TFTP an toàn trong LAN/VLAN

TFTP (RFC 1350) **không có xác thực, mã hóa hay phân quyền người dùng**. Địa chỉ
IP trong log không chứng minh danh tính. Chỉ triển khai khi:

- Máy chủ và client nằm trong LAN/VLAN tin cậy, đã được phê duyệt.
- Dữ liệu truyền đi có thể chấp nhận rủi ro bị đọc/sửa bởi thiết bị trong cùng
  miền broadcast hoặc bởi người có quyền trên đường mạng.
- Có firewall/VPN/ACL giới hạn nguồn truy cập.
- Nếu cần xác thực, mã hóa hoặc truyền qua Internet, dùng SFTP/SCP/HTTPS thay thế.

## 1. Mô hình mạng tối thiểu

| Hạng mục | Ví dụ |
|---|---|
| Server | Windows Server hoặc Linux, IP `10.20.0.10` / `2001:db8:20::10` |
| Client được duyệt | subnet `10.20.10.0/24`, `2001:db8:20:10::/64` |
| Well-known port | UDP 6969 (hoặc 69 nếu có đặc quyền) |
| Dải TID | UDP 50000-50100 (`--tid-port-range 50000-50100`) |
| Tài khoản chạy | tài khoản dịch vụ không thuộc Administrators/root |

Không dùng quy tắc “allow UDP any → any”. TFTP chuyển dữ liệu sang TID/port
ngẫu nhiên sau RRQ/WRQ, nên chỉ mở port 6969 là chưa đủ.

## 2. Cấu hình mẫu

`C:\ProgramData\TftpServer\server.json` hoặc `/etc/tftp/server.json`:

```json
{
  "host": "0.0.0.0",
  "host6": "::",
  "family": "dual",
  "port": 6969,
  "root": "C:/ProgramData/TftpServer/files",
  "allow_overwrite": false,
  "max_clients": 32,
  "max_file_size": 268435456,
  "min_free_bytes": 16777216,
  "max_total_bytes": 10737418240,
  "max_transfer_seconds": 3600,
  "tid_port_range": [50000, 50100],
  "cleanup_temp_age": 86400,
  "log_file": "C:/ProgramData/TftpServer/logs/server.log",
  "log_max_bytes": 10485760,
  "log_backups": 5
}
```

Kiểm tra trước khi chạy:

```bash
python -m tftp.tftp_server --config server.json --check-config
```

Lệnh trên in cấu hình hiệu lực và thoát với mã `0` nếu hợp lệ. Khóa lạ, sai kiểu,
port/blksize/quota ngoài khoảng đều bị từ chối với mã `2`.

## 3. Quyền hệ thống

### Windows

1. Tạo tài khoản dịch vụ `svc-tftp` (không thuộc Administrators).
2. Cấp quyền **Read & Execute** trên thư mục đọc và **Modify** trên thư mục
   upload; cấp **Modify** riêng cho thư mục log.
3. Không cấp quyền ghi vào thư mục cài đặt/code.
4. Nếu cần tách đọc/ghi, dùng hai instance root khác nhau và firewall theo port.

### Linux

```bash
sudo useradd --system --home /var/lib/tftp --shell /usr/sbin/nologin tftp
sudo install -d -o tftp -g tftp -m 0750 /var/lib/tftp/files /var/lib/tftp/logs
sudo install -d -o tftp -g tftp -m 0700 /var/lib/tftp/incoming
```

Chạy bằng systemd unit mẫu trong `packaging/tftp-server.service`, chỉnh
`ExecStart`, `User`, `Group`, `ReadWritePaths`.

## 4. Firewall

### Windows Defender Firewall (PowerShell, chạy quản trị)

```powershell
New-NetFirewallRule -DisplayName "TFTP well-known" -Direction Inbound `
  -Protocol UDP -LocalPort 6969 -RemoteAddress 10.20.10.0/24,2001:db8:20:10::/64 `
  -Action Allow

New-NetFirewallRule -DisplayName "TFTP TID range" -Direction Inbound `
  -Protocol UDP -LocalPort 50000-50100 -RemoteAddress 10.20.10.0/24,2001:db8:20:10::/64 `
  -Action Allow
```

### Linux nftables

```nft
table inet tftp {
  set clients4 { type ipv4_addr; flags interval; elements = { 10.20.10.0/24 } }
  set clients6 { type ipv6_addr; flags interval; elements = { 2001:db8:20:10::/64 } }
  chain input {
    type filter hook input priority 0; policy drop;
    udp dport 6969 ip saddr @clients4 accept
    udp dport 6969 ip6 saddr @clients6 accept
    udp dport 50000-50100 ip saddr @clients4 accept
    udp dport 50000-50100 ip6 saddr @clients6 accept
  }
}
```

Ghi chú:

- Firewall stateful theo kết nối TCP không theo dõi đúng luồng TFTP/UDP; cần mở
  dải TID hoặc dùng rule động ở tầng thiết bị hỗ trợ TFTP ALG.
- NAT chỉ hoạt động nếu thiết bị theo dõi được TID; không giả định NAT traversal.
- IPv6 privacy address làm địa chỉ client thay đổi; ACL theo prefix thay vì
  từng địa chỉ.

## 5. Chạy nền

### Linux systemd

```bash
sudo cp packaging/tftp-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tftp-server
sudo systemctl status tftp-server
journalctl -u tftp-server -f
```

### Windows Service

Dùng NSSM hoặc công cụ tương đương, vì repo không phụ thuộc `pywin32`:

```powershell
nssm install TftpServer "C:\Python311\python.exe" "-m tftp.tftp_server --config C:\ProgramData\TftpServer\server.json"
nssm set TftpServer AppDirectory "C:\baitap\Trivial File Transfer Protocol"
nssm set TftpServer ObjectName ".\svc-tftp" "<mật-khẩu>"
nssm set TftpServer Start SERVICE_AUTO_START
nssm start TftpServer
```

Không chạy `tftp.bat` trong production: script dành cho phát triển và có thể cài
package.

## 6. Readiness và lỗi khởi động

Server chỉ được coi là sẵn sàng khi:

1. Cấu hình hợp lệ (`--check-config` trả `0`).
2. Bind được **tất cả** listener đã cấu hình; nếu dual-stack mà IPv6 bind lỗi,
   tiến trình thoát với mã `2` thay vì chạy nửa vời.
3. Thư mục root tồn tại/ghi được (server tạo root nếu chưa có; quyền ghi do tài
   khoản dịch vụ quyết định).
4. Log ghi được.

Kiểm tra nhanh từ máy client:

```bash
python -m tftp.tftp_client --server 10.20.0.10:6969 --get ping.bin --timeout 2
python -m tftp.tftp_client --server "[2001:db8:20::10]:6969" --family ipv6 --get ping.bin --timeout 2
```

## 7. Nâng cấp và rollback

1. Sao lưu `server.json`, thư mục log và dữ liệu nghiệp vụ.
2. Chạy `--check-config` trên cấu hình mới.
3. Dừng dịch vụ, cập nhật code, khởi động lại.
4. Kiểm tra readiness và một transfer đọc/ghi mẫu.
5. Nếu lỗi: dừng dịch vụ, khôi phục code + config + dữ liệu từ bản sao lưu.

Giữ ít nhất một bản code và config trước đó; không rollback bằng cách xóa log
hoặc file `.part` đang được phiên khác sử dụng.
