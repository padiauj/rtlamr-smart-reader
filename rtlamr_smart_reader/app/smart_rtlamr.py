#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import queue
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt


OPTIONS_PATH = Path(os.environ.get("OPTIONS_PATH", "/data/options.json"))
STATE_PATH = Path(os.environ.get("STATE_PATH", "/data/rtlamr_smart_state.json"))


def utc_now_iso(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts or time.time(), timezone.utc)
    return dt.replace(microsecond=0).isoformat()


def sanitize(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9_]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "meter"


def as_int_list(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, list):
        return [int(item) for item in value]
    if isinstance(value, str):
        return [int(part.strip()) for part in value.split(",") if part.strip()]
    raise ValueError(f"Expected list or comma-separated string, got {type(value).__name__}")


def unique_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        value = value.strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


@dataclass
class MeterConfig:
    meter_id: int
    name: str
    protocol: str = "scm"
    unit_of_measurement: str = "kWh"
    device_class: str = "energy"
    state_class: str = "total_increasing"
    multiplier: float = 1.0
    value_round: int = 3
    allow_decrease: bool = False
    max_delta: float = 1000.0
    max_rate_per_hour: float = 1000.0
    min_confirmations: int = 2
    confirmation_window_seconds: int = 90

    @property
    def slug(self) -> str:
        return sanitize(self.name)

    @classmethod
    def from_options(cls, item: dict[str, Any], defaults: dict[str, Any]) -> "MeterConfig":
        meter_id = int(item["id"])
        if meter_id <= 0:
            raise ValueError("Each configured meter must have a positive ERT endpoint id")
        name = str(item.get("name") or f"Meter {meter_id}")
        device_class = str(item.get("device_class", defaults.get("device_class", "energy")))
        if "max_rate_per_hour" in item:
            max_rate = float(item["max_rate_per_hour"])
        elif device_class == "energy":
            max_rate = 1000.0
        else:
            max_rate = 1_000_000.0
        return cls(
            meter_id=meter_id,
            name=name,
            protocol=str(item.get("protocol", defaults.get("protocol", "scm"))),
            unit_of_measurement=str(item.get("unit_of_measurement", defaults.get("unit_of_measurement", "kWh"))),
            device_class=device_class,
            state_class=str(item.get("state_class", defaults.get("state_class", "total_increasing"))),
            multiplier=float(item.get("multiplier", defaults.get("multiplier", 1.0))),
            value_round=int(item.get("value_round", defaults.get("value_round", 3))),
            allow_decrease=bool(item.get("allow_decrease", defaults.get("allow_decrease", False))),
            max_delta=float(item.get("max_delta", defaults.get("max_delta", 1000.0))),
            max_rate_per_hour=max_rate,
            min_confirmations=max(1, int(item.get("min_confirmations", defaults.get("min_confirmations", 2)))),
            confirmation_window_seconds=max(
                1,
                int(item.get("confirmation_window_seconds", defaults.get("confirmation_window_seconds", 90))),
            ),
        )


