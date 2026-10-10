# APsystems Modbus for Home Assistant

Local integration for an APsystems ECU and its microinverters over Modbus TCP.
One device for the ECU with the system totals, one device per microinverter
with its own measurements. Local only, no cloud connection.

**Status: 0.2.** Reading and the lifetime counter for the Energy dashboard
work and are tested against an ECU-R. Export limit control is the next
milestone.

## Supported hardware

According to APsystems' SunSpec Modbus guide, support depends on the ECU
model and firmware. The first digits of the ECU serial number tell the model:

| ECU | Serial starts with | SunSpec Modbus | Status with this integration |
|---|---|---|---|
| ECU-R | 2160 | from firmware 1.3.7 | Tested (firmware 1.3.23, DS3 microinverters) |
| ECU-R Pro | 2162 | from firmware 2.0.2 | Expected to work, not tested |
| ECU-C | 215 | from firmware C1.1.3 | Expected to work, not tested |
| ECU-B | 2163 | not supported by APsystems | Not supported |
| EZ1 microinverters | — | — | Use the core `apsystems` integration |

Supported ECUs carry a SunSpec logo on the device. Microinverter models other
than DS3 (QT2, DS3D, YC600, …) should work as long as the ECU serves them over
Modbus; only DS3 has been verified. If you have an untested ECU or inverter,
please open an issue with a register dump made with `tools/dump_registers.py`
(see Troubleshooting).

## Before you start

1. Connect the ECU with an **Ethernet cable**. Modbus TCP is only served on
   the wired port, not over Wi-Fi. Give it a fixed IP address in your router.
2. **Enable Modbus on the ECU.** Modbus is off by default and the setting is
   only reachable through the ECU's own access point with the *EMA Manager*
   app (the installer app, not the EMA app for owners):
   - Press the **AP button** on the side of the ECU. The ECU starts a Wi-Fi
     network named like `ECU_R_2160…`; connect your phone to it (default
     password `88888888` unless it was changed).
   - Open EMA Manager and choose **Local Access** on the login screen; no
     installer account is needed.
   - Go to *Workspace → Modbus settings* (on the ECU-C: *Modbus
     Configuration*), switch Modbus on, give every inverter an address
     (1, 2, 3, …) in the table and save. The baud rate only matters for RS485
     and can be ignored; the TCP port is always 502.
   - Reconnect your phone to your normal Wi-Fi afterwards.
3. Note that with Modbus enabled the ECU is controlled over Modbus. APsystems
   features such as Dynamic Tariff stop working; it is one or the other.
4. Make sure **nothing else polls the ECU over Modbus** (for example another
   Home Assistant integration or a script). The ECU is a small device and
   overloads easily.
5. Home Assistant 2026.10.0 or newer.

## Installation

