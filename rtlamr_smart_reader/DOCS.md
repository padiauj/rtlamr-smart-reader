# RTLAMR Smart Reader

## What It Does

This add-on runs `rtl_tcp` and `rtlamr` inside Home Assistant OS, decodes configured Itron ERT utility meters, publishes Home Assistant MQTT discovery sensors, and keeps its own raw sample database.

It starts in lock mode at `910.5 MHz`. If any configured meter goes quiet for `stale_seconds`, it enters scan mode and tests the configured center frequencies. It learns per-meter/per-frequency hit rates over time, then chooses the lock center that best covers the configured meters with the fewest frequency changes. Learned state is saved in `/data/rtlamr_smart_state.json`.

## Requirements

- Home Assistant OS or Supervised installation with add-ons/apps
- Mosquitto Broker add-on or another MQTT service exposed to Supervisor
- RTL-SDR USB dongle passed through to the Home Assistant VM
- An Itron ERT-compatible meter

## Meter Configuration

The add-on ships with placeholder meter configuration. Replace `id: 0` with your own ERT endpoint IDs before starting it.

- Default protocol: `scm`
- Lock center: `910500000`
- Sample rate: `1048576`
- Symbol length: `32`
- Gain: `40.2`

Edit `meters:` in the add-on options for the exact meters you want to track. Remove any placeholder or candidate ID that never appears in the logs, otherwise the reader will keep scanning for it.

Example:

```yaml
meters:
  - id: 12345678
    name: "Electric Meter"
    protocol: "scm"
    unit_of_measurement: "kWh"
    device_class: "energy"
    state_class: "total_increasing"
    multiplier: 1.0
    value_round: 3
    allow_decrease: false
    max_delta: 1000.0
    max_rate_per_hour: 1000.0
    min_confirmations: 2
    confirmation_window_seconds: 90
  - id: 23456789
    name: "Gas Meter"
    protocol: "scm"
    unit_of_measurement: "CCF"
    device_class: "gas"
    state_class: "total_increasing"
    multiplier: 1.0
    value_round: 3
    allow_decrease: false
    max_delta: 10000.0
    max_rate_per_hour: 10000.0
    min_confirmations: 2
    confirmation_window_seconds: 90
```

## Sensors

The add-on publishes MQTT discovery for each configured meter:

- Main cumulative reading
- Last seen timestamp
- Packet age
- Receiver center frequency
- Frames per minute
- Receiver mode
- Receiver status
- Last receiver error
- Consecutive receiver failure count
- Rejected packet count

The electric default is configured as `device_class: energy` and `state_class: total_increasing`, so it can be used in the Home Assistant Energy dashboard.

## Interactive Dashboard

The add-on exposes a Home Assistant ingress page named **RTLAMR**. Open it from the add-on page or the Home Assistant sidebar to explore retained SQLite samples without leaving Home Assistant.

The dashboard includes:

- Current reading, today usage, last-30-day usage, latest sample time, receiver center, and frames per minute.
- A cumulative-reading chart with ranges for today, 24 hours, 7 days, 30 days, 90 days, 1 year, and all retained samples.
- Hover tooltips, mouse-wheel zoom, drag-to-zoom, reset zoom, and CSV export for the selected cumulative range.
- Usage bar charts for hourly usage today, daily usage over the last 31 days, and monthly usage over the last 12 months.

Long ranges are downsampled by the add-on API before they reach the browser, so the page remains responsive even when the SQLite store contains years of 5-second samples.

## Recovery Behavior

The add-on is designed to keep running through ordinary radio, USB, process, MQTT, and storage failures:

