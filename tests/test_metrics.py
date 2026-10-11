"""Unit test cho registry số liệu vận hành `src/tftp_metrics.py`."""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.tftp_metrics import MAX_SAMPLES, Metrics, Timer


class TestTimer(unittest.TestCase):
    def test_basic_statistics(self):
        timer = Timer()
        for value in (0.001, 0.002, 0.003, 0.004):
            timer.observe(value)
        self.assertEqual(timer.count, 4)
        self.assertAlmostEqual(timer.total, 0.010, places=6)
        self.assertAlmostEqual(timer.minimum, 0.001, places=6)
        self.assertAlmostEqual(timer.maximum, 0.004, places=6)
        snapshot = timer.snapshot()
        self.assertEqual(snapshot["unit"], "ms")
        self.assertEqual(snapshot["count"], 4)
        self.assertAlmostEqual(snapshot["avg"], 2.5, places=3)
        self.assertAlmostEqual(snapshot["max"], 4.0, places=3)

    def test_percentiles_use_nearest_rank(self):
        timer = Timer()
        for index in range(1, 101):
            timer.observe(index / 1000.0)
        self.assertAlmostEqual(timer.percentile(50), 0.050, places=6)
        self.assertAlmostEqual(timer.percentile(95), 0.095, places=6)
        self.assertAlmostEqual(timer.percentile(99), 0.099, places=6)
        self.assertAlmostEqual(timer.percentile(100), 0.100, places=6)

    def test_empty_timer_is_zero(self):
        timer = Timer()
        self.assertEqual(timer.percentile(95), 0.0)
        self.assertEqual(timer.snapshot()["count"], 0)
        self.assertEqual(timer.snapshot()["avg"], 0.0)

    def test_negative_and_nan_values_are_ignored(self):
        timer = Timer()
        timer.observe(-1.0)
        timer.observe(float("nan"))
        timer.observe(float("inf"))
        self.assertEqual(timer.count, 0)

    def test_samples_are_thinned_but_bounded(self):
        timer = Timer()
        for index in range(MAX_SAMPLES * 4):
            timer.observe(index / 1_000_000.0)
        self.assertEqual(timer.count, MAX_SAMPLES * 4)
        self.assertLessEqual(len(timer._samples), MAX_SAMPLES * 2)
        # Thưa dần không được làm mất giá trị lớn nhất.
        self.assertAlmostEqual(timer.percentile(100), (MAX_SAMPLES * 4 - 1) / 1_000_000.0, places=5)

    def test_reset(self):
        timer = Timer()
        timer.observe(0.5)
        timer.reset()
        self.assertEqual(timer.count, 0)
        self.assertEqual(timer.total, 0.0)
        self.assertEqual(timer.percentile(50), 0.0)


