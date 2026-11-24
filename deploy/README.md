# Triển khai nhiều trụ sở

Bộ triển khai này dựng **một** cổng chat/file trung tâm cho hai (hoặc nhiều) trụ sở,
nối với nhau bằng VPN site-to-site. Cả hai nơi dùng chung một `portal.sqlite3`, một
phòng chat và một bảng quản trị.

```
Trụ sở A 192.168.10.0/24 ──┐
                           ├── WireGuard 10.8.0.0/24 ──►  Server trung tâm
Trụ sở B 192.168.20.0/24 ──┘                               192.168.10.50:8080
                                                           ├─ web-data/portal.sqlite3
                                                           └─ TFTP 127.0.0.1:6969
```

Điểm quan trọng: **không NAT** lưu lượng chi nhánh. Gói tin từ B đến server mang IP
nguồn `192.168.20.x`, nhờ vậy ACL `--allowed-network` phân biệt được client thuộc
trụ sở nào. TFTP không đi qua WAN: trình duyệt gửi file qua HTTP, web gateway ghi
vào TFTP nội bộ trên cùng máy chủ.

## Yêu cầu

- Một máy chủ Linux (khuyến nghị Ubuntu 22.04/24.04) có IP LAN cố định, ví dụ
  `192.168.10.50`.
- Docker Engine 24+ và Docker Compose v2.
- WireGuard trên máy chủ hoặc trên router hai đầu.
- Hai subnet LAN **không trùng nhau** (ví dụ `192.168.10.0/24` và `192.168.20.0/24`).
- DNS nội bộ trỏ `chat.congty.local` về IP máy chủ.

> Docker Desktop trên Windows không phù hợp với mô hình này vì host networking và
> cách nhìn IP client khác nhau. Trên Windows, dùng cách chạy native ở mục 9.

## Cấu trúc

```
deploy/
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── run_web.py
├── lab_two_sites.py
├── caddy/Caddyfile.example
├── wireguard/wg0-hq.conf.example
├── wireguard/wg0-branch.conf.example
└── README.md
```

## 1. Chuẩn bị máy chủ

```bash
sudo mkdir -p /opt/tftp-chat/files /opt/tftp-chat/web-data
sudo chown -R 10001:10001 /opt/tftp-chat
```

Container chạy UID `10001`; thư mục bind mount phải ghi được bởi UID này.

Lấy mã nguồn:

```bash
git clone <repo-url> /opt/tftp-chat-src
cd /opt/tftp-chat-src/deploy
```

## 2. Cấu hình

```bash
cp .env.example .env
```

Sửa `deploy/.env`:

```dotenv
WEB_HOST=192.168.10.50
WEB_PORT=8080
ALLOWED_NETWORKS=192.168.10.0/24,192.168.20.0/24
HOST_FILES_DIR=/opt/tftp-chat/files
HOST_DATA_DIR=/opt/tftp-chat/web-data
```

`WEB_HOST` phải là IP private **thực sự có trên máy chủ**; không dùng `0.0.0.0`.
Nếu đặt server trong dải WireGuard thì dùng IP đó (`10.8.0.1`).
`HOST_FILES_DIR`/`HOST_DATA_DIR` là đường dẫn trên host; trong container chúng
được mount tại `/srv/tftp/files` và `/srv/tftp/web-data`.

## 3. WireGuard

Sinh khoá trên máy chủ:

```bash
wg genkey | tee hq-private.key | wg pubkey > hq-public.key
wg genkey | tee branch-private.key | wg pubkey > branch-public.key
chmod 600 hq-private.key branch-private.key
```

Điền khoá vào `wireguard/wg0-hq.conf.example` và
`wireguard/wg0-branch.conf.example`, rồi copy thành `/etc/wireguard/wg0.conf` trên
từng máy/router. Đổi `br0` trong file branch thành interface LAN thật.

```bash
sudo systemctl enable --now wg-quick@wg0
sudo wg show
```

