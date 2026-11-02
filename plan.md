# Kế hoạch đưa TFTP Client/Server vào sử dụng thực tế và bổ sung IPv6

## 1. Mục tiêu và quyết định sử dụng

Hoàn thiện ứng dụng hiện tại thành dịch vụ TFTP có thể cài đặt, cấu hình, giám
sát, cập nhật và vận hành an toàn trong **mạng nội bộ được kiểm soát**, đồng
thời hỗ trợ IPv4 và IPv6 ở cả client lẫn server.

TFTP theo RFC 1350 không có xác thực người dùng, mã hóa hay tính năng quản lý
quyền truy cập ở tầng giao thức. Vì vậy:

- **Không công khai TFTP trực tiếp ra Internet.**
- Chỉ mở dịch vụ trên mạng/VLAN tin cậy; giới hạn nguồn truy cập bằng firewall,
VPN hoặc thiết bị mạng.
- Nếu yêu cầu bảo mật đầu-cuối, xác thực người dùng, kiểm toán danh tính hoặc
truyền qua Internet, chọn SFTP/SCP/HTTPS thay vì cố bổ sung các cơ chế riêng
không tương thích vào TFTP.
- Chốt trước mô hình triển khai: máy chủ Windows hay Linux, số client đồng thời,
ai được đọc/ghi, dung lượng tối đa, thời gian lưu file, yêu cầu IPv6 và cách
khôi phục sau sự cố.

## 2. Hiện trạng trong repository

Các mục dưới đây đã có theo mã nguồn và tài liệu hiện tại; cần giữ tương thích,
không xây dựng lại:

- Client CLI và server UDP hỗ trợ RRQ/WRQ, `octet`/`netascii`, timeout,
retransmission, TID riêng và nhiều phiên đồng thời.
- Thương lượng option `blksize`, `timeout`, `tsize`; GUI PySide6 là tùy chọn.
- Server có thư mục gốc, kiểm tra tên/path, giới hạn số client, giới hạn request,
giới hạn kích thước tùy chọn, tùy chọn không ghi đè, ghi file tạm và thay file
đích khi hoàn tất.
- Có unit/integration/fuzz/soak tests và metrics/logging cơ bản.
- Client và server hiện tạo socket IPv4 (`AF_INET`). Client chỉ phân tích cú
pháp địa chỉ IPv6 trong CLI; điều đó **chưa có nghĩa là IPv6 hoạt động**.
- Server tạo TID bằng cổng UDP tạm. Đây là hành vi quan trọng của TFTP và ảnh
hưởng đến firewall, NAT và cấu hình triển khai.

Trước khi thay đổi, chạy test hiện có và ghi lại kết quả làm baseline. Không
được xem số lượng test trong README là bằng chứng kiểm thử liên thông với một
implementation TFTP khác.

## 3. P0 — Rà soát an toàn trước khi cho người dùng khác truy cập

### 3.1 Threat model và chính sách truy cập

- [ ] Viết rõ deployment chỉ dành cho LAN/VLAN tin cậy; liệt kê các nhóm client,
  dải IPv4/IPv6 được phép, dữ liệu được phục vụ và người chịu trách nhiệm.
- [ ] Mặc định firewall chỉ nhận UDP từ các subnet/client được duyệt; không tạo
  quy tắc mở toàn bộ Internet hoặc mọi interface một cách vô điều kiện.
- [ ] Tài liệu hóa rằng TFTP không có danh tính client: log địa chỉ IP không
  chứng minh danh tính người dùng.
- [ ] Nếu mạng không tin cậy hoặc cần bảo mật danh tính/nội dung, dừng triển
  khai TFTP và chuyển sang giao thức có xác thực/mã hóa.

### 3.2 Giới hạn quyền và dữ liệu

- [ ] Chạy server bằng tài khoản dịch vụ không có quyền quản trị; chỉ cấp quyền
  đọc/ghi cần thiết trên thư mục TFTP và quyền ghi riêng cho log/config.
