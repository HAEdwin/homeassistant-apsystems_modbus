# APsystems Modbus for Home Assistant

Local integration for an APsystems ECU and its microinverters over Modbus TCP.
One device for the ECU with the system totals and a power limit, one device
per microinverter with its own measurements.

Everything happens on your own network, between Home Assistant and the ECU,
using the SunSpec Modbus interface built into the ECU: no cloud, no account,
no app in between, and your data never leaves your home.

**Unofficial, use at your own risk.** This project is not made, supported or
endorsed by APsystems. It can change the power limit of your inverters; read
the *Power limit* section before you use that.

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
4. Make sure **nothing else talks to the ECU over Modbus** (another Home
   Assistant integration, a script, a second Modbus client). A second
   connection makes the ECU drop the first one, and the ECU is a small device
   that overloads easily.
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

**ECU device:** power limit (control), power, energy today, lifetime energy,
peak power, inverters online, last data update (diagnostic), grid connection
(diagnostic, disabled by default).

**Per inverter:** power, energy today, AC voltage, AC current, frequency,
temperature, status, event code, DC voltage/current/power per channel, online.
Disabled by default: lifetime energy per inverter, apparent power, reactive
power, power factor and DC channels the inverter model does not have.

Entity names are English for everyone, so entity IDs are the same in every
installation. Device names can be changed in Home Assistant.

## Power limit

**Power limit** on the ECU device limits the output of all inverters together,
in whole percent. 100 means no limit; the inverters then
deliver whatever the sun gives, and what you do not use yourself goes to the
grid. Set it by hand or from an automation with `number.set_value`.

- **It takes a few minutes.** The ECU talks to the inverters over Zigbee about
  every 5 minutes and passes a new limit on in that round; not every inverter
  gets it in the same round. In tests all inverters followed within 5 to about
  15 minutes. Plan your automations with that delay in mind.
- **Watch the power, not the status.** The inverters keep reporting *MPPT*
  while limited. The scale is not exactly linear at the low end: with 730 W
  DS3-L inverters, 10 % gave about 65 W and 5 % about 20 W per inverter. 0 % is the lowest setting; how much is
  left at 0 % has not been measured yet.
- **The slider shows what the ECU reports.** After you set it, it shows your
  value right away and the next poll confirms it. If the ECU does not answer,
  you get an error, the slider stays where it was, and the integration writes
  your value as soon as the ECU answers again; you do not need to repeat it.
  The attributes `desired` and `pending` show what is still being applied.
- **The integration keeps the ECU on your value.** The ECU sometimes reports a
  wrong limit for one poll; that is ignored. If it keeps reporting another
  value, your value is written again, at most once every 10 minutes.
- **After an ECU restart** the ECU may report 30 % without applying it. Set
  the slider once and it is in sync again.
- **Nothing reverts on its own.** The ECU has no fallback timer. If Home
  Assistant stops or restarts, or you reload the integration, the last limit
  stays in force and the integration only guards it again after you set it.
  Set the limit to 100 before removing the integration; removing also tries
  to release it, and tells you if that failed.
- A write normally takes a few seconds; up to about a minute when the ECU is
  silent.

Let automations set the slider rather than react to it. Two examples; replace
the entity id with yours (it follows the device name, for example
`number.ecu_2160xxxxxxxx_power_limit`). The first one only acts when the price
crosses zero, so combine it with the second one or add a time trigger if the
price can stay negative overnight.

```yaml
# Limit production while the feed-in price is negative, release afterwards.
automation:
  - alias: Solar power limit on negative price
    triggers:
      - trigger: numeric_state
        entity_id: sensor.feed_in_price
        below: 0
      - trigger: numeric_state
        entity_id: sensor.feed_in_price
        above: 0
    actions:
      - action: number.set_value
        target:
          entity_id: number.apsystems_ecu_power_limit
        data:
          value: "{{ 0 if states('sensor.feed_in_price') | float(0) < 0 else 100 }}"
```

```yaml
# Release the limit at sunset.
automation:
  - alias: Solar power limit release at sunset
    triggers:
      - trigger: sun
        event: sunset
    actions:
      - action: number.set_value
        target:
          entity_id: number.apsystems_ecu_power_limit
        data:
          value: 100
```

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
- The power limit is written through one inverter address; the ECU applies it
  to all inverters. The grid connection register is never written by this
  integration.

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

Set the power limit to 100 first. Then delete the integration under
*Settings → Devices & services*. Its devices and entities are removed with it;
if a limit is still active, the integration tries to release it and shows a
notification when that is not possible. Only the lifetime total and peak power
stay in a small file in `.storage`, so they can continue when you add the same
ECU again.

## Disclaimer

This software is provided "as is", without warranty of any kind, as stated in
the Apache-2.0 license. You use it at your own risk; the author is not liable
for any loss of production, damage or other consequences of using it,
including changes made to your ECU or inverters. APsystems is a trademark of
its owner; this project is not affiliated with APsystems.

## License

Apache-2.0.
