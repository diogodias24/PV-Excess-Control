"""Tests for solar-led Battery Assist (ADR 0001).

Pure-logic optimizer cases first, then entity / coordinator / lifecycle cases.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.pv_excess_control.const import (
    CONF_BATTERY_ASSIST_APPLIANCES,
    CONF_BATTERY_ASSIST_MAX_SHARE,
    CONF_BATTERY_ASSIST_MIN_SOC,
    CONF_DYNAMIC_CURRENT,
    DOMAIN,
)
from custom_components.pv_excess_control.models import (
    Action,
    ApplianceConfig,
    ApplianceState,
    BatteryStrategy,
    BatteryTarget,
    Plan,
    PowerState,
    TariffInfo,
)
from custom_components.pv_excess_control.optimizer import Optimizer

V = 230
MIN_W = 6 * V  # 1380 W at 6 A, single phase


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _tesla(**kw) -> ApplianceConfig:
    base = dict(
        id="tesla", name="Tesla", entity_id="switch.tesla", priority=1, phases=1,
        nominal_power=7360.0, actual_power_entity=None, dynamic_current=True,
        current_entity="number.tesla_current", min_current=6.0, max_current=32.0,
        ev_soc_entity=None, ev_connected_entity=None, is_big_consumer=False,
        battery_max_discharge_override=None, on_only=False, min_daily_runtime=None,
        max_daily_runtime=None, schedule_deadline=None, switch_interval=300,
        allow_grid_supplement=False, max_grid_power=None, current_step=1.0,
        on_threshold=0, battery_assist_enabled=True, battery_assist_max_share=30.0,
        battery_assist_min_soc=61.0,
    )
    base.update(kw)
    return ApplianceConfig(**base)


def _state(is_on: bool = False, power: float = 0.0) -> ApplianceState:
    return ApplianceState(
        appliance_id="tesla", is_on=is_on, current_power=power, current_amperage=None,
        runtime_today=timedelta(), energy_today=0.0, last_state_change=None,
        ev_connected=None, ev_at_home=None, ev_charging=None, activations_today=0,
    )


def _power(excess: float, soc: float | None) -> PowerState:
    return PowerState(
        pv_production=5000.0, grid_export=max(excess, 0.0), grid_import=max(-excess, 0.0),
        load_power=5000.0 - excess, excess_power=excess, battery_soc=soc,
        battery_power=None, ev_soc=None, timestamp=datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc),
    )


def _plan() -> Plan:
    return Plan(
        created_at=datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc),
        horizon=timedelta(hours=12), entries=[],
        battery_target=BatteryTarget(
            target_soc=90.0, target_time=datetime(2026, 10, 31, 7, 0, tzinfo=timezone.utc),
            strategy=BatteryStrategy.BALANCED,
        ),
        confidence=0.8,
    )


def _tariff() -> TariffInfo:
    return TariffInfo(
        current_price=0.25, feed_in_tariff=0.08, cheap_price_threshold=0.10,
        battery_charge_price_threshold=0.10,
    )


def _run(opt, appliance, *, excess, soc, running_w=None, **kw):
    """One optimizer cycle; `running_w` = car draw if it is already charging."""
    power = _power(excess, soc)
    state = _state(is_on=running_w is not None, power=running_w or 0.0)
    result = opt.optimize(
        power_state=power, appliances=[appliance], appliance_states=[state],
        plan=_plan(), power_history=[power], tariff=_tariff(), **kw,
    )
    return result.decisions[0]


def _opt() -> Optimizer:
    return Optimizer(min_good_samples=1)


# ---------------------------------------------------------------------------
# optimizer: start gate
# ---------------------------------------------------------------------------

class TestStartGate:
    def test_starts_at_70_percent_share(self):
        # 1000 W of sun / 0.7 = 1428 W >= 1380 W: starts at 6 A
        d = _run(_opt(), _tesla(), excess=1000.0, soc=88.0)
        assert d.action == Action.SET_CURRENT
        assert d.target_current == 6.0

    def test_does_not_start_below_share_times_min(self):
        # share * 1380 = 966 W; 950 W of sun is not enough
        d = _run(_opt(), _tesla(), excess=950.0, soc=88.0)
        assert d.action == Action.IDLE
        assert d.reason.startswith("Battery assist: waiting for sun")

    def test_solar_only_would_not_start_at_1000w(self):
        d = _run(_opt(), _tesla(battery_assist_enabled=False), excess=1000.0, soc=88.0)
        assert d.action == Action.IDLE

    def test_start_current_is_available_over_share_stepped(self):
        # 2000 / 0.7 = 2857 W = 12.4 A -> 12 A
        d = _run(_opt(), _tesla(), excess=2000.0, soc=88.0)
        assert d.target_current == 12.0


# ---------------------------------------------------------------------------
# optimizer: running
# ---------------------------------------------------------------------------

class TestRunning:
    def test_running_current_is_available_over_share_stepped(self):
        # Car draws 2300 W (10 A). Sun available = excess + draw = -500 + 2300 = 1800 W.
        # 1800 / 0.7 = 2571 W = 11.18 A -> 11 A (solar only would be 7 A).
        d = _run(_opt(), _tesla(), excess=-500.0, soc=88.0, running_w=2300.0)
        assert d.action == Action.SET_CURRENT
        assert d.target_current == 11.0
        solar_only = _run(_opt(), _tesla(battery_assist_enabled=False),
                          excess=-500.0, soc=88.0, running_w=2300.0)
        assert solar_only.target_current == 7.0

    def test_running_current_clamped_to_max(self):
        d = _run(_opt(), _tesla(), excess=9000.0, soc=88.0, running_w=5000.0)
        assert d.target_current == 32.0

    def test_running_current_clamped_to_min_when_in_band(self):
        # Sun 1000 W >= 966 W (share * min): keeps running at the 6 A minimum
        d = _run(_opt(), _tesla(), excess=1000.0 - 1380.0, soc=88.0, running_w=1380.0)
        assert d.action == Action.SET_CURRENT
        assert d.target_current == 6.0

    @pytest.mark.parametrize("sun", [1000, 1300, 1800, 2500, 3200, 4000, 5500, 7000, 9000])
    def test_battery_share_never_exceeds_max_share(self, sun):
        draw = 3000.0
        d = _run(_opt(), _tesla(), excess=sun - draw, soc=88.0, running_w=draw)
        assert d.action == Action.SET_CURRENT
        total = d.target_current * V
        battery = max(total - sun, 0.0)
        assert battery <= 0.30 * total + 1e-6

    def test_stops_when_sun_below_share_times_min(self):
        # 900 W of sun < 966 W: car (at 6 A, 1380 W) stops
        d = _run(_opt(), _tesla(), excess=900.0 - 1380.0, soc=88.0, running_w=1380.0)
        assert d.action == Action.OFF

    def test_keeps_running_just_above_stop_threshold(self):
        d = _run(_opt(), _tesla(), excess=1000.0 - 1380.0, soc=88.0, running_w=1380.0)
        assert d.action == Action.SET_CURRENT

    def test_steady_state_does_not_shed_lower_priority(self):
        """The battery-covered part of the car's draw is not a deficit for others."""
        opt = _opt()
        car = _tesla()
        heater = _tesla(
            id="heater", name="Heater", priority=2, dynamic_current=False, current_entity=None,
            nominal_power=500.0, min_current=0.0, max_current=0.0, battery_assist_enabled=False,
        )
        # Car at 10 A (2300 W) is the steady state for 1610 W of sun: excess = 1610 - 2300 - 500
        power = _power(1610.0 - 2300.0 - 500.0, 88.0)
        states = [
            _state(is_on=True, power=2300.0),
            ApplianceState(
                appliance_id="heater", is_on=True, current_power=500.0, current_amperage=None,
                runtime_today=timedelta(), energy_today=0.0, last_state_change=None,
                ev_connected=None, ev_at_home=None, ev_charging=None, activations_today=0,
            ),
        ]
        result = opt.optimize(
            power_state=power, appliances=[car, heater], appliance_states=states,
            plan=_plan(), power_history=[power], tariff=_tariff(),
        )
        by_id = {d.appliance_id: d for d in result.decisions}
        assert by_id["tesla"].action == Action.SET_CURRENT
        assert by_id["heater"].action == Action.ON

    def test_shed_reduces_by_deficit_over_share(self):
        """A real extra deficit (another load) shrinks the car by deficit/share."""
        opt = _opt()
        car = _tesla()
        other = _tesla(
            id="other", name="Other", priority=2, dynamic_current=False, current_entity=None,
            nominal_power=1000.0, min_current=0.0, max_current=0.0, battery_assist_enabled=False,
        )
        # Sun available for the car after the other load starts: 1610 W - 700 W deficit
        power = _power(1610.0 - 2300.0 - 700.0, 88.0)
        states = [
            _state(is_on=True, power=2300.0),
            ApplianceState(
                appliance_id="other", is_on=False, current_power=0.0, current_amperage=None,
                runtime_today=timedelta(), energy_today=0.0, last_state_change=None,
                ev_connected=None, ev_at_home=None, ev_charging=None, activations_today=0,
            ),
        ]
        result = opt.optimize(
            power_state=power, appliances=[car, other], appliance_states=states,
            plan=_plan(), power_history=[power], tariff=_tariff(),
        )
        car_d = next(d for d in result.decisions if d.appliance_id == "tesla")
        # 910 W of sun / 0.7 = 1300 W < 1380 W min: the car stops
        assert car_d.action == Action.OFF


