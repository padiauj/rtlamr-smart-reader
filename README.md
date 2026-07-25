# RTLAMR Smart Reader

Home Assistant add-on for reading Itron/Schlumberger ERT utility meters with an RTL-SDR.

## Add-ons

- `rtlamr_smart_reader`: Adaptive multi-meter `rtlamr` reader with MQTT discovery, stale-triggered frequency scanning, robust sampling, and a local SQLite retention store.

## Install From GitHub

In Home Assistant, open **Settings -> Add-ons -> Add-on Store**, use the three-dot menu, choose **Repositories**, and add:

```text
https://github.com/padiauj/rtlamr-smart-reader
```

Then install **RTLAMR Smart Reader** from the add-on store.

## Local Install

Copy this repository folder into Home Assistant's `/addons` directory, then open **Settings -> Add-ons -> Add-on Store**, use the three-dot menu to check for updates, and install **RTLAMR Smart Reader** from the local add-ons section.

Pass the RTL-SDR USB device through to the Home Assistant VM before starting the add-on. The add-on publishes Home Assistant sensors over MQTT and stores raw 5-second samples in its own retained `/data` SQLite database.
