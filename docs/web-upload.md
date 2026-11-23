# Cổng file và chat TFTP trong LAN

Cổng web cung cấp đăng ký/đăng nhập/đăng xuất, danh sách file, gửi/tải file,
phòng chat chung và bảng điều khiển quản trị. Mọi thao tác file/chat yêu cầu đăng
nhập. Đăng ký thành công tự đăng nhập. Máy chủ giữ tài khoản, vai trò, lịch sử
kiểm duyệt và tin nhắn qua các lần khởi động.

## Cài đặt và chạy

Trên Windows, mở `tftp.bat`: launcher tự khởi động TFTP qua `127.0.0.1:6969`
trong GUI rồi mở gateway web. Đợi GUI báo **Server đang chạy** trước khi gửi file.
Nếu mở GUI thủ công bằng `python -m tftp.tftp_gui` thì vẫn cần bấm
**Khởi động server**; thêm `--start-server` để tự bật TFTP loopback.

Yêu cầu Python 3.10+. Tại thư mục repository:

```powershell
python -m pip install -e ".[web]"
```

Terminal thứ nhất:

```powershell
python -m tftp.tftp_server --host 127.0.0.1 --port 6969 --root files
```

Terminal thứ hai, chạy thử trên cùng máy:

```powershell
python -m tftp.web_upload --host 127.0.0.1 --port 8080 --root files
```

Mở `http://127.0.0.1:8080`. Có thể dùng entry point `tftp-web-upload` thay cho
`python -m tftp.web_upload` sau khi cài package.

## Cho thiết bị cùng LAN truy cập

Chạy `ipconfig`, lấy IPv4 và subnet của card Wi-Fi/Ethernet đang dùng. Ví dụ
máy chủ có IP `192.168.1.10`, subnet mask `255.255.255.0`:

```powershell
python -m tftp.web_upload --host 192.168.1.10 --port 8080 --allowed-network 192.168.1.0/24 --tftp-server 127.0.0.1:6969 --root files
```

Thiết bị cùng LAN mở `http://192.168.1.10:8080`. Cho phép TCP 8080 trong Windows
Defender Firewall cho subnet LAN đã chọn, không tự mở firewall cho mọi mạng.
Thiết bị không cần cài TFTP client. Khi gateway và TFTP dùng loopback, không cần
mở UDP ra LAN. Guest Wi-Fi/client isolation có thể ngăn thiết bị kết nối nhau.

`--host` phải là IP private/loopback (hoặc CGNAT `100.64.0.0/10` cho Tailscale) cụ
thể, không dùng `0.0.0.0` hoặc địa chỉ public. Bind LAN bắt buộc có
`--allowed-network`; có thể lặp lại cờ cho nhiều subnet. ACL dùng địa chỉ kết nối
thật; chỉ khi peer nằm trong `--trusted-proxy` gateway mới đọc
`X-Forwarded-For/Proto/Host` (xem mục nhiều trụ sở).

## Cấu hình

| Tham số | Mặc định | Công dụng |
|---|---|---|
| `--host` | `127.0.0.1` | IP web cụ thể |
| `--port` | `8080` | TCP port web |
| `--allowed-network` | loopback khi bind loopback | Subnet được phép truy cập |
| `--trusted-proxy` | không đặt | CIDR của reverse proxy được đọc `X-Forwarded-*`; lặp lại được |
| `--root` | `files` | Cùng thư mục root với TFTP server |
| `--tftp-server` | `127.0.0.1:6969` | IPv4, hostname hoặc `[IPv6]:port` TFTP |
| `--data-dir` | `web-data` | Dữ liệu tài khoản/phiên/chat, nằm ngoài root TFTP |
| `--max-file-size` | `100` | MiB tối đa mỗi file, cả tải và gửi |
| `--secure-cookie` | tắt | Chỉ dùng nếu phục vụ web qua HTTPS |
| `--max-concurrent-uploads` | `4` | Tổng lượt tải/gửi đồng thời |
| `--trusted-host` | địa chỉ bind + localhost | Host header được phép thêm (tên miền nội bộ); lặp lại được |
| `--threads` | `8` | Số worker Waitress |
| `--admin` | không đặt | Cấp quyền quản trị cho tài khoản đã có trong data-dir |

Host header được kiểm tra theo allowlist để chặn DNS rebinding: mặc định chỉ
chấp nhận địa chỉ bind, `localhost`, `127.0.0.1` và `::1`. Nếu người dùng mở
portal bằng tên miền nội bộ (ví dụ `http://upload.lan:8080`), thêm
`--trusted-host upload.lan`; không cần thêm khi truy cập bằng IP LAN.

Chạy hai tiến trình cùng máy với cùng root. TFTP không hỗ trợ liệt kê file, nên
gateway đọc danh sách từ thư mục root. Nếu đổi root ở một bên phải đổi bên còn lại.
TFTP client hỗ trợ IPv4/IPv6. Với mạng IPv6 private, có thể bind web vào IP ULA,
cho phép subnet tương ứng và dùng `--tftp-server "[::1]:6969"` khi TFTP cũng
đang lắng nghe trên IPv6.