- [ ] Tách thư mục chỉ đọc để tải xuống khỏi thư mục nhận upload nếu quy trình
  nghiệp vụ cần phân quyền hai chiều.
- [ ] Bật chính sách không ghi đè làm mặc định an toàn; yêu cầu cấu hình rõ ràng
  nếu cần cho phép ghi đè. Kiểm tra GUI, CLI và tài liệu nhất quán.
- [ ] Đặt giới hạn dung lượng upload mặc định phù hợp, giới hạn số phiên và
  request; từ chối giá trị cấu hình không hợp lệ khi khởi động thay vì âm thầm
  chuẩn hóa cấu hình nguy hiểm.
- [ ] Thêm quota/tổng dung lượng hoặc giám sát dung lượng trống để tránh nhiều
  upload hợp lệ làm đầy ổ đĩa.
- [ ] Rà soát đường dẫn symlink/reparse point, race giữa kiểm tra đường dẫn và
  mở file, tên file đặc biệt trên Windows/Linux, quyền file, thư mục con và
  file `.part`. Thư mục upload không được phép ghi ra ngoài root kể cả qua
  symlink.
- [ ] Đảm bảo giới hạn dung lượng được kiểm tra lại trong lúc nhận dữ liệu, kể
  cả khi client khai báo `tsize` sai hoặc không khai báo.
- [ ] Kiểm tra chính sách quyền đọc: không để RRQ tải được file cấu hình, log,
  backup, file tạm hoặc dữ liệu không thuộc vùng chia sẻ.
- [ ] Xác định cách xử lý file trùng tên, upload bị ngắt, file tạm còn sót, thời
  gian lưu, sao lưu và khôi phục.

### 3.3 Độ tin cậy và xử lý lỗi

- [ ] Kiểm tra các đường lỗi đóng socket/file/thread và giải phóng lock phiên
  upload; không để lỗi ghi file hoặc shutdown để lại file đích bị cắt cụt.
- [ ] Xác minh thay file đích là atomic trên từng filesystem/OS được hỗ trợ;
  kiểm tra hành vi khi server crash hoặc máy mất điện giữa lúc upload.
- [ ] Client tải xuống vào file tạm cạnh file đích, chỉ thay file đích sau khi
  nhận đủ file; khi lỗi/timeout không để một file tải dở trông như file hoàn tất.
- [ ] Đặt timeout tổng cho một transfer và chính sách dọn session/file tạm để
  client chậm hoặc biến mất không giữ tài nguyên vô hạn.
- [ ] Bảo đảm lỗi bind/permission/cấu hình không bị báo trạng thái server đang
  chạy; lỗi khởi động phải hiện rõ trong CLI, GUI, log và mã thoát.
- [ ] Kiểm tra shutdown/restart khi có transfer đang chạy; ghi rõ transfer bị
  hủy hay được chờ hoàn tất.

### 3.4 Kiểm thử an toàn

- [ ] Thêm test cho symlink/reparse point, path traversal, upload ghi đè, hai
  upload cùng tên, disk full/permission denied, file quá giới hạn, client ngắt
  giữa chừng và dọn file `.part`.
- [ ] Kiểm thử request flood và nhiều client đồng thời ở giới hạn cấu hình; đo
  thread, socket, bộ nhớ và thời gian phục hồi.
- [ ] Tiếp tục fuzz parser và socket; mọi lỗi đầu vào phải được xử lý thành lỗi
  giao thức/log phù hợp, không crash server hay để lộ traceback cho client.
- [ ] Có kiểm tra phụ thuộc và cập nhật bản vá định kỳ; khóa phiên bản phụ thuộc
  tùy chọn GUI và công cụ triển khai theo chính sách dự án.

## 4. P1 — Hỗ trợ IPv6 end-to-end

### 4.1 Chốt hành vi giao thức và cấu hình

