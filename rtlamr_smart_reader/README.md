# RTLAMR Smart Reader

Adaptive Home Assistant add-on for reading multiple Itron ERT meters with an RTL-SDR and publishing MQTT discovery sensors.

It defaults to a lock center of `911.0 MHz`, which is a useful starting point for many Itron ERT meter-bank scans. Configure your own meter endpoint IDs before starting the add-on.

It samples the latest confirmed reading every `sample_interval_seconds` seconds, stores retained raw samples in SQLite, and scans the configured frequency list only when one or more configured meters go stale.

See `DOCS.md` for installation and configuration details.
