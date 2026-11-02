# Cổng file và chat TFTP trong LAN

Cổng web cung cấp đăng ký/đăng nhập/đăng xuất, danh sách file, gửi/tải file và
phòng chat chung. Mọi thao tác file/chat yêu cầu đăng nhập. Đăng ký thành công
tự đăng nhập. Máy chủ giữ tài khoản và tin nhắn qua các lần khởi động.

## Cài đặt và chạy

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

`--host` phải là IP private/loopback cụ thể, không dùng `0.0.0.0` hoặc địa chỉ public.
Bind LAN bắt buộc có `--allowed-network`; có thể lặp lại cờ cho nhiều subnet.
ACL dùng địa chỉ kết nối thực, bỏ qua `X-Forwarded-For`. Không cấu hình reverse
proxy làm peer chung nếu muốn giữ ACL cho từng client.

## Cấu hình

| Tham số | Mặc định | Công dụng |
|---|---|---|
| `--host` | `127.0.0.1` | IP web cụ thể |
| `--port` | `8080` | TCP port web |
| `--allowed-network` | loopback khi bind loopback | Subnet được phép truy cập |
| `--root` | `files` | Cùng thư mục root với TFTP server |
| `--tftp-server` | `127.0.0.1:6969` | IPv4, hostname hoặc `[IPv6]:port` TFTP |
| `--data-dir` | `web-data` | Dữ liệu tài khoản/phiên/chat, nằm ngoài root TFTP |
| `--max-file-size` | `100` | MiB tối đa mỗi file, cả tải và gửi |
| `--secure-cookie` | tắt | Chỉ dùng nếu phục vụ web qua HTTPS |
| `--max-concurrent-uploads` | `4` | Tổng lượt tải/gửi đồng thời |
| `--threads` | `8` | Số worker Waitress |

Chạy hai tiến trình cùng máy với cùng root. TFTP không hỗ trợ liệt kê file, nên
gateway đọc danh sách từ thư mục root. Nếu đổi root ở một bên phải đổi bên còn lại.
TFTP client hỗ trợ IPv4/IPv6. Với mạng IPv6 private, có thể bind web vào IP ULA,
cho phép subnet tương ứng và dùng `--tftp-server "[::1]:6969"` khi TFTP cũng
đang lắng nghe trên IPv6.

## Tài khoản và chat

- Tên tài khoản 3–32 ký tự: chữ ASCII, số, dấu `_`; tên trùng không phân biệt hoa thường.
- Mật khẩu 8–128 ký tự, lưu hash có salt. Phiên 12 giờ, cookie HttpOnly và
  SameSite=Lax; đăng xuất hủy phiên phía server. Có CSRF và giới hạn đăng nhập/đăng ký.
- Một phòng chat chung, không có tin nhắn riêng hoặc vai trò quản trị. Tin 1–2.000
  ký tự, tối đa 30 tin/phút mỗi tài khoản. Nội dung hiển thị như văn bản thuần.
- Mở phòng tải 50 tin mới nhất; cập nhật mỗi 2 giây. Thời gian theo thiết bị người xem.
- Sao lưu `web-data/portal.sqlite3` bằng SQLite backup khi đang chạy hoặc sao chép
  cả thư mục dữ liệu sau khi dừng gateway. Giữ thư mục này riêng tư và không commit.
- Không có chức năng khôi phục mật khẩu, duyệt tài khoản hay xóa tin trong phiên bản này.

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
