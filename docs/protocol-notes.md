# Protocol notes: Miele XKM3000Z

## Confirmed endpoints and clusters

| Endpoint | Cluster | Meaning |
|---:|---:|---|
| 210 | `0x001B` | appliance control alias, readable/writeable for delay-start |
| 210 | `0x7D00` | appliance control direct/incoming alias |
| 210 | `0xFD02` | program/parameter write/read target |
| 210 | `0x7DFD` | incoming/report alias for program/parameter data |
| 210 | `0x0B02` | raw event/door cluster |
| 212 | `0x000A` | Zigbee Time Cluster |
| 213 | `0xFD01` | device/module info and capability metadata |
| 214 | `0xFD00` | capability descriptors |

## Fault recognition

`status_code=8` is confirmed as fault/alarm.

`phase_raw` is interpreted as three bytes:

```text
B0 = phase_a = phase_raw & 0xFF
B1 = phase_b = (phase_raw >> 8) & 0xFF
B2 = phase_c = (phase_raw >> 16) & 0xFF
```

`B0=0x0A` is confirmed as fault/alarm substate.

Water inlet fault candidate:

```text
status_code=8
B0=0x0A
B1=4
```

This is more precise than only saying “phase_b=4”. The specific internal F-code was not observed over Zigbee.

## Service and diagnostic observations

- Service mode appears as `status_code=12` and `B0=7`.
- `FD01/0x0020` exists but returns `0x8F` in normal operation, fault state and service test mode.
- No confirmed live exposure found for:
  - NTC temperature value,
  - float switch state,
  - separate door locked state,
  - heater relay state,
  - valve state,
  - pump state,
  - exact internal service F-code.

## Door events

Cluster `0x0B02` sends raw manufacturer-specific events.

Known patterns:

```text
... 00 01 00 = door closed
... 00 11 00 = door open
... 02 01 00 = door closed, alternate context
... 02 11 00 = door open, alternate context
```

## Temperature

The XKM3000Z does not expose a confirmed live temperature. The bridge publishes only `temperature_target_estimated`, derived from a lookup table by program.

## Water Plus and Short

- Water Plus bit is interpreted as `water_plus_available`, meaning the option is available for the current program.
- Short remains `short_option_candidate`; no reliable state change was observed.
