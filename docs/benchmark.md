# Benchmark tương đối: blksize và RFC 7440 windowsize

> Số đo trên **loopback của một máy Windows** (Python 3.14), server + client cùng
> process. Mục đích là **so sánh tương đối** giữa cấu hình, không phải cam kết
> hiệu năng tuyệt đối: loopback bị giới hạn bởi GIL/CPU nên không đại diện cho
> LAN có RTT thật, nơi windowsize phát huy tác dụng rõ hơn.

## 1. Cách chạy

```bash
python tools/benchmark.py                       # RRQ, 8 MiB, 3 lần mỗi cấu hình
python tools/benchmark.py --upload              # đo chiều WRQ
python tools/benchmark.py --size 32 --repeat 5 --json bench.json
```

Mỗi lượt khởi động server mới, xoá file đích, truyền file sinh sẵn và đo bằng
`perf_counter()`. Báo cáo lấy `best` và `median` để giảm nhiễu.

## 2. Kết quả (RRQ, 8 MiB, repeat = 3)

| blksize | windowsize | best (MiB/s) | median (MiB/s) | so với cùng blksize ws=1 |
|---:|---:|---:|---:|---:|
| 512 | 1 | 4.51 | 3.54 | — |
| 512 | 4 | 3.23 | 3.20 | −10 % |
| 512 | 16 | 3.10 | 3.07 | −13 % |
| 1468 | 1 | 8.17 | 7.79 | — |
| 1468 | 4 | 9.22 | 9.01 | **+16 %** |
| 1468 | 16 | 9.28 | 9.17 | **+18 %** |
| 8192 | 1 | 41.16 | 40.46 | — |
| 8192 | 4 | 46.76 | 46.58 | **+15 %** |

## 3. Đọc kết quả

- **blksize là đòn bẩy lớn nhất trên loopback**: 512 → 8192 tăng ~11× vì giảm
  số lần syscall/ACK và số block phải xử lý.
- **windowsize có lợi ở blksize 1468/8192** (+15–18 %): ít lần dừng chờ ACK hơn.
- **windowsize không có lợi ở blksize 512 trên loopback**: mỗi ACK chỉ 512 byte
  nên chi phí quản lý cửa sổ (deque, quét ACK, nhiều gói nhỏ) xấp xỉ lợi ích;
  CPU là nút cổ chai, không phải RTT. Trên link thật có RTT ≥ vài ms, cửa sổ
  giúp nhiều hơn hẳn vì mỗi round-trip chở được N block thay vì 1.

## 4. Khuyến nghị cấu hình

| Tình huống | blksize | windowsize |
|---|---:|---:|
| LAN nhanh, mất gói thấp | 1468 | 4–8 |
| LAN qua Wi‑Fi/PLC, RTT 5–30 ms | 1468 | 8–16 |
| Mạng có mất gói > 1 % | 1024–1468 | 2–4 (go-back-N phát lại cả cửa sổ) |
| Tương thích client cũ | 512 | 1 |
| Firmware/bootloader cổ điển | 512 | 1 (không option) |

`windowsize` chỉ được dùng khi **hai bên cùng thương lượng** qua OACK; client
không hỗ trợ option sẽ tự rơi về 1. Server không bao giờ trả lớn hơn giá trị
client xin và giới hạn trần 64 block.
