from __future__ import annotations

import html
import json
import logging
import os
import re
import smtplib
import sqlite3
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import date as dt_date
from datetime import datetime, time as dt_time, timedelta, timezone, tzinfo
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_REPORT_STATE_PATH = Path(
    os.environ.get("REPORT_STATE_PATH", "/data/rtlamr_smart_report_state.json")
)
DEFAULT_REPORT_TIME = "06:30"
DEFAULT_REPORT_TIMEZONE = os.environ.get("TZ") or "UTC"
PALETTE = [
    (37, 99, 235),
    (5, 150, 105),
    (217, 119, 6),
    (124, 58, 237),
    (220, 38, 38),
]


def as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    raise ValueError(f"Expected list or comma-separated string, got {type(value).__name__}")


def parse_report_time(value: Any) -> tuple[dt_time, str]:
    label = str(value or DEFAULT_REPORT_TIME).strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", label)
    if not match:
        raise ValueError("daily_report_time must use HH:MM in 24-hour local time")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError("daily_report_time must be between 00:00 and 23:59")
    return dt_time(hour=hour, minute=minute), f"{hour:02d}:{minute:02d}"


def local_day_bounds(day: dt_date, tz: tzinfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, dt_time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


def local_date_label(day: dt_date) -> str:
    return day.strftime("%A, %B %d, %Y")


def compact_number(value: float | None, decimals: int = 2) -> str:
    if value is None:
        return "n/a"
    if abs(value) >= 100:
        return f"{value:.1f}"
    if abs(value) >= 10:
        return f"{value:.2f}"
    return f"{value:.{decimals}f}"


@dataclass
class DailyEmailReportConfig:
    enabled: bool
    report_time: dt_time
    report_time_label: str
    timezone_name: str
    recipients: list[str]
    sender: str
    smtp_host: str
    smtp_port: int
    smtp_username: str | None
    smtp_password: str | None
    smtp_starttls: bool
    smtp_ssl: bool
    smtp_timeout_seconds: int
    retry_seconds: int
    month_days: int
    subject_prefix: str
    state_path: Path

    @classmethod
    def from_options(cls, options: dict[str, Any]) -> "DailyEmailReportConfig":
        report_time, report_time_label = parse_report_time(
            options.get("daily_report_time", DEFAULT_REPORT_TIME)
        )
        return cls(
            enabled=bool(options.get("daily_report_enabled", False)),
            report_time=report_time,
            report_time_label=report_time_label,
            timezone_name=str(
                options.get("daily_report_timezone")
                or DEFAULT_REPORT_TIMEZONE
            ),
            recipients=as_string_list(options.get("daily_report_recipients")),
            sender=str(options.get("daily_report_sender", "")).strip(),
            smtp_host=str(options.get("smtp_host", "")).strip(),
            smtp_port=int(options.get("smtp_port", 587)),
            smtp_username=(str(options.get("smtp_username", "")).strip() or None),
            smtp_password=(str(options.get("smtp_password", "")).strip() or None),
            smtp_starttls=bool(options.get("smtp_starttls", True)),
            smtp_ssl=bool(options.get("smtp_ssl", False)),
            smtp_timeout_seconds=max(1, int(options.get("smtp_timeout_seconds", 30))),
            retry_seconds=max(60, int(options.get("daily_report_retry_seconds", 3600))),
            month_days=max(2, int(options.get("daily_report_month_days", 31))),
            subject_prefix=str(options.get("daily_report_subject_prefix", "RTLAMR Smart Reader")).strip()
            or "RTLAMR Smart Reader",
            state_path=Path(str(options.get("daily_report_state_path", DEFAULT_REPORT_STATE_PATH))),
        )

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.recipients and self.sender and self.smtp_host)

    def timezone(self) -> tzinfo:
        try:
            return ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError:
            logging.warning("Unknown report timezone %s; using UTC", self.timezone_name)
            return timezone.utc


@dataclass
class MeterReport:
    meter_id: int
    name: str
    unit: str
    value_round: int
    start_reading: float | None
    end_reading: float | None
    usage: float | None
    samples: int
    stale_samples: int
    avg_frames_per_minute: float | None
    min_frames_per_minute: float | None
    max_frames_per_minute: float | None
    first_sample_local: str | None
    last_sample_local: str | None
    hourly_usage: list[float | None]
    month_days: list[dt_date]
    month_usage: list[float | None]

    @property
    def stale_percent(self) -> float | None:
        if not self.samples:
            return None
        return self.stale_samples * 100.0 / self.samples


