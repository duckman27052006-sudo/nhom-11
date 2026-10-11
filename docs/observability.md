# Số liệu vận hành (observability)

> Bù cho phần còn thiếu của project: log dạng chữ trả lời "chuyện gì xảy ra",
> còn metrics trả lời "hệ thống đang thế nào".

## 1. Dùng thế nào

### Server

```bash
# In bảng số liệu mỗi 60 giây
python -m src.tftp_server --stats-interval 60

# Ghi số liệu ra JSON khi dừng (Ctrl+C hoặc SIGTERM)
python -m src.tftp_server --stats-json metrics.json
```

Mẫu log:

```
14:20:00 INFO    thống kê:
tftp.server | chạy 3600.4s
  bộ đếm: bytes.received=41943040 bytes.sent=41943384 packets.received=1024
          packets.retransmitted=12 packets.sent=1036 payload.sent=41943040
          sessions.finished.ok=512 sessions.started.RRQ=512 timeouts=12
  đo hiện tại: sessions.active=3 writes.active=1
  độ trễ (ms):
    exchange.rtt: n=1024 avg=0.31 p50=0.24 p95=0.87 p99=1.42 max=3.10
    session.duration: n=512 avg=180.5 p50=150.0 p95=402.0 p99=610.0 max=880.0
    session.ttfb: n=512 avg=1.10 p50=0.90 p95=2.30 p99=5.40 max=12.0
```

### Client

```bash
python -m src.tftp_client --get big.bin --save big.bin --stats
python -m src.tftp_client --get big.bin --save big.bin --stats-json client.json
```

### Thư viện

```python
server = TftpServer(root="files", stats_interval=60)
print(server.metrics.render())          # chuỗi cho log
data = server.metrics.snapshot()        # dict để đẩy vào hệ thống metrics khác
```

`TftpServer` và `TftpClient` đều tự tạo một `Metrics` riêng nếu không truyền
vào; muốn gộp chung thì:

```python
from src.tftp_metrics import Metrics
from src.tftp_client import TftpClient
from src.tftp_server import TftpServer

shared = Metrics("tftp")                     # server + client dùng chung
TftpServer(root="files", metrics=shared)
TftpClient("127.0.0.1", 6969, metrics=shared)
```

## 2. Ba loại số đo

| Loại | Ngữ nghĩa | Ví dụ |
|---|---|---|
| `counter` | đếm sự kiện lũy kế, chỉ tăng | `packets.sent`, `timeouts` |
| `gauge` | giá trị hiện tại, tăng lẫn giảm | `sessions.active`, `writes.active` |
| `timer` | bộ mẫu thời gian (giây), xuất ra mili giây | `exchange.rtt`, `session.duration` |

Mỗi `timer` cho ra: `count`, `avg`, `min`, `max`, `p50`, `p95`, `p99`, `total_s`.

Mẫu được giữ tối đa `MAX_SAMPLES` (4096) rồi thưa dần một nửa, nên soak test chạy
hàng giờ không phình RAM mà p50/p95/p99 vẫn có ý nghĩa.

Mọi thao tác đều an toàn khi gọi từ nhiều thread (server mở 1 thread/phiên).

## 3. Danh mục chỉ số

### 3.1 Server

