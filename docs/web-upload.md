# Cổng web chuyển tiếp file qua TFTP trong mạng LAN

Cổng này giúp người dùng gửi file từ trình duyệt mà không cần cài client TFTP
hay tải repository. Trình duyệt gửi HTTP multipart tới web app; web app tạm
ghi file để dùng TFTP client tích hợp gửi WRQ tới TFTP server. File cuối cùng
được TFTP server lưu trong thư mục gốc TFTP, không phải thư mục riêng của web.
Trình duyệt không tự gửi UDP/TFTP.

## Cài đặt và khởi động trên Windows

Có thể chạy `tftp.bat` ở thư mục gốc project để tự cài extras `[gui,web]` nếu
còn thiếu, dò IPv4/subnet của card mạng có default route, rồi mở GUI TFTP và web
gateway trong hai cửa sổ riêng. URL được hiển thị ở cửa sổ khởi chạy; bấm
**Khởi động server** trong GUI để gateway chuyển file tới TFTP tại
`127.0.0.1:6969`. Nếu gateway không tự dò đúng card mạng hoặc cần subnet khác,
chạy lệnh thủ công bên dưới.

Trong thư mục project:

```powershell
python -m pip install -e ".[web]"
ipconfig
python -m src.web_upload `
  --host 192.168.1.10 `
  --port 8080 `
  --allowed-network 192.168.1.0/24 `
  --tftp-server 127.0.0.1:6969
```

Thay địa chỉ mẫu bằng IPv4 private trên máy chủ và đúng subnet LAN. Chỉ mở
**TCP 8080** trong Windows Defender Firewall cho subnet client đã duyệt; không
mở cổng cho mọi địa chỉ. Người dùng cùng LAN truy cập
`http://192.168.1.10:8080`. TFTP server cần chạy và nhận được tại địa chỉ
`--tftp-server`; mở UDP cho TFTP theo cấu hình server. Nếu gateway và TFTP server
cùng máy, địa chỉ loopback mặc định là phù hợp.

### Dùng GitHub Pages làm link vào

Trang tĩnh GitHub Pages của repository có địa chỉ
`https://duckman27052006-sudo.github.io/nhom-11/`. Trang này không nhận file:
người dùng nhập URL gateway LAN một lần, trang lưu URL trong trình duyệt rồi
chuyển tới gateway. Upload chỉ hoạt động khi thiết bị đang kết nối cùng mạng và
truy cập được gateway. File không được gửi lên GitHub.

GitHub Pages chỉ host nội dung tĩnh, không chạy gateway TFTP. Vì vậy trang Pages
mở gateway bằng điều hướng rõ ràng thay vì gửi request từ HTTPS GitHub tới
HTTP/private IP, vốn bị trình duyệt chặn. Người quản lý cung cấp địa chỉ gateway
LAN cho người dùng, ví dụ `http://192.168.1.10:8080`. Có thể cấu hình DNS nội bộ
để dùng tên dễ nhớ như `http://upload.lan:8080`.

Workflow `.github/workflows/pages.yml` triển khai thư mục `site/` khi có thay đổi
trên nhánh `main`. Bật GitHub Pages cho repository với nguồn **GitHub Actions**.
Trang Pages công khai; không đưa thông tin bí mật hoặc file người dùng lên đó.

TFTP server là nơi thực thi quyền ghi đè, giới hạn dung lượng, quota, dung lượng
đĩa và lưu file. Bật `--overwrite` trên TFTP server nếu muốn cho phép ghi đè;
mặc định file trùng tên bị từ chối. TFTP server ghi log phiên WRQ từ máy gateway.
Web log giữ địa chỉ IP trình duyệt để đối chiếu; TFTP server sẽ thấy địa chỉ
mạng của máy chạy gateway, không phải IP trình duyệt.

Mặc định app chỉ bind `127.0.0.1`, nên không truy cập được từ máy khác. Đây là
chủ ý fail-closed. `--host` bắt buộc là IP private/loopback cụ thể; wildcard
`0.0.0.0`/`::` và địa chỉ public bị từ chối. Cần ít nhất một `--allowed-network`
private/loopback; có thể truyền cờ này nhiều lần nếu cần cho các subnet đã duyệt.