# ---------------------------------------------------------------------------
# optimizer: floor and hysteresis
# ---------------------------------------------------------------------------

class TestFloor:
    def test_below_floor_is_plain_solar_only(self):
        d = _run(_opt(), _tesla(), excess=1000.0, soc=60.0)
        assert d.action == Action.IDLE
        assert d.reason.startswith("Battery assist paused (SoC 60% below 61% floor, resumes at 63%)")

    def test_running_below_floor_falls_back_to_solar_only_current(self):
        d = _run(_opt(), _tesla(), excess=-500.0, soc=60.0, running_w=2300.0)
        assert d.target_current == 7.0  # solar only

    def test_stops_at_floor_when_sun_cannot_carry_it(self):
        # With assist the car would run on 1000 W of sun; below the floor it cannot
        opt = _opt()
        assert _run(opt, _tesla(), excess=-380.0, soc=62.0, running_w=1380.0).action == Action.SET_CURRENT
        assert _run(opt, _tesla(), excess=-380.0, soc=60.9, running_w=1380.0).action == Action.OFF

    def test_no_restart_until_floor_plus_two(self):
        opt = _opt()
        app = _tesla()
        assert _run(opt, app, excess=1000.0, soc=60.0).action == Action.IDLE
        assert _run(opt, app, excess=1000.0, soc=61.0).action == Action.IDLE
        assert _run(opt, app, excess=1000.0, soc=62.9).action == Action.IDLE
        assert _run(opt, app, excess=1000.0, soc=63.0).action == Action.SET_CURRENT

    def test_no_flapping_around_floor(self):
        opt = _opt()
        app = _tesla()
        socs = [62, 61, 60.5, 61.5, 62, 61, 60, 62.5, 61, 63, 62, 61, 61.0, 60.9]
        engaged = [opt._battery_assist_factor(app, s) > 1.0 for s in socs]
        # Engaged from the start (>= floor), drops once below 61, re-engages only at 63
        assert engaged == [
            True, True, False, False, False, False, False, False, False,
            True, True, True, True, False,
        ]

    def test_first_engagement_at_floor_exactly(self):
        assert _opt()._battery_assist_factor(_tesla(), 61.0) > 1.0


