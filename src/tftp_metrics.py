"""Số liệu vận hành (metrics) cho TFTP.

Bù cho phần còn thiếu của project: ngoài log dạng chữ, cần câu trả lời được các
câu hỏi vận hành: tốc độ bao nhiêu, bao nhiêu lần timeout, tỉ lệ lỗi ra sao,
độ trễ mỗi block thế nào, server đang phục vụ bao nhiêu phiên.

Ba loại số đo:

| Loại | Dùng cho | Ví dụ |
|---|---|---|
| counter | đếm sự kiện lũy kế | ``packets.retransmitted``, ``sessions.timeout`` |
| gauge | giá trị hiện tại (tăng/giảm được) | ``sessions.active`` |
| timer | bộ mẫu độ trễ (giây) | ``session.duration``, ``transfer.duration`` |

Ràng buộc thiết kế:

- Chỉ dùng thư viện chuẩn: server mở một thread cho mỗi phiên nên mọi thao tác
  phải an toàn khi gọi đồng thời (dùng một khoá trong toàn bộ registry).
- Mẫu độ trễ bị thưa dần khi vượt ``MAX_SAMPLES`` để soak test chạy hàng giờ
  không phình RAM vô hạn, nhưng vẫn giữ được p50/p95/p99 có ý nghĩa.
- Đơn vị độ trễ xuất ra luôn là **mili giây** (đọc log mắt thường hơn giây với
  truyền nội bộ), nhưng nhận vào luôn là **giây** cho khớp `time.monotonic()`.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

MAX_SAMPLES = 4096
"""Trần số mẫu giữ lại cho mỗi timer trước khi thưa dần."""

PERCENTILES = (50, 95, 99)


class Timer:
    """Bộ số liệu độ trễ: số mẫu, tổng, min/max và phân vị."""

    __slots__ = ("count", "total", "minimum", "maximum", "_samples")

    def __init__(self) -> None:
        self.count = 0
        self.total = 0.0
        self.minimum = math.inf
        self.maximum = 0.0
        self._samples: list[float] = []

    def observe(self, seconds: float) -> None:
        value = float(seconds)
        if value < 0.0 or not math.isfinite(value):
            return
        self.count += 1
        self.total += value
        if value < self.minimum:
            self.minimum = value
        if value > self.maximum:
            self.maximum = value
        samples = self._samples
        samples.append(value)
        # Thưa dần khi đạt 2x trần: giữ ~MAX_SAMPLES mẫu đều nhất, bỏ đi một nửa.
        if len(samples) >= MAX_SAMPLES * 2:
            self._samples = samples[::2]

    def percentile(self, percent: float) -> float:
        """Phân vị theo phương pháp nearest-rank (0 nếu chưa có mẫu nào)."""
        if not self._samples:
            return 0.0
        ordered = sorted(self._samples)
        rank = math.ceil(percent / 100.0 * len(ordered))
        index = min(max(rank - 1, 0), len(ordered) - 1)
        return ordered[index]

    def snapshot(self) -> dict[str, Any]:
        average = self.total / self.count if self.count else 0.0
        result: dict[str, Any] = {
            "unit": "ms",
            "count": self.count,
            "avg": _ms(average),
            "min": _ms(self.minimum if self.count else 0.0),
            "max": _ms(self.maximum),
            "total_s": round(self.total, 6),
        }
        for percent in PERCENTILES:
            result[f"p{percent}"] = _ms(self.percentile(percent))
        return result

    def reset(self) -> None:
        self.count = 0
        self.total = 0.0
        self.minimum = math.inf
        self.maximum = 0.0
        self._samples = []


def _ms(seconds: float) -> float:
    return round(seconds * 1000.0, 3)


class Span:
    """Trạng thái của một khoảng thời gian đang đo (trả về cho context manager)."""

    __slots__ = ("ok", "tags")

    def __init__(self) -> None:
        self.ok = True
        self.tags: dict[str, str] = {}


class Metrics:
    """Registry số liệu dùng chung cho server hoặc client."""

    def __init__(self, name: str = "tftp") -> None:
        self.name = name
        self._lock = threading.Lock()
        self._counters: dict[str, float] = {}
        self._gauges: dict[str, float] = {}
        self._timers: dict[str, Timer] = {}
        self._started_at = time.monotonic()

    # ------------------------------------------------------------------ ghi ---
    def incr(self, name: str, value: float = 1.0) -> float:
        """Tăng counter `name`, trả về giá trị mới."""
        with self._lock:
            total = self._counters.get(name, 0.0) + float(value)
            self._counters[name] = total
            return total

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = float(value)

    def add_gauge(self, name: str, delta: float) -> float:
        """Cộng dồn vào gauge (dùng cho số phiên đang chạy)."""
        with self._lock:
            total = self._gauges.get(name, 0.0) + float(delta)
            self._gauges[name] = total
            return total

    def observe(self, name: str, seconds: float) -> None:
        """Ghi một mẫu độ trễ (giây) vào timer `name`."""
        with self._lock:
            timer = self._timers.get(name)
            if timer is None:
                timer = self._timers[name] = Timer()
            timer.observe(seconds)

    # ------------------------------------------------------------------ đọc ---
    def counter(self, name: str) -> float:
        with self._lock:
            return self._counters.get(name, 0.0)

    def gauge(self, name: str) -> float:
        with self._lock:
            return self._gauges.get(name, 0.0)

    def uptime(self) -> float:
        return time.monotonic() - self._started_at

    def counter_sum(self, prefix: str) -> float:
        """Tổng mọi counter bắt đầu bằng `prefix` (vd ``sessions.``)."""
        with self._lock:
            return sum(v for k, v in self._counters.items() if k.startswith(prefix))

    # -------------------------------------------------------------- đo đạc ---
    @contextmanager
    def span(self, timer: str, *, fail_counter: str | None = None) -> Iterator[Span]:
        """Đo thời gian một khối công việc vào `timer`.

        `fail_counter` tạo ra hai số đếm `<fail_counter>.ok` và
        `<fail_counter>.fail` nên tỉ lệ lỗi tính được ngay.
        """
        state = Span()
        started = time.monotonic()
        try:
            yield state
        except BaseException:
            state.ok = False
            raise
        finally:
            self.observe(timer, time.monotonic() - started)
            if fail_counter:
                self.incr(f"{fail_counter}.ok" if state.ok else f"{fail_counter}.fail")

    # ------------------------------------------------------------- xuất ra ---
    def snapshot(self) -> dict[str, Any]:
        """Ảnh chụp toàn bộ số đo (counters/gauges/timers) dưới dạng dict."""
        with self._lock:
            counters = {k: _number(v) for k, v in sorted(self._counters.items())}
            gauges = {k: _number(v) for k, v in sorted(self._gauges.items())}
            timers = {k: v.snapshot() for k, v in sorted(self._timers.items())}
        return {
            "name": self.name,
            "uptime_s": round(self.uptime(), 3),
            "counters": counters,
            "gauges": gauges,
            "timers": timers,
        }

    def render(self) -> str:
        """Tóm tắt nhiều dòng để ghi thẳng vào log."""
        snap = self.snapshot()
        lines = [f"{self.name} | chạy {snap['uptime_s']:.1f}s"]
        for section, title in (("counters", "bộ đếm"), ("gauges", "đo hiện tại")):
            data = snap[section]
            if not data:
                continue
            body = " ".join(f"{k}={_fmt(v)}" for k, v in data.items())
            lines.append(f"  {title}: {body}")
        timers = snap["timers"]
        if timers:
            lines.append("  độ trễ (ms):")
            for key, value in timers.items():
                parts = [f"p{p}={_fmt(value[f'p{p}'])}" for p in PERCENTILES]
                lines.append(
                    f"    {key}: n={value['count']} "
                    f"avg={_fmt(value['avg'])} {' '.join(parts)} max={_fmt(value['max'])}"
                )
        return "\n".join(lines)

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.snapshot(), indent=indent, ensure_ascii=False)

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._timers.clear()
            self._started_at = time.monotonic()

    def __repr__(self) -> str:
        return f"Metrics({self.name!r}, counters={len(self._counters)}, timers={len(self._timers)})"


def _number(value: float) -> float | int:
    return int(value) if float(value).is_integer() else round(float(value), 6)


def _fmt(value: float) -> str:
    if isinstance(value, (int,)) or float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}"