- If a meter goes stale but the receiver is still healthy, the reader enters scan mode and tests the configured centers.
- If `rtl_tcp` or `rtlamr` exits unexpectedly, the current receiver session is stopped and restarted.
- If the SDR path reports USB or tuner failures such as `usb_open error`, `rtlsdr_write_reg failed`, `r82xx_init: failed`, `Failed to set sample rate`, or `receiver connect`, the reader classifies the session as a receiver failure instead of a frequency miss.
- Receiver failures use exponential backoff, starting at `receiver_failure_backoff_seconds` and capped by `receiver_failure_backoff_max_seconds`, so a loose dongle or missing USB passthrough does not hot-loop.
- A successful confirmed packet clears the consecutive receiver failure count and returns the diagnostic status to `receiving`.
- MQTT startup connection failures are retried. If MQTT restarts later, the client reconnects and republishes discovery.
- SQLite insert or prune errors are logged but do not stop radio reception or MQTT publishing.

The receiver recovery options are:

- `receiver_startup_timeout_seconds`: how long to wait for `rtl_tcp` to tune before treating startup as failed.
- `receiver_failure_backoff_seconds`: initial retry delay after receiver hardware/process failures.
- `receiver_failure_backoff_max_seconds`: maximum retry delay after repeated receiver failures.

## Sampling And Storage

Home Assistant is good at current sensors, dashboards, and long-term statistics, but it is not a great place to keep 5-second raw data for years. The recorder integration defaults to purging raw history after 10 days, and Home Assistant long-term statistics are downsampled rather than raw 5-second rows:

- Recorder docs: <https://www.home-assistant.io/integrations/recorder/>
- Statistics data docs: <https://data.home-assistant.io/docs/statistics/>
- Sensor device class docs: <https://developers.home-assistant.io/docs/core/entity/sensor/>

This add-on therefore does both:

- Publishes MQTT sensors so Home Assistant can show live readings and build normal long-term statistics.
- Stores raw samples locally in `/data/rtlamr_smart_samples.sqlite`.

The defaults are:

- `sample_interval_seconds: 5`
- `retention_days: 1095`
- `database_path: "/data/rtlamr_smart_samples.sqlite"`
- `store_unchanged_samples: true`

At 5-second sampling, each meter produces about 17,280 rows per day, 6.3 million rows per year, and 18.9 million rows over three years. If disk space becomes more important than perfectly regular samples, set `store_unchanged_samples: false`; the add-on will still publish MQTT updates but only writes changed readings to SQLite.

Old rows are pruned hourly based on `retention_days`, which acts as a time-based circular buffer.

## Daily Email Reports

The add-on can send an optional daily email report with one section per configured meter. Reports are disabled by default. When enabled, each report covers the previous completed local calendar day, from midnight to midnight, even if the email is sent the next morning.

Each meter section includes:

- Total usage for the reported day.
- Start and end cumulative readings.
- Sample count, stale-sample percentage, and frames-per-minute receiver health.
- A PNG hourly-usage chart for the reported day.
- A PNG cumulative-usage chart for the reported day.
- A PNG daily-usage chart for the trailing `daily_report_month_days` days.

Example:

```yaml
daily_report_enabled: true
daily_report_time: "06:30"
daily_report_timezone: "America/New_York"
daily_report_recipients:
  - "you@example.com"
  - "someone_else@example.com"
daily_report_sender: ""
smtp_host: "smtp.example.com"
smtp_port: 587
smtp_username: "rtlamr@example.com"
smtp_password: "your-app-password"
smtp_starttls: true
smtp_ssl: false
```

Use `daily_report_time: "00:05"` for a just-after-midnight report, or a morning time such as `"06:30"` to avoid nuisance overnight email. If the add-on or mail server is unavailable at the scheduled time, it retries every `daily_report_retry_seconds` seconds and remembers the last successfully sent report date in `/data/rtlamr_smart_report_state.json`.

Leave `daily_report_timezone` blank to use the add-on's `TZ` environment setting, or set it explicitly to a timezone such as `America/New_York`.

Leave `daily_report_sender` blank to use `smtp_username` as the From address, which is usually what Gmail expects.

The report generator reads from the add-on SQLite database and uses accepted retained samples, so the same confirmation and plausibility filters used for Home Assistant state also protect the report calculations. Email failures are logged but do not stop radio reception, MQTT publishing, or SQLite sampling.

To test delivery without waiting until tomorrow, set `daily_report_time` to a time earlier than the current local time and restart the add-on. It will send the previous day's report unless that report date is already marked as sent in `daily_report_state_path`.