# ---------------------------------------------------------------------------
# optimizer: switches off the feature
# ---------------------------------------------------------------------------

class TestOff:
    CASES = [(-500.0, 2300.0), (1000.0, None), (2000.0, None), (-380.0, 1380.0), (5000.0, 3000.0)]

    @pytest.mark.parametrize("excess,running", CASES)
    def test_slider_zero_is_solar_only(self, excess, running):
        off = _run(_opt(), _tesla(battery_assist_max_share=0.0), excess=excess, soc=88.0, running_w=running)
        ref = _run(_opt(), _tesla(battery_assist_enabled=False), excess=excess, soc=88.0, running_w=running)
        assert (off.action, off.target_current, off.reason) == (ref.action, ref.target_current, ref.reason)

    @pytest.mark.parametrize("excess,running", CASES)
    def test_switch_off_is_solar_only(self, excess, running):
        off = _run(_opt(), _tesla(battery_assist_enabled=False), excess=excess, soc=88.0, running_w=running)
        plain = _run(
            _opt(), _tesla(battery_assist_enabled=False, battery_assist_max_share=0.0),
            excess=excess, soc=88.0, running_w=running,
        )
        assert (off.action, off.target_current, off.reason) == (plain.action, plain.target_current, plain.reason)
        assert "Battery assist" not in off.reason

    def test_switch_off_clears_hysteresis(self):
        opt = _opt()
        assert opt._battery_assist_factor(_tesla(), 60.0) == 1.0  # blocked
        assert opt._battery_assist_factor(_tesla(battery_assist_enabled=False), 62.0) == 1.0
        assert opt._battery_assist_factor(_tesla(), 62.0) > 1.0  # fresh start at the floor

    def test_soc_unavailable_never_enables_assist(self):
        d = _run(_opt(), _tesla(), excess=1000.0, soc=None)
        assert d.action == Action.IDLE
        assert "unavailable" in d.reason

    def test_max_share_capped_at_90(self):
        factor = _opt()._battery_assist_factor(_tesla(battery_assist_max_share=500.0), 88.0)
        assert factor == pytest.approx(10.0)