- File HQ cho phép peer chi nhánh `10.8.0.2/32` và `192.168.20.0/24`.
- File branch cho phép `10.8.0.0/24` và `192.168.10.0/24`.
- **Không** thêm `MASQUERADE` trên branch. Nếu NAT, ACL chỉ thấy `10.8.0.2` và
  không phân biệt được trụ sở.
- Bật `net.ipv4.ip_forward=1` trên router hai đầu.
- Nếu chỉ muốn chi nhánh truy cập server (không cần máy khác trong LAN HQ truy cập
  ngược lại), có thể bỏ các rule FORWARD trên máy chủ; kết nối tới chính server
  không cần forward.

Kiểm tra:

```bash
ping 10.8.0.2
ping 192.168.20.10     # một máy bất kỳ trong LAN chi nhánh
```

Nếu server HQ nằm sau NAT, forward UDP `51820` từ router công cộng vào server;
chi nhánh khởi tạo kết nối nên không cần mở inbound ở phía chi nhánh. Nếu distro
dùng nftables thay vì iptables, thay các rule `FORWARD` trong file mẫu bằng rule
tương ứng. Kiểm tra `wg show` phải thấy peer `latest handshake` và route
`192.168.20.0/24 dev wg0`.

Nếu ping được nhưng web 403, xem mục 7 và 10.

## 4. Chạy cổng chat

```bash
cd /opt/tftp-chat-src/deploy
docker compose up -d --build
docker compose ps
docker compose logs -f web
```

Kiểm tra nhanh:

```bash
curl http://192.168.10.50:8080/healthz
# {"status":"ok"}
```

Hai service dùng host network:

- `tftp` chỉ nghe `127.0.0.1:6969`.
- `web` bind `WEB_HOST:8080`, thấy IP thật của client.
- File dùng chung volume `/opt/tftp-chat/files`.
- SQLite nằm ở `/opt/tftp-chat/web-data/portal.sqlite3`.

## 5. Firewall

Chỉ mở 8080 cho đúng hai subnet, không mở ra Internet:

```bash
sudo ufw allow from 192.168.10.0/24 to any port 8080 proto tcp
sudo ufw allow from 192.168.20.0/24 to any port 8080 proto tcp
sudo ufw allow 51820/udp
# Không mở 6969.
```

Nếu dùng nftables, thay bằng rule tương ứng. Kiểm tra từ ngoài:

```bash
nc -vz <public-ip> 8080   # phải bị từ chối
```

## 6. DNS và tài khoản đầu tiên

Thêm bản ghi nội bộ ở cả hai site:

```
chat.congty.local.  A  192.168.10.50
```

Mở `http://chat.congty.local:8080`:

- Tài khoản đầu tiên tự là quản trị viên.
- Muốn nâng một tài khoản đã có, đặt `ADMIN_USER=ten_tai_khoan` trong `.env` rồi
  `docker compose up -d web`.

## 7. Kiểm tra hai trụ sở

Từ một máy ở B:

```bash
curl http://chat.congty.local:8080/healthz
```

Mở trình duyệt, đăng nhập, gửi tin nhắn và gửi một file. Từ A kiểm tra:

- Tin nhắn xuất hiện ở cả hai nơi.
- File nằm trong danh sách chung.
- Tài khoản admin ở A khóa/mute được tài khoản đang online ở B.
- Nhật ký kiểm duyệt ghi đúng người thực hiện.

Nếu B bị 403 với thông báo “không nằm trong mạng LAN được phép”, kiểm tra:

```bash
# trên máy B
curl -s http://chat.congty.local:8080/healthz
# trên server
sudo tcpdump -ni any port 8080
```

Gói tin phải có IP nguồn `192.168.20.x`. Nếu thấy `10.8.0.2` hoặc IP Docker, bạn
đang NAT hoặc dùng bridge network — xem mục 10.

## 7b. Demo hai trụ sở trên một máy

Không có sẵn hai văn phòng/VPN vẫn có thể xem mô hình hoạt động bằng lab tích hợp.
Lab chạy **một** cổng chat trung tâm, đặt hai reverse proxy nhỏ phía trước và gắn
`X-Forwarded-For` giả cho từng site:

