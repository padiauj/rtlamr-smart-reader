# RTLAMR Smart Reader

## What It Does

This add-on runs `rtl_tcp` and `rtlamr` inside Home Assistant OS, decodes configured Itron ERT utility meters, publishes Home Assistant MQTT discovery sensors, and keeps its own raw sample database.

It starts in lock mode at `911.0 MHz`. If any configured meter goes quiet for `stale_seconds`, it enters scan mode and tests the configured center frequencies. It learns per-meter/per-frequency hit rates over time, then chooses the lock center that best covers the configured meters with the fewest frequency changes. Learned state is saved in `/data/rtlamr_smart_state.json`.

## Requirements

- Home Assistant OS or Supervised installation with add-ons/apps
- Mosquitto Broker add-on or another MQTT service exposed to Supervisor
- RTL-SDR USB dongle passed through to the Home Assistant VM
- An Itron ERT-compatible meter

## Meter Configuration

The add-on ships with placeholder meter configuration. Replace `id: 0` with your own ERT endpoint IDs before starting it.

- Default protocol: `scm`
- Lock center: `911000000`
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
- Rejected packet count

The electric default is configured as `device_class: energy` and `state_class: total_increasing`, so it can be used in the Home Assistant Energy dashboard.

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

## Troubleshooting

If the add-on cannot open the RTL-SDR:

- Confirm the dongle is passed through to the Home Assistant VM.
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