class TestPrecedence:
    def test_min_battery_soc_still_wins(self):
        """Global min_battery_soc sheds the car even with Battery Assist engaged."""
        d = _run(
            _opt(), _tesla(), excess=3000.0, soc=65.0, running_w=3000.0,
            min_battery_soc=70.0,
        )
        assert d.action == Action.OFF

    def test_big_consumer_discharge_protection_still_applies(self):
        opt = _opt()
        app = _tesla(is_big_consumer=True)
        power = _power(-500.0, 88.0)
        result = opt.optimize(
            power_state=power, appliances=[app], appliance_states=[_state(True, 2300.0)],
            plan=_plan(), power_history=[power], tariff=_tariff(),
            battery_protection_active=False,
        )
        assert result.battery_discharge_action is not None


# ---------------------------------------------------------------------------
# status text
# ---------------------------------------------------------------------------

class TestStatusText:
    def test_running_status_text(self):
        d = _run(_opt(), _tesla(), excess=-500.0, soc=88.0, running_w=2300.0)
        assert re.fullmatch(
            r"Battery assist: \d+% solar, ~\d\.\d kW from battery \(SoC 88% >= 61%\)", d.reason
        ), d.reason
        # 11 A = 2530 W on 1800 W of sun: 71% solar, ~0.7 kW from battery
        assert d.reason == "Battery assist: 71% solar, ~0.7 kW from battery (SoC 88% >= 61%)"

    def test_start_status_text(self):
        d = _run(_opt(), _tesla(), excess=2000.0, soc=88.0)
        assert d.reason.startswith("Battery assist: ")
        assert "from battery (SoC 88% >= 61%)" in d.reason

    def test_waiting_status_text(self):
        d = _run(_opt(), _tesla(), excess=900.0, soc=88.0)
        assert d.reason == "Battery assist: waiting for sun (900W < 966W needed at 70% solar, SoC 88% >= 61%)"

    def test_below_floor_status_text(self):
        d = _run(_opt(), _tesla(), excess=2000.0, soc=58.0)
        assert d.reason.startswith("Battery assist paused (SoC 58% below 61% floor, resumes at 63%): ")

    def test_soc_unavailable_status_text(self):
        d = _run(_opt(), _tesla(), excess=2000.0, soc=None)
        assert d.reason.startswith("Battery assist paused (battery SoC unavailable): ")


# ---------------------------------------------------------------------------
# entities / coordinator / lifecycle
# ---------------------------------------------------------------------------

def _entry_with_subentry(sub_data: dict, entry_data: dict | None = None):
    from tests.test_init import _make_config_entry
    sub = MagicMock()
    sub.data = {CONF_DYNAMIC_CURRENT: True, **sub_data}
    entry = _make_config_entry(data=entry_data, subentries={"sub1": sub})
    return entry, sub


def _mock_coordinator(entry, **dicts) -> MagicMock:
    coord = MagicMock()
    coord.config_entry = entry
    coord.appliance_battery_assist = dicts.get("assist", {})
    coord.appliance_battery_assist_max_share = dicts.get("max_share", {})
    coord.appliance_battery_assist_min_soc = dicts.get("min_soc", {})
    return coord


def _hass_with_seed(value: str | None) -> MagicMock:
    hass = MagicMock()
    if value is None:
        hass.states.get.return_value = None
    else:
        state = MagicMock()
        state.state = value
        hass.states.get.return_value = state
    return hass


