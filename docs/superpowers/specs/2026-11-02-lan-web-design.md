# Thiết kế web LAN: tài khoản, tải file và chat

Ngày: 2026-11-02. Phạm vi người dùng đã chọn: chạy trong LAN.

## Hiện trạng và lựa chọn

Repository có lõi TFTP Python và trang tĩnh `site/` dẫn tới địa chỉ LAN. Tài liệu
`docs/web-upload.md` nhắc tới gateway nhưng code gateway và extras `[web]` chưa tồn tại.

Chọn backend Python cùng máy với TFTP server, SQLite lưu dữ liệu và giao diện
HTML/CSS/JavaScript tiếng Việt. Cách này tận dụng lõi hiện có và không cần dịch vụ
bên ngoài. Hai phương án khác: tự viết HTTP bằng thư viện chuẩn (giảm dependency
nhưng tăng phần xử lý HTTP cần bảo trì), hoặc dùng dịch vụ cloud (cần hạ tầng và
kết nối Internet, không phù hợp phạm vi LAN đã chọn).

## Thành phần và vận hành

- Gateway dùng Flask và Waitress, được cài qua extras `[web]` và chạy bằng
  `python -m src.web_upload`. Dependency web không ảnh hưởng CLI/GUI hiện tại.
- Backend, template và static assets tách khỏi giao thức TFTP và trang Pages.
- Mặc định bind `127.0.0.1:8080`; hỗ trợ IP LAN cụ thể và allowlist subnet.
  Người quản lý chạy TFTP server riêng, chỉ định địa chỉ TFTP và thư mục root
  mà cả hai tiến trình dùng chung. Từ chối cấu hình root chứa dữ liệu SQLite.
- Trang GitHub Pages tiếp tục mở địa chỉ LAN, cập nhật nội dung thành cổng file/chat.
- Cơ sở dữ liệu và khóa phiên nằm trong thư mục dữ liệu riêng, bị Git bỏ qua.
  Khởi động lại giữ tài khoản và lịch sử chat.

## Tài khoản và quyền truy cập

- Đăng ký bằng tên tài khoản duy nhất và mật khẩu, không cần email.
- Tên tài khoản 3–32 ký tự chữ ASCII, số hoặc dấu gạch dưới; không phân biệt hoa
  thường khi kiểm tra trùng. Mật khẩu 8–128 ký tự; chỉ lưu hash có salt.
- Đăng ký thành công tự đăng nhập. Có đăng nhập, đăng xuất và trạng thái phiên.
- Phiên lưu phía server trong SQLite, cookie HttpOnly, SameSite=Lax và hết hạn
  sau 12 giờ. Đăng xuất vô hiệu hóa phiên ngay tại server.
- Khách xem được trang đăng nhập/đăng ký; API liệt kê file, tải/gửi file và chat
  đều yêu cầu phiên hợp lệ. Không có phân quyền quản trị hoặc phòng chat riêng.
- Kiểm tra CSRF cho thao tác thay đổi dữ liệu; giới hạn đăng nhập và gửi tin
  để giảm dò mật khẩu/spam. HTTP LAN không mã hóa, ghi rõ giới hạn trong hướng dẫn.

## File

- Sau đăng nhập, hiển thị file thường ngay trong root TFTP: tên, dung lượng,
  ngày sửa đổi và nút tải. Không duyệt thư mục con hoặc symlink.
- Gateway lấy danh sách từ root dùng chung vì TFTP không có lệnh liệt kê file.
- Tải qua TFTP RRQ vào file tạm, sau đó trả HTTP attachment cho trình duyệt;
  dọn file tạm sau khi trả phản hồi hoặc khi lỗi. File rỗng vẫn tải được.
- Hoàn thiện luồng gửi file được tài liệu hiện có mô tả: HTTP upload tới file
  tạm rồi TFTP WRQ, tối đa 100 MiB mặc định. Giữ chính sách ghi đè của TFTP server.
- Tên file được kiểm tra chặt; chặn đường dẫn tuyệt đối, traversal và symlink.
  Giới hạn kích thước cả hai chiều; tải vượt giới hạn bị hủy và dọn file tạm.
- Hiện lỗi dễ hiểu khi mất kết nối TFTP, file không tồn tại, bị từ chối hoặc quá lớn;
  không trả traceback/đường dẫn nội bộ ra giao diện.

## Chat và giao diện

- Một phòng chat chung cho mọi tài khoản đã đăng nhập, hiển thị người gửi và thời gian.
- SQLite lưu nội dung, người gửi và thời gian UTC. Trình duyệt hiển thị giờ địa phương.
- Mỗi tin 1–2.000 ký tự sau khi bỏ khoảng trắng đầu/cuối. Render bằng text,
  không thực thi HTML do người dùng gửi.
- Lấy 50 tin gần nhất khi mở, polling theo ID mỗi 2 giây, mỗi lượt tối đa 100 tin.
  Chỉ tiếp tục polling khi đang đăng nhập; tránh tin trùng và giữ nội dung đang gõ
  khi gửi thất bại. Có trạng thái lỗi/kết nối lại.
- Giao diện gồm khu đăng nhập/đăng ký và màn hình chính: danh sách/gửi file,
  phòng chat, tên tài khoản và đăng xuất. Dùng được trên điện thoại và bàn phím,
  có nhãn input, trạng thái tải, danh sách trống và thông báo lỗi tiếng Việt.

## Xác minh và bàn giao

- Test đăng ký trùng, hash mật khẩu, đăng nhập sai/đúng, hết phiên, đăng xuất,
  truy cập không có phiên, CSRF và giới hạn request.
- Test danh sách file, traversal/symlink, tải/gửi file qua TFTP thật trên loopback,
  file rỗng, lỗi TFTP, giới hạn dung lượng và dọn file tạm.
- Test chat yêu cầu đăng nhập, validation, thứ tự/cursor, giới hạn số tin và
  dữ liệu tồn tại sau khi tạo lại app.
- Chạy suite hiện có, test web, lint và kiểm tra luồng hai tài khoản trên trình duyệt
  nếu môi trường có trình duyệt khả dụng.
- Cập nhật README, hướng dẫn LAN, lệnh cài/chạy và CI cho dependency web.
  Bàn giao code cục bộ; không tự push hoặc triển khai lên GitHub.

## Trạng thái quy trình

- [x] Khảo sát repository và commit gần nhất.
- [x] Chốt phạm vi LAN với người dùng.
- [x] So sánh phương án, viết và tự rà soát thiết kế.
- [ ] Người dùng duyệt bản thiết kế bằng văn bản.
- [ ] Lập kế hoạch, triển khai và kiểm thử.