IPv6 LAN cũng được hỗ trợ bằng IP private cụ thể và subnet ULA, ví dụ:

```powershell
python -m src.web_upload `
  --host fd12:3456:789a::10 `
  --port 8080 `
  --allowed-network fd12:3456:789a::/64 `
  --tftp-server "[::1]:6969"
```

Waitress chỉ bind family của địa chỉ host đã chỉ định. Muốn phục vụ cả IPv4 và
IPv6 thì chạy hai instance riêng, mỗi instance có địa chỉ và subnet allowlist
riêng, hoặc đặt reverse proxy nội bộ có cấu hình ACL đúng. Không dùng proxy
header để giả địa chỉ client: ACL dựa trên peer IP thực. TFTP server cũng phải
lắng nghe trên IPv6 để nhận kết nối tại `[::1]:6969`.

## Giới hạn và chính sách file

- Mặc định giới hạn HTTP 100 MiB mỗi file, điều chỉnh bằng `--max-file-size`
  (MiB). TFTP server cũng áp dụng các giới hạn của chính nó.
- Gateway cho phép tối đa 5 upload đang chạy cùng lúc; lượt tiếp theo nhận
  HTTP 503 và có thể thử lại sau. Đây là giới hạn lượt gửi đồng thời, không phải
  giới hạn số người xem trang hay danh tính; hệ thống không yêu cầu tài khoản.
  Có thể đổi bằng `--max-concurrent-uploads`.
- TFTP server mặc định giữ tổng file trong thư mục gốc ở mức tối đa 500 MiB,
  tính đệ quy cho cả file gửi từ app và web. Khi cần thêm chỗ, server xóa file
  thường cũ nhất theo thời gian sửa đổi; không xóa file `.part`, file đang được
  TFTP transfer sử dụng, symlink hoặc reparse point. Tắt eviction bằng `--no-evict-oldest`;
  khi đó upload sẽ bị từ chối nếu quota đầy. File đơn lớn hơn quota luôn bị từ
  chối trước khi xóa file cũ. CLI và GUI đều áp dụng mặc định này; JSON config
  có thể đặt `max_total_bytes` và `evict_oldest` (dùng `null` để bỏ quota).
- Tên file được làm sạch trước khi gửi WRQ; TFTP server xử lý xung đột tên.
- Web app giữ một bản tạm ngắn hạn trên máy gateway để TFTP client đọc khi gửi.
  Bản tạm được xóa sau khi transfer thành công hoặc thất bại; file hoàn chỉnh
  chỉ được lưu theo chính sách của TFTP server.
- Không có route để liệt kê hay tải file; chỉ người có quyền hệ thống trên thư
  mục gốc TFTP mới xem/quản lý được dữ liệu.
- Cổng upload không quét virus và không xác thực danh tính người gửi. Nếu cần
  kiểm tra malware, chuyển file vào vùng quarantine và quét trước khi sử dụng.

## Ranh giới mạng và bảo mật

Đây là HTTP không TLS dành cho LAN được kiểm soát. Allowlist private CIDR và bind
vào IP private là defense-in-depth, **không phải xác thực người dùng**. Không
port-forward từ router, không đưa ra Internet và không coi Wi-Fi công cộng,
guest network hoặc mạng dùng chung là LAN tin cậy. Dữ liệu nhạy cảm cần kênh
HTTPS có xác thực hoặc giao thức truyền file an toàn.

Gateway kiểm tra peer IP, `Origin` và token CSRF của trang, giới hạn request/file
và số upload đồng thời, thêm CSP và các security headers cơ bản. Không bật CORS
hoặc proxy trust mặc định. Đường truyền HTTP đến gateway không mã hóa; đoạn
gateway tới TFTP server cũng dùng TFTP thuần, vốn không có xác thực hay mã hóa.

## Chạy như dịch vụ

Đối với triển khai lâu dài, cài package extras `[web]`, chạy entry point
`tftp-web-upload` qua Windows Service manager (ví dụ NSSM) bằng tài khoản không
đặc quyền, cấu hình restart/log/working directory và giới hạn firewall. Không
dùng Flask development server; lệnh trên dùng Waitress. Xem thêm hướng dẫn nền
tảng trong `docs/deployment.md`.

## Kiểm thử

```powershell
python -m unittest tests.test_web_upload -v
```
