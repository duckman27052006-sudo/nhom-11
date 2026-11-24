# Đóng góp cho dự án TFTP (Nhóm 11)

Cảm ơn bạn đã quan tâm. Tài liệu này mô tả cách dựng môi trường, chạy kiểm thử
và tiêu chí cho một thay đổi được chấp nhận.

## 1. Dựng môi trường

Yêu cầu: Python 3.10+. Trên Windows dùng PowerShell.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,web,gui]"
```

Trên Linux/macOS:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,web,gui]"
```

Không cần cài gì nếu chỉ sửa lõi (`tftp/`) và chạy `python -m tftp.tftp_server`:
lõi chỉ dùng thư viện chuẩn.

## 2. Chạy kiểm thử

```bash
# nhanh, bỏ soak
TFTP_SKIP_SOAK=1 python -m unittest discover -s tests -t . -v

# đầy đủ (bao gồm soak, mất gói, block wrap)
python -m unittest discover -s tests -t . -v

# lint + type check
ruff check .
mypy tftp

# coverage (ngưỡng 75 % trong pyproject.toml)
coverage run --source=tftp -m unittest discover -s tests -t . && coverage report
```

Trên Windows:

```powershell
$env:TFTP_SKIP_SOAK = "1"
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
```

GUI test chạy headless:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe -m unittest tests.test_gui_smoke -v
```

Interop (cần Linux + `tftpd-hpa` + `atftp`):

```bash
sudo apt-get install -y tftpd-hpa atftp
bash scripts/interop.sh
```

## 3. Quy ước code

- Python 3.10+, `from __future__ import annotations`.
- **Lõi `tftp/` không import PySide6/Flask.** GUI và web là extras.
- Dòng tối đa 100 ký tự (ruff `E501`), import sắp xếp bằng ruff.
- `mypy tftp` phải sạch; không thêm `# type: ignore` nếu có thể sửa kiểu.
- Chú thích tiếng Việt, ngắn, giải thích **vì sao** (đặc biệt các quyết định
  RFC/OS) chứ không mô tả lại code.
- Mọi thay đổi hành vi giao thức phải kèm test hồi quy và cập nhật
  `docs/RFC-1350-notes.md` hoặc `docs/interop.md` nếu liên quan tương thích.

## 4. Thêm một tính năng giao thức mới

Ví dụ khi thêm option RFC:

1. Định nghĩa tên option + hằng số trong `tftp/tftp_packet.py` (thêm vào
   `OPTION_NAMES` để parser kiểm tra kiểu số).
2. Thương lượng ở `TftpServer.negotiate_options()` và `Session._negotiate()`;
   client ở `TftpClient._requested_options()` + `_apply_oack()`.
3. Không bao giờ trả giá trị lớn hơn giá trị client xin (RFC 2347/2348/7440).
4. Thêm test: negotiation, e2e cả hai chiều, biên (0, max, giá trị lạ), và một
   test mất gói nếu có retransmit.
5. Cập nhật README bảng CLI, CHANGELOG, docs liên quan; chạy `bash
   scripts/interop.sh` nếu option ảnh hưởng bên thứ ba.

## 5. Commit và pull request

- Commit nhỏ, message dạng `feat(web): ...`, `fix(server): ...`,
  `test: ...`, `docs: ...`, `chore: ...` (repo đang dùng quy ước này).
- Mỗi PR: mô tả vấn đề, cách sửa, lệnh đã chạy + kết quả.
- Không commit: `.venv/`, `web-data/`, `files/` cá nhân, log, file `.part`,
  khóa WireGuard/Caddy, tài liệu báo cáo `.docx/.pptx`.
- Không đưa bí mật vào code/log. `tests/test_privacy.py` quét lịch sử git để
  chặn một số mẫu phổ biến.

## 6. Báo lỗi bảo mật

TFTP không có xác thực/mã hóa — đó là giới hạn giao thức, không phải lỗi cài
đặt. Lỗi thuộc phạm vi dự án gồm: thoát khỏi `--root` (symlink/reparse), đọc
file ẩn, ghi đè ngoài ý muốn, từ chối dịch vụ do gói tin, lỗ hổng web
(auth/CSRF/XSS). Vui lòng **không mở issue công khai** cho lỗ hổng chưa vá;
gửi cho nhóm trước kèm bước tái hiện tối thiểu.

## 7. Checklist trước khi gửi PR

- [ ] `ruff check .` sạch.
- [ ] `mypy tftp` sạch.
- [ ] Test liên quan pass; nếu sửa lõi, chạy toàn bộ (gồm soak).
- [ ] Có test cho hành vi mới hoặc lỗi được sửa.
- [ ] Cập nhật docs/CHANGELOG nếu thay đổi người dùng nhìn thấy.
- [ ] Không thêm dependency vào lõi; extras phải tuỳ chọn.