- [ ] Chọn và tài liệu hóa chế độ server: chỉ IPv4, chỉ IPv6 hoặc dual-stack.
  Khuyến nghị dual-stack có cấu hình rõ ràng; không phụ thuộc ngầm vào hành vi
  khác nhau của `IPV6_V6ONLY` trên từng OS.
- [ ] Quy định cú pháp CLI cho địa chỉ IPv6: `[2001:db8::10]:6969`; địa chỉ
  link-local cần hỗ trợ zone/interface ID, ví dụ `[fe80::1%12]:6969`, và phải
  giải thích cách lấy interface ID theo OS.
- [ ] Cho phép bind địa chỉ cụ thể hoặc wildcard theo từng họ địa chỉ; hiển thị
  chính xác địa chỉ và port đang bind. Không dùng `0.0.0.0` để diễn đạt IPv6.
- [ ] Dùng UDP trên IPv6; không thay đổi định dạng gói TFTP vì RFC 1350 chạy
  trên UDP và payload TFTP không phụ thuộc họ IP.

### 4.2 Client IPv6

- [ ] Chuyển tạo socket sang kết quả `getaddrinfo()`/family phù hợp thay vì
  socket cố định `AF_INET`.
- [ ] Hỗ trợ tên DNS có cả bản ghi A và AAAA, địa chỉ IPv4, IPv6 global/ULA và
  link-local có scope ID.
- [ ] Xác minh nguồn gói theo địa chỉ socket đã resolve và TID, không so sánh
  chuỗi hostname người dùng nhập với địa chỉ IP nguồn.
- [ ] Lưu và tái sử dụng đầy đủ sockaddr của TID IPv6 (bao gồm flow/scope khi
  cần); xử lý tuple địa chỉ IPv4 và IPv6 đúng kiểu, không cắt còn `(host, port)`.
- [ ] Xác định chính sách thử địa chỉ DNS khi có nhiều kết quả; timeout một
  địa chỉ không được làm mất khả năng thử địa chỉ khác trước khi transfer bắt
  đầu. Sau khi TID được chọn, không chuyển địa chỉ giữa chừng.
- [ ] Cập nhật parse/help/error message/metrics/log để phân biệt IPv4, IPv6 và
  hostname; giữ tương thích cú pháp IPv4 hiện tại.

### 4.3 Server và session IPv6

- [ ] Tạo listener theo family được chọn; với dual-stack có thể dùng socket
  riêng cho IPv4 và IPv6 để hành vi nhất quán trên Windows/Linux.
- [ ] Mỗi session phải tạo socket TID cùng family và bind trên địa chỉ local
  phù hợp với interface nhận request; reply phải đi về đúng địa chỉ client.
- [ ] Cập nhật kiểu địa chỉ client/session và khóa session để phân biệt đúng
  IPv4, IPv6, scope ID và port; không replay nhầm request khi có địa chỉ trùng
  một phần.
- [ ] Xử lý đúng client IPv4-mapped IPv6 nếu hệ điều hành trả dạng này; chuẩn
  hóa địa chỉ để rate limit, log và lookup session không tạo hai danh tính giả.
- [ ] Cập nhật server GUI, trạng thái chạy, lỗi bind, danh sách địa chỉ và
  thông tin kết nối để hiển thị IPv6 có dấu ngoặc vuông; tránh phá layout GUI.
- [ ] Bảo đảm shutdown đóng mọi listener và session của cả hai family.

### 4.4 Mạng và firewall IPv6

- [ ] Tài liệu hóa rằng chỉ mở UDP 6969 là chưa đủ: sau RRQ/WRQ, dữ liệu chạy
  trên TID/port UDP khác. Phải thiết kế rule firewall phù hợp với kiến trúc
  session, không hướng dẫn mở toàn bộ UDP một cách thiếu kiểm soát.
- [ ] Thêm cấu hình dải port TID có giới hạn cho server; bind session trong dải
  đó và trả lỗi khởi động rõ nếu cạn port hoặc xung đột.
