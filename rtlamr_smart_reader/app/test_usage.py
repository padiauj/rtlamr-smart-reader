import sqlite3
import unittest
from types import SimpleNamespace
from usage import period_usage


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE samples (id INTEGER PRIMARY KEY, meter_id, ts, reading, source_packet_ts, stale)")
        self.meter = SimpleNamespace(meter_id=1, value_round=3)

    def add(self, ts, reading, packet=None, stale=0):
        self.db.execute("INSERT INTO samples VALUES (NULL, 1, ?, ?, ?, ?)",
                        (ts, reading, ts if packet is None else packet, stale))

    def usage(self, start=0, end=3600):
        return period_usage(self.db, self.meter, start, end, (end-start)/3600)

    def test_healthy_and_true_zero(self):
        for ts in range(0, 3601, 60):
            self.add(ts, 100 + ts/3600)
        self.assertEqual(self.usage(), 1)
        self.db.execute("UPDATE samples SET reading=100")
        self.assertEqual(self.usage(), 0)

    def test_stale_day_is_unknown(self):
        for ts in range(0, 3601, 60):
            self.add(ts, 100, packet=-10000, stale=1)
        self.assertIsNone(self.usage())

    def test_recovery_jump_is_not_usage(self):
        for ts in range(0, 1800, 60):
            self.add(ts, 100, packet=-10000, stale=1)
        for ts in range(1800, 7201, 60):
            self.add(ts, 300 + (ts-1800)/3600)
        self.assertIsNone(self.usage())
        self.assertEqual(self.usage(3600, 7200), 1)

    def test_missing_rows_and_missing_boundary(self):
        self.add(0, 100)
        self.add(3600, 300)
        self.assertIsNone(self.usage())
        self.assertIsNone(self.usage(1800, 3600))

    def test_hidden_gap_in_source_packets(self):
        for ts in range(0, 3601, 60):
            self.add(ts, 100 + ts/3600, packet=ts if ts > 600 else 0)
        self.assertIsNone(self.usage())


if __name__ == "__main__":
    unittest.main()