class TestNumbers:
    def test_ranges_and_defaults(self):
        from custom_components.pv_excess_control.number import (
            BatteryAssistMaxShareNumber,
            BatteryAssistMinSocNumber,
        )
        entry, _ = _entry_with_subentry({})
        coord = _mock_coordinator(entry)
        share = BatteryAssistMaxShareNumber(coord, "sub1", "Tesla")
        floor = BatteryAssistMinSocNumber(coord, "sub1", "Tesla")
        assert (share.native_min_value, share.native_max_value, share.native_step) == (0.0, 90.0, 5.0)
        assert (floor.native_min_value, floor.native_max_value, floor.native_step) == (20.0, 95.0, 1.0)
        assert share.native_value == 30.0
        assert floor.native_value == 61.0
        assert share.name == "Tesla Battery Assist Max Share"
        assert floor.name == "Tesla Battery Assist Min SoC"

    @pytest.mark.asyncio
    async def test_set_value_updates_coordinator_and_persists(self):
        from custom_components.pv_excess_control.number import BatteryAssistMaxShareNumber
        entry, sub = _entry_with_subentry({})
        coord = _mock_coordinator(entry)
        num = BatteryAssistMaxShareNumber(coord, "sub1", "Tesla")
        num.hass = MagicMock()
        num.async_write_ha_state = MagicMock()
        await num.async_set_native_value(45.0)
        assert coord.appliance_battery_assist_max_share["sub1"] == 45.0
        assert num.native_value == 45.0
        _, kwargs = num.hass.config_entries.async_update_subentry.call_args
        assert kwargs["data"][CONF_BATTERY_ASSIST_MAX_SHARE] == 45.0

    @pytest.mark.asyncio
    async def test_set_value_clamped(self):
        from custom_components.pv_excess_control.number import BatteryAssistMaxShareNumber
        entry, _ = _entry_with_subentry({})
        coord = _mock_coordinator(entry)
        num = BatteryAssistMaxShareNumber(coord, "sub1", "Tesla")
        num.hass = MagicMock()
        num.async_write_ha_state = MagicMock()
        await num.async_set_native_value(100.0)
        assert coord.appliance_battery_assist_max_share["sub1"] == 90.0

    @pytest.mark.parametrize("seed,expected", [
        ("55", 55.0), ("61.0", 61.0), ("10", 20.0), ("99", 95.0),
        ("unavailable", 61.0), ("unknown", 61.0), (None, 61.0),
    ])
    @pytest.mark.asyncio
    async def test_min_soc_seeded_once_from_input_number(self, seed, expected):
        from homeassistant.helpers.update_coordinator import CoordinatorEntity
        from custom_components.pv_excess_control.number import BatteryAssistMinSocNumber
        entry, _ = _entry_with_subentry({})
        coord = _mock_coordinator(entry)
        num = BatteryAssistMinSocNumber(coord, "sub1", "Tesla")
        num.hass = _hass_with_seed(seed)
        with patch.object(CoordinatorEntity, "async_added_to_hass", new=AsyncMock()):
            await num.async_added_to_hass()
        assert coord.appliance_battery_assist_min_soc["sub1"] == expected
        _, kwargs = num.hass.config_entries.async_update_subentry.call_args
        assert kwargs["data"][CONF_BATTERY_ASSIST_MIN_SOC] == expected

    @pytest.mark.asyncio
    async def test_min_soc_not_reseeded_when_stored(self):
        from homeassistant.helpers.update_coordinator import CoordinatorEntity
        from custom_components.pv_excess_control.number import BatteryAssistMinSocNumber
        entry, _ = _entry_with_subentry({CONF_BATTERY_ASSIST_MIN_SOC: 70.0})
        coord = _mock_coordinator(entry, min_soc={"sub1": 70.0})
        num = BatteryAssistMinSocNumber(coord, "sub1", "Tesla")
        num.hass = _hass_with_seed("55")
        with patch.object(CoordinatorEntity, "async_added_to_hass", new=AsyncMock()):
            await num.async_added_to_hass()
        assert coord.appliance_battery_assist_min_soc["sub1"] == 70.0
        num.hass.config_entries.async_update_subentry.assert_not_called()

    @pytest.mark.asyncio
    async def test_platform_adds_numbers_only_for_dynamic_current(self):
        from custom_components.pv_excess_control import number as number_mod
        entry, sub = _entry_with_subentry({})
        static = MagicMock()
        static.data = {CONF_DYNAMIC_CURRENT: False}
        entry.subentries = {"sub1": sub, "sub2": static}
        coord = _mock_coordinator(entry)
        hass = MagicMock()
        hass.data = {DOMAIN: {entry.entry_id: coord}}
        added: list = []
        await number_mod.async_setup_entry(hass, entry, lambda ents: added.extend(ents))
        kinds = [type(e).__name__ for e in added]
        assert kinds.count("BatteryAssistMaxShareNumber") == 1
        assert kinds.count("BatteryAssistMinSocNumber") == 1