## Tài khoản và chat

- Tên tài khoản 3–32 ký tự: chữ ASCII, số, dấu `_`; tên trùng không phân biệt hoa thường.
- Mật khẩu 8–128 ký tự, lưu hash có salt. Phiên 12 giờ, cookie HttpOnly và
  SameSite=Lax; đăng xuất hủy phiên phía server. Có CSRF và giới hạn đăng nhập/đăng ký.
- Một phòng chat chung, không có tin nhắn riêng. Tin 1–2.000 ký tự, tối đa 30
  tin/phút mỗi tài khoản. Nội dung hiển thị như văn bản thuần.
- Mở phòng tải 50 tin mới nhất; cập nhật mỗi 2 giây. Thời gian theo thiết bị người xem.
- Sao lưu `web-data/portal.sqlite3` bằng SQLite backup khi đang chạy hoặc sao chép
  cả thư mục dữ liệu sau khi dừng gateway. Giữ thư mục này riêng tư và không commit.
- Không có chức năng khôi phục mật khẩu hay duyệt tài khoản trước khi vào cổng.

## Vai trò và kiểm duyệt

Tài khoản đầu tiên tạo trong một data-dir trống tự nhận vai trò `admin`. Với
data-dir đã có tài khoản từ bản cũ, tài khoản sớm nhất được nâng lên `admin` khi
mở cổng lần đầu để luôn có người quản trị. Muốn cấp quyền cho tài khoản khác đã
đăng ký, chạy gateway kèm `--admin <tên_tài_khoản>`; lệnh này idempotent và chỉ
đổi vai trò, không đổi mật khẩu hay phiên hiện có.

Mọi thành viên đã đăng nhập có thể đọc và gửi tin trong phòng chat chung, xem và
tải file. Quản trị viên có thêm bảng điều khiển "Quản trị" với các quyền sau:

| Thao tác | Hiệu lực |
|---|---|
| Khóa / mở khóa tài khoản | Chặn đăng nhập; thu hồi mọi phiên đang mở ngay lập tức. Có thể chọn 1 giờ, 24 giờ, 7 ngày hoặc vĩnh viễn kèm lý do. |
| Hạn chế / bỏ hạn chế chat | Tài khoản vẫn đọc được phòng chat nhưng không gửi được tin mới; hết hạn tự mở. |
| Cấp / hạ quyền quản trị | Chỉ đổi vai trò; không thể tự đổi vai trò của mình hoặc hạ quyền quản trị viên cuối cùng. |
| Xóa một tin nhắn | Quản trị viên xóa được mọi tin; mỗi thành viên cũng xóa được tin của chính mình. |
| Xóa lịch sử chat | Xóa toàn bộ phòng chat hoặc toàn bộ tin của một tài khoản. Không thể hoàn tác. |

Các quy tắc an toàn: quản trị viên phải được hạ quyền trước khi bị khóa hoặc hạn
chế chat; không thể tự khóa, tự hạn chế hay tự đổi vai trò; tài khoản đang bị khóa
hoặc hạn chế chat không được cấp quyền quản trị cho tới khi gỡ hạn chế. Mọi thao
tác kiểm duyệt được ghi vào bảng `moderation` và hiển thị trong nhật ký 24 mục gần
nhất. Xóa tin nhắn làm tăng mốc `chat_epoch`, vì vậy các trình duyệt đang mở tự tải
lại danh sách thay vì giữ bản sao cũ. Khóa tài khoản không xóa file đã tải lên.

## Nhiều trụ sở

Công ty có hai văn phòng nên chạy **một** gateway trung tâm và nối hai LAN bằng VPN
site-to-site; cả hai dùng chung `web-data/portal.sqlite3`. Không chạy hai gateway
với hai DB riêng vì phòng chat sẽ tách làm hai.

- Bind web vào IP private của máy chủ trong VPN/LAN, ví dụ `--host 192.168.10.50`.
- Thêm cả hai subnet: `--allowed-network 192.168.10.0/24 --allowed-network 192.168.20.0/24`.
- Không NAT lưu lượng chi nhánh để ACL thấy IP nguồn thật; NAT sẽ biến mọi client
  thành IP tunnel và mất khả năng phân biệt trụ sở.
- TFTP vẫn `127.0.0.1:6969` cạnh gateway; client chỉ đi HTTP qua VPN, file do
  gateway chuyển vào TFTP nội bộ.
- Dùng DNS nội bộ (`chat.congty.local`) thay vì IP.
- Bộ Docker Compose + WireGuard mẫu: [deploy/README.md](../deploy/README.md).