## Spurious Reading Handling

The reader does not store every decoded packet immediately. For each meter it:

- Requires a new reading to repeat `min_confirmations` times within `confirmation_window_seconds`.
- Rejects decreases unless `allow_decrease` is true.
- Rejects jumps larger than `max_delta`.
- Rejects rates above `max_rate_per_hour`.
- Samples the latest accepted reading at the configured interval.

For electric meters, `min_confirmations: 2` is usually cheap because the same value is broadcast many times per minute. If a gas endpoint is very sparse, lower that meter to `min_confirmations: 1`.

## Tuning Notes

The C1SR/R300 meter family frequency-hops. The add-on does not try to predict the exact next hop. Instead it uses the least-moving strategy that worked in local testing:

1. Stay locked at the best aggregate center.
2. Track hit rates per configured meter and center frequency.
3. Watch for stale reception per meter.
4. Scan only when one or more meters are stale.
5. Prefer centers that recently produced packets for the stale meter while still scoring centers that cover all configured meters.

If one configured center gets strong packet rates, the reader will keep using it and only scan again when one or more meters go stale.

The default `1048576`/`32` profile is intentionally wider than the lowest-CPU `262144`/`8` profile because it was the profile that reliably decoded the local C1SR meter bank. For narrow low-CPU profiles such as `262144`/`8`, the center frequency matters more than it does with the default. A receiver centered at `911.0 MHz` no longer covers traffic at both `910.5 MHz` and `911.5 MHz`; use `910500000` or `911500000` directly, or let scan mode test both.

The `lock_sample_rate` and `lock_symbol_length` settings should be kept as a matched pair. `rtlamr` uses a 32768 symbols/second data rate and only accepts specific symbol lengths. The practical pairs for this add-on are:

- `262144` sample rate with symbol length `8`
- `1048576` sample rate with symbol length `32`

Do not use `524288` with symbol length `16`; current `rtlamr` rejects `16` as an invalid symbol length.

If you see repeated `not keeping up with rtl_tcp` messages, compare the reported `rate=` to `lock_sample_rate`. A warning near the target rate, such as `260096` when configured for `262144`, is usually harmless. A much lower rate, such as half the target, means the VM is falling behind the SDR stream. The add-on counts only severe overloads, controlled by `overload_min_rate_ratio`, and restarts the receiver after `overload_restart_threshold` severe warnings in one session.

`[R82XX] PLL not locked!` is common during tuner startup or retune. Occasional messages are not a problem by themselves.

If you upgraded from an earlier add-on version, Home Assistant may keep your previous options. Check the add-on configuration screen and manually use the verified `lock_sample_rate: 1048576` and `lock_symbol_length: 32` profile for the C1SR meter bank. Version `0.2.6` and newer will fall back to `1048576`/`32` when the configured receiver profile is not supported by `rtlamr`.

Also check `lock_center_hz`. If it still shows `911000000`, set it to `910500000` and make sure `scan_centers_hz` starts with `910500000` and `911500000`.

## Troubleshooting

If the add-on cannot open the RTL-SDR:

- Confirm the dongle is passed through to the Home Assistant VM.
- In the HA SSH terminal, `lsusb` should show a Realtek RTL-SDR device such as `0bda:2838`; seeing only VirtualBox devices means the VM does not have the dongle yet.
- Stop any other add-on using the same RTL-SDR.
- Check the add-on log for `rtl_tcp` errors.

If Home Assistant does not show sensors:

- Confirm the Mosquitto Broker add-on and MQTT integration are working.
- Restart this add-on after MQTT is online.
- Check MQTT topics under `rtlamr_smart/#`.

If readings are sparse:

- Move the antenna closer to the meter bank.
- Try raising `stale_seconds`.
- Add more center frequencies to `scan_centers_hz`.
- Try `lock_center_hz: 910500000` or `911500000`.
- Watch the `Receiver center`, `Frames per minute`, and `Rejected packets` diagnostic sensors for each meter.
