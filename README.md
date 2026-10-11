# APsystems Modbus for Home Assistant

[![Release](https://img.shields.io/github/v/release/christianvaes/homeassistant-apsystems_modbus)](https://github.com/christianvaes/homeassistant-apsystems_modbus/releases)
[![HACS](https://img.shields.io/badge/HACS-custom-41BDF5)](https://hacs.xyz)
[![CI](https://github.com/christianvaes/homeassistant-apsystems_modbus/actions/workflows/ci.yml/badge.svg)](https://github.com/christianvaes/homeassistant-apsystems_modbus/actions/workflows/ci.yml)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2026.10%2B-18BCF2)](https://www.home-assistant.io)
[![Maintained](https://img.shields.io/badge/Maintained-yes-brightgreen)](https://github.com/christianvaes/homeassistant-apsystems_modbus/commits/main)
[![Donate](https://img.shields.io/badge/Donate-PayPal-00457C)](https://www.paypal.com/donate/?business=FAQ9J5JDQY7NW&no_recurring=1&currency_code=EUR)

Fully local Home Assistant integration for solar panels with APsystems
microinverters and an ECU, over Modbus TCP.

Features:

- **One device for the whole system (the ECU)** with current solar power,
  energy today, lifetime energy for the Energy dashboard, highest power ever
  measured and inverters online.
- **One device per microinverter** with its own power, energy today, voltage,
  current, frequency, temperature, DC values per panel, status and event code.
- **Control: power limit.** Curtail the output of all inverters, in percent
  or in kW, by hand or from an automation. With a dynamic tariff you can go
  to zero export at negative prices and back to full power when they recover.
- **Setup**: the ECU is discovered on your network and the inverters are found
  automatically.

Unofficial, not affiliated with APsystems. Use at your own risk. The power
limit changes what your inverters do, so read that section first.

## Supported hardware

| ECU | Serial starts with | Modbus from firmware | Status |
|---|---|---|---|
| ECU-R | 2160 | 1.3.7 | Tested (1.3.23, DS3 inverters) |
| ECU-R Pro | 2162 | 2.0.2 | Expected to work, not tested |
| ECU-C | 215 | C1.1.3 | Expected to work, not tested |
| ECU-B | 2163 | not supported by APsystems | Not supported |

Supported ECUs have a SunSpec logo on the device. Other inverter models should
work but only DS3 is tested. For the EZ1, use the core `apsystems` integration.
Have an untested ECU or inverter? Open an issue with a register dump (see
Troubleshooting).

## Before you start

1. Connect the ECU with an **Ethernet cable** and give it a fixed IP address.
   Modbus only works on the wired port.
2. **Enable Modbus on the ECU.** It is off by default. The setting is in the
   *EMA Manager* app, which normally needs an installer account. Without one,
   use the ECU's local access point:
   - Install EMA Manager on your phone.
   - Press the **AP button** on the side of the ECU and connect your phone to
     the Wi-Fi network it starts (`ECU_R_2160…`, default password `88888888`).
   - Open EMA Manager and choose **Local Access**.
   - Go to *Workspace → Modbus settings* (in some app versions *Modbus
     Configuration*), switch Modbus on, give every inverter an address (1, 2,
     3, …) and save.
3. With Modbus on, APsystems features such as Dynamic Tariff stop working.
4. Let **nothing else talk to the ECU over Modbus**. In tests a second
   connection made the ECU stop answering the integration.
5. Home Assistant 2026.10.0 or newer.

## Installation and setup

[![Open your Home Assistant instance and open this repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=christianvaes&repository=homeassistant-apsystems_modbus&category=integration)

Open the repository in HACS with the button, choose *Download* and restart
Home Assistant. Or in HACS, open the menu → *Custom repositories*, enter
`https://github.com/christianvaes/homeassistant-apsystems_modbus` and choose
type *Integration*.

Home Assistant discovers the ECU on your network. Accept it under *Settings →
Devices & services*, or add it yourself:

[![Open your Home Assistant instance and start setting up APsystems Modbus.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=apsystems_modbus)

| Field | Meaning |
|---|---|
| Host | IP address of the ECU |
| Port | 502 |
| Inverter addresses | The Modbus addresses from EMA Manager, like `1-8` or `1, 2, 5`. Leave empty to find them automatically. |
| Lifetime energy (optional) | Your total from the EMA app in kWh. It counts on from there. Leave empty to start at 0. |
| Installation power (optional) | Output of all inverters together in kW, for example 5.8 for eight DS3-L of 730 W. Not the peak power of the panels. Only for one inverter type. |

Under *Configure* you can change the poll interval, enter the installation
power and correct the lifetime energy and lifetime peak power. A correction
shows up once as production in that hour. Fix that under
*Developer tools → Statistics*.

## Energy dashboard

Add **Lifetime energy** of the ECU device as solar production under
*Settings → Dashboards → Energy*.

## Power limit

**Power limit** on the ECU device curtails the output of all inverters, in
whole percent. 100 is no limit. Set it by hand or with `number.set_value`.

- Inverters follow a change within a few minutes, in tests up to about 15.
- At 0 each inverter still delivers about 20 to 30 W.
- If a change does not reach the ECU, you get a message and the integration
  tries again by itself.
- An inverter sometimes misses a change, and a restart of the ECU clears the
  limit. So the integration sends the limit again 15 and 75 minutes after a
  change, and when it notices that the ECU was off. For a few minutes it
  uses one percent more or less, otherwise the ECU does not pass it on.
- The limit stays when Home Assistant stops, and the integration picks it up
  again when it starts.
- With the installation power entered, a second control sets the same limit
  in kW. Below about 10 % this is an estimate.
- The integration then also notices an inverter that delivers clearly more
  than the limit allows, for example after a restart of the ECU it did not
  see, and sends the limit again. If that does not help, a notice under
  *Settings → Repairs* asks you to check the installation power. This margin
  was measured on DS3-L inverters.
- If an inverter stays limited, set the limit to 100. If that does not help,
  switch the ECU off for a minute. In our tests this cleared the limit on all
  inverters.

Example: no export at negative feed-in prices. Replace the entity IDs with
yours.

```yaml
automation:
  - alias: Solar panels and negative price
    triggers:
      - trigger: numeric_state
        entity_id: sensor.feed_in_price
        below: 0
        id: "off"
      - trigger: numeric_state
        entity_id: sensor.feed_in_price
        above: 0
        id: "on"
    actions:
      - action: number.set_value
        target:
          entity_id: number.apsystems_ecu_power_limit
        data:
          value: "{{ 0 if trigger.id == 'off' else 100 }}"
```

## Good to know

- Values change when the ECU fetches new data from the inverters over Zigbee,
  about every 5 minutes. Daily totals can differ about 1 % from the EMA app.
- At night inverters read 0 V and 0 Hz. Energy today follows the ECU, which
  resets it during the night.
- A silent inverter becomes unavailable, but the totals keep its share.
  If the ECU stops answering, the integration tries again after two minutes,
  then less often, and recovers by itself.

## Troubleshooting

| Message | What to do |
|---|---|
| The ECU refused the connection | Modbus is off: enable it in EMA Manager and assign addresses |
| No answer from the ECU | Check the IP address and the Ethernet cable |
| No inverter answered | Assign addresses in EMA Manager or enter them manually |
| The ECU stopped answering | Stop other Modbus clients, wait five minutes, try again |
| Register layout differs | Share a register dump in an issue |

**Reporting an issue:** add the diagnostics from the menu of the integration.
They contain no addresses or serial numbers. Turn on debug logging in the same
menu if you can reproduce the problem.

For an untested ECU, also add a register dump. Disable the integration first,
because a second Modbus connection disturbs the ECU. Then run on a computer in
your network:

```bash
python3 tools/dump_registers.py --host <ecu-ip> --units 1 2 --probe-unit 9 --out .
```

It only reads. Replace the serial numbers in the JSON file before sharing, and
enable the integration again.

## Removing the integration

Set the power limit to 100 and check that all inverters deliver power again.
Then delete the integration under *Settings → Devices & services*.
The lifetime total is kept, so it continues when you add the same ECU again.

## Support the project

If this integration is useful to you:

- **Star this repository** on GitHub
- [Buy me a coffee](https://www.paypal.com/donate/?business=FAQ9J5JDQY7NW&no_recurring=1&currency_code=EUR)

## License

Apache-2.0. Unofficial and not affiliated with APsystems.
