# RTLAMR Smart Reader

Adaptive Home Assistant add-on for reading multiple Itron ERT meters with an RTL-SDR and publishing MQTT discovery sensors.

It defaults to a lock center of `910.5 MHz`, which matched local testing for the target meter bank. Configure your own meter endpoint IDs before starting the add-on.

It samples the latest confirmed reading every `sample_interval_seconds` seconds, stores retained raw samples in SQLite, and scans the configured frequency list only when one or more configured meters go stale.

Optional daily email reports can summarize the previous midnight-to-midnight day with per-meter usage tables and PNG charts for hourly usage, same-day cumulative usage, and trailing daily usage.

The add-on also exposes a Home Assistant ingress dashboard for interactive cumulative-reading charts, usage bars, hover tooltips, zooming, and CSV export.

See `DOCS.md` for installation and configuration details.
