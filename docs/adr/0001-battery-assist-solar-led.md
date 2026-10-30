# ADR 0001: Battery Assist is solar-led, with the battery capped by a share

- Status: accepted (owner decision, 2026-10-30).
- Scope: the Tesla appliance in PV Excess Control and `automation.tesla_dropdown_mode_controller`.

## Context
There are two modes for charging the Tesla on a sunny day, and neither does what the owner wants.

- **Solar Only** charges only while spare solar covers the car's whole draw. A passing cloud lowers the current, and then stops the car.
  - On 2026-10-30, solar fell from 9.6 to 4.2 kW between 12:25 and 12:45 UTC, and the car was shed at 12:45.
  - In the meantime the home battery covered the gap anyway (100 → 97%), but only by accident. Two rules caused it: the 5-minute current deferral, and the 59-minute switch cooldown.
- **Battery Assist** switches PV Excess Control off for the Tesla. It starts at 22 A and steers the current on the grid CT, so it charges at 5–7 kW with no sun at all, night included, until the home battery falls below `input_number.battery_assist_threshold` (61%).
  - The battery covers everything the sun doesn't. That drains the battery that the evening and the cheap-tariff plan rely on.

The owner wants this: when the sun still covers most of the car's draw, the battery may fill the rest, but only down to a floor, and the owner sets both numbers.

## Decision
Battery Assist becomes a solar-led mode that runs inside PV Excess Control.

**Rule.** It applies when Battery Assist is on for the appliance, max share > 0, and battery SoC ≥ the floor:

    share = 1 − max_share / 100            (max_share 30% → share 0.70)
    allowed car power = solar available for the car / share

The rule is applied in two places in `optimizer.py`, through one helper, `_battery_assist_factor`:
- at the start gate, in `_allocate_dynamic_current`, on `min_watts_needed`;
- when running, in `_shed_dynamic_current`, on `available_power`.

The current is stepped and clamped as today. The car stops when solar falls below share × the minimum-current power, or when SoC drops below the floor, with 2% hysteresis. `min_battery_soc` and battery discharge protection still take precedence.

**Knobs.** All three are owned by the integration, live on the appliance's device, and are restored across restarts:

| Entity | Range | Default |
|---|---|---|
| `number.pv_excess_control_tesla_battery_assist_max_share` | 0–90 %, step 5 | 30 |
| `number.pv_excess_control_tesla_battery_assist_min_soc` | 20–95 %, step 1 | Seeded once from `input_number.battery_assist_threshold` (61) |
| `switch.pv_excess_control_tesla_battery_assist` | on/off | off |

Max share is capped at 90%, so the sun always covers at least 10% of the draw, and the formula never divides by zero. At 0%, or with the switch off, the behaviour is exactly today's Solar Only.

**Dropdown.**
- **Battery Assist:**
  - turns `switch.pv_excess_control_tesla_enabled` and the assist switch on;
  - keeps the "below floor, not starting" notice, which now reads the min-SoC number;
  - drops its own 22 A start and its 5-minute grid-CT current loop.
- **Every other mode:** turns the assist switch off.
- **Unchanged:** Solar Only, Charge at Night, Solar + Night Buffer and Charge Now.

**Retired.** These are removed once the dropdown no longer references them:
- `input_number.battery_assist_threshold`;
- the dead `input_boolean.tesla_battery_assist_50` and its unavailable automation.

## Alternatives considered
- **Top-up inside Solar Only.** Rejected: the owner wants Solar Only to stay strictly solar, and Battery Assist to be the mode where the battery helps.
- **A sixth dropdown mode.** Rejected: it would add a mode while Battery Assist kept its night-draining behaviour.
- **HA `input_number` helpers for the share and floor.** Rejected: the knobs belong to the appliance they control, and having them in the integration keeps them with its device, restore logic and tests.
- **Leaving the gap to the deferral and cooldown.** Rejected: the battery's share is then accidental and unbounded, and there is no floor.

## Consequences
- In Battery Assist, the battery's contribution is bounded: at 30% it is about 0.4 kW at 6 A and about 2.2 kW at 32 A, and nothing is drawn from the battery without sun.
- "Charge from the battery at night" is no longer available as a mode. Charge Now (grid) and Charge at Night cover charging regardless of sun.
- The amps loop moves from YAML into the integration, so there is one controller for the car's current instead of two.
- Monitoring after deploy covers:
  - the status reads "Battery assist: X% solar, ~Y kW from battery (SoC Z% ≥ floor)";
  - the battery share stays at or below the max share;
  - the car stops at the floor;
  - no charging after sunset;
  - no flapping at the floor.
