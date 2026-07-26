# Changelog

## 0.3.1

- Use `smtp_username` as the daily email report sender when `daily_report_sender` is blank, matching common Gmail SMTP setup.

## 0.3.0

- Add optional daily SMTP email reports with per-meter summary tables and PNG plots.
- Schedule reports by local time while summarizing the previous completed midnight-to-midnight day.
- Include hourly usage, same-day cumulative usage, trailing daily usage, sample counts, stale percentage, and receiver frame-rate health.
- Retry failed email sends without stopping radio reception, MQTT publishing, or SQLite sampling.

## 0.2.6

- Detect RTL-SDR USB/tuner failures from `rtl_tcp`, `rtlamr`, and startup logs, then back off and retry instead of treating them as ordinary meter silence.
- Detect unexpected `rtl_tcp`/`rtlamr` process exits and restart the receiver session cleanly.
- Add configurable receiver startup timeout and exponential receiver-failure backoff settings.
- Add MQTT diagnostic sensors for receiver status, last receiver error, and consecutive receiver failures.
- Retry MQTT startup connections and keep radio reception alive when SQLite sample writes or pruning fail.
- Reduce log volume by throttling repeated identical meter-reading info messages during healthy reception.
- Change new-install and invalid-profile fallback defaults to the verified `1048576`/`32` receiver profile.

## 0.2.5

- Wait for `rtl_tcp` to report that it is listening before starting `rtlamr`, avoiding false `receiver connect` failures on slow USB/tuner startup.

## 0.2.4

- Reject unsupported `rtlamr` receiver profiles and fall back to the valid `262144`/`8` profile.
- Removed stale documentation that suggested the invalid `524288`/`16` profile.
- Updated add-on text to match the current `910.5 MHz` default.

## 0.2.3

- Changed the default lock center to `910.5 MHz` and scan order to try `910.5 MHz` and `911.5 MHz` before `911.0 MHz`.
- Honor the configured lock center on startup instead of reusing a stale saved center.
- Start with scan mode when configured meters have never been seen.
- Removed the experimental add-on stage flag.

## 0.2.2

- Changed new-install defaults to the lower-CPU `262144` samples/second profile with symbol length `8`.
- Added rate-aware overload classification so near-target `rtlamr` catch-up warnings do not trigger restarts.
- Added `overload_min_rate_ratio` to tune severe overload detection.

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
