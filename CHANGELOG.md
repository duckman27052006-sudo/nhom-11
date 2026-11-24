# Changelog

Định dạng theo [Keep a Changelog](https://keepachangelog.com/vi/1.1.0/),
phiên bản theo [Semantic Versioning](https://semver.org/lang/vi/).

## [1.1.0] — 2026-11-24

### Added

- **RFC 7440 Windowsize Option**: `--windowsize` cho server và client (1..64).
  Phía gửi gửi tối đa N DATA trước khi chờ ACK, retransmit go-back-N khi mất
  gói; server trả giá trị ≤ giá trị client xin. Benchmark: `docs/benchmark.md`,
  công cụ `tools/benchmark.py`, test `tests/test_windowsize.py` gồm mất gói
  cả hai chiều.
- **Prometheus exporter**: `Metrics.render_prometheus()` và `/metrics` trên
  gateway web (admin session hoặc `--metrics-token`), hướng dẫn scrape trong
  `docs/observability.md`.
- **Health split**: `/livez` (chỉ SQLite) và `/readyz` (SQLite + TFTP); `/healthz`
  giữ hành vi cũ để tương thích monitoring.
- **TLS mặc định an toàn hơn**: cookie tự động `Secure` + tiền tố `__Host-` khi
  origin là HTTPS, HSTS, `Cross-Origin-Opener-Policy`,
  `Cross-Origin-Resource-Policy`, `Permissions-Policy`; static asset có
  `Cache-Control` thay vì `no-store`.
- **CI**: matrix Linux 3.10–3.14 + Windows 3.12/3.14, job GUI offscreen, job
  coverage (`--fail-under=75`), smoke test console entry points, job interop
  `tftpd-hpa` + `atftp` (`scripts/interop.sh`), job `pip-audit`, Dependabot.
- **Đóng gói**: `LICENSE` MIT, version một nguồn (`tftp.__version__`),
  systemd unit dùng `StateDirectory`/`LogsDirectory` + `--check-config`,
  Dockerfile multi-user/non-root và compose có healthcheck/giới hạn tài nguyên.
- **Tài liệu**: `docs/benchmark.md`, `docs/architecture.md`, `CONTRIBUTING.md`,
  cập nhật interop/observability/deployment.
- GUI: lưu cấu hình bằng `QSettings`, huỷ transfer, hàng đợi transfer, tên
  accessible cho điều khiển chính.
- Web: SSE cho chat, vòng đời file (chủ sở hữu, xoá, tự đổi tên khi trùng),
  dark mode, log audit đăng nhập/tải/gửi.

### Fixed

- **Lọc log theo mức trong GUI luôn rỗng**: bộ lọc tìm chuỗi `" INFO "` trong
  text hiển thị thay vì dùng level đã có; test cũ nhồi sẵn từ khoá nên che mất
  lỗi.
- **`--allow-subdirs` đọc được file ẩn**: kiểm tra basename thay vì từng thành
  phần, nên `.git/config`/`.env`/`x.part` lọt qua.
- **Session bị giữ vô hạn bằng gói rác**: `_recv` chỉ `continue` mãi mà không
  kiểm deadline; nay có ngân sách gói không tiến triển + deadline.
- **Rate limit bị vượt qua**: ERROR cho gói malformed/opcode lạ được gửi trước
  khi kiểm rate limit → khuếch đại UDP; nay có hạn mức ERROR theo nguồn.
- **RFC 2348**: blksize ngoài 8..65464 không còn bị OACK bằng giá trị lớn hơn
  giá trị client xin.
- **Dally ngắn hơn retransmit**: cửa sổ sau ACK cuối của WRQ giờ dài hơn
  `timeout` đã thương lượng (tối đa 10 s), và TID được một thread chung giữ
  thay vì chặn thread phiên.
- **Client**: huỷ transfer ném `TftpCancelled` (không còn bị coi là timeout rồi
  thử DNS kế tiếp); progress netascii không vượt 100 %; lỗi route/family được
  thử candidate DNS kế tiếp thay vì bỏ cuộc.
- **Web**: slot transfer được nhả trước khi stream file về trình duyệt (4
  client đọc chậm không còn khoá portal); `/healthz` không còn probe TFTP
  trong lock; download bật `conditional`/range.

### Changed

- Quota root dùng cache + cập nhật khi commit/evict, chỉ đi lại cây mỗi 30 s.
- Cấp phát TID có trần (random + scan) thay vì dựng `list(range(...))` mỗi
  session.
- Log view của GUI cắt dòng cũ bằng thao tác block thay vì dựng lại document.
- Sửa test web `test_transfer_capacity_released_after_disconnect` thành
  `test_slow_downloads_do_not_hold_transfer_slots` theo hợp đồng mới.

## [1.0.0] — 2026-11-02

Bản hardened cho LAN: RFC 1350/2347/2348/2349, IPv6, quota/atomic commit, fuzz +
soak + metrics, GUI PySide6, portal LAN và Docker/WireGuard mẫu.