```powershell
# Windows PowerShell
.\.venv\Scripts\python deploy/lab_two_sites.py
```

```bash
# Linux/macOS
python deploy/lab_two_sites.py
```

Mở:

| Vai trò | URL |
|---|---|
| Trụ sở A | `http://127.0.0.1:8094` |
| Trụ sở B | `http://[::1]:8095` hoặc `http://localhost:8095` |
| Site lạ (bị chặn) | `http://127.0.0.1:8096` |

Tài khoản: `alice_nguyen` / `matkhau123` (quản trị), `bob_tran` / `matkhau123`
(thành viên). Hai địa chỉ có host khác nhau nên phiên đăng nhập tách biệt; tin nhắn,
file và bảng quản trị dùng chung. Site C trả 403 vì IP giả `203.0.113.5` không nằm
trong `--allowed-network`.

Đây chỉ là **mô phỏng bằng header**, không phải VPN thật. Dùng để kiểm tra UI/ACL/
luồng nhiều site trước khi dựng WireGuard. Lab tạo dữ liệu trong thư mục tạm; thêm
`--data-dir ./lab-data` nếu muốn giữ lại. Ctrl+C để dừng.

## 8. HTTPS tuỳ chọn với Caddy

Chạy Caddy trên **cùng máy chủ**, trỏ về loopback:

```bash
sudo apt install caddy
sudo cp caddy/Caddyfile.example /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Sửa `.env`:

```dotenv
WEB_HOST=127.0.0.1
TRUSTED_PROXIES=127.0.0.1/32
SECURE_COOKIE=1
```

Khởi động lại web:

```bash
docker compose up -d web
```

Mở `https://chat.congty.local`. Caddy tự gửi `X-Forwarded-For/Proto/Host`; app chỉ
tin các header này khi IP kết nối thật nằm trong `TRUSTED_PROXIES`, nên client bên
ngoài không thể tự đặt header để giả IP.

Firewall cho HTTPS:

```bash
sudo ufw allow from 192.168.10.0/24 to any port 443 proto tcp
sudo ufw allow from 192.168.20.0/24 to any port 443 proto tcp
# Có thể đóng 8080 với hai subnet vì web đã bind loopback:
sudo ufw delete allow from 192.168.10.0/24 to any port 8080 proto tcp
sudo ufw delete allow from 192.168.20.0/24 to any port 8080 proto tcp
```

Lưu ý:

- `tls internal` tạo CA nội bộ; máy client phải tin CA (`caddy trust` hoặc GPO/MDM).
- Nếu Caddy chạy trên máy khác, thêm IP Caddy vào `TRUSTED_PROXIES` và bind web
  vào IP mà Caddy kết nối tới; firewall chỉ cho Caddy gọi 8080.
- Không đặt `TRUSTED_PROXIES=0.0.0.0/0`.
- `TRUSTED_PROXIES` phải là proxy thật; proxy phải ghi đè/append `X-Forwarded-For`.

## 9. Phương án Tailscale

Nếu không muốn tự dựng WireGuard:

1. Cài Tailscale trên máy chủ và trên một **subnet router** ở chi nhánh.
2. Ở chi nhánh:

   ```bash
   sudo tailscale up --advertise-routes=192.168.20.0/24
   ```

3. Approve route trong Tailscale admin console.
4. Máy ở B truy cập `http://192.168.10.50:8080` (địa chỉ LAN thật), nhờ vậy ACL
   vẫn thấy `192.168.20.x`.

Có thể bind trực tiếp vào IP Tailscale `100.x` — phiên bản này đã chấp nhận
`100.64.0.0/10` trong `--host` và `--allowed-network`:

```dotenv
WEB_HOST=100.100.100.100
ALLOWED_NETWORKS=100.64.0.0/10
```

Khi đó mọi thiết bị trong tailnet đều thuộc một dải; hãy giới hạn bằng ACL của
Tailscale và tài khoản quản trị.

