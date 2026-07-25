# Changelog

## 0.2.1

- Lowered the default receiver profile to `524288` samples/second with symbol length `16` for Home Assistant VMs.
- Added receiver restart after repeated `rtlamr` overload warnings.
- Documented `not keeping up with rtl_tcp` and tuner PLL startup messages.

## 0.2.0

- Added multiple configured meters.
- Added per-meter adaptive frequency scoring and aggregate lock-center selection.
- Added 5-second configurable sampling to a retained SQLite database.
- Added `retention_days` pruning as a time-based circular buffer.
- Added per-meter plausibility filters and repeated-reading confirmation.
- Added extra MQTT diagnostics for packet age and rejected packets.

## 0.1.0

- Initial adaptive `rtlamr` reader.
- MQTT discovery for cumulative meter reading and diagnostics.
- Lock mode plus stale-triggered scan mode.