@dataclass
class ReportBundle:
    report_date: dt_date
    timezone_name: str
    generated_at: datetime
    meters: list[MeterReport]


class ReportDataStore:
    def __init__(self, database_path: str, meters: dict[int, Any], tz: tzinfo, month_days: int):
        self.database_path = Path(database_path)
        self.meters = meters
        self.tz = tz
        self.month_days = month_days

    def collect(self, report_date: dt_date) -> ReportBundle:
        if not self.database_path.exists():
            logging.warning("Report database does not exist yet: %s", self.database_path)
            return ReportBundle(
                report_date=report_date,
                timezone_name=str(self.tz),
                generated_at=datetime.now(timezone.utc).astimezone(self.tz),
                meters=[
                    self._empty_meter_report(meter, report_date)
                    for meter in self.meters.values()
                ],
            )
        conn = sqlite3.connect(self.database_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            return ReportBundle(
                report_date=report_date,
                timezone_name=str(self.tz),
                generated_at=datetime.now(timezone.utc).astimezone(self.tz),
                meters=[
                    self._meter_report(conn, meter, report_date)
                    for meter in self.meters.values()
                ],
            )
        finally:
            conn.close()

    def _empty_meter_report(self, meter: Any, report_date: dt_date) -> MeterReport:
        month_start = report_date - timedelta(days=self.month_days - 1)
        month_days = [month_start + timedelta(days=offset) for offset in range(self.month_days)]
        return MeterReport(
            meter_id=int(meter.meter_id),
            name=str(meter.name),
            unit=str(meter.unit_of_measurement),
            value_round=int(meter.value_round),
            start_reading=None,
            end_reading=None,
            usage=None,
            samples=0,
            stale_samples=0,
            avg_frames_per_minute=None,
            min_frames_per_minute=None,
            max_frames_per_minute=None,
            first_sample_local=None,
            last_sample_local=None,
            hourly_usage=[None] * 24,
            month_days=month_days,
            month_usage=[None] * self.month_days,
        )

    def _meter_report(self, conn: sqlite3.Connection, meter: Any, report_date: dt_date) -> MeterReport:
        start_local, end_local = local_day_bounds(report_date, self.tz)
        start_ts = start_local.timestamp()
        end_ts = end_local.timestamp()
        start_reading, _ = self._start_reading(conn, int(meter.meter_id), start_ts, end_ts)
        end_reading, _ = self._end_reading(conn, int(meter.meter_id), start_ts, end_ts)
        usage = self._valid_usage_delta(meter, start_reading, end_reading, 24.0)
        stats = self._daily_stats(conn, int(meter.meter_id), start_ts, end_ts)

        hourly_usage: list[float | None] = []
        for hour in range(24):
            hour_start = (start_local + timedelta(hours=hour)).timestamp()
            hour_end = (start_local + timedelta(hours=hour + 1)).timestamp()
            hourly_usage.append(self._period_usage(conn, meter, hour_start, hour_end, 1.0))

        month_start = report_date - timedelta(days=self.month_days - 1)
        month_days = [month_start + timedelta(days=offset) for offset in range(self.month_days)]
        month_usage = [
            self._day_usage(conn, meter, day)
            for day in month_days
        ]

        return MeterReport(
            meter_id=int(meter.meter_id),
            name=str(meter.name),
            unit=str(meter.unit_of_measurement),
            value_round=int(meter.value_round),
            start_reading=start_reading,
            end_reading=end_reading,
            usage=usage,
            samples=int(stats["samples"] or 0),
            stale_samples=int(stats["stale_samples"] or 0),
            avg_frames_per_minute=stats["avg_frames_per_minute"],
            min_frames_per_minute=stats["min_frames_per_minute"],
            max_frames_per_minute=stats["max_frames_per_minute"],
            first_sample_local=self._local_ts_label(stats["first_sample_ts"]),
            last_sample_local=self._local_ts_label(stats["last_sample_ts"]),
            hourly_usage=hourly_usage,
            month_days=month_days,
            month_usage=month_usage,
        )

    def _day_usage(self, conn: sqlite3.Connection, meter: Any, day: dt_date) -> float | None:
        start_local, end_local = local_day_bounds(day, self.tz)
        return self._period_usage(
            conn,
            meter,
            start_local.timestamp(),
            end_local.timestamp(),
            24.0,
        )

    def _period_usage(
        self,
        conn: sqlite3.Connection,
        meter: Any,
        start_ts: float,
        end_ts: float,
        hours: float,
    ) -> float | None:
        start_reading, _ = self._start_reading(conn, int(meter.meter_id), start_ts, end_ts)
        end_reading, _ = self._end_reading(conn, int(meter.meter_id), start_ts, end_ts)
        return self._valid_usage_delta(meter, start_reading, end_reading, hours)

    def _start_reading(
        self,
        conn: sqlite3.Connection,
        meter_id: int,
        start_ts: float,
        end_ts: float,
    ) -> tuple[float | None, float | None]:
        row = conn.execute(
            """
            SELECT reading, ts
            FROM samples
            WHERE meter_id = ? AND ts <= ?
            ORDER BY ts DESC
            LIMIT 1
            """,
            (meter_id, start_ts),
        ).fetchone()
        if row:
            return float(row["reading"]), float(row["ts"])
        row = conn.execute(
            """
            SELECT reading, ts
            FROM samples
            WHERE meter_id = ? AND ts >= ? AND ts < ?
            ORDER BY ts ASC
            LIMIT 1
            """,
            (meter_id, start_ts, end_ts),
        ).fetchone()
        if not row:
            return None, None
        return float(row["reading"]), float(row["ts"])

    def _end_reading(
        self,
        conn: sqlite3.Connection,
        meter_id: int,
        start_ts: float,
        end_ts: float,
    ) -> tuple[float | None, float | None]:
        row = conn.execute(
            """
            SELECT reading, ts
            FROM samples
            WHERE meter_id = ? AND ts >= ? AND ts <= ?
            ORDER BY ts DESC
            LIMIT 1
            """,
            (meter_id, start_ts, end_ts),
        ).fetchone()
        if not row:
            return None, None
        return float(row["reading"]), float(row["ts"])

    def _valid_usage_delta(
        self,
        meter: Any,
        start_reading: float | None,
        end_reading: float | None,
        hours: float,
    ) -> float | None:
        if start_reading is None or end_reading is None:
            return None
        delta = end_reading - start_reading
        allow_decrease = bool(getattr(meter, "allow_decrease", False))
        if delta < 0 and not allow_decrease:
            return None
        max_rate = float(getattr(meter, "max_rate_per_hour", -1.0))
        if max_rate >= 0 and delta > max_rate * max(hours, 1 / 60):
            return None
        return round(delta, int(getattr(meter, "value_round", 3)))

    def _daily_stats(
        self,
        conn: sqlite3.Connection,
        meter_id: int,
        start_ts: float,
        end_ts: float,
    ) -> sqlite3.Row:
        return conn.execute(
            """
            SELECT
                COUNT(*) AS samples,
                COALESCE(SUM(stale), 0) AS stale_samples,
                AVG(frames_per_minute) AS avg_frames_per_minute,
                MIN(frames_per_minute) AS min_frames_per_minute,
                MAX(frames_per_minute) AS max_frames_per_minute,
                MIN(ts) AS first_sample_ts,
                MAX(ts) AS last_sample_ts
            FROM samples
            WHERE meter_id = ? AND ts >= ? AND ts < ?
            """,
            (meter_id, start_ts, end_ts),
        ).fetchone()

    def _local_ts_label(self, ts: float | None) -> str | None:
        if ts is None:
            return None
        return datetime.fromtimestamp(float(ts), timezone.utc).astimezone(self.tz).strftime("%H:%M:%S")


class ChartRenderer:
    def __init__(self) -> None:
        from PIL import Image, ImageDraw, ImageFont

        self.Image = Image
        self.ImageDraw = ImageDraw
        self.ImageFont = ImageFont
        self.font_regular = self._font(16)
        self.font_small = self._font(13)
        self.font_title = self._font(22)

    def _font(self, size: int) -> Any:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        ]
        for path in candidates:
            try:
                return self.ImageFont.truetype(path, size=size)
            except OSError:
                continue
        return self.ImageFont.load_default()

    def bar_chart(
        self,
        title: str,
        labels: list[str],
        values: list[float | None],
        unit: str,
        color: tuple[int, int, int],
    ) -> bytes:
        image, draw, area = self._base_chart(title)
        left, top, right, bottom = area
        plot_w = right - left
        plot_h = bottom - top
        valid = [value for value in values if value is not None]
        max_value = max(valid) if valid else 1.0
        max_value = max(max_value, 1.0)
        self._draw_grid(draw, area, max_value, unit)
        count = max(1, len(values))
        slot = plot_w / count
        bar_w = max(2, slot * 0.7)
        for idx, value in enumerate(values):
            if value is None:
                continue
            x0 = left + idx * slot + (slot - bar_w) / 2
            x1 = x0 + bar_w
            y0 = bottom - (max(0.0, value) / max_value) * plot_h
            draw.rounded_rectangle((x0, y0, x1, bottom), radius=3, fill=color)
        self._draw_x_labels(draw, labels, area)
        return self._to_png(image)

    def line_chart(
        self,
        title: str,
        labels: list[str],
        values: list[float | None],
        unit: str,
        color: tuple[int, int, int],
    ) -> bytes:
        image, draw, area = self._base_chart(title)
        left, top, right, bottom = area
        plot_w = right - left
        plot_h = bottom - top
        valid = [value for value in values if value is not None]
        max_value = max(valid) if valid else 1.0
        max_value = max(max_value, 1.0)
        self._draw_grid(draw, area, max_value, unit)
        points: list[tuple[float, float] | None] = []
        count = max(1, len(values) - 1)
        for idx, value in enumerate(values):
            if value is None:
                points.append(None)
                continue
            x = left + (idx / count) * plot_w if count else left
            y = bottom - (max(0.0, value) / max_value) * plot_h
            points.append((x, y))
        previous: tuple[float, float] | None = None
        for point in points:
            if point is None:
                previous = None
                continue
            if previous is not None:
                draw.line((*previous, *point), fill=color, width=4)
            draw.ellipse((point[0] - 3, point[1] - 3, point[0] + 3, point[1] + 3), fill=color)
            previous = point
        self._draw_x_labels(draw, labels, area)
        return self._to_png(image)

    def _base_chart(self, title: str) -> tuple[Any, Any, tuple[int, int, int, int]]:
        image = self.Image.new("RGB", (900, 360), "white")
        draw = self.ImageDraw.Draw(image)
        draw.text((28, 20), title, fill=(17, 24, 39), font=self.font_title)
        area = (76, 72, 872, 294)
        draw.rectangle(area, outline=(209, 213, 219), width=1)
        return image, draw, area

    def _draw_grid(self, draw: Any, area: tuple[int, int, int, int], max_value: float, unit: str) -> None:
        left, top, right, bottom = area
        plot_h = bottom - top
        for step in range(5):
            value = max_value * step / 4
            y = bottom - plot_h * step / 4
            draw.line((left, y, right, y), fill=(229, 231, 235), width=1)
            label = f"{compact_number(value)} {unit}" if step in (0, 4) else compact_number(value)
            draw.text((12, y - 8), label, fill=(75, 85, 99), font=self.font_small)

    def _draw_x_labels(self, draw: Any, labels: list[str], area: tuple[int, int, int, int]) -> None:
        left, _top, right, bottom = area
        if not labels:
            return
        plot_w = right - left
        count = len(labels)
        step = max(1, count // 8)
        for idx, label in enumerate(labels):
            if idx % step != 0 and idx != count - 1:
                continue
            x = left + (idx / max(1, count - 1)) * plot_w
            draw.text((x - 18, bottom + 14), label, fill=(75, 85, 99), font=self.font_small)

    def _to_png(self, image: Any) -> bytes:
        from io import BytesIO

        buffer = BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()


class DailyReportEmailBuilder:
    def __init__(self, report_config: DailyEmailReportConfig):
        self.report_config = report_config

    def build(self, bundle: ReportBundle) -> EmailMessage:
        images: list[tuple[str, str, bytes]] = []
        renderer: ChartRenderer | None = None
        try:
            renderer = ChartRenderer()
        except ImportError as exc:
            logging.warning("Chart renderer unavailable; sending report without plots: %s", exc)
        except Exception:
            logging.exception("Could not initialize chart renderer; sending report without plots")

        if renderer is not None:
            for index, meter in enumerate(bundle.meters):
                color = PALETTE[index % len(PALETTE)]
                hour_labels = [f"{hour:02d}:00" for hour in range(24)]
                cumulative = self._cumulative(meter.hourly_usage)
                daily_labels = [day.strftime("%m/%d") for day in meter.month_days]
                images.extend(
                    [
                        (
                            f"hourly-{meter.meter_id}",
                            f"{meter.meter_id}-hourly.png",
                            renderer.bar_chart(
                                f"{meter.name}: hourly usage on {bundle.report_date.isoformat()}",
                                hour_labels,
                                meter.hourly_usage,
                                meter.unit,
                                color,
                            ),
                        ),
                        (
                            f"cumulative-{meter.meter_id}",
                            f"{meter.meter_id}-cumulative.png",
                            renderer.line_chart(
                                f"{meter.name}: cumulative usage during the day",
                                hour_labels,
                                cumulative,
                                meter.unit,
                                color,
                            ),
                        ),
                        (
                            f"month-{meter.meter_id}",
                            f"{meter.meter_id}-last-{self.report_config.month_days}-days.png",
                            renderer.bar_chart(
                                f"{meter.name}: daily usage, last {self.report_config.month_days} days",
                                daily_labels,
                                meter.month_usage,
                                meter.unit,
                                color,
                            ),
                        ),
                    ]
                )

        subject = f"{self.report_config.subject_prefix}: usage for {bundle.report_date.isoformat()}"
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.report_config.sender
        message["To"] = ", ".join(self.report_config.recipients)
        message["Date"] = formatdate(localtime=True)
        message.set_content(self._plain_text(bundle))
        message.add_alternative(self._html(bundle, images, renderer is not None), subtype="html")
        html_part = message.get_payload()[-1]
        for cid, filename, image_bytes in images:
            html_part.add_related(
                image_bytes,
                maintype="image",
                subtype="png",
                cid=f"<{cid}>",
                filename=filename,
            )
        return message

    def _plain_text(self, bundle: ReportBundle) -> str:
        lines = [
            f"Utility usage report for {local_date_label(bundle.report_date)}",
            f"Generated {bundle.generated_at.strftime('%Y-%m-%d %H:%M:%S %Z')}",
            "",
        ]
        for meter in bundle.meters:
            lines.append(
                f"{meter.name}: {compact_number(meter.usage, meter.value_round)} {meter.unit} "
                f"from {compact_number(meter.start_reading, meter.value_round)} "
                f"to {compact_number(meter.end_reading, meter.value_round)}; "
                f"{meter.samples} samples; stale {compact_number(meter.stale_percent, 1)}%"
            )
        return "\n".join(lines)

    def _html(
        self,
        bundle: ReportBundle,
        images: list[tuple[str, str, bytes]],
        has_charts: bool,
    ) -> str:
        image_cids = {cid for cid, _filename, _bytes in images}
        parts = [
            "<!doctype html>",
            "<html><body style=\"font-family:Arial,sans-serif;color:#111827;line-height:1.45\">",
            f"<h1 style=\"margin-bottom:0\">Utility usage report</h1>",
            f"<p style=\"margin-top:4px;color:#4b5563\">{html.escape(local_date_label(bundle.report_date))} "
            f"midnight-to-midnight, generated {html.escape(bundle.generated_at.strftime('%Y-%m-%d %H:%M:%S %Z'))}</p>",
        ]
        for meter in bundle.meters:
            parts.append(f"<h2>{html.escape(meter.name)}</h2>")
            parts.append(self._summary_table(meter))
            if has_charts:
                for cid in (
                    f"hourly-{meter.meter_id}",
                    f"cumulative-{meter.meter_id}",
                    f"month-{meter.meter_id}",
                ):
                    if cid in image_cids:
                        parts.append(
                            f"<p><img src=\"cid:{cid}\" "
                            "style=\"max-width:900px;width:100%;height:auto;border:1px solid #e5e7eb\" "
                            f"alt=\"{html.escape(cid)}\"></p>"
                        )
            else:
                parts.append(
                    "<p style=\"color:#b45309\">Charts were unavailable because the image renderer "
                    "could not start. The summary table is still included.</p>"
                )
        parts.append("</body></html>")
        return "\n".join(parts)

    def _summary_table(self, meter: MeterReport) -> str:
        rows = [
            ("Usage", f"{compact_number(meter.usage, meter.value_round)} {html.escape(meter.unit)}"),
            (
                "Reading range",
                f"{compact_number(meter.start_reading, meter.value_round)} to "
                f"{compact_number(meter.end_reading, meter.value_round)} {html.escape(meter.unit)}",
            ),
            ("Samples", str(meter.samples)),
            ("Stale samples", f"{meter.stale_samples} ({compact_number(meter.stale_percent, 1)}%)"),
            (
                "Frames/min",
                f"avg {compact_number(meter.avg_frames_per_minute, 1)}, "
                f"min {compact_number(meter.min_frames_per_minute, 1)}, "
                f"max {compact_number(meter.max_frames_per_minute, 1)}",
            ),
            (
                "Sample window",
                f"{html.escape(meter.first_sample_local or 'n/a')} to "
                f"{html.escape(meter.last_sample_local or 'n/a')}",
            ),
        ]
        cells = [
            "<table style=\"border-collapse:collapse;margin:12px 0 18px 0\">"
        ]
        for label, value in rows:
            cells.append(
                "<tr>"
                f"<th style=\"text-align:left;padding:6px 14px 6px 0;color:#4b5563\">{html.escape(label)}</th>"
                f"<td style=\"padding:6px 0\">{value}</td>"
                "</tr>"
            )
        cells.append("</table>")
        return "\n".join(cells)

    def _cumulative(self, values: list[float | None]) -> list[float | None]:
        total = 0.0
        seen = False
        result: list[float | None] = []
        for value in values:
            if value is None:
                result.append(total if seen else None)
                continue
            total += value
            seen = True
            result.append(total)
        return result


class DailyReportMailer:
    def __init__(
        self,
        report_config: DailyEmailReportConfig,
        database_path: str,
        meters: dict[int, Any],
    ):
        self.report_config = report_config
        self.database_path = database_path
        self.meters = meters

    def send(self, report_date: dt_date) -> None:
        tz = self.report_config.timezone()
        bundle = ReportDataStore(
            self.database_path,
            self.meters,
            tz,
            self.report_config.month_days,
        ).collect(report_date)
        message = DailyReportEmailBuilder(self.report_config).build(bundle)
        context = ssl.create_default_context()
        if self.report_config.smtp_ssl:
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(
                self.report_config.smtp_host,
                self.report_config.smtp_port,
                timeout=self.report_config.smtp_timeout_seconds,
                context=context,
            )
        else:
            smtp = smtplib.SMTP(
                self.report_config.smtp_host,
                self.report_config.smtp_port,
                timeout=self.report_config.smtp_timeout_seconds,
            )
        with smtp:
            smtp.ehlo()
            if self.report_config.smtp_starttls and not self.report_config.smtp_ssl:
                smtp.starttls(context=context)
                smtp.ehlo()
            if self.report_config.smtp_username or self.report_config.smtp_password:
                smtp.login(
                    self.report_config.smtp_username or "",
                    self.report_config.smtp_password or "",
                )
            smtp.send_message(
                message,
                from_addr=self.report_config.sender,
                to_addrs=self.report_config.recipients,
            )


class DailyReportScheduler:
    def __init__(
        self,
        report_config: DailyEmailReportConfig,
        database_path: str,
        meters: dict[int, Any],
    ):
        self.report_config = report_config
        self.database_path = database_path
        self.meters = meters
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_attempt = 0.0

    def start(self) -> None:
        if not self.report_config.enabled:
            return
        if not self.report_config.ready:
            logging.warning(
                "Daily email reports are enabled but missing smtp_host, sender, or recipients; reports disabled"
            )
            return
        self.thread = threading.Thread(target=self._run, name="daily-report-scheduler", daemon=True)
        self.thread.start()
        logging.info(
            "Daily email reports enabled for %s recipient(s) at %s %s",
            len(self.report_config.recipients),
            self.report_config.report_time_label,
            self.report_config.timezone_name,
        )

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=10)

    def _run(self) -> None:
        self._tick()
        while not self.stop_event.wait(30):
            self._tick()

    def _tick(self) -> None:
        report_date = self._due_report_date()
        if report_date is None:
            return
        state = self._load_state()
        if state.get("last_sent_report_date") == report_date.isoformat():
            return
        now = time.time()
        if now - self.last_attempt < self.report_config.retry_seconds:
            return
        self.last_attempt = now
        try:
            DailyReportMailer(self.report_config, self.database_path, self.meters).send(report_date)
        except Exception:
            logging.exception("Could not send daily email report for %s", report_date.isoformat())
            return
        state["last_sent_report_date"] = report_date.isoformat()
        state["last_sent_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        self._save_state(state)
        logging.info("Sent daily email report for %s", report_date.isoformat())

    def _due_report_date(self) -> dt_date | None:
        tz = self.report_config.timezone()
        now = datetime.now(timezone.utc).astimezone(tz)
        scheduled = datetime.combine(now.date(), self.report_config.report_time, tzinfo=tz)
        if now < scheduled:
            return None
        return now.date() - timedelta(days=1)

    def _load_state(self) -> dict[str, Any]:
        path = self.report_config.state_path
        if not path.exists():
            return {}
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                return data
        except Exception:
            logging.exception("Could not read report state from %s", path)
        return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        path = self.report_config.state_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(".tmp")
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
            tmp_path.replace(path)
        except OSError:
            logging.exception("Could not save report state to %s", path)