- [ ] Chỉ cho phép dải TID cần thiết từ subnet client đã duyệt trên IPv4 và
  IPv6; ghi rõ giới hạn/rủi ro khi firewall stateful, NAT hoặc router không
  theo dõi đúng luồng TFTP.
- [ ] Hướng dẫn kiểm tra route, interface, Windows Defender Firewall/Linux
  firewall, router ACL và IPv6 privacy addresses.
- [ ] Không giả định NAT traversal; chỉ hỗ trợ các topology được xác nhận trong
  triển khai.

## 5. P1 — Tương thích và kiểm chứng chuẩn

- [ ] Đối chiếu luồng RRQ/WRQ, ACK/DATA, ERROR, TID, retransmission và kết thúc
  file với RFC 1350; option negotiation với RFC 2347, `blksize` với RFC 2348,
  `timeout`/`tsize` với RFC 2349.
- [ ] Rà soát RFC 2347: gói request có option bị giới hạn kích thước; server
  phải bỏ qua option không hỗ trợ theo chuẩn, client phải xử lý đúng OACK và
  phản hồi lỗi option negotiation.
- [ ] Xác minh cách dùng `timeout` và `retries` nhất quán giữa cấu hình nội bộ,
  option RFC (giây nguyên) và hành vi retransmission.
- [ ] Xác minh giới hạn `blksize` theo chuẩn và tránh phân mảnh IP không cần
  thiết với cấu hình mặc định trên Ethernet/IPv6.
- [ ] Tạo interoperability matrix với ít nhất một client/server độc lập phổ
  biến (ví dụ `tftp-hpa`/`tftpd-hpa`) trên IPv4 và IPv6 nếu môi trường hỗ trợ.
- [ ] Kiểm tra các chiều: project client → implementation khác, implementation
  khác → project server, upload/download, `octet`/`netascii`, option bật/tắt,
  file rỗng, file đúng bội block size, file lớn và mất gói.
- [ ] Lưu kết quả test, phiên bản implementation/hệ điều hành, lệnh chạy và
  packet capture mẫu; không tuyên bố tương thích rộng chỉ dựa trên test nội bộ.

## 6. P1 — Vận hành như một dịch vụ

### 6.1 Cài đặt và quản lý tiến trình

- [ ] Cung cấp cấu hình dịch vụ tách khỏi code: listen addresses/families,
  port, root, port range, giới hạn file/session/request, overwrite policy,
  timeout, log và metrics.
- [ ] Kiểm tra config lúc khởi động; in cấu hình hiệu lực nhưng không lộ thông
  tin nhạy cảm. Cung cấp lệnh kiểm tra cấu hình hoặc dry-run nếu phù hợp.
- [ ] Viết hướng dẫn chạy nền và tự khởi động lại có giới hạn:
  Windows Service hoặc Linux systemd; chạy foreground vẫn dùng được để debug.
- [ ] Chạy dưới tài khoản dịch vụ không đặc quyền; cấu hình working directory,
  thư mục dữ liệu, quyền truy cập, restart policy và shutdown timeout.
- [ ] Cung cấp quy trình upgrade/rollback có backup config và file nghiệp vụ;
  tránh tự cài package khi chạy `tftp.bat` trong môi trường production.
- [ ] Có kiểm tra readiness: bind thành công, thư mục root sẵn sàng và listener
  UDP hoạt động trước khi đánh dấu dịch vụ ready.

### 6.2 Logging, metrics và cảnh báo

- [ ] Log thời gian, transfer ID/session, hướng RRQ/WRQ, tên file đã chuẩn hóa,
  địa chỉ client, family, byte, thời lượng, kết quả và mã lỗi.
- [ ] Không ghi nội dung file; tránh log chuỗi điều khiển/CRLF từ tên file hoặc
  thông báo không tin cậy mà không escape.