@dataclass
class Config:
    meters: dict[int, MeterConfig]
    lock_center_hz: int
    lock_sample_rate: int
    lock_symbol_length: int
    tuner_gain: float
    overload_restart_threshold: int
    overload_min_rate_ratio: float
    stale_seconds: int
    lock_restart_seconds: int
    scan_seconds: int
    scan_backoff_seconds: int
    scan_centers_hz: list[int]
    rtltcp_port: int
    sample_interval_seconds: int
    retention_days: int
    database_path: str
    store_unchanged_samples: bool
    base_topic: str
    discovery_prefix: str
    discovery_interval: int
    retain_state: bool
    force_update: bool
    log_level: str
    mqtt_host: str
    mqtt_port: int
    mqtt_username: str | None
    mqtt_password: str | None

    @property
    def meter_ids(self) -> list[int]:
        return list(self.meters.keys())

    @property
    def meter_filter(self) -> str:
        return ",".join(str(meter_id) for meter_id in self.meter_ids)

    @property
    def protocol_filter(self) -> str:
        protocols: list[str] = []
        for meter in self.meters.values():
            protocols.extend(part.strip() for part in meter.protocol.split(","))
        return ",".join(unique_preserving_order(protocols))

    @classmethod
    def load(cls) -> "Config":
        with OPTIONS_PATH.open("r", encoding="utf-8") as handle:
            options = json.load(handle)

        meter_defaults = {
            "protocol": options.get("protocol", "scm"),
            "unit_of_measurement": options.get("unit_of_measurement", "kWh"),
            "device_class": options.get("device_class", "energy"),
            "state_class": options.get("state_class", "total_increasing"),
            "multiplier": options.get("multiplier", 1.0),
            "value_round": options.get("value_round", 3),
            "allow_decrease": options.get("allow_decrease", False),
            "max_delta": options.get("max_delta", 1000.0),
            "min_confirmations": options.get("min_confirmations", 2),
            "confirmation_window_seconds": options.get("confirmation_window_seconds", 90),
        }

        meters_option = options.get("meters")
        if not meters_option:
            if "meter_id" not in options:
                raise ValueError("No meters configured. Add at least one meter under the meters option.")
            meters_option = [
                {
                    "id": int(options["meter_id"]),
                    "name": str(options.get("meter_name", "Utility Meter")),
                }
            ]
        meters = {
            meter.meter_id: meter
            for meter in (MeterConfig.from_options(item, meter_defaults) for item in meters_option)
        }
        if not meters:
            raise ValueError("At least one meter must be configured")

        mqtt_host = (
            os.environ.get("MQTT_SERVICE_HOST")
            or options.get("mqtt_host")
            or "core-mosquitto"
        )
        mqtt_port = int(
            os.environ.get("MQTT_SERVICE_PORT")
            or options.get("mqtt_port")
            or 1883
        )
        mqtt_username = os.environ.get("MQTT_SERVICE_USERNAME") or options.get("mqtt_username")
        mqtt_password = os.environ.get("MQTT_SERVICE_PASSWORD") or options.get("mqtt_password")

        scan_centers = as_int_list(options.get("scan_centers_hz"))
        lock_center = int(options.get("lock_center_hz", 911000000))
        if lock_center not in scan_centers:
            scan_centers.insert(0, lock_center)

        return cls(
            meters=meters,
            lock_center_hz=lock_center,
            lock_sample_rate=int(options.get("lock_sample_rate", 262144)),
            lock_symbol_length=int(options.get("lock_symbol_length", 8)),
            tuner_gain=float(options.get("tuner_gain", 40.2)),
            overload_restart_threshold=int(options.get("overload_restart_threshold", 3)),
            overload_min_rate_ratio=float(options.get("overload_min_rate_ratio", 0.85)),
            stale_seconds=int(options.get("stale_seconds", 300)),
            lock_restart_seconds=int(options.get("lock_restart_seconds", 3600)),
            scan_seconds=int(options.get("scan_seconds", 45)),
            scan_backoff_seconds=int(options.get("scan_backoff_seconds", 60)),
            scan_centers_hz=scan_centers,
            rtltcp_port=int(options.get("rtltcp_port", 1234)),
            sample_interval_seconds=int(options.get("sample_interval_seconds", 5)),
            retention_days=int(options.get("retention_days", 1095)),
            database_path=str(options.get("database_path", "/data/rtlamr_smart_samples.sqlite")),
            store_unchanged_samples=bool(options.get("store_unchanged_samples", True)),
            base_topic=str(options.get("base_topic", "rtlamr_smart")).strip("/"),
            discovery_prefix=str(options.get("discovery_prefix", "homeassistant")).strip("/"),
            discovery_interval=int(options.get("discovery_interval", 300)),
            retain_state=bool(options.get("retain_state", False)),
            force_update=bool(options.get("force_update", True)),
            log_level=str(options.get("log_level", "info")),
            mqtt_host=mqtt_host,
            mqtt_port=mqtt_port,
            mqtt_username=mqtt_username or None,
            mqtt_password=mqtt_password or None,
        )