class TestCountersAndGauges(unittest.TestCase):
    def setUp(self):
        self.metrics = Metrics("t")

    def test_counter_accumulates(self):
        self.assertEqual(self.metrics.incr("a"), 1)
        self.assertEqual(self.metrics.incr("a"), 2)
        self.metrics.incr("a", 10)
        self.assertEqual(self.metrics.counter("a"), 12)
        self.assertEqual(self.metrics.counter("khong-co"), 0)

    def test_counter_sum_by_prefix(self):
        self.metrics.incr("sessions.started.RRQ", 3)
        self.metrics.incr("sessions.started.WRQ", 2)
        self.metrics.incr("packets.sent", 100)
        self.assertEqual(self.metrics.counter_sum("sessions."), 5)
        self.assertEqual(self.metrics.counter_sum("packets."), 100)

    def test_gauge_goes_up_and_down(self):
        self.metrics.add_gauge("active", 1)
        self.metrics.add_gauge("active", 1)
        self.assertEqual(self.metrics.gauge("active"), 2)
        self.metrics.add_gauge("active", -2)
        self.assertEqual(self.metrics.gauge("active"), 0)
        self.metrics.set_gauge("active", 7)
        self.assertEqual(self.metrics.gauge("active"), 7)

    def test_snapshot_shapes_numbers(self):
        self.metrics.incr("sessions.ok")
        self.metrics.add_gauge("sessions.active", 2)
        self.metrics.observe("exchange.rtt", 0.001)
        snap = self.metrics.snapshot()
        self.assertEqual(snap["name"], "t")
        self.assertEqual(snap["counters"], {"sessions.ok": 1})
        self.assertEqual(snap["gauges"], {"sessions.active": 2})
        self.assertEqual(snap["timers"]["exchange.rtt"]["p50"], 1.0)
        self.assertIsInstance(snap["uptime_s"], float)

    def test_to_json_round_trips(self):
        self.metrics.incr("sessions.ok")
        self.metrics.observe("exchange.rtt", 0.0025)
        data = json.loads(self.metrics.to_json())
        self.assertEqual(data["counters"]["sessions.ok"], 1)
        self.assertAlmostEqual(data["timers"]["exchange.rtt"]["avg"], 2.5, places=3)
        self.assertIn('"name": "t"', self.metrics.to_json(indent=2))

    def test_render_is_human_readable(self):
        self.metrics.incr("packets.sent", 12)
        self.metrics.add_gauge("sessions.active", 3)
        self.metrics.observe("exchange.rtt", 0.004)
        text = self.metrics.render()
        self.assertIn("t | chạy", text)
        self.assertIn("packets.sent=12", text)
        self.assertIn("sessions.active=3", text)
        self.assertIn("exchange.rtt", text)
        self.assertIn("p95=", text)

    def test_render_without_data(self):
        text = Metrics("rỗng").render()
        self.assertIn("rỗng | chạy", text)
        self.assertEqual(len(text.splitlines()), 1)

    def test_reset_clears_everything(self):
        self.metrics.incr("a")
        self.metrics.add_gauge("g", 1)
        self.metrics.observe("t", 1.0)
        self.metrics.reset()
        snap = self.metrics.snapshot()
        self.assertEqual(snap["counters"], {})
        self.assertEqual(snap["gauges"], {})
        self.assertEqual(snap["timers"], {})
        self.assertLess(snap["uptime_s"], 1.0)

    def test_repr(self):
        self.assertIn("Metrics('abc'", repr(Metrics("abc")))


class TestSpan(unittest.TestCase):
    def setUp(self):
        self.metrics = Metrics("t")

    def test_successful_span_counts_ok(self):
        with self.metrics.span("work", fail_counter="work") as span:
            span.tags["k"] = "v"
            self.assertTrue(span.ok)
        self.assertEqual(self.metrics.counter("work.ok"), 1)
        self.assertEqual(self.metrics.counter("work.fail"), 0)
        self.assertIn("work", self.metrics.snapshot()["timers"])

    def test_failing_span_counts_fail_and_reraises(self):
        with self.assertRaises(ValueError):
            with self.metrics.span("work", fail_counter="work"):
                raise ValueError("x")
        self.assertEqual(self.metrics.counter("work.ok"), 0)
        self.assertEqual(self.metrics.counter("work.fail"), 1)

    def test_span_without_fail_counter(self):
        with self.metrics.span("only.timer"):
            pass
        self.assertEqual(self.metrics.snapshot()["counters"], {})


class TestThreadSafety(unittest.TestCase):
    def test_concurrent_updates_are_not_lost(self):
        metrics = Metrics("t")
        threads_count = 8
        per_thread = 500

        def worker() -> None:
            for _ in range(per_thread):
                metrics.incr("hits")
                metrics.add_gauge("live", 1)
                metrics.observe("rtt", 0.0001)
                metrics.add_gauge("live", -1)

        threads = [threading.Thread(target=worker) for _ in range(threads_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(metrics.counter("hits"), threads_count * per_thread)
        self.assertEqual(metrics.gauge("live"), 0)
        self.assertEqual(metrics.snapshot()["timers"]["rtt"]["count"], threads_count * per_thread)

    def test_snapshot_is_safe_while_writing(self):
        metrics = Metrics("t")
        stop = threading.Event()

        def writer() -> None:
            while not stop.is_set():
                metrics.incr("x")
                metrics.observe("t", 0.001)

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        try:
            for _ in range(200):
                metrics.snapshot()
                time.sleep(0.0005)
        finally:
            stop.set()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