| Chỉ số | Loại | Ý nghĩa |
|---|---|---|
| `datagrams.received` | counter | datagram tới well-known port |
| `packets.received` / `packets.sent` | counter | gói tin hợp lệ nhận/gửi qua TID |
| `packets.retransmitted` | counter | **gửi lại** do timeout hoặc gặp gói cũ |
| `packets.stale` | counter | gói sai block (người nhận đã đi tiếp) |
| `packets.malformed` | counter | gói không giải mã được (đã trả ERROR 4) |
| `packets.unexpected` | counter | opcode hợp lệ nhưng không đúng lúc nhận |
| `packets.unknown_tid` | counter | DATA/ACK/OACK tới well-known port (ERROR 5) |
| `packets.foreign_tid` | counter | gói từ địa chỉ khác client của phiên |
| `packets.oversized_data` | counter | DATA vượt blksize đã thương lượng (ERROR 4) |
| `bytes.sent` / `bytes.received` | counter | byte trên dây (kể cả header) |
| `payload.sent` / `payload.received` | counter | **byte dữ liệu thật** (không tính header) |
| `sessions.started.RRQ` / `.WRQ` | counter | phiên mới theo hướng |
| `sessions.finished.ok` | counter | phiên hoàn tất |
| `sessions.finished.timeout` | counter | phiên hết số lần thử lại |
| `sessions.finished.error` / `.remote_error` / `.os_error` | counter | phiên kết thúc do lỗi khác |
| `sessions.active` | gauge | phiên đang chạy |
| `writes.active` | gauge | file đang được ghi |
| `requests.rate_limited` | counter | vượt `--max-requests-per-second` |
| `requests.overloaded` | counter | vượt `--max-clients` |
| `requests.replayed` | counter | client lặp lại RRQ/WRQ, server phát lại gói đang chờ |
| `requests.port_reuse` | counter | cùng (ip, port) nhưng khác file → mở phiên mới |
| `requests.bad_filename` | counter | tên file bị `resolve_path` từ chối |
| `requests.locked` / `requests.refused_overwrite` / `requests.oversized` | counter | WRQ bị từ chối |
| `errors.sent.<mã>` | counter | số ERROR theo từng mã RFC 1350 (1, 2, 3, 4, 5, 6) |
| `errors.unhandled` | counter | **lỗi lạ chưa xử lý** — luôn phải bằng 0 |
| `timeouts` | counter | số lần chờ quá hạn |
| `exchange.rtt` | timer | thời gian chờ gói khớp sau khi gửi |
| `session.duration` | timer | tổng thời gian một phiên |
| `session.ttfb` | timer | từ lúc mở phiên tới gói đầu tiên gửi đi |

### 3.2 Client

| Chỉ số | Loại | Ý nghĩa |
|---|---|---|
| `transfers.started.<hướng>` / `.completed.<hướng>` | counter | lượt truyền (`download`/`upload`) |
| `download.ok` / `download.fail` | counter | tỉ lệ thành công (từ `Metrics.span`) |
| `download.timeout` | counter | hết số lần thử lại |
| `download.remote_error.<mã>` | counter | ERROR của server theo mã RFC |
| `errors.received.<mã>` | counter | ERROR nhận được trên đường truyền |
| `transfer.duration.<hướng>` | timer | tổng thời gian một lượt truyền |
| `transfer.ttfb` | timer | thời gian chờ gói đầu tiên từ server |
| `exchange.rtt` | timer | round-trip từng gói |
| `packets.*`, `bytes.*`, `payload.*`, `timeouts` | — | giống server |

## 4. Câu hỏi vận hành và cách tính

| Muốn biết | Công thức |
|---|---|
| Tỉ lệ phiên hỏng | `sessions.finished.timeout / sessions.started.*` |
| Tỉ lệ mất gói | `packets.retransmitted / (packets.sent + packets.retransmitted)` |
| Throughput | `payload.sent / (session.duration.total_s)` |
| Tỉ lệ gói rác đến từ môi trường | `packets.malformed / datagrams.received` |
| Độ trễ tail | `exchange.rtt.p99`, `exchange.rtt.max` |

## 5. Cảnh báo nên cài

- `errors.unhandled > 0` — server gặp lỗi chưa xử lý, cần xem log.
- `sessions.finished.timeout / sessions.started.* > 0.05` — mạng chập chờn
  hoặc client treo.
- `packets.malformed / datagrams.received > 0.05` — có quét port hoặc client sai.
- `sessions.active` bằng `max_clients` thường xuyên — cần nâng `--max-clients`
  hoặc thêm server.
- `exchange.rtt.p99 > timeout / 2` — timeout đang quá chặt, dễ retry giả.