class TestSwitch:
    def test_default_off(self):
        from custom_components.pv_excess_control.switch import BatteryAssistSwitch
        entry, _ = _entry_with_subentry({})
        sw = BatteryAssistSwitch(_mock_coordinator(entry), "sub1", "Tesla")
        assert sw.is_on is False
        assert sw.name == "Tesla Battery Assist"

    @pytest.mark.asyncio
    async def test_turn_on_off_persists_runtime_list(self):
        from custom_components.pv_excess_control.switch import BatteryAssistSwitch
        entry, _ = _entry_with_subentry({})
        coord = _mock_coordinator(entry)
        sw = BatteryAssistSwitch(coord, "sub1", "Tesla")
        sw.hass = MagicMock()
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_on()
        assert sw.is_on is True
        _, kwargs = sw.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"][CONF_BATTERY_ASSIST_APPLIANCES] == ["sub1"]
        await sw.async_turn_off()
        assert sw.is_on is False
        _, kwargs = sw.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"][CONF_BATTERY_ASSIST_APPLIANCES] == []


class TestCoordinatorRestore:
    def _coordinator(self, sub_data, entry_data_extra=None):
        from tests.test_init import MockHass
        from custom_components.pv_excess_control.coordinator import PvExcessCoordinator
        entry, _ = _entry_with_subentry(sub_data)
        entry.data = {**entry.data, **(entry_data_extra or {})}
        return PvExcessCoordinator(MockHass(), entry)

    def test_restore_from_persisted_values(self):
        coord = self._coordinator(
            {CONF_BATTERY_ASSIST_MAX_SHARE: 40.0, CONF_BATTERY_ASSIST_MIN_SOC: 70.0},
            {CONF_BATTERY_ASSIST_APPLIANCES: ["sub1"]},
        )
        assert coord.appliance_battery_assist == {"sub1": True}
        fields = coord._battery_assist_fields("sub1")
        assert fields == {
            "battery_assist_enabled": True,
            "battery_assist_max_share": 40.0,
            "battery_assist_min_soc": 70.0,
        }

    def test_defaults_when_nothing_stored(self):
        coord = self._coordinator({})
        assert coord._battery_assist_fields("sub1") == {
            "battery_assist_enabled": False,
            "battery_assist_max_share": 30.0,
            "battery_assist_min_soc": 61.0,
        }

    def test_max_share_clamped_to_90(self):
        coord = self._coordinator({CONF_BATTERY_ASSIST_MAX_SHARE: 250.0})
        assert coord._battery_assist_fields("sub1")["battery_assist_max_share"] == 90.0
        coord.appliance_battery_assist_max_share["sub1"] = -10.0
        assert coord._battery_assist_fields("sub1")["battery_assist_max_share"] == 0.0


class TestNoReload:
    @pytest.mark.asyncio
    async def test_runtime_key_is_registered(self):
        from custom_components.pv_excess_control import _RUNTIME_STATE_KEYS
        assert CONF_BATTERY_ASSIST_APPLIANCES in _RUNTIME_STATE_KEYS

    @pytest.mark.asyncio
    async def test_toggling_assist_switch_does_not_reload(self):
        from custom_components.pv_excess_control import _async_update_listener
        entry, _ = _entry_with_subentry({})
        old = dict(entry.data)
        hass = MagicMock()
        hass.data = {
            DOMAIN: {
                f"{entry.entry_id}_config_snapshot": old,
                f"{entry.entry_id}_subentry_count": 1,
            }
        }
        hass.config_entries.async_reload = AsyncMock()
        entry.data = {**old, CONF_BATTERY_ASSIST_APPLIANCES: ["sub1"]}
        await _async_update_listener(hass, entry)
        hass.config_entries.async_reload.assert_not_called()

    @pytest.mark.asyncio
    async def test_structural_change_still_reloads(self):
        from custom_components.pv_excess_control import _async_update_listener
        entry, _ = _entry_with_subentry({})
        old = dict(entry.data)
        hass = MagicMock()
        hass.data = {
            DOMAIN: {
                f"{entry.entry_id}_config_snapshot": old,
                f"{entry.entry_id}_subentry_count": 1,
            }
        }
        hass.config_entries.async_reload = AsyncMock()
        entry.data = {**old, "some_structural_key": 1}
        await _async_update_listener(hass, entry)
        hass.config_entries.async_reload.assert_awaited_once()