Nếu đặt reverse proxy TLS (Caddy/nginx) trước gateway, khai báo IP proxy bằng
`--trusted-proxy` (lặp lại được) và bật `--secure-cookie`. Gateway chỉ đọc
`X-Forwarded-For/Proto/Host` khi IP kết nối thật thuộc `--trusted-proxy`; địa chỉ
client lấy từ phần tử không tin cậy bên phải nhất của `X-Forwarded-For`. Proxy phải
ghi đè/append header này. Không khai báo `0.0.0.0/0`.

`--host` và `--allowed-network` chấp nhận dải CGNAT `100.64.0.0/10` để dùng trực
tiếp IP Tailscale. Nếu dùng Tailscale subnet router, client vẫn giữ IP LAN thật và
không cần dải này.

## File và giới hạn

Gateway nhận HTTP multipart rồi gửi WRQ qua TFTP; tải file bằng RRQ rồi trả HTTP
attachment. Trình duyệt không trực tiếp gửi UDP. File tạm nằm trong data-dir và
được dọn sau khi kết thúc hoặc lỗi; tối đa bốn lượt truyền đồng thời. Nếu tiến
trình bị tắt cưỡng bức, quản lý có thể xóa các thư mục còn lại trong
`web-data/temporary/` sau khi dừng gateway.

Mọi thành viên đã đăng nhập dùng chung danh sách file. Chỉ phục vụ file ngay trong
root; không cho duyệt thư mục con, symlink, tên đường dẫn hoặc file `.part`.
Lõi TFTP yêu cầu tên ASCII: file upload có dấu được đổi sang tên không dấu
(ví dụ `Đề tài.txt` → `De_tai.txt`) và giao diện báo tên đã lưu. File được quản lý
chép trực tiếp vào root cần có tên ASCII để xuất hiện trong danh sách web.
Giữ quyền ghi đè/giới hạn của TFTP server; mặc định không ghi đè file trùng tên; chỉ bật `--overwrite` khi cần.
Không có chức năng xóa file trên web. File tải về luôn là attachment, không mở
HTML do người dùng gửi trong origin của ứng dụng.

Đây là HTTP LAN không mã hóa, không phải dịch vụ Internet. Chỉ dùng trên mạng
tin cậy; không port-forward router. TFTP thuần không có xác thực/mã hóa: tài khoản
web chỉ bảo vệ cổng HTTP. Giữ TFTP bind loopback để người khác không bỏ qua tài
khoản bằng TFTP trực tiếp. Không quét virus tự động; chỉ mở file từ nguồn tin cậy.

## GitHub Pages

Trang `site/` tiếp tục làm cổng vào: nhập địa chỉ LAN rồi chuyển trình duyệt tới
gateway. Đăng nhập, file và chat chạy trên máy chủ LAN, không lưu trên GitHub.
Pages không chạy Python/SQLite và không thể thay gateway. Workflow Pages chỉ
triển khai `site/`; không đưa data-dir hoặc file người dùng vào thư mục này.

## Chạy lâu dài và kiểm thử

### Khi web báo không kết nối được TFTP

Trang web có thể đăng nhập/chat và liệt kê file ngay cả khi TFTP chưa chạy:
các chức năng này dùng SQLite/thư mục trên máy, còn upload/download cần TFTP thực sự.

- Mở TFTP Console, bấm **Khởi động server** và kiểm tra trạng thái đang chạy.
- Gateway mặc định kết nối `127.0.0.1:6969`. Server cần nhận được ở địa chỉ này;
  nếu bind riêng một IP LAN hoặc đổi cổng, sửa `--tftp-server IP:port` tương ứng.
- Dùng cùng thư mục `--root` cho gateway và server, giữ cả hai tiến trình đang chạy.
- Khi gateway/TFTP khác máy, kiểm tra firewall UDP ở máy TFTP và quyền ghi thư mục.

Windows có thể báo `WinError 10054` khi không có dịch vụ ở cổng UDP đích. Web
hiển thị HTTP 504 kèm địa chỉ/cổng và hướng dẫn khởi động, thay cho lỗi gửi file chung.

### Health check

`GET /healthz` kiểm tra cả SQLite (một truy vấn `SELECT 1`) lẫn TFTP (gửi RRQ
một tên chắc chắn không tồn tại, coi ERROR trả về là server còn sống). Kết quả
`200` khi cả hai OK, `503` kèm `{"status":"degraded","checks":{...}}` khi một
thành phần lỗi. Kết quả được cache 5 giây để monitoring không tạo phiên TFTP
liên tục; đổi bằng `HEALTH_CACHE_SECONDS` khi dùng `create_app`.

### Dịch vụ và kiểm thử

CLI dùng Waitress, không dùng Flask development server. Có thể cấu hình dịch vụ
Windows với working directory là repository, tài khoản không đặc quyền, log và
restart; chạy TFTP riêng và bật gateway sau khi TFTP sẵn sàng.

```powershell
python -m unittest tests.test_web_upload -v
python -m unittest discover -v
ruff check .
```

CI cài extras `[web]` để chạy test web. Nếu không cài Flask, suite web được bỏ qua
để các kiểm thử lõi TFTP vẫn dùng được trong môi trường không có dependency web.