- [ ] Thêm rotation/retention log, quyền đọc log và xử lý khi ổ log đầy.
- [ ] Xuất metrics có thể thu thập bởi hệ thống giám sát: transfer thành công/
  thất bại, timeout/retry, client bị giới hạn, byte nhận/gửi, session đang hoạt
  động, dung lượng đĩa và lỗi bind.
- [ ] Đặt cảnh báo khi tỷ lệ lỗi/retry tăng, disk gần đầy, đạt giới hạn session
  hoặc dịch vụ không phản hồi.
- [ ] Xác định yêu cầu audit và thời gian lưu log phù hợp chính sách tổ chức.

### 6.3 Quy trình dữ liệu

- [ ] Quy định ai xác nhận file upload, quét malware ở tầng ngoài nếu cần, cách
  chuyển file từ incoming sang nơi xử lý và cách xử lý file không hợp lệ.
- [ ] Thiết lập backup/restore, kiểm tra phục hồi định kỳ, retention và quy
  trình dọn file cũ/tạm.
- [ ] Nếu cần đảm bảo nội dung không bị thay đổi, thiết kế xác minh checksum
  ngoài TFTP (manifest/chữ ký hoặc kênh ứng dụng tin cậy); không coi UDP checksum
  là xác minh tính toàn vẹn nghiệp vụ.

## 7. P2 — Hoàn thiện client, GUI và trải nghiệm người dùng

- [ ] Có progress/cancel rõ ràng; hủy transfer phải đóng socket, giải phóng
  session và xử lý file tạm.
- [ ] Bảo đảm tải file không ghi đè file cục bộ ngoài ý muốn; quy định hành vi
  khi file đích đã tồn tại và khi đổi tên.
- [ ] GUI hiển thị địa chỉ LAN/IPv6 có thể chia sẻ cho client, nhưng cảnh báo
  khi bind wildcard và nhắc người dùng cấu hình firewall.
- [ ] GUI/CLI không cho rằng mọi địa chỉ `0.0.0.0` hoặc `::` là địa chỉ để client
  kết nối; hiển thị địa chỉ interface có thể dùng riêng.
- [ ] Tài liệu các mã lỗi thường gặp: sai IP/port, firewall chặn TID, sai root,
  hết quyền/đĩa, overwrite bị từ chối, IPv6 không có route.
- [ ] Giữ GUI là tùy chọn; server CLI/service không phụ thuộc PySide6.

## 8. Kế hoạch kiểm thử và tiêu chí nghiệm thu

### 8.1 Kiểm thử tự động

- [ ] Giữ toàn bộ test hiện có chạy pass trước và sau mỗi thay đổi.
- [ ] Thêm unit test parse địa chỉ, `getaddrinfo`, chuẩn hóa địa chỉ, TID IPv6,
  IPv4-mapped address, scope ID và thông báo lỗi.
- [ ] Thêm end-to-end IPv4 và IPv6: RRQ/WRQ, `octet`/`netascii`, empty/exact
  block/large file, options on/off, timeout/retransmission, nhiều client, giới
  hạn file, shutdown và cleanup.
- [ ] IPv6 tests phải skip có lý do rõ ràng khi host/CI không có IPv6 loopback;
  pipeline phát hành phải có ít nhất một môi trường IPv6 thật.
- [ ] Thêm test cấu hình dải TID, cạn port, bind IPv4-only/IPv6-only/dual-stack
  và lỗi khi port đã được dùng.
- [ ] Chạy formatter/linter/type check đã cấu hình và toàn bộ test liên quan.

### 8.2 Kiểm thử staging/thực tế

- [ ] Kiểm thử trên Windows và Linux được hỗ trợ; ghi rõ phiên bản Python/OS.
- [ ] Chạy interoperability tests với implementation độc lập, kiểm tra packet
  capture và xác nhận TID/option negotiation.
- [ ] Thử transfer trong từng topology được hỗ trợ: cùng subnet IPv4, cùng
  subnet IPv6, firewall bật, DNS A/AAAA và link-local nếu đã cam kết hỗ trợ.