[![Open your Home Assistant instance and open this repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=christianvaes&repository=homeassistant-apsystems_modbus&category=integration)

Click the button above to open the repository in HACS, then choose *Download*
and restart Home Assistant. Without the button: in HACS add
`christianvaes/homeassistant-apsystems_modbus` as a custom repository of type
*Integration*. Or copy `custom_components/apsystems_modbus` into your
`config/custom_components/` and restart.

## Setup

When the ECU is on the same network, Home Assistant discovers it and shows it
under *Settings → Devices & services*. Accept it, confirm the inverters that
were found and you are done.

Otherwise add the integration manually with the button below, or via
*Settings → Devices & services → Add integration → APsystems Modbus*.

[![Open your Home Assistant instance and start setting up APsystems Modbus.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=apsystems_modbus)

| Field | Meaning |
|---|---|
| Host | IP address of the ECU |
| Port | 502 unless you changed it |
| Inverter addresses | Leave empty to search automatically, or enter the addresses from EMA Manager, for example `1-8` or `1, 2, 5` |

The confirmation screen lists the inverters found per address with their
serial numbers; compare it with the table in EMA Manager. Two optional fields:

- **ECU ID** from the label, used for the device name.
- **Energy produced so far**: the total energy shown in the EMA app, in kWh.
  The lifetime counter continues from there. Leave it empty to start at 0.

Settings (*Configure* on the integration):

- **Poll interval**, default 300 s. The ECU refreshes its values about every
  5 minutes, so polling faster adds load without new data.
- **Lifetime energy** and **peak power**, shown with their current values.
  Change them only to correct the counters; leaving them as they are changes
  nothing.

## Energy dashboard

Use **Lifetime energy** of the ECU device as *Solar production* under
*Settings → Dashboards → Energy*.

Over Modbus the ECU only reports a daily counter per inverter, which it resets
at night. The integration turns those into a lifetime total that never goes
down:

- A drop of a daily counter counts as a new day only once the counter runs up
  again from the low value, and a large jump only once the next poll confirms
  it. Single wrong readings, low or high, are ignored.
- An inverter that does not answer keeps its share; nothing is lost while it
  is offline.
- The total is stored, so it survives restarts, reconfiguring, and removing and
  adding the integration again. When you add the same ECU again, the stored
  total is offered as the starting value.

Enter your starting value when you set up the integration; that way the Energy
dashboard starts cleanly. A correction later under *Configure*, up or down,
shows up once in the Energy dashboard in the hour you made it. Adjust that hour
under *Developer tools → Statistics → Lifetime energy*.

## What you get

**ECU device:** power, energy today, lifetime energy, peak power, inverters
online, last data update (diagnostic), grid connection (diagnostic, disabled by
default).

**Per inverter:** power, energy today, AC voltage, AC current, frequency,
temperature, status, event code, DC voltage/current/power per channel, online.
Disabled by default: lifetime energy per inverter, apparent power, reactive
power, power factor and DC channels the inverter model does not have.

Entity names are English for everyone, so entity IDs are the same in every
installation. Device names can be changed in Home Assistant.

## Behaviour and limitations

- Values change only when the ECU has fetched new data from the inverters,
  about every 5 minutes.
- At night inverters are offline: voltage and frequency read 0 and *online*
  is off. Energy today keeps yesterday's value through the night and starts
  the new day when the inverters produce again in the morning.
- An inverter that does not answer becomes unavailable on its own; the others
  keep updating. If the ECU stops answering altogether, polling slows down
  automatically and resumes at the normal interval once it answers again. No
  restart is needed.
- Power and inverters online count the inverters that answered in the last
  poll; energy today and lifetime energy keep the share of one that did not.
- Daily totals can differ about 1 % from what EMA shows.
- Not available over Modbus: ECU firmware version, Zigbee signal strength, ECU
  restart.
- The grid connection register is never written by this integration.

## Troubleshooting

| Message | Cause | What to do |
|---|---|---|
| The ECU refused the connection | Modbus is off, or wrong port | Enable Modbus in EMA Manager (Workspace → Modbus settings) and assign addresses |
| No answer from the ECU | Wrong IP, ECU on Wi-Fi only, or not reachable | Check the IP address and connect the ECU by cable |
| No inverter answered | Inverters have no address yet | Assign addresses in EMA Manager or enter them manually |
| The ECU stopped answering during the search | Another Modbus client or an overloaded ECU | Stop other clients, wait five minutes and try again |
| Register layout differs | Unknown ECU or inverter type | Share a register dump in an issue |

Debug logging: *Settings → Devices & services → APsystems Modbus → Enable debug
logging*. Diagnostics can be downloaded from the same menu; they contain no
addresses or serial numbers.

A register dump for an issue:

```bash
python3 tools/dump_registers.py --host <ecu-ip> --units 1 2 --probe-unit 9 --out .
```

It reads two inverters and one empty address, writes nothing, and stores a
JSON file with the raw registers. Replace the serial numbers before sharing.

## Removing the integration

Delete the integration under *Settings → Devices & services*. Its devices and
entities are removed with it. Only the lifetime total and peak power stay in a
small file in `.storage`, so they can continue when you add the same ECU again.

## License

Apache-2.0.
