"""Usage is only attributable to a period with continuous fresh coverage."""
from typing import Any
import sqlite3


def period_usage(conn: sqlite3.Connection, meter: Any, start: float,
                 end: float, hours: float) -> float | None:
    if end <= start:
        return None
    # Match the receiver's default stale threshold. Never bridge an outage,
    # even when repeated saved samples give the appearance of continuous data.
    tolerance = 300
    rows = conn.execute(
        """SELECT ts, reading, source_packet_ts, stale FROM samples
           WHERE meter_id = ? AND ts >= ? AND ts <= ? ORDER BY ts, id""",
        (int(meter.meter_id), start - tolerance, end),
    ).fetchall()
    before = [row for row in rows if row["ts"] <= start]
    if not before:
        return None
    baseline = before[-1]
    rows = [baseline] + [row for row in rows if row["ts"] > start]
    if len(rows) < 2 or end - rows[-1]["ts"] > tolerance:
        return None
    previous = None
    total = 0.0
    for row in rows:
        packet = row["source_packet_ts"]
        if (row["stale"] or packet is None
                or row["ts"] - packet >= tolerance):
            return None
        if previous is not None:
            if (row["ts"] - previous["ts"] > tolerance
                    or packet - previous["source_packet_ts"] > tolerance):
                return None
            delta = row["reading"] - previous["reading"]
            if delta < 0 and not bool(getattr(meter, "allow_decrease", False)):
                return None
            total += delta
        previous = row
    max_rate = float(getattr(meter, "max_rate_per_hour", -1))
    if max_rate >= 0 and total > max_rate * max(hours, 1 / 60):
        return None
    return round(total, int(getattr(meter, "value_round", 3)))