- [ ] Thử mất gói, trễ, client tắt ngang, server restart, disk đầy, quyền bị thu
  hồi, log đầy và port TID không khả dụng.
- [ ] Thử soak ở tải mục tiêu và cao hơn tải dự kiến; xác nhận không rò rỉ socket,
  thread, session, file tạm hoặc tăng bộ nhớ không giới hạn.
- [ ] Thực hiện review bảo mật trước khi mở quyền truy cập cho người dùng; ghi
  nhận rủi ro chấp nhận được, người phê duyệt và ngày đánh giá lại.

### 8.3 Điều kiện được phép phát hành

Chỉ phát hành khi tất cả điều kiện sau đạt:

- Không có test regression; test IPv4 và IPv6 bắt buộc pass trên môi trường có
  hỗ trợ tương ứng.
- Interoperability matrix đạt các implementation và mode đã cam kết.
- Không thể đọc/ghi ngoài thư mục được cấp quyền trong các test path/symlink;
  giới hạn upload, overwrite, rate/session và dọn dẹp đã được xác nhận.
- Dịch vụ chạy bằng tài khoản không đặc quyền, firewall chỉ mở cho subnet được
  duyệt và dải TID được cấu hình/kiểm chứng.
- Có hướng dẫn cài đặt, cấu hình, vận hành, theo dõi, backup/restore, nâng cấp,
  rollback và xử lý sự cố.
- Chủ dịch vụ xác nhận dữ liệu có thể được truyền không mã hóa và chấp nhận
  giới hạn không xác thực của TFTP; nếu không, không phát hành TFTP cho use case
  đó.

## 9. Thứ tự thực hiện đề xuất

| Thứ tự | Giai đoạn | Kết quả cần có |
|---|---|---|
| 1 | Baseline và threat model | Test hiện tại được ghi nhận; chốt LAN/topology, OS, ACL, dữ liệu và tải mục tiêu |
| 2 | Hardening P0 | Quyền tối thiểu, chính sách overwrite/size/quota, kiểm tra path/symlink, atomic file và cleanup |
| 3 | IPv6 socket model | Chốt dual-stack/family/session/TID/zone ID; thiết kế tương thích ngược |
| 4 | IPv6 client và server | IPv4 không regression; IPv6-only và dual-stack hoạt động end-to-end |
| 5 | Firewall và port range | TID nằm trong dải cấu hình; tài liệu rule IPv4/IPv6 được kiểm thử |
| 6 | Interoperability | Giao tiếp hai chiều với implementation độc lập; lưu packet capture và kết quả |
| 7 | Service/observability | Cài đặt service, config validation, log rotation, metrics, alert, backup/rollback |
| 8 | Staging và phát hành | Soak, fault injection, security review, checklist nghiệm thu được ký duyệt |

Mỗi giai đoạn cần tạo thay đổi nhỏ, có review, test và tài liệu tương ứng; không
mở listener production trước khi hoàn thành các điều kiện P0 và điều kiện phát
hành ở mục 8.3.

## 10. Tài liệu chuẩn/tham khảo

- [RFC 1350 — The TFTP Protocol (Revision 2)](https://www.rfc-editor.org/rfc/rfc1350)
- [RFC 2347 — TFTP Option Extension](https://www.rfc-editor.org/rfc/rfc2347)
- [RFC 2348 — TFTP Blocksize Option](https://www.rfc-editor.org/rfc/rfc2348)
- [RFC 2349 — TFTP Timeout Interval and Transfer Size Options](https://www.rfc-editor.org/rfc/rfc2349)
- [Ubuntu manpage: tftpd-hpa](https://manpages.ubuntu.com/manpages/jammy/man8/in.tftpd.8.html)
- [Ubuntu manpage: tftp-hpa client](https://manpages.ubuntu.com/manpages/jammy/man1/tftp.1.html)
