from __future__ import annotations

import csv
import io
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import date as dt_date
from datetime import datetime, time as dt_time, timedelta, timezone, tzinfo
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = 8099
WEB_ROOT = Path(__file__).with_name("web")
RANGE_SECONDS = {
    "24h": 86400,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
    "90d": 90 * 86400,
    "1y": 365 * 86400,
}
MAX_LINE_POINTS = 1400


def utc_iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), timezone.utc).replace(microsecond=0).isoformat()


def month_start(day: dt_date) -> dt_date:
    return dt_date(day.year, day.month, 1)


def add_months(day: dt_date, months: int) -> dt_date:
    month_index = day.year * 12 + day.month - 1 + months
    year = month_index // 12
    month = month_index % 12 + 1
    return dt_date(year, month, 1)


class DashboardData:
    def __init__(
        self,
        database_path: str,
        meters: dict[int, Any],
        state: Any,
        timezone_name: str,
    ):
        self.database_path = Path(database_path)
        self.meters = meters
        self.state = state
        self.timezone_name = timezone_name or os.environ.get("TZ") or "UTC"

    def timezone(self) -> tzinfo:
        try:
            return ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError:
            return timezone.utc

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def overview(self) -> dict[str, Any]:
        tz = self.timezone()
        now = datetime.now(timezone.utc).astimezone(tz)
        meters: list[dict[str, Any]] = []
        with self.connect() as conn:
            for meter in self.meters.values():
                meter_id = int(meter.meter_id)
                latest = conn.execute(
                    """
                    SELECT ts, ts_iso, reading, raw_reading, frames_per_minute,
                           packet_age_seconds, center_hz, stale, mode
                    FROM samples
                    WHERE meter_id = ?
                    ORDER BY ts DESC
                    LIMIT 1
                    """,
                    (meter_id,),
                ).fetchone()
                bounds = conn.execute(
                    """
                    SELECT MIN(ts) AS first_ts, MAX(ts) AS last_ts, COUNT(*) AS samples
                    FROM samples
                    WHERE meter_id = ?
                    """,
                    (meter_id,),
                ).fetchone()
                today_start = datetime.combine(now.date(), dt_time.min, tzinfo=tz)
                runtime = self.state.ensure_meter(meter_id)
                meters.append(
                    {
                        "id": meter_id,
                        "name": str(meter.name),
                        "unit": str(meter.unit_of_measurement),
                        "device_class": str(meter.device_class),
                        "reading": latest["reading"] if latest else runtime.last_reading,
                        "raw_reading": latest["raw_reading"] if latest else runtime.last_raw,
                        "latest_ts": utc_iso(latest["ts"]) if latest else runtime.last_seen_iso,
                        "latest_local": self._local_label(latest["ts"], tz) if latest else None,
                        "frames_per_minute": latest["frames_per_minute"] if latest else runtime.frames_per_minute(),
                        "packet_age_seconds": latest["packet_age_seconds"] if latest else None,
                        "center_hz": latest["center_hz"] if latest else runtime.last_center_hz,
                        "stale": bool(latest["stale"]) if latest else runtime.is_stale(300),
                        "mode": latest["mode"] if latest else None,
                        "first_ts": utc_iso(bounds["first_ts"]) if bounds and bounds["first_ts"] else None,
                        "last_ts": utc_iso(bounds["last_ts"]) if bounds and bounds["last_ts"] else None,
                        "sample_count": int(bounds["samples"] or 0) if bounds else 0,
                        "today_usage": self._period_usage(
                            conn,
                            meter,
                            today_start.timestamp(),
                            now.timestamp(),
                            max((now - today_start).total_seconds() / 3600.0, 1 / 60),
                        ),
                        "last_30d_usage": self._period_usage(
                            conn,
                            meter,
                            (now - timedelta(days=30)).timestamp(),
                            now.timestamp(),
                            30 * 24,
                        ),
                        "last_365d_usage": self._period_usage(
                            conn,
                            meter,
                            (now - timedelta(days=365)).timestamp(),
                            now.timestamp(),
                            365 * 24,
                        ),
                    }
                )
        return {
            "timezone": self.timezone_name,
            "generated_at": utc_iso(time.time()),
            "meters": meters,
        }

    def line_series(self, meter_id: int, range_name: str) -> dict[str, Any]:
        meter = self._meter_or_default(meter_id)
        now = time.time()
        with self.connect() as conn:
            first_ts = self._first_ts(conn, int(meter.meter_id))
            if first_ts is None:
                return self._empty_series(meter, range_name)
            if range_name == "today":
                local_now = datetime.now(timezone.utc).astimezone(self.timezone())
                start = datetime.combine(local_now.date(), dt_time.min, tzinfo=self.timezone()).timestamp()
            elif range_name == "all":
                start = first_ts
            else:
                start = now - RANGE_SECONDS.get(range_name, RANGE_SECONDS["30d"])
            start = max(first_ts, start)
            end = now
            bucket_seconds = max(1, int((end - start) / MAX_LINE_POINTS))
            if bucket_seconds <= 10:
                rows = conn.execute(
                    """
                    SELECT ts, reading, frames_per_minute, stale
                    FROM samples
                    WHERE meter_id = ? AND ts >= ? AND ts <= ?
                    ORDER BY ts
                    LIMIT 5000
                    """,
                    (int(meter.meter_id), start, end),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    WITH bucketed AS (
                        SELECT CAST((ts - ?) / ? AS INTEGER) AS bucket, MAX(ts) AS max_ts
                        FROM samples
                        WHERE meter_id = ? AND ts >= ? AND ts <= ?
                        GROUP BY bucket
                    )
                    SELECT s.ts, s.reading, s.frames_per_minute, s.stale
                    FROM samples s
                    JOIN bucketed b ON s.ts = b.max_ts
                    WHERE s.meter_id = ?
                    ORDER BY s.ts
                    """,
                    (start, bucket_seconds, int(meter.meter_id), start, end, int(meter.meter_id)),
                ).fetchall()
            return {
                "meter": self._meter_json(meter),
                "range": range_name,
                "start": utc_iso(start),
                "end": utc_iso(end),
                "bucket_seconds": bucket_seconds,
                "points": [
                    {
                        "t": float(row["ts"]),
                        "iso": utc_iso(row["ts"]),
                        "v": row["reading"],
                        "frames_per_minute": row["frames_per_minute"],
                        "stale": bool(row["stale"]),
                    }
                    for row in rows
                ],
            }

    def usage_bars(self, meter_id: int, period: str) -> dict[str, Any]:
        meter = self._meter_or_default(meter_id)
        tz = self.timezone()
        local_now = datetime.now(timezone.utc).astimezone(tz)
        with self.connect() as conn:
            if period == "hourly":
                day = local_now.date()
                start = datetime.combine(day, dt_time.min, tzinfo=tz)
                buckets = []
                for hour in range(24):
                    begin = start + timedelta(hours=hour)
                    end = begin + timedelta(hours=1)
                    buckets.append(
                        self._usage_bucket(
                            conn,
                            meter,
                            begin.timestamp(),
                            end.timestamp(),
                            1,
                            f"{hour:02d}:00",
                        )
                    )
                title = f"Hourly usage for {day.isoformat()}"
            elif period == "monthly":
                start_month = add_months(month_start(local_now.date()), -11)
                buckets = []
                for offset in range(12):
                    begin_day = add_months(start_month, offset)
                    end_day = add_months(begin_day, 1)
                    begin = datetime.combine(begin_day, dt_time.min, tzinfo=tz)
                    end = datetime.combine(end_day, dt_time.min, tzinfo=tz)
                    hours = (end - begin).total_seconds() / 3600.0
                    buckets.append(
                        self._usage_bucket(
                            conn,
                            meter,
                            begin.timestamp(),
                            min(end.timestamp(), local_now.timestamp()),
                            hours,
                            begin.strftime("%b %Y"),
                        )
                    )
                title = "Monthly usage, last 12 months"
            else:
                days = 31
                first_day = local_now.date() - timedelta(days=days - 1)
                buckets = []
                for offset in range(days):
                    day = first_day + timedelta(days=offset)
                    begin = datetime.combine(day, dt_time.min, tzinfo=tz)
                    end = begin + timedelta(days=1)
                    buckets.append(
                        self._usage_bucket(
                            conn,
                            meter,
                            begin.timestamp(),
                            min(end.timestamp(), local_now.timestamp()),
                            24,
                            day.strftime("%m/%d"),
                        )
                    )
                period = "daily"
                title = "Daily usage, last 31 days"
            return {
                "meter": self._meter_json(meter),
                "period": period,
                "title": title,
                "generated_at": utc_iso(time.time()),
                "bars": buckets,
            }

    def csv_export(self, meter_id: int, range_name: str) -> tuple[str, str]:
        series = self.line_series(meter_id, range_name)
        meter = series["meter"]
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["iso_time", "reading", "frames_per_minute", "stale"])
        for point in series["points"]:
            writer.writerow([
                point["iso"],
                point["v"],
                point["frames_per_minute"],
                int(point["stale"]),
            ])
        filename = f"rtlamr-{meter['id']}-{range_name}.csv"
        return filename, buffer.getvalue()

    def _usage_bucket(
        self,
        conn: sqlite3.Connection,
        meter: Any,
        start_ts: float,
        end_ts: float,
        hours: float,
        label: str,
    ) -> dict[str, Any]:
        return {
            "label": label,
            "start": utc_iso(start_ts),
            "end": utc_iso(end_ts),
            "value": self._period_usage(conn, meter, start_ts, end_ts, hours),
        }

    def _period_usage(
        self,
        conn: sqlite3.Connection,
        meter: Any,
        start_ts: float,
        end_ts: float,
        hours: float,
    ) -> float | None:
        if end_ts <= start_ts:
            return None
        start_reading = self._start_reading(conn, int(meter.meter_id), start_ts, end_ts)
        end_reading = self._end_reading(conn, int(meter.meter_id), start_ts, end_ts)
        if start_reading is None or end_reading is None:
            return None
        delta = end_reading - start_reading
        if delta < 0 and not bool(getattr(meter, "allow_decrease", False)):
            return None
        max_rate = float(getattr(meter, "max_rate_per_hour", -1.0))
        if max_rate >= 0 and delta > max_rate * max(hours, 1 / 60):
            return None
        return round(delta, int(getattr(meter, "value_round", 3)))

    def _start_reading(
        self,
        conn: sqlite3.Connection,
        meter_id: int,
        start_ts: float,
        end_ts: float,
    ) -> float | None:
        row = conn.execute(
            """
            SELECT reading
            FROM samples
            WHERE meter_id = ? AND ts <= ?
            ORDER BY ts DESC
            LIMIT 1
            """,
            (meter_id, start_ts),
        ).fetchone()
        if row:
            return float(row["reading"])
        row = conn.execute(
            """
            SELECT reading
            FROM samples
            WHERE meter_id = ? AND ts >= ? AND ts <= ?
            ORDER BY ts ASC
            LIMIT 1
            """,
            (meter_id, start_ts, end_ts),
        ).fetchone()
        return float(row["reading"]) if row else None

    def _end_reading(
        self,
        conn: sqlite3.Connection,
        meter_id: int,
        start_ts: float,
        end_ts: float,
    ) -> float | None:
        row = conn.execute(
            """
            SELECT reading
            FROM samples
            WHERE meter_id = ? AND ts >= ? AND ts <= ?
            ORDER BY ts DESC
            LIMIT 1
            """,
            (meter_id, start_ts, end_ts),
        ).fetchone()
        return float(row["reading"]) if row else None

    def _first_ts(self, conn: sqlite3.Connection, meter_id: int) -> float | None:
        row = conn.execute(
            "SELECT MIN(ts) AS first_ts FROM samples WHERE meter_id = ?",
            (meter_id,),
        ).fetchone()
        return float(row["first_ts"]) if row and row["first_ts"] is not None else None

    def _meter_or_default(self, meter_id: int) -> Any:
        if meter_id in self.meters:
            return self.meters[meter_id]
        return next(iter(self.meters.values()))

    def _meter_json(self, meter: Any) -> dict[str, Any]:
        return {
            "id": int(meter.meter_id),
            "name": str(meter.name),
            "unit": str(meter.unit_of_measurement),
        }

    def _empty_series(self, meter: Any, range_name: str) -> dict[str, Any]:
        return {
            "meter": self._meter_json(meter),
            "range": range_name,
            "start": None,
            "end": None,
            "bucket_seconds": None,
            "points": [],
        }

    def _local_label(self, ts: float | None, tz: tzinfo) -> str | None:
        if ts is None:
            return None
        return datetime.fromtimestamp(float(ts), timezone.utc).astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server_version = "RTLAMRDashboard/1.0"

    @property
    def data(self) -> DashboardData:
        return self.server.dashboard_data  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        logging.debug("dashboard: " + fmt, *args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path in ("", "/"):
                self._send_file(WEB_ROOT / "index.html", "text/html; charset=utf-8")
            elif parsed.path == "/app.js":
                self._send_file(WEB_ROOT / "app.js", "application/javascript; charset=utf-8")
            elif parsed.path == "/styles.css":
                self._send_file(WEB_ROOT / "styles.css", "text/css; charset=utf-8")
            elif parsed.path == "/api/overview":
                self._send_json(self.data.overview())
            elif parsed.path == "/api/series":
                params = parse_qs(parsed.query)
                meter_id = int(params.get("meter_id", ["0"])[0])
                range_name = params.get("range", ["30d"])[0]
                self._send_json(self.data.line_series(meter_id, range_name))
            elif parsed.path == "/api/usage":
                params = parse_qs(parsed.query)
                meter_id = int(params.get("meter_id", ["0"])[0])
                period = params.get("period", ["daily"])[0]
                self._send_json(self.data.usage_bars(meter_id, period))
            elif parsed.path == "/api/export.csv":
                params = parse_qs(parsed.query)
                meter_id = int(params.get("meter_id", ["0"])[0])
                range_name = params.get("range", ["30d"])[0]
                filename, body = self.data.csv_export(meter_id, range_name)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception:
            logging.exception("Dashboard request failed: %s", self.path)
            self._send_json({"error": "request_failed"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class DashboardHTTPServer(ThreadingHTTPServer):
    dashboard_data: DashboardData


class DashboardServer:
    def __init__(
        self,
        database_path: str,
        meters: dict[int, Any],
        state: Any,
        timezone_name: str,
    ):
        self.data = DashboardData(database_path, meters, state, timezone_name)
        self.httpd: DashboardHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        try:
            self.httpd = DashboardHTTPServer((DASHBOARD_HOST, DASHBOARD_PORT), DashboardRequestHandler)
            self.httpd.dashboard_data = self.data
        except OSError:
            logging.exception("Could not start dashboard web server on port %s", DASHBOARD_PORT)
            return
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="dashboard-web", daemon=True)
        self.thread.start()
        logging.info("Dashboard web UI listening on port %s", DASHBOARD_PORT)

    def stop(self) -> None:
        if self.httpd is None:
            return
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.thread is not None:
            self.thread.join(timeout=10)