## 10. Chạy native trên Windows (không Docker)

Docker Desktop không phù hợp với ACL theo IP ở mô hình này. Trên Windows:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\pip install -e ".[web]"

# Terminal 1: TFTP nội bộ
.\.venv\Scripts\python -m tftp.tftp_server `
  --host 127.0.0.1 --port 6969 --root files

# Terminal 2: web gateway, cả hai subnet
.\.venv\Scripts\python -m tftp.web_upload `
  --host 192.168.10.50 --port 8080 `
  --allowed-network 192.168.10.0/24 `
  --allowed-network 192.168.20.0/24 `
  --root files --data-dir web-data
```

Cài WireGuard for Windows trên máy chủ, bật forwarding nếu cần, và mở firewall
TCP 8080 cho hai subnet. Dùng NSSM hoặc Task Scheduler để chạy hai lệnh trên khi
khởi động. Không dùng `--host 0.0.0.0`.

## 11. Vận hành

### Sao lưu

SQLite ở `/opt/tftp-chat/web-data/portal.sqlite3`. Dùng backup API thay vì copy
file đang mở:

```bash
sqlite3 /opt/tftp-chat/web-data/portal.sqlite3 \
  ".backup '/var/backups/tftp-chat/portal-$(date +%F-%H%M).sqlite3'"
```

Sao lưu cả `/opt/tftp-chat/files` nếu cần giữ file người dùng. Bản backup chứa
hash mật khẩu và nội dung chat — bảo vệ như dữ liệu nội bộ.

### Cập nhật

```bash
cd /opt/tftp-chat-src && git pull
cd deploy && docker compose up -d --build
```

Migration DB tự chạy khi khởi động. Sao lưu trước khi nâng phiên bản lớn.

### Log và sức khoẻ

```bash
docker compose logs --tail=200 web
curl -fsS http://192.168.10.50:8080/healthz
```

Bật NTP trên máy chủ. Polling chat 2 giây phù hợp tới khoảng 50–100 người dùng
đồng thời với cấu hình mặc định; vượt ngưỡng đó nên chuyển PostgreSQL + WebSocket.

## 12. Xử lý sự cố

| Hiện tượng | Nguyên nhân thường gặp | Cách xử lý |
|---|---|---|
| `bind: address already in use` | Cổng 8080/6969 đang bị tiến trình khác giữ | `ss -lntup \| grep -E '8080|6969'`; dừng tiến trình cũ |
| 403 “không nằm trong mạng LAN” | Client đi qua NAT/Docker bridge; thiếu subnet | Kiểm tra IP nguồn; thêm subnet; bỏ NAT; dùng host network |
| 403 “Nguồn yêu cầu không hợp lệ” | Reverse proxy thiếu `X-Forwarded-Host/Proto` hoặc chưa tin proxy | Kiểm tra `TRUSTED_PROXIES`, cấu hình Caddy/nginx |
| Upload báo 502/504 | TFTP chưa chạy hoặc sai root | `docker compose ps`; kiểm tra `--root` và `--tftp-server` |
| Hai site thấy dữ liệu khác nhau | Mỗi site chạy một DB/container riêng | Chỉ chạy một `web` + một `web-data` |
| Mất phiên khi đổi IP | Dùng IP thay vì DNS | Dùng `chat.congty.local` |
| `permission denied` trong volume | UID container khác chủ thư mục | `chown -R 10001:10001 /opt/tftp-chat` |

## 13. Bảo mật

- HTTP không mã hoá: bắt buộc VPN, hoặc bật HTTPS như mục 8.
- Giữ TFTP ở loopback; không publish 6969.
- Không mở 8080 từ Internet.
- `TRUSTED_PROXIES` càng hẹp càng tốt; chỉ proxy thật.
- Proxy phải ghi đè `X-Forwarded-For`; không chuyển nguyên header từ client.
- Cập nhật định kỳ, sao lưu DB, giới hạn quyền thư mục dữ liệu.
