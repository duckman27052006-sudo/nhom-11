# Triển khai nền tảng (Windows/Linux)

Hướng dẫn này dành cho chạy **lâu dài**: TFTP server và/hoặc cổng web upload như
một dịch vụ, không phải mở terminal rồi Ctrl+C. Phần bảo mật mạng vẫn theo
[`web-upload.md`](web-upload.md): chỉ dùng trong LAN tin cậy, không port-forward
ra Internet.

## 1. Chuẩn bị

```powershell
cd C:\tftp\nhom-11
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e ".[web]"   # thêm ".[gui]" nếu cần GUI
```

- Dùng tài khoản **không phải Administrator** để chạy dịch vụ. TFTP mặc định bind
  port 6969 nên không cần quyền đặc biệt; chỉ port 69 mới cần.
- Tạo thư mục dữ liệu riêng, ví dụ `C:\tftp\data`, và cấp quyền ghi cho tài khoản
  dịch vụ. Đây là `--root` của TFTP server.
- Không đặt thư mục dữ liệu trong thư mục cài đặt để nâng cấp không đụng dữ liệu.

## 2. Chạy TFTP server như dịch vụ (Windows, NSSM)

```powershell
nssm install TftpServer "C:\tftp\nhom-11\.venv\Scripts\python.exe"
nssm set TftpServer AppParameters "-m src.tftp_server --host 192.168.1.10 --port 6969 --root C:\tftp\data --max-clients 32 --no-overwrite --stats-interval 300"
nssm set TftpServer AppDirectory "C:\tftp\nhom-11"
nssm set TftpServer AppStdout "C:\tftp\logs\tftp-server.log"
nssm set TftpServer AppStderr "C:\tftp\logs\tftp-server.log"
nssm set TftpServer AppRotateFiles 1
nssm set TftpServer AppRotateBytes 10485760
nssm set TftpServer Start SERVICE_AUTO_START
nssm start TftpServer
```

`--host` phải là IP cụ thể của máy chủ (không dùng `0.0.0.0` nếu chỉ phục vụ một
subnet). Ghi log ra file bằng `--log-file` nếu muốn tách khỏi stdout của NSSM.

## 3. Chạy cổng web upload như dịch vụ

```powershell
nssm install TftpWeb "C:\tftp\nhom-11\.venv\Scripts\python.exe"
nssm set TftpWeb AppParameters "-m src.web_upload --host 192.168.1.10 --port 8080 --allowed-network 192.168.1.0/24 --tftp-server 127.0.0.1:6969 --max-file-size 100"
nssm set TftpWeb AppDirectory "C:\tftp\nhom-11"
nssm set TftpWeb AppStdout "C:\tftp\logs\tftp-web.log"
nssm set TftpWeb AppStderr "C:\tftp\logs\tftp-web.log"
nssm set TftpWeb Start SERVICE_AUTO_START
nssm start TftpWeb
```

Có thể dùng entry point `tftp-web-upload` thay cho `-m src.web_upload` sau khi
`pip install -e ".[web]"`. Mỗi subnet cần thêm một `--allowed-network`.

## 4. Firewall

Chỉ mở đúng cổng, đúng subnet:

```powershell
New-NetFirewallRule -DisplayName "TFTP UDP 6969" -Direction Inbound -Protocol UDP `
  -LocalPort 6969 -RemoteAddress 192.168.1.0/24 -Action Allow
New-NetFirewallRule -DisplayName "TFTP web 8080" -Direction Inbound -Protocol TCP `
  -LocalPort 8080 -RemoteAddress 192.168.1.0/24 -Action Allow
```

Không mở UDP 69 trừ khi thật sự cần client legacy và đã chấp nhận rủi ro.

## 5. Giới hạn tài nguyên

- `--max-clients` (TFTP) và `--threads` (web) chặn số phiên đồng thời.
- `--max-file-size` (TFTP: byte, web: MiB) chặn file quá lớn; đặt cả hai.
- `--max-requests-per-second` chặn flood request.
- Cân nhắc quota/hạn mức đĩa ở mức hệ điều hành cho thư mục dữ liệu.

## 6. Giám sát

- `--stats-interval 300` in bảng số liệu mỗi 5 phút vào log dịch vụ.
- `--stats-json metrics.json` ghi số liệu khi dừng; thu bằng agent nếu cần.
- Các chỉ số đáng báo động: `sessions.timeout`, `packets.retransmitted`,
  `errors.unhandled`, `requests.rate_limited`, `sessions.active` sát trần.
- Log web có IP trình duyệt; log TFTP có IP gateway. Đối chiếu hai bên khi điều tra.

## 7. Nâng cấp và sao lưu

1. Dừng dịch vụ: `nssm stop TftpWeb`, `nssm stop TftpServer`.
2. Sao lưu `C:\tftp\data` (chỉ là thư mục file, không có database).
3. Cập nhật mã nguồn, chạy lại `pip install -e ".[web]"`.
4. Chạy test nhanh: `python -m unittest discover` (đặt `TFTP_SKIP_SOAK=1` nếu cần).
5. Khởi động lại và kiểm tra `http://<ip>:8080/` cùng một upload thử.

## 8. Linux (systemd)

```ini
# /etc/systemd/system/tftp-server.service
[Unit]
Description=TFTP server
After=network-online.target

[Service]
User=tftp
WorkingDirectory=/opt/nhom-11
ExecStart=/opt/nhom-11/.venv/bin/python -m src.tftp_server \
  --host 192.168.1.10 --port 6969 --root /srv/tftp --no-overwrite
Restart=on-failure
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tftp-server
```

Với cổng web, dùng unit tương tự với `ExecStart=... -m src.web_upload ...`.
Không chạy Flask development server trong production; lệnh `python -m
src.web_upload` đã dùng Waitress.

## 9. Gỡ lỗi thường gặp

| Triệu chứng | Kiểm tra |
|---|---|
| `WinError 10049` khi bind | `--host` không phải IP của máy này |
| Client timeout dù server chạy | firewall UDP, đúng subnet, `--host` đúng card mạng |
| Web trả 403 | peer IP ngoài `--allowed-network`, thiếu CSRF, hoặc `Origin` lệch |
| Web trả 413 | vượt `--max-file-size` (MiB) hoặc giới hạn của TFTP server |
| Web trả 502 | TFTP server không chạy/không tới được `--tftp-server` |
| Upload xong nhưng không thấy file | kiểm tra `--root` của TFTP server và quyền ghi |
