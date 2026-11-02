# Kế hoạch triển khai web LAN

Thiết kế được người dùng duyệt ngày 2026-11-02.

1. Thêm extras web và package data, lớp SQLite cho tài khoản, phiên, rate limit,
   lịch sử chat; dữ liệu chạy được bỏ qua bởi Git.
2. Thêm Flask app/Waitress CLI: ACL LAN, CSRF, auth, file listing, TFTP RRQ/WRQ
   với giới hạn kích thước/đồng thời và dọn file tạm, chat có cursor.
3. Thêm giao diện tiếng Việt responsive: auth, quản lý file, chat và polling.
4. Viết test HTTP/auth/chat, test tích hợp gateway ↔ TFTP, chạy regression/lint;
   kiểm tra bằng trình duyệt nếu khả dụng.
5. Cập nhật README, hướng dẫn LAN, CI và bàn giao lệnh chạy.