@dataclass
class CenterMeterStats:
    center_hz: int
    meter_id: int
    sessions: int = 0
    hits: int = 0
    last_hit: float = 0.0
    last_reading: float | None = None
    ewma_hits_per_minute: float = 0.0

    def record_session(self, hits: int, duration_seconds: float, reading: float | None) -> None:
        self.sessions += 1
        self.hits += hits
        rate = hits * 60.0 / duration_seconds if duration_seconds > 0 else 0.0
        alpha = 0.35
        self.ewma_hits_per_minute = rate if self.sessions == 1 else (
            alpha * rate + (1.0 - alpha) * self.ewma_hits_per_minute
        )
        if hits:
            self.last_hit = time.time()
            self.last_reading = reading

    def score(self, now: float, stale: bool) -> float:
        freshness = 0.0
        if self.last_hit:
            freshness = max(0.0, 1.0 - ((now - self.last_hit) / 3600.0))
        multiplier = 3.0 if stale else 1.0
        return multiplier * self.ewma_hits_per_minute + freshness * 5.0

    def to_json(self) -> dict[str, Any]:
        return {
            "center_hz": self.center_hz,
            "meter_id": self.meter_id,
            "sessions": self.sessions,
            "hits": self.hits,
            "last_hit": self.last_hit,
            "last_reading": self.last_reading,
            "ewma_hits_per_minute": self.ewma_hits_per_minute,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "CenterMeterStats":
        return cls(
            center_hz=int(data["center_hz"]),
            meter_id=int(data["meter_id"]),
            sessions=int(data.get("sessions", 0)),
            hits=int(data.get("hits", 0)),
            last_hit=float(data.get("last_hit", 0.0)),
            last_reading=data.get("last_reading"),
            ewma_hits_per_minute=float(data.get("ewma_hits_per_minute", 0.0)),
        )


@dataclass
class MeterRuntime:
    meter_id: int
    last_seen: float = 0.0
    last_seen_iso: str | None = None
    last_reading: float | None = None
    last_raw: int | None = None
    last_center_hz: int = 0
    last_packet_type: str | None = None
    last_ert_type: int | None = None
    last_rejected_reason: str | None = None
    pending_reading: float | None = None
    pending_raw: int | None = None
    pending_count: int = 0
    pending_first_seen: float = 0.0
    accepted_packets: int = 0
    rejected_packets: int = 0
    frames_window: deque[float] = field(default_factory=lambda: deque(maxlen=2000))
    last_sample_time: float = 0.0
    last_sample_value: float | None = None

    def frames_per_minute(self) -> float:
        now = time.time()
        while self.frames_window and now - self.frames_window[0] > 60.0:
            self.frames_window.popleft()
        return float(len(self.frames_window))

    def is_stale(self, stale_seconds: int, now: float | None = None) -> bool:
        now = now or time.time()
        if not self.last_seen:
            return True
        return now - self.last_seen >= stale_seconds

    def to_json(self) -> dict[str, Any]:
        return {
            "meter_id": self.meter_id,
            "last_seen": self.last_seen,
            "last_seen_iso": self.last_seen_iso,
            "last_reading": self.last_reading,
            "last_raw": self.last_raw,
            "last_center_hz": self.last_center_hz,
            "last_packet_type": self.last_packet_type,
            "last_ert_type": self.last_ert_type,
            "last_rejected_reason": self.last_rejected_reason,
            "pending_reading": self.pending_reading,
            "pending_raw": self.pending_raw,
            "pending_count": self.pending_count,
            "pending_first_seen": self.pending_first_seen,
            "accepted_packets": self.accepted_packets,
            "rejected_packets": self.rejected_packets,
            "last_sample_time": self.last_sample_time,
            "last_sample_value": self.last_sample_value,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "MeterRuntime":
        return cls(
            meter_id=int(data["meter_id"]),
            last_seen=float(data.get("last_seen", 0.0)),
            last_seen_iso=data.get("last_seen_iso"),
            last_reading=data.get("last_reading"),
            last_raw=data.get("last_raw"),
            last_center_hz=int(data.get("last_center_hz", 0)),
            last_packet_type=data.get("last_packet_type"),
            last_ert_type=data.get("last_ert_type"),
            last_rejected_reason=data.get("last_rejected_reason"),
            pending_reading=data.get("pending_reading"),
            pending_raw=data.get("pending_raw"),
            pending_count=int(data.get("pending_count", 0)),
            pending_first_seen=float(data.get("pending_first_seen", 0.0)),
            accepted_packets=int(data.get("accepted_packets", 0)),
            rejected_packets=int(data.get("rejected_packets", 0)),
            last_sample_time=float(data.get("last_sample_time", 0.0)),
            last_sample_value=data.get("last_sample_value"),
        )


@dataclass
class RuntimeState:
    meters: dict[int, MeterRuntime] = field(default_factory=dict)
    center_stats: dict[tuple[int, int], CenterMeterStats] = field(default_factory=dict)
    current_center_hz: int = 0
    mode: str = "starting"
    last_state_save: float = 0.0

    def ensure_meter(self, meter_id: int) -> MeterRuntime:
        if meter_id not in self.meters:
            self.meters[meter_id] = MeterRuntime(meter_id=meter_id)
        return self.meters[meter_id]

    def ensure_center_meter(self, center_hz: int, meter_id: int) -> CenterMeterStats:
        key = (center_hz, meter_id)
        if key not in self.center_stats:
            self.center_stats[key] = CenterMeterStats(center_hz=center_hz, meter_id=meter_id)
        return self.center_stats[key]

    def stale_meter_ids(self, meter_ids: list[int], stale_seconds: int) -> list[int]:
        now = time.time()
        return [
            meter_id
            for meter_id in meter_ids
            if self.ensure_meter(meter_id).is_stale(stale_seconds, now)
        ]

    def to_json(self) -> dict[str, Any]:
        return {
            "current_center_hz": self.current_center_hz,
            "mode": self.mode,
            "meters": [meter.to_json() for meter in self.meters.values()],
            "center_stats": [stat.to_json() for stat in self.center_stats.values()],
        }

    @classmethod
    def load(cls) -> "RuntimeState":
        if not STATE_PATH.exists():
            return cls()
        try:
            with STATE_PATH.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            state = cls(
                current_center_hz=int(data.get("current_center_hz", 0)),
                mode=str(data.get("mode", "starting")),
            )
            for item in data.get("meters", []):
                meter = MeterRuntime.from_json(item)
                state.meters[meter.meter_id] = meter
            for item in data.get("center_stats", []):
                stat = CenterMeterStats.from_json(item)
                state.center_stats[(stat.center_hz, stat.meter_id)] = stat
            return state
        except Exception:
            logging.exception("Could not load saved state; starting fresh")
            return cls()

    def save(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = STATE_PATH.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(self.to_json(), handle, indent=2, sort_keys=True)
        tmp_path.replace(STATE_PATH)
        self.last_state_save = time.time()


class SampleStore:
    def __init__(self, config: Config):
        self.config = config
        self.path = Path(config.database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                ts_iso TEXT NOT NULL,
                meter_id INTEGER NOT NULL,
                reading REAL NOT NULL,
                raw_reading INTEGER,
                source_packet_ts REAL,
                source_packet_ts_iso TEXT,
                center_hz INTEGER,
                frames_per_minute REAL,
                packet_age_seconds REAL,
                stale INTEGER NOT NULL,
                mode TEXT NOT NULL
            )
            """
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_meter_ts ON samples (meter_id, ts)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples (ts)")
        self.conn.commit()
        self.last_prune = 0.0

    def close(self) -> None:
        self.conn.close()

    def insert_sample(self, meter: MeterRuntime, mode: str, stale_seconds: int) -> None:
        if meter.last_reading is None:
            return
        now = time.time()
        packet_age = now - meter.last_seen if meter.last_seen else None
        stale = 1 if packet_age is None or packet_age >= stale_seconds else 0
        self.conn.execute(
            """
            INSERT INTO samples (
                ts, ts_iso, meter_id, reading, raw_reading, source_packet_ts,
                source_packet_ts_iso, center_hz, frames_per_minute,
                packet_age_seconds, stale, mode
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                utc_now_iso(now),
                meter.meter_id,
                meter.last_reading,
                meter.last_raw,
                meter.last_seen or None,
                meter.last_seen_iso,
                meter.last_center_hz or None,
                meter.frames_per_minute(),
                packet_age,
                stale,
                mode,
            ),
        )
        self.conn.commit()

    def prune_if_needed(self) -> None:
        now = time.time()
        if now - self.last_prune < 3600:
            return
        cutoff = now - (self.config.retention_days * 86400)
        cursor = self.conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        self.conn.commit()
        self.last_prune = now
        if cursor.rowcount:
            logging.info("Pruned %s old sample rows", cursor.rowcount)


class MqttPublisher:
    def __init__(self, config: Config, state: RuntimeState):
        self.config = config
        self.state = state
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="rtlamr-smart-reader",
        )
        if config.mqtt_username:
            self.client.username_pw_set(config.mqtt_username, config.mqtt_password)
        self.client.will_set(self.status_topic, "offline", retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.last_discovery = 0.0

    @property
    def status_topic(self) -> str:
        return f"{self.config.base_topic}/status"

    def state_topic(self, meter_id: int) -> str:
        return f"{self.config.base_topic}/{meter_id}/state"

    def attributes_topic(self, meter_id: int) -> str:
        return f"{self.config.base_topic}/{meter_id}/attributes"

    def connect(self) -> None:
        logging.info("Connecting to MQTT broker %s:%s", self.config.mqtt_host, self.config.mqtt_port)
        self.client.connect(self.config.mqtt_host, self.config.mqtt_port, keepalive=60)
        self.client.loop_start()

    def stop(self) -> None:
        try:
            self.client.publish(self.status_topic, "offline", retain=True)
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            logging.exception("Error while stopping MQTT client")

    def _on_connect(self, client: mqtt.Client, _userdata: Any, _flags: Any, reason_code: Any, _properties: Any) -> None:
        code = getattr(reason_code, "value", reason_code)
        if int(code) == 0:
            logging.info("Connected to MQTT")
            client.publish(self.status_topic, "online", retain=True)
            client.subscribe("homeassistant/status")
            self.publish_discovery(force=True)
        else:
            logging.error("MQTT connect failed: %s", reason_code)

    def _on_message(self, _client: mqtt.Client, _userdata: Any, message: mqtt.MQTTMessage) -> None:
        if message.topic == "homeassistant/status" and message.payload.decode(errors="ignore") == "online":
            logging.info("Home Assistant birth message received; republishing discovery")
            self.publish_discovery(force=True)

    def publish_discovery(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self.last_discovery < self.config.discovery_interval:
            return
        self.last_discovery = now
        for meter in self.config.meters.values():
            self._publish_meter_discovery(meter)
        logging.info("Published MQTT discovery for %s meter(s)", len(self.config.meters))

    def _publish_meter_discovery(self, meter: MeterConfig) -> None:
        device = {
            "identifiers": [f"rtlamr_smart_{meter.meter_id}"],
            "name": meter.name,
            "manufacturer": "Itron/Schlumberger",
            "model": "ERT",
            "sw_version": "rtlamr-smart-reader 0.2.0",
        }
        availability = {
            "availability_topic": self.status_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
        }
        sensors = [
            (
                "reading",
                {
                    "name": None,
                    "object_id": meter.slug,
                    "unique_id": f"rtlamr_smart_{meter.meter_id}_reading",
                    "state_topic": self.state_topic(meter.meter_id),
                    "value_template": "{{ value_json.reading }}",
                    "unit_of_measurement": meter.unit_of_measurement,
                    "device_class": meter.device_class,
                    "state_class": meter.state_class,
                    "force_update": self.config.force_update,
                },
            ),
            (
                "last_seen",
                {
                    "name": "Last seen",
                    "unique_id": f"rtlamr_smart_{meter.meter_id}_last_seen",
                    "state_topic": self.state_topic(meter.meter_id),
                    "value_template": "{{ value_json.last_seen }}",
                    "device_class": "timestamp",
                    "entity_category": "diagnostic",
                },
            ),
            (
                "packet_age",
                {
                    "name": "Packet age",
                    "unique_id": f"rtlamr_smart_{meter.meter_id}_packet_age",
                    "state_topic": self.state_topic(meter.meter_id),
                    "value_template": "{{ value_json.packet_age_seconds }}",
                    "unit_of_measurement": "s",
                    "device_class": "duration",
                    "entity_category": "diagnostic",
                },
            ),
            (
                "center_mhz",
                {
                    "name": "Receiver center",
                    "unique_id": f"rtlamr_smart_{meter.meter_id}_center_mhz",
                    "state_topic": self.state_topic(meter.meter_id),
                    "value_template": "{{ value_json.center_mhz }}",
                    "unit_of_measurement": "MHz",
                    "device_class": "frequency",
                    "entity_category": "diagnostic",
                },
            ),
            (
                "frames_per_minute",
                {
                    "name": "Frames per minute",
                    "unique_id": f"rtlamr_smart_{meter.meter_id}_frames_per_minute",
                    "state_topic": self.state_topic(meter.meter_id),
                    "value_template": "{{ value_json.frames_per_minute }}",
                    "unit_of_measurement": "frames/min",
                    "entity_category": "diagnostic",
                },
            ),
            (
                "mode",
                {
                    "name": "Receiver mode",
                    "unique_id": f"rtlamr_smart_{meter.meter_id}_mode",
                    "state_topic": self.state_topic(meter.meter_id),
                    "value_template": "{{ value_json.mode }}",
                    "entity_category": "diagnostic",
                },
            ),
            (
                "rejected_packets",
                {
                    "name": "Rejected packets",
                    "unique_id": f"rtlamr_smart_{meter.meter_id}_rejected_packets",
                    "state_topic": self.state_topic(meter.meter_id),
                    "value_template": "{{ value_json.rejected_packets }}",
                    "entity_category": "diagnostic",
                },
            ),
        ]
        for key, payload in sensors:
            payload.update(availability)
            payload["device"] = device
            topic = f"{self.config.discovery_prefix}/sensor/rtlamr_smart_{meter.meter_id}_{key}/config"
            self.client.publish(topic, json.dumps(payload), retain=True)

    def publish_sample(self, meter_config: MeterConfig, meter: MeterRuntime, mode: str) -> None:
        now = time.time()
        packet_age = now - meter.last_seen if meter.last_seen else None
        payload = {
            "reading": meter.last_reading,
            "raw_reading": meter.last_raw,
            "last_seen": meter.last_seen_iso,
            "packet_age_seconds": round(packet_age, 1) if packet_age is not None else None,
            "center_hz": meter.last_center_hz or self.state.current_center_hz,
            "center_mhz": round((meter.last_center_hz or self.state.current_center_hz) / 1_000_000.0, 6)
            if (meter.last_center_hz or self.state.current_center_hz)
            else None,
            "frames_per_minute": round(meter.frames_per_minute(), 1),
            "mode": mode,
            "accepted_packets": meter.accepted_packets,
            "rejected_packets": meter.rejected_packets,
            "last_rejected_reason": meter.last_rejected_reason,
            "pending_reading": meter.pending_reading,
            "pending_count": meter.pending_count,
            "sample_time": utc_now_iso(now),
        }
        attrs = {
            "meter_id": meter_config.meter_id,
            "protocol": meter_config.protocol,
            "packet_type": meter.last_packet_type,
            "ert_type": meter.last_ert_type,
            "multiplier": meter_config.multiplier,
            "value_round": meter_config.value_round,
            "max_delta": meter_config.max_delta,
            "max_rate_per_hour": meter_config.max_rate_per_hour,
            "min_confirmations": meter_config.min_confirmations,
            "confirmation_window_seconds": meter_config.confirmation_window_seconds,
            "adaptive_stats": [
                stat.to_json()
                for (center, meter_id), stat in sorted(self.state.center_stats.items())
                if meter_id == meter_config.meter_id
            ],
        }
        self.client.publish(self.state_topic(meter_config.meter_id), json.dumps(payload), retain=self.config.retain_state)
        self.client.publish(self.attributes_topic(meter_config.meter_id), json.dumps(attrs), retain=False)


class ProcessSession:
    def __init__(self, config: Config, center_hz: int):
        self.config = config
        self.center_hz = center_hz
        self.lines: queue.Queue[tuple[str, str]] = queue.Queue()
        self.processes: list[subprocess.Popen[str]] = []

    def start(self) -> None:
        rtl_tcp_args = [
            "rtl_tcp",
            "-a",
            "127.0.0.1",
            "-p",
            str(self.config.rtltcp_port),
            "-f",
            str(self.center_hz),
            "-s",
            str(self.config.lock_sample_rate),
            "-g",
            str(self.config.tuner_gain),
        ]
        logging.info("Starting rtl_tcp at %.6f MHz", self.center_hz / 1_000_000.0)
        rtl_tcp = subprocess.Popen(
            rtl_tcp_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.processes.append(rtl_tcp)
        self._start_reader("rtl_tcp_stdout", rtl_tcp.stdout)
        self._start_reader("rtl_tcp_stderr", rtl_tcp.stderr)
        time.sleep(2.0)

        rtlamr_args = [
            "rtlamr",
            "-format=json",
            f"-msgtype={self.config.protocol_filter}",
            f"-server=127.0.0.1:{self.config.rtltcp_port}",
            f"-centerfreq={self.center_hz}",
            f"-samplerate={self.config.lock_sample_rate}",
            f"-symbollength={self.config.lock_symbol_length}",
            "-tunergainmode=true",
            f"-tunergain={self.config.tuner_gain}",
            f"-filterid={self.config.meter_filter}",
        ]
        logging.info("Starting rtlamr for meters %s", self.config.meter_filter)
        rtlamr = subprocess.Popen(
            rtlamr_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.processes.append(rtlamr)
        self._start_reader("rtlamr_stdout", rtlamr.stdout)
        self._start_reader("rtlamr_stderr", rtlamr.stderr)

    def _start_reader(self, name: str, stream: Any) -> None:
        if stream is None:
            return

        def read_stream() -> None:
            try:
                for line in stream:
                    self.lines.put((name, line.rstrip()))
            except Exception:
                logging.debug("Reader thread for %s stopped", name, exc_info=True)

        thread = threading.Thread(target=read_stream, name=f"reader-{name}", daemon=True)
        thread.start()

    def stop(self) -> None:
        for proc in reversed(self.processes):
            if proc.poll() is None:
                proc.terminate()
        deadline = time.time() + 4.0
        for proc in reversed(self.processes):
            while proc.poll() is None and time.time() < deadline:
                time.sleep(0.1)
            if proc.poll() is None:
                proc.kill()
        self.processes.clear()


class SmartReader:
    def __init__(self, config: Config):
        self.config = config
        self.state = RuntimeState.load()
        self.stop_event = threading.Event()
        self.store = SampleStore(config)
        for meter_id in config.meter_ids:
            self.state.ensure_meter(meter_id)
            for center in config.scan_centers_hz:
                self.state.ensure_center_meter(center, meter_id)
        self.publisher = MqttPublisher(config, self.state)
        self.last_sample_tick = 0.0

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        self.publisher.connect()
        self.publisher.publish_discovery(force=True)
        self.state.current_center_hz = self.state.current_center_hz or self.config.lock_center_hz
        self.state.save()

        try:
            while not self.stop_event.is_set():
                self.state.mode = "lock"
                reason = self.run_session(
                    center_hz=self.state.current_center_hz,
                    duration_seconds=self.config.lock_restart_seconds,
                    stale_meter_ids=None,
                )
                if self.stop_event.is_set():
                    break
                if reason == "stale":
                    stale_ids = self.state.stale_meter_ids(self.config.meter_ids, self.config.stale_seconds)
                    logging.warning("Stale meters %s; entering scan mode", stale_ids)
                    found_center = self.scan_for_meters(stale_ids)
                    if found_center is None:
                        logging.warning(
                            "Scan did not reacquire stale meters; backing off for %s seconds",
                            self.config.scan_backoff_seconds,
                        )
                        self._sleep(self.config.scan_backoff_seconds)
                    else:
                        self.state.current_center_hz = found_center
                        self.state.save()
                else:
                    logging.info("Restarting lock receiver after reason=%s", reason)
        finally:
            self.state.mode = "stopped"
            self.publish_samples(force=True)
            self.state.save()
            self.store.close()
            self.publisher.stop()

    def ordered_scan_centers(self, stale_ids: list[int]) -> list[int]:
        now = time.time()
        stale_set = set(stale_ids)

        def center_score(center: int) -> float:
            total = 0.0
            for meter_id in self.config.meter_ids:
                stat = self.state.ensure_center_meter(center, meter_id)
                total += stat.score(now, meter_id in stale_set)
            if center == self.config.lock_center_hz:
                total += 1.0
            return total

        return sorted(list(dict.fromkeys(self.config.scan_centers_hz)), key=center_score, reverse=True)

    def best_lock_center(self, prefer_center: int | None = None) -> int:
        now = time.time()
        centers = list(dict.fromkeys(self.config.scan_centers_hz))

        def score(center: int) -> float:
            total = 0.0
            recently_heard = 0
            for meter_id in self.config.meter_ids:
                stat = self.state.ensure_center_meter(center, meter_id)
                total += stat.score(now, stale=False)
                if stat.last_hit and now - stat.last_hit <= max(self.config.stale_seconds * 2, 60):
                    recently_heard += 1
            total += recently_heard * 20.0
            if center == self.state.current_center_hz:
                total += 2.0
            if prefer_center is not None and center == prefer_center:
                total += 1.0
            if center == self.config.lock_center_hz:
                total += 0.5
            return total

        return max(centers, key=score)

    def scan_for_meters(self, stale_ids: list[int]) -> int | None:
        self.state.mode = "scan"
        self.publish_samples(force=True)
        wanted = set(stale_ids or self.config.meter_ids)
        best_center: int | None = None
        best_hits = 0
        for center in self.ordered_scan_centers(list(wanted)):
            if self.stop_event.is_set():
                return None
            previous_hits = {
                meter_id: self.state.ensure_center_meter(center, meter_id).hits
                for meter_id in wanted
            }
            logging.info("Scanning %.6f MHz for meters %s", center / 1_000_000.0, sorted(wanted))
            reason = self.run_session(
                center_hz=center,
                duration_seconds=self.config.scan_seconds,
                stale_meter_ids=list(wanted),
            )
            new_hits = 0
            found_here: set[int] = set()
            for meter_id in wanted:
                stat = self.state.ensure_center_meter(center, meter_id)
                delta = stat.hits - previous_hits[meter_id]
                if delta > 0:
                    found_here.add(meter_id)
                    new_hits += delta
            if new_hits > best_hits:
                best_center = center
                best_hits = new_hits
            wanted -= found_here
            if found_here:
                logging.info("Found meters %s at %.6f MHz", sorted(found_here), center / 1_000_000.0)
            if not wanted:
                lock_center = self.best_lock_center(prefer_center=center)
                logging.info("All stale meters reacquired; locking at %.6f MHz", lock_center / 1_000_000.0)
                return lock_center
            logging.info(
                "Scan center %.6f MHz ended with reason=%s; still looking for %s",
                center / 1_000_000.0,
                reason,
                sorted(wanted),
            )
        if best_center is not None:
            return self.best_lock_center(prefer_center=best_center)
        return None

    def run_session(
        self,
        center_hz: int,
        duration_seconds: int,
        stale_meter_ids: list[int] | None,
    ) -> str:
        session = ProcessSession(self.config, center_hz=center_hz)
        start = time.time()
        hits_by_meter: dict[int, int] = {meter_id: 0 for meter_id in self.config.meter_ids}
        readings_by_meter: dict[int, float | None] = {meter_id: None for meter_id in self.config.meter_ids}
        wanted = set(stale_meter_ids or [])
        overload_errors = 0
        self.state.current_center_hz = center_hz
        self.publish_samples(force=True)

        try:
            session.start()
            while not self.stop_event.is_set():
                now = time.time()
                if duration_seconds > 0 and now - start >= duration_seconds:
                    return "duration"
                if stale_meter_ids is None and self.should_leave_lock_mode(start):
                    return "stale"
                if wanted and all(not self.state.ensure_meter(meter_id).is_stale(self.config.stale_seconds) for meter_id in wanted):
                    return "hit"

                self.publisher.publish_discovery()
                self.publish_samples()

                try:
                    source, line = session.lines.get(timeout=1.0)
                except queue.Empty:
                    continue
                if not line:
                    continue
                if source == "rtlamr_stdout":
                    packet = self.parse_packet_line(line)
                    if packet is None:
                        continue
                    meter_id = int(packet["Message"]["ID"])
                    reading = self.handle_packet(packet, center_hz)
                    if reading is not None:
                        hits_by_meter[meter_id] += 1
                        readings_by_meter[meter_id] = reading
                else:
                    if "not keeping up with rtl_tcp" in line:
                        severe = self.is_severe_overload(line)
                        if severe:
                            overload_errors += 1
                        else:
                            overload_errors = 0
                        self.log_overload_line(source, line, severe, overload_errors)
                        if severe and (
                            self.config.overload_restart_threshold > 0
                            and overload_errors >= self.config.overload_restart_threshold
                        ):
                            logging.warning(
                                "Restarting receiver after %s rtl_tcp overload warning(s)",
                                overload_errors,
                            )
                            return "overload"
                        continue
                    self.log_process_line(source, line)
        finally:
            elapsed = max(0.1, time.time() - start)
            for meter_id in self.config.meter_ids:
                self.state.ensure_center_meter(center_hz, meter_id).record_session(
                    hits_by_meter[meter_id],
                    elapsed,
                    readings_by_meter[meter_id],
                )
            self.publish_samples(force=True)
            self.state.save()
            session.stop()
        return "stopped"

    def should_leave_lock_mode(self, session_start: float) -> bool:
        now = time.time()
        for meter_id in self.config.meter_ids:
            meter = self.state.ensure_meter(meter_id)
            last_seen = meter.last_seen or session_start
            if now - last_seen >= self.config.stale_seconds:
                return True
        return False

    def parse_packet_line(self, line: str) -> dict[str, Any] | None:
        if not line.startswith("{"):
            logging.debug("rtlamr stdout: %s", line)
            return None
        try:
            packet = json.loads(line)
        except json.JSONDecodeError:
            logging.debug("Could not parse rtlamr JSON line: %s", line)
            return None
        meter_id = int(packet.get("Message", {}).get("ID", -1))
        if meter_id not in self.config.meters:
            logging.debug("Ignoring non-target meter id %s", meter_id)
            return None
        return packet

    def handle_packet(self, packet: dict[str, Any], center_hz: int) -> float | None:
        message = packet.get("Message", {})
        meter_id = int(message.get("ID"))
        meter_config = self.config.meters[meter_id]
        runtime = self.state.ensure_meter(meter_id)
        raw_reading = int(message.get("Consumption", 0))
        reading = round(raw_reading * meter_config.multiplier, meter_config.value_round)
        now = time.time()

        reject_reason = self.validate_reading(meter_config, runtime, reading, now)
        if reject_reason:
            runtime.rejected_packets += 1
            runtime.last_rejected_reason = reject_reason
            logging.warning(
                "Rejected meter %s reading=%s reason=%s raw=%s",
                meter_id,
                reading,
                reject_reason,
                raw_reading,
            )
            return None

        if not self.confirm_reading(meter_config, runtime, reading, raw_reading, now):
            logging.debug(
                "Pending meter %s reading=%s confirmation %s/%s",
                meter_id,
                reading,
                runtime.pending_count,
                meter_config.min_confirmations,
            )
            return None

        runtime.last_seen = now
        runtime.last_seen_iso = utc_now_iso(now)
        runtime.last_reading = reading
        runtime.last_raw = raw_reading
        runtime.last_center_hz = center_hz
        runtime.last_packet_type = packet.get("Type")
        runtime.last_ert_type = message.get("Type")
        runtime.accepted_packets += 1
        runtime.frames_window.append(now)
        runtime.last_rejected_reason = None
        runtime.pending_reading = None
        runtime.pending_raw = None
        runtime.pending_count = 0
        runtime.pending_first_seen = 0.0
        stat = self.state.ensure_center_meter(center_hz, meter_id)
        stat.last_hit = now
        stat.last_reading = reading

        if now - self.state.last_state_save > 30:
            self.state.save()

        logging.info(
            "Meter %s reading=%s %s center=%.6f MHz frames/min=%.1f",
            meter_id,
            reading,
            meter_config.unit_of_measurement,
            center_hz / 1_000_000.0,
            runtime.frames_per_minute(),
        )
        return reading

    def confirm_reading(
        self,
        meter_config: MeterConfig,
        runtime: MeterRuntime,
        reading: float,
        raw_reading: int,
        now: float,
    ) -> bool:
        if meter_config.min_confirmations <= 1 or runtime.last_reading == reading:
            return True
        pending_fresh = now - runtime.pending_first_seen <= meter_config.confirmation_window_seconds
        if runtime.pending_reading == reading and pending_fresh:
            runtime.pending_count += 1
        else:
            runtime.pending_reading = reading
            runtime.pending_raw = raw_reading
            runtime.pending_count = 1
            runtime.pending_first_seen = now
        return runtime.pending_count >= meter_config.min_confirmations

    def overload_rate_from_line(self, line: str) -> int | None:
        match = re.search(r"\brate=(\d+)\b", line)
        if not match:
            return None
        return int(match.group(1))

    def is_severe_overload(self, line: str) -> bool:
        reported_rate = self.overload_rate_from_line(line)
        if reported_rate is None:
            return True
        minimum_rate = self.config.lock_sample_rate * self.config.overload_min_rate_ratio
        return reported_rate < minimum_rate

    def log_overload_line(self, source: str, line: str, severe: bool, count: int) -> None:
        reported_rate = self.overload_rate_from_line(line)
        if reported_rate is None:
            logging.warning("%s: rtlamr overload warning without rate: %s", source, line)
            return
        ratio = reported_rate / max(self.config.lock_sample_rate, 1)
        if severe:
            logging.warning(
                "%s: severe rtlamr overload rate=%s target=%s ratio=%.2f count=%s",
                source,
                reported_rate,
                self.config.lock_sample_rate,
                ratio,
                count,
            )
            return
        logging.debug(
            "%s: mild rtlamr catch-up warning rate=%s target=%s ratio=%.2f",
            source,
            reported_rate,
            self.config.lock_sample_rate,
            ratio,
        )

    def validate_reading(
        self,
        meter_config: MeterConfig,
        runtime: MeterRuntime,
        reading: float,
        now: float,
    ) -> str | None:
        previous = runtime.last_reading
        if previous is None:
            return None
        delta = reading - previous
        if delta < 0 and not meter_config.allow_decrease:
            return "decrease"
        if delta <= 0:
            return None
        if meter_config.max_delta >= 0 and delta > meter_config.max_delta:
            return f"delta>{meter_config.max_delta}"
        if meter_config.max_rate_per_hour >= 0 and runtime.last_seen:
            elapsed = max(now - runtime.last_seen, 0.001)
            rate = delta * 3600.0 / elapsed
            if rate > meter_config.max_rate_per_hour:
                return f"rate>{meter_config.max_rate_per_hour}/h"
        return None

    def publish_samples(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self.last_sample_tick < self.config.sample_interval_seconds:
            return
        self.last_sample_tick = now
        for meter_id, meter_config in self.config.meters.items():
            runtime = self.state.ensure_meter(meter_id)
            if runtime.last_reading is None:
                continue
            if (
                not self.config.store_unchanged_samples
                and runtime.last_sample_value == runtime.last_reading
                and not force
            ):
                self.publisher.publish_sample(meter_config, runtime, self.state.mode)
                continue
            runtime.last_sample_time = now
            runtime.last_sample_value = runtime.last_reading
            self.store.insert_sample(runtime, self.state.mode, self.config.stale_seconds)
            self.publisher.publish_sample(meter_config, runtime, self.state.mode)
        self.store.prune_if_needed()

    def log_process_line(self, source: str, line: str) -> None:
        if "not keeping up with rtl_tcp" in line:
            logging.debug("%s: %s", source, line)
            return
        if "[R82XX] PLL not locked!" in line:
            logging.debug("%s: %s", source, line)
            return
        if "ERROR" in line or "Failed" in line or "error" in line.lower():
            logging.warning("%s: %s", source, line)
            return
        logging.debug("%s: %s", source, line)

    def _sleep(self, seconds: int) -> None:
        deadline = time.time() + seconds
        while not self.stop_event.is_set() and time.time() < deadline:
            self.publisher.publish_discovery()
            self.publish_samples(force=True)
            time.sleep(min(5.0, max(0.1, deadline - time.time())))


def configure_logging(level: str) -> None:
    numeric = {
        "trace": logging.DEBUG,
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }.get(level.lower(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> int:
    if not OPTIONS_PATH.exists():
        print(f"Options file not found: {OPTIONS_PATH}", file=sys.stderr)
        return 2
    config = Config.load()
    configure_logging(config.log_level)
    logging.info("Starting RTLAMR Smart Reader for meters %s", config.meter_filter)
    logging.info("Protocol filter %s", config.protocol_filter)
    logging.info("Lock center %.6f MHz", config.lock_center_hz / 1_000_000.0)
    logging.info(
        "Sample interval %ss retention %s days db=%s",
        config.sample_interval_seconds,
        config.retention_days,
        config.database_path,
    )

    reader = SmartReader(config)

    def handle_signal(signum: int, _frame: Any) -> None:
        logging.info("Received signal %s, stopping", signum)
        reader.stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        reader.run()
    except Exception:
        logging.exception("Fatal reader error")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
