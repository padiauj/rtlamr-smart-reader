from __future__ import annotations

import html
import json
import logging
import math
import os
import re
import smtplib
import sqlite3
from usage import period_usage
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
from urllib import error as urlerror
from urllib import parse, request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_REPORT_STATE_PATH = Path(
    os.environ.get("REPORT_STATE_PATH", "/data/rtlamr_smart_report_state.json")
)
DEFAULT_WEATHER_CACHE_PATH = Path(
    os.environ.get("WEATHER_CACHE_PATH", "/data/rtlamr_smart_weather_cache.json")
)
DEFAULT_REPORT_TIME = "06:30"
DEFAULT_REPORT_TIMEZONE = os.environ.get("TZ") or "UTC"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
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


def optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return float(value)


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
    weather_enabled: bool
    weather_latitude: float | None
    weather_longitude: float | None
    weather_temperature_unit: str
    weather_degree_day_base_temperature: float | None
    weather_cache_path: Path
    weather_api_timeout_seconds: int

    @classmethod
    def from_options(cls, options: dict[str, Any]) -> "DailyEmailReportConfig":
        report_time, report_time_label = parse_report_time(
            options.get("daily_report_time", DEFAULT_REPORT_TIME)
        )
        smtp_username = str(options.get("smtp_username", "")).strip()
        sender = str(options.get("daily_report_sender", "")).strip() or smtp_username
        weather_temperature_unit = str(options.get("weather_temperature_unit", "fahrenheit")).strip().lower() or "fahrenheit"
        if weather_temperature_unit not in {"auto", "fahrenheit", "celsius"}:
            logging.warning("Unknown weather_temperature_unit %s; using fahrenheit", weather_temperature_unit)
            weather_temperature_unit = "fahrenheit"
        return cls(
            enabled=bool(options.get("daily_report_enabled", False)),
            report_time=report_time,
            report_time_label=report_time_label,
            timezone_name=str(
                options.get("daily_report_timezone")
                or DEFAULT_REPORT_TIMEZONE
            ),
            recipients=as_string_list(options.get("daily_report_recipients")),
            sender=sender,
            smtp_host=str(options.get("smtp_host", "")).strip(),
            smtp_port=int(options.get("smtp_port", 587)),
            smtp_username=smtp_username or None,
            smtp_password=(str(options.get("smtp_password", "")).strip() or None),
            smtp_starttls=bool(options.get("smtp_starttls", True)),
            smtp_ssl=bool(options.get("smtp_ssl", False)),
            smtp_timeout_seconds=max(1, int(options.get("smtp_timeout_seconds", 30))),
            retry_seconds=max(60, int(options.get("daily_report_retry_seconds", 3600))),
            month_days=max(2, int(options.get("daily_report_month_days", 31))),
            subject_prefix=str(options.get("daily_report_subject_prefix", "RTLAMR Smart Reader")).strip()
            or "RTLAMR Smart Reader",
            state_path=Path(str(options.get("daily_report_state_path", DEFAULT_REPORT_STATE_PATH))),
            weather_enabled=bool(options.get("daily_report_weather_enabled", True)),
            weather_latitude=optional_float(options.get("weather_latitude")),
            weather_longitude=optional_float(options.get("weather_longitude")),
            weather_temperature_unit=weather_temperature_unit,
            weather_degree_day_base_temperature=optional_float(
                options.get("weather_degree_day_base_temperature")
            ),
            weather_cache_path=Path(str(options.get("weather_cache_path", DEFAULT_WEATHER_CACHE_PATH))),
            weather_api_timeout_seconds=max(1, int(options.get("weather_api_timeout_seconds", 15))),
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
class WeatherReport:
    latitude: float
    longitude: float
    temperature_unit: str
    base_temperature: float
    source: str
    daily_mean_temps: dict[str, float | None]
    hourly_mean_temps: dict[str, list[float | None]]

    @property
    def unit_label(self) -> str:
        return "F" if self.temperature_unit == "fahrenheit" else "C"

    @property
    def degree_unit_label(self) -> str:
        return r"$^\circ$F" if self.temperature_unit == "fahrenheit" else r"$^\circ$C"

    def mean_temps_for(self, days: list[dt_date]) -> list[float | None]:
        return [self.daily_mean_temps.get(day.isoformat()) for day in days]

    def degree_days_for(self, days: list[dt_date], driver: str) -> list[float | None]:
        return [
            self._degree_load(self.daily_mean_temps.get(day.isoformat()), driver)
            for day in days
        ]

    def degree_hours_for(self, day: dt_date, driver: str) -> list[float | None]:
        values = self.hourly_mean_temps.get(day.isoformat()) or [None] * 24
        return [self._degree_load(value, driver) for value in values[:24]]

    def _degree_load(self, temperature: float | None, driver: str) -> float | None:
        if temperature is None:
            return None
        if driver == "heating":
            return round(max(0.0, self.base_temperature - temperature), 1)
        return round(max(0.0, temperature - self.base_temperature), 1)


@dataclass
class ReportBundle:
    report_date: dt_date
    timezone_name: str
    generated_at: datetime
    meters: list[MeterReport]
    weather: WeatherReport | None = None


class WeatherProvider:
    def __init__(self, report_config: DailyEmailReportConfig):
        self.report_config = report_config
        self._home_assistant_config: dict[str, Any] | None = None

    def collect(
        self,
        days: list[dt_date],
        hourly_day: dt_date,
        timezone_name: str,
    ) -> WeatherReport | None:
        if not self.report_config.weather_enabled or not days:
            return None
        location = self._location()
        if location is None:
            logging.warning(
                "Daily report weather is enabled, but no latitude/longitude is available; "
                "set weather_latitude and weather_longitude or enable Home Assistant API access"
            )
            return None

        latitude, longitude, source = location
        temperature_unit = self._temperature_unit()
        base_temperature = self._base_temperature(temperature_unit)
        cache = self._load_cache(latitude, longitude, temperature_unit)
        day_keys = [day.isoformat() for day in days]
        mean_temps = {
            key: self._coerce_optional_float(cache.get("daily_mean_temps", {}).get(key))
            for key in day_keys
        }
        hourly_key = hourly_day.isoformat()
        hourly_temps = self._coerce_hourly_list(
            cache.get("hourly_mean_temps", {}).get(hourly_key)
        )
        missing_daily = [key for key, value in mean_temps.items() if value is None]
        missing_hourly = hourly_temps is None

        if missing_daily or missing_hourly:
            try:
                fetched = self._fetch_range(
                    min(days),
                    max(days),
                    hourly_day,
                    latitude,
                    longitude,
                    timezone_name,
                    temperature_unit,
                )
            except Exception:
                logging.exception("Could not fetch weather temperatures")
                fetched = {"daily_mean_temps": {}, "hourly_mean_temps": {}}
            if fetched:
                fetched_daily = fetched.get("daily_mean_temps", {})
                fetched_hourly = fetched.get("hourly_mean_temps", {})
                mean_temps.update(
                    {key: fetched_daily.get(key, mean_temps.get(key)) for key in day_keys}
                )
                if hourly_key in fetched_hourly:
                    hourly_temps = self._coerce_hourly_list(fetched_hourly[hourly_key])
                cache["daily_mean_temps"].update(
                    {key: value for key, value in fetched_daily.items() if value is not None}
                )
                cache["hourly_mean_temps"].update(fetched_hourly)
                self._save_cache(cache, latitude, longitude, temperature_unit)

        if all(value is None for value in mean_temps.values()) and hourly_temps is None:
            return None

        return WeatherReport(
            latitude=latitude,
            longitude=longitude,
            temperature_unit=temperature_unit,
            base_temperature=base_temperature,
            source=source,
            daily_mean_temps=mean_temps,
            hourly_mean_temps={hourly_key: hourly_temps or [None] * 24},
        )

    def _location(self) -> tuple[float, float, str] | None:
        if (
            self.report_config.weather_latitude is not None
            and self.report_config.weather_longitude is not None
        ):
            return (
                self.report_config.weather_latitude,
                self.report_config.weather_longitude,
                "configured coordinates",
            )

        ha_config = self._fetch_home_assistant_config()
        if not ha_config:
            return None
        try:
            latitude = float(ha_config["latitude"])
            longitude = float(ha_config["longitude"])
        except (KeyError, TypeError, ValueError):
            return None
        return latitude, longitude, "Home Assistant location"

    def _fetch_home_assistant_config(self) -> dict[str, Any] | None:
        if self._home_assistant_config is not None:
            return self._home_assistant_config
        token = os.environ.get("SUPERVISOR_TOKEN", "").strip()
        if not token:
            return None
        core_config_url = "http://supervisor/core/api/config"
        req = request.Request(
            core_config_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with request.urlopen(
                req,
                timeout=self.report_config.weather_api_timeout_seconds,
            ) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (OSError, TimeoutError, urlerror.URLError, json.JSONDecodeError):
            logging.exception("Could not read Home Assistant Core config for weather location")
            return None
        if not isinstance(data, dict):
            return None
        self._home_assistant_config = data
        return data

    def _temperature_unit(self) -> str:
        configured = self.report_config.weather_temperature_unit
        if configured in {"fahrenheit", "celsius"}:
            return configured
        ha_config = self._fetch_home_assistant_config()
        unit_system = ha_config.get("unit_system") if ha_config else None
        if isinstance(unit_system, dict):
            temperature = str(unit_system.get("temperature", "")).lower()
            if "f" in temperature:
                return "fahrenheit"
            if "c" in temperature:
                return "celsius"
        return "fahrenheit"

    def _base_temperature(self, temperature_unit: str) -> float:
        if self.report_config.weather_degree_day_base_temperature is not None:
            return self.report_config.weather_degree_day_base_temperature
        return 65.0 if temperature_unit == "fahrenheit" else 18.3

    def _fetch_range(
        self,
        start: dt_date,
        end: dt_date,
        hourly_day: dt_date,
        latitude: float,
        longitude: float,
        timezone_name: str,
        temperature_unit: str,
    ) -> dict[str, dict[str, Any]]:
        today = datetime.now(timezone.utc).astimezone(self.report_config.timezone()).date()
        recent_start = today - timedelta(days=92)
        fetched: dict[str, dict[str, Any]] = {
            "daily_mean_temps": {},
            "hourly_mean_temps": {},
        }
        fetch_start = min(start, hourly_day)
        fetch_end = max(end, hourly_day)
        if fetch_start < recent_start:
            archive_end = min(fetch_end, recent_start - timedelta(days=1))
            self._merge_weather_payload(
                fetched,
                self._fetch_open_meteo(
                    OPEN_METEO_ARCHIVE_URL,
                    fetch_start,
                    archive_end,
                    latitude,
                    longitude,
                    timezone_name,
                    temperature_unit,
                )
            )
        if fetch_end >= recent_start:
            forecast_start = max(fetch_start, recent_start)
            self._merge_weather_payload(
                fetched,
                self._fetch_open_meteo(
                    OPEN_METEO_FORECAST_URL,
                    forecast_start,
                    fetch_end,
                    latitude,
                    longitude,
                    timezone_name,
                    temperature_unit,
                )
            )
        return fetched

    def _merge_weather_payload(
        self,
        target: dict[str, dict[str, Any]],
        source: dict[str, dict[str, Any]],
    ) -> None:
        target["daily_mean_temps"].update(source.get("daily_mean_temps", {}))
        target["hourly_mean_temps"].update(source.get("hourly_mean_temps", {}))

    def _fetch_open_meteo(
        self,
        endpoint: str,
        start: dt_date,
        end: dt_date,
        latitude: float,
        longitude: float,
        timezone_name: str,
        temperature_unit: str,
    ) -> dict[str, float | None]:
        if end < start:
            return {}
        query = parse.urlencode(
            {
                "latitude": f"{latitude:.6f}",
                "longitude": f"{longitude:.6f}",
                "daily": "temperature_2m_mean",
                "hourly": "temperature_2m",
                "temperature_unit": temperature_unit,
                "timezone": timezone_name,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            }
        )
        with request.urlopen(
            f"{endpoint}?{query}",
            timeout=self.report_config.weather_api_timeout_seconds,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("error"):
            raise ValueError(f"Open-Meteo error: {payload!r}")
        result: dict[str, dict[str, Any]] = {
            "daily_mean_temps": {},
            "hourly_mean_temps": {},
        }
        daily = payload.get("daily")
        if isinstance(daily, dict):
            times = daily.get("time")
            means = daily.get("temperature_2m_mean")
            if isinstance(times, list) and isinstance(means, list):
                result["daily_mean_temps"] = {
                    str(day): self._coerce_optional_float(value)
                    for day, value in zip(times, means)
                }
        hourly = payload.get("hourly")
        if isinstance(hourly, dict):
            times = hourly.get("time")
            temps = hourly.get("temperature_2m")
            if isinstance(times, list) and isinstance(temps, list):
                for time_label, value in zip(times, temps):
                    day_key, hour = self._hour_key(str(time_label))
                    if day_key is None or hour is None:
                        continue
                    day_values = result["hourly_mean_temps"].setdefault(day_key, [None] * 24)
                    day_values[hour] = self._coerce_optional_float(value)
        return result

    def _hour_key(self, time_label: str) -> tuple[str | None, int | None]:
        match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})T(\d{2}):\d{2}", time_label)
        if not match:
            return None, None
        hour = int(match.group(2))
        if hour < 0 or hour > 23:
            return None, None
        return match.group(1), hour

    def _load_cache(
        self,
        latitude: float,
        longitude: float,
        temperature_unit: str,
    ) -> dict[str, Any]:
        path = self.report_config.weather_cache_path
        default = {
            "latitude": latitude,
            "longitude": longitude,
            "temperature_unit": temperature_unit,
            "daily_mean_temps": {},
            "hourly_mean_temps": {},
        }
        if not path.exists():
            return default
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            logging.exception("Could not read weather cache from %s", path)
            return default
        if not isinstance(data, dict):
            return default
        try:
            cached_latitude = float(data.get("latitude", 999.0))
            cached_longitude = float(data.get("longitude", 999.0))
        except (TypeError, ValueError):
            return default
        if (
            abs(cached_latitude - latitude) > 0.01
            or abs(cached_longitude - longitude) > 0.01
            or str(data.get("temperature_unit")) != temperature_unit
            or not isinstance(data.get("daily_mean_temps"), dict)
            or not isinstance(data.get("hourly_mean_temps"), dict)
        ):
            return default
        return data

    def _save_cache(
        self,
        cache: dict[str, Any],
        latitude: float,
        longitude: float,
        temperature_unit: str,
    ) -> None:
        path = self.report_config.weather_cache_path
        cache["latitude"] = latitude
        cache["longitude"] = longitude
        cache["temperature_unit"] = temperature_unit
        cache["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(".tmp")
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(cache, handle, indent=2, sort_keys=True)
            tmp_path.replace(path)
        except OSError:
            logging.exception("Could not save weather cache to %s", path)

    def _coerce_optional_float(self, value: Any) -> float | None:
        if value is None:
            return None
        try:
            return round(float(value), 1)
        except (TypeError, ValueError):
            return None

    def _coerce_hourly_list(self, value: Any) -> list[float | None] | None:
        if not isinstance(value, list) or len(value) < 24:
            return None
        return [self._coerce_optional_float(item) for item in value[:24]]


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
        usage = self._period_usage(conn, meter, start_ts, end_ts, (end_ts - start_ts) / 3600)
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
        return period_usage(conn, meter, start_ts, end_ts, hours)

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
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.ticker import MaxNLocator

        self.plt = plt
        self.MaxNLocator = MaxNLocator
        self.usage_color = "#2A6FBB"
        self.weather_color = "#9CCC65"
        self.edge_color = "#1E1E1E"
        self.grid_color = "#CFCFCF"
        self.strip_point_limit = 15

    def _apply_style(self) -> None:
        self.plt.rcParams.update(
            {
                "text.usetex": False,
                "font.family": "serif",
                "font.serif": ["cmr10", "Computer Modern Roman", "DejaVu Serif"],
                "mathtext.fontset": "cm",
                "axes.formatter.use_mathtext": True,
                "axes.unicode_minus": False,
                "figure.facecolor": "white",
                "axes.facecolor": "white",
                "savefig.facecolor": "white",
                "savefig.edgecolor": "white",
                "axes.edgecolor": "#222222",
                "axes.labelcolor": "#222222",
                "xtick.color": "#222222",
                "ytick.color": "#222222",
            }
        )

    def bar_chart(
        self,
        title: str,
        labels: list[str],
        values: list[float | None],
        unit: str,
        color: tuple[int, int, int],
    ) -> bytes:
        self._apply_style()
        labels, values = self._meter_value_series(labels, values)
        if len(values) > self.strip_point_limit:
            return self._strip_bar_chart(title, labels, values, unit)
        return self._single_bar_chart(title, labels, values, unit)

    def grouped_bar_chart(
        self,
        title: str,
        subtitle: str,
        labels: list[str],
        left_values: list[float | None],
        left_axis_label: str,
        right_values: list[float | None],
        right_axis_label: str,
        split: bool = True,
        right_decimals: int = 0,
        right_value_suffix: str = "",
        weather_label_mode: str = "all",
        right_style: str = "bar",
    ) -> bytes:
        self._apply_style()
        if right_style not in {"bar", "line"}:
            raise ValueError("right_style must be 'bar' or 'line'")
        points = self._meter_grouped_series(labels, left_values, right_values)
        strips = (
            self._strips(points, preferred=self.strip_point_limit)
            if split and len(points) > self.strip_point_limit
            else [points]
        )
        fig_w = 8.4
        if len(strips) == 1 and len(points) > self.strip_point_limit:
            fig_w = min(13.2, 8.4 + (len(points) - self.strip_point_limit) * 0.28)
        fig_h = 5.2 if len(strips) == 1 else 7.8
        fig, axes = self.plt.subplots(len(strips), 1, figsize=(fig_w, fig_h), dpi=220, sharey=False)
        if len(strips) == 1:
            axes = [axes]
        fig.text(0.075, 0.97, title, ha="left", va="top", fontsize=22, color="#111111")
        fig.text(
            0.075,
            0.895,
            subtitle,
            ha="left",
            va="top",
            fontsize=15,
            color="#303030",
        )
        total = sum(point[1] for point in points)
        total_unit = left_axis_label[left_axis_label.find("(") + 1:left_axis_label.rfind(")")]
        fig.text(0.985, 0.895, f"total {self._fmt(total)} {total_unit}", ha="right", va="top", fontsize=15, color="#303030")
        left_max, left_ticks = self._nice_axis([point[1] for point in points])
        right_max, right_ticks = self._nice_axis(
            [point[2] for point in points if point[2] is not None]
        )
        all_labels = [point[0] for point in points]
        bottom = 0.17 if len(strips) == 1 and self._should_rotate_labels(all_labels) else 0.13
        top = 0.79 if len(strips) == 1 else 0.82
        fig.subplots_adjust(left=0.105, right=0.895, top=top, bottom=bottom, hspace=0.50)
        fig.canvas.draw()
        label_texts: list[tuple[Any, Any, int]] = []
        for ax, strip in zip(axes, strips):
            ax2 = ax.twinx()
            ax.set_zorder(ax2.get_zorder() + 1)
            ax.patch.set_visible(False)
            strip_labels = [item[0] for item in strip]
            strip_left = [item[1] for item in strip]
            strip_right = [item[2] for item in strip]
            xs = list(range(len(strip)))
            bar_width = 0.34 if right_style == "bar" else 0.58
            bar_offset = bar_width / 2
            usage_x = [
                (x - bar_offset if right_style == "bar" else x)
                for x, value in zip(xs, strip_left)
                if value is not None
            ]
            usage_y = [value for value in strip_left if value is not None]
            right_points = [
                (x + bar_offset if right_style == "bar" else x, value)
                for x, value in zip(xs, strip_right)
                if value is not None
            ]
            usage_bars = ax.bar(
                usage_x,
                usage_y,
                width=bar_width,
                color=self.usage_color,
                edgecolor=self.edge_color,
                linewidth=0.6,
            )
            if right_style == "bar":
                temp_bars = ax2.bar(
                    [point[0] for point in right_points],
                    [point[1] for point in right_points],
                    width=bar_width,
                    color=self.weather_color,
                    edgecolor=self.edge_color,
                    linewidth=0.6,
                )
                right_label_points = [
                    (rect.get_x() + rect.get_width() / 2 + 0.02, value, ax2, value)
                    for rect, (_x, value) in zip(temp_bars, right_points)
                ]
            else:
                line_points = [
                    (
                        x,
                        self._scale_right_to_left(value, right_max, left_max),
                        value,
                    )
                    for x, value in right_points
                ]
                if line_points:
                    ax.plot(
                        [point[0] for point in line_points],
                        [point[1] for point in line_points],
                        color=self.weather_color,
                        marker="o",
                        markeredgecolor=self.edge_color,
                        markerfacecolor="white",
                        markeredgewidth=0.6,
                        markersize=4.8,
                        linewidth=2.0,
                        zorder=4,
                    )
                right_label_points = [
                    (x + 0.06, y, ax, value)
                    for x, y, value in line_points
                ]
            ax.set_ylim(0, left_max)
            ax2.set_ylim(0, right_max)
            self._set_x_domain(ax, len(strip))
            ax.set_xticks(xs, strip_labels)
            if self._should_rotate_labels(strip_labels):
                self._rotate_x_labels(ax)
            ax.set_ylabel(left_axis_label, fontsize=13, labelpad=10)
            ax2.set_ylabel(right_axis_label, fontsize=13, labelpad=10)
            self._style_axes(ax, tick_size=11.8, ticks=left_ticks)
            self._style_axes(ax2, tick_size=14, right_axis=True, grid=False, ticks=right_ticks)
            for rect, value in zip(usage_bars, usage_y):
                label_texts.append(
                    (
                        ax.text(
                            rect.get_x() + rect.get_width() / 2 - 0.02,
                            self._label_y_above_bar(
                                ax,
                                rect.get_x() + rect.get_width() / 2 - 0.02,
                                rect.get_height(),
                            ),
                            self._fmt(value),
                            ha="center",
                            va="bottom",
                            fontsize=12,
                            color="#151515",
                            zorder=5,
                        ),
                        ax,
                        0,
                    )
                )
            right_label_values = [point[3] for point in right_label_points]
            temp_label_indexes = self._label_indexes(right_label_values, weather_label_mode)
            for idx, point in enumerate(right_label_points):
                x, y, label_ax, value = point
                if value == 0:
                    continue
                if idx not in temp_label_indexes:
                    continue
                label_texts.append(
                    (
                        self._weather_value_label(
                            label_ax,
                            x,
                            self._label_y_above_bar(
                                label_ax,
                                x,
                                y,
                                pixels=5.0 if right_style == "line" else 2.0,
                            ),
                            self._fmt(value, decimals=right_decimals),
                            right_value_suffix,
                        ),
                        label_ax,
                        1,
                    )
                )
        self._resolve_label_overlaps(fig, label_texts)
        return self._to_png(fig)

    def _single_bar_chart(
        self,
        title: str,
        labels: list[str],
        values: list[float | None],
        unit: str,
    ) -> bytes:
        fig, ax = self.plt.subplots(1, 1, figsize=(8.4, 4.6), dpi=220)
        fig.text(0.075, 0.965, title, ha="left", va="top", fontsize=22, color="#111111")
        fig.text(0.075, 0.91, unit, ha="left", va="top", fontsize=15, color="#303030")
        ymax = self._nice_max([value for value in values if value is not None])
        xs = list(range(len(values)))
        bar_x = [x for x, value in zip(xs, values) if value is not None]
        bar_y = [value for value in values if value is not None]
        bars = ax.bar(bar_x, bar_y, width=0.58, color=self.usage_color, edgecolor=self.edge_color, linewidth=0.65)
        ax.set_ylim(0, ymax)
        self._set_x_domain(ax, len(values))
        ax.set_xticks(xs, [label[:2] if ":" in label else label for label in labels])
        ax.set_ylabel(f"usage ({unit})", fontsize=13, labelpad=10)
        self._style_axes(ax, tick_size=12)
        for rect, value in zip(bars, bar_y):
            ax.text(
                rect.get_x() + rect.get_width() / 2,
                rect.get_height() + ymax * 0.026,
                self._fmt(value),
                ha="center",
                va="bottom",
                fontsize=12,
                color="#151515",
            )
        fig.subplots_adjust(left=0.09, right=0.985, top=0.79, bottom=0.15)
        return self._to_png(fig)

    def _strip_bar_chart(
        self,
        title: str,
        labels: list[str],
        values: list[float | None],
        unit: str,
    ) -> bytes:
        points = list(zip(labels, values))
        strips = self._strips(points, preferred=15)
        fig, axes = self.plt.subplots(len(strips), 1, figsize=(8.4, 7.8), dpi=220, sharey=True)
        if len(strips) == 1:
            axes = [axes]
        total = sum(value for value in values if value is not None)
        ymax = self._nice_max([value for value in values if value is not None])
        fig.text(0.075, 0.965, title, ha="left", va="top", fontsize=22, color="#111111")
        fig.text(0.075, 0.925, f"{unit} per day", ha="left", va="top", fontsize=15, color="#303030")
        fig.text(0.985, 0.925, f"total {self._fmt(total)} {unit}", ha="right", va="top", fontsize=15, color="#303030")
        for ax, strip in zip(axes, strips):
            strip_labels = [item[0] for item in strip]
            strip_values = [item[1] for item in strip]
            xs = list(range(len(strip_values)))
            bar_x = [x for x, value in zip(xs, strip_values) if value is not None]
            bar_y = [value for value in strip_values if value is not None]
            bars = ax.bar(bar_x, bar_y, width=0.58, color=self.usage_color, edgecolor=self.edge_color, linewidth=0.65)
            ax.set_ylim(0, ymax)
            self._set_x_domain(ax, len(strip_values))
            ax.set_xticks(xs, strip_labels)
            if self._should_rotate_labels(strip_labels):
                self._rotate_x_labels(ax)
            ax.set_ylabel(f"usage ({unit})", fontsize=13, labelpad=10)
            self._style_axes(ax, tick_size=11.8)
            for rect, value in zip(bars, bar_y):
                ax.text(
                    rect.get_x() + rect.get_width() / 2,
                    rect.get_height() + ymax * 0.026,
                    self._fmt(value),
                    ha="center",
                    va="bottom",
                    fontsize=13,
                    color="#151515",
                )
        fig.subplots_adjust(left=0.09, right=0.985, top=0.835, bottom=0.075, hspace=0.50)
        return self._to_png(fig)

    def _meter_value_series(
        self,
        labels: list[str],
        values: list[float | None],
    ) -> tuple[list[str], list[float]]:
        points = [(label, value) for label, value in zip(labels, values) if value is not None]
        return [label for label, _value in points], [value for _label, value in points]

    def _meter_grouped_series(
        self,
        labels: list[str],
        left_values: list[float | None],
        right_values: list[float | None],
    ) -> list[tuple[str, float, float | None]]:
        return [
            (label, left_value, right_value)
            for label, left_value, right_value in zip(labels, left_values, right_values)
            if left_value is not None
        ]

    def _set_x_domain(self, ax: Any, count: int) -> None:
        if count <= 0:
            ax.set_xlim(-0.5, 0.5)
            return
        ax.set_xlim(-0.62, count - 0.38)

    def _weather_value_label(
        self,
        ax: Any,
        x: float,
        y: float,
        value_label: str,
        unit_suffix: str,
    ) -> Any:
        label = self._temperature_bar_label(value_label, unit_suffix)
        return ax.text(
            x,
            y,
            label,
            ha="center",
            va="bottom",
            fontsize=7.6,
            color="#7D8590",
            zorder=5,
        )

    def _temperature_bar_label(self, value_label: str, unit_suffix: str) -> str:
        if unit_suffix in {"F", "C"}:
            return rf"${value_label}^{{\mathrm{{{unit_suffix}}}}}$"
        if "\\circ" in unit_suffix and unit_suffix.endswith(("F", "C")):
            unit = unit_suffix[-1]
            return rf"${value_label}^{{\mathrm{{{unit}}}}}$"
        return f"{value_label}{unit_suffix}" if unit_suffix else value_label

    def _resolve_label_overlaps(
        self,
        fig: Any,
        label_texts: list[tuple[Any, Any, int]],
    ) -> None:
        for _attempt in range(8):
            fig.canvas.draw()
            renderer = fig.canvas.get_renderer()
            boxes = [
                (text.get_window_extent(renderer).expanded(1.04, 1.15), text, ax, priority)
                for text, ax, priority in label_texts
            ]
            moved = False
            for left_index, left in enumerate(boxes):
                left_box, left_text, left_ax, left_priority = left
                for right_box, right_text, right_ax, right_priority in boxes[left_index + 1:]:
                    if not left_box.overlaps(right_box):
                        continue
                    overlap = min(left_box.y1, right_box.y1) - max(left_box.y0, right_box.y0)
                    if overlap <= 0:
                        continue
                    target_text, target_ax = (
                        (left_text, left_ax)
                        if left_priority >= right_priority
                        else (right_text, right_ax)
                    )
                    self._move_text_up_pixels(target_text, target_ax, overlap + 3.0)
                    moved = True
            if not moved:
                return

    def _move_text_up_pixels(self, text: Any, ax: Any, pixels: float) -> None:
        x, y = text.get_position()
        x_display, y_display = ax.transData.transform((x, y))
        _x_new, y_new = ax.transData.inverted().transform((x_display, y_display + pixels))
        text.set_y(y_new)

    def _label_y_above_bar(self, ax: Any, x: float, y: float, pixels: float = 2.0) -> float:
        x_display, y_display = ax.transData.transform((x, y))
        _x_new, y_new = ax.transData.inverted().transform((x_display, y_display + pixels))
        return y_new

    def _scale_right_to_left(
        self,
        value: float,
        right_max: float,
        left_max: float,
    ) -> float:
        if right_max <= 0:
            return 0.0
        return value * left_max / right_max

    def _style_axes(
        self,
        ax: Any,
        tick_size: float,
        right_axis: bool = False,
        grid: bool = True,
        ticks: list[float] | None = None,
    ) -> None:
        ax.tick_params(axis="x", labelsize=tick_size, length=4, width=0.7, direction="out", pad=8)
        ax.tick_params(axis="y", labelsize=15 if not right_axis else tick_size, length=4, width=0.7, pad=7)
        if ticks is None:
            ax.yaxis.set_major_locator(self.MaxNLocator(nbins=4, integer=True))
        else:
            ax.set_yticks(ticks)
        if grid:
            ax.yaxis.grid(True, linestyle=":", linewidth=1.0, color=self.grid_color)
            ax.set_axisbelow(True)
        ax.spines["top"].set_visible(False)
        if right_axis:
            ax.spines["left"].set_visible(False)
            ax.spines["right"].set_linewidth(0.8)
        else:
            ax.spines["right"].set_visible(False)
            ax.spines["left"].set_linewidth(0.8)
        ax.spines["bottom"].set_linewidth(0.8)

    def _strips(self, points: list[Any], preferred: int) -> list[list[Any]]:
        if len(points) <= preferred:
            return [points]
        first = (len(points) + 1) // 2
        return [points[:first], points[first:]]

    def _should_rotate_labels(self, labels: list[str]) -> bool:
        return bool(labels) and any("/" in label for label in labels)

    def _rotate_x_labels(self, ax: Any) -> None:
        for label in ax.get_xticklabels():
            label.set_rotation(35)
            label.set_ha("right")
            label.set_rotation_mode("anchor")

    def _nice_axis(self, values: list[float]) -> tuple[float, list[float]]:
        max_value = max(values) if values else 1.0
        max_value = max(max_value, 1.0)
        target_top = max_value * 1.18
        raw_step = target_top / 4
        magnitude = 10 ** math.floor(math.log10(raw_step))
        candidates = [1, 2, 2.5, 5, 10]
        step = candidates[-1] * magnitude
        for candidate in candidates:
            candidate_step = candidate * magnitude
            if candidate_step >= raw_step:
                step = candidate_step
                break
        top = max(step, math.ceil(target_top / step) * step)
        ticks = [round(step * idx, 10) for idx in range(int(round(top / step)) + 1)]
        return top, ticks

    def _nice_max(self, values: list[float], step: int = 10) -> float:
        max_value = max(values) if values else 1.0
        max_value = max(max_value, 1.0)
        return max(step, math.ceil(max_value * 1.22 / step) * step)

    def _label_indexes(self, values: list[float], mode: str) -> set[int]:
        if mode == "all" or len(values) <= 8:
            return set(range(len(values)))
        positive = [(idx, value) for idx, value in enumerate(values) if value > 0]
        if not positive:
            return set()
        by_value = sorted(positive, key=lambda item: item[1])
        selected = {by_value[-1][0], positive[-1][0]}
        if len(by_value) > 1:
            selected.add(by_value[0][0])
        if len(by_value) > 6:
            selected.add(by_value[len(by_value) // 2][0])
        return selected

    def _fmt(self, value: float | None, decimals: int = 1) -> str:
        if value is None:
            return "n/a"
        if decimals <= 0:
            return f"{int(round(value))}"
        if abs(value - round(value)) < 1e-9:
            return f"{int(round(value))}"
        return f"{value:.{decimals}f}".rstrip("0").rstrip(".")

    def _to_png(self, fig: Any) -> bytes:
        from io import BytesIO

        buffer = BytesIO()
        fig.savefig(buffer, format="png", dpi=220, bbox_inches="tight", pad_inches=0.15)
        self.plt.close(fig)
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
                short_hour_labels = [f"{hour:02d}" for hour in range(24)]
                daily_labels = [day.strftime("%m/%d") for day in meter.month_days]
                hourly_image = renderer.bar_chart(
                    f"Hourly usage on {bundle.report_date.isoformat()}",
                    hour_labels,
                    meter.hourly_usage,
                    meter.unit,
                    color,
                )
                daily_image = renderer.bar_chart(
                    f"Daily usage, last {self.report_config.month_days} days",
                    daily_labels,
                    meter.month_usage,
                    meter.unit,
                    color,
                )
                if bundle.weather is not None:
                    driver = self._weather_driver(meter)
                    labels = self._driver_labels(driver)
                    base = self._format_temperature(
                        bundle.weather.base_temperature,
                        bundle.weather.degree_unit_label,
                    )
                    hourly_image = renderer.grouped_bar_chart(
                        f"Hourly usage and {labels['hour_name']}",
                        f"{meter.unit} per hour, {labels['hour_abbrev']} base {base}",
                        short_hour_labels,
                        meter.hourly_usage,
                        f"usage ({meter.unit})",
                        bundle.weather.degree_hours_for(bundle.report_date, driver),
                        f"{labels['hour_abbrev']} ({bundle.weather.degree_unit_label} h)",
                        split=False,
                        right_decimals=0,
                        right_value_suffix=bundle.weather.unit_label,
                        weather_label_mode="selected",
                        right_style="line",
                    )
                    daily_image = renderer.grouped_bar_chart(
                        f"Daily usage and {labels['day_name']}",
                        f"{meter.unit} per day, {labels['day_abbrev']} base {base}",
                        daily_labels,
                        meter.month_usage,
                        f"usage ({meter.unit})",
                        bundle.weather.degree_days_for(meter.month_days, driver),
                        f"{labels['day_abbrev']} ({bundle.weather.degree_unit_label} d)",
                        split=False,
                        right_decimals=0,
                        right_value_suffix=bundle.weather.unit_label,
                        weather_label_mode="selected",
                        right_style="line",
                    )
                images.extend(
                    [
                        (
                            f"hourly-{meter.meter_id}",
                            f"{meter.meter_id}-hourly.png",
                            hourly_image,
                        ),
                        (
                            f"month-{meter.meter_id}",
                            f"{meter.meter_id}-last-{self.report_config.month_days}-days.png",
                            daily_image,
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
        if bundle.weather is not None:
            mean_temp = bundle.weather.daily_mean_temps.get(bundle.report_date.isoformat())
            lines.append(
                f"Daily mean temperature: {compact_number(mean_temp, 1)} deg {bundle.weather.unit_label}; "
                f"degree-day base {compact_number(bundle.weather.base_temperature, 1)} "
                f"deg {bundle.weather.unit_label}; "
                f"from {bundle.weather.source}"
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
        if bundle.weather is not None:
            mean_temp = bundle.weather.daily_mean_temps.get(bundle.report_date.isoformat())
            parts.append(
                f"<p style=\"color:#4b5563\">Daily mean temperature: "
                f"{html.escape(compact_number(mean_temp, 1))} deg {html.escape(bundle.weather.unit_label)}; "
                f"degree-day base {html.escape(compact_number(bundle.weather.base_temperature, 1))} "
                f"deg {html.escape(bundle.weather.unit_label)} "
                f"({html.escape(bundle.weather.source)})</p>"
            )
        for meter in bundle.meters:
            parts.append(f"<h2>{html.escape(meter.name)}</h2>")
            parts.append(self._summary_table(meter))
            if has_charts:
                for cid in (
                    f"hourly-{meter.meter_id}",
                    f"month-{meter.meter_id}",
                ):
                    if cid in image_cids:
                        parts.append(
                            f"<p><img src=\"cid:{cid}\" "
                            "style=\"max-width:1280px;width:100%;height:auto;border:1px solid #e5e7eb\" "
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
            ("Usage", "Unavailable (incomplete reception)" if meter.usage is None else f"{compact_number(meter.usage, meter.value_round)} {html.escape(meter.unit)}"),
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


    def _weather_driver(self, meter: MeterReport) -> str:
        text = f"{meter.name} {meter.unit}".lower()
        if any(token in text for token in ("gas", "ccf", "therm", "ft3", "m3")):
            return "heating"
        return "cooling"

    def _driver_labels(self, driver: str) -> dict[str, str]:
        if driver == "heating":
            return {
                "day_name": "heating degree-days",
                "day_abbrev": "HDD",
                "hour_name": "heating degree-hours",
                "hour_abbrev": "HDH",
            }
        return {
            "day_name": "cooling degree-days",
            "day_abbrev": "CDD",
            "hour_name": "cooling degree-hours",
            "hour_abbrev": "CDH",
        }

    def _format_temperature(self, value: float, unit_label: str) -> str:
        return f"{compact_number(value, 1)} {unit_label}"


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
        if bundle.meters:
            days = bundle.meters[0].month_days
            bundle.weather = WeatherProvider(self.report_config).collect(
                days,
                report_date,
                self.report_config.timezone_name,
            )
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
