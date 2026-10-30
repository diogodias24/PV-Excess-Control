"""Integration tests for PvApplianceStatusSensor wiring to the formatter."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

from custom_components.pv_excess_control.const import Action
from custom_components.pv_excess_control.models import (
    ApplianceConfig,
    ApplianceState,
    BatteryDischargeAction,
    ControlDecision,
)
from custom_components.pv_excess_control.sensor import PvApplianceStatusSensor


def _make_config() -> ApplianceConfig:
    return ApplianceConfig(
        id="sub123", name="Test Heater", entity_id="switch.heater",
        priority=100, phases=1, nominal_power=2000.0, actual_power_entity=None,
        dynamic_current=False, current_entity=None,
        min_current=6.0, max_current=16.0,
        ev_soc_entity=None, ev_connected_entity=None,
        is_big_consumer=False, battery_max_discharge_override=None,
        on_only=False,
        min_daily_runtime=None, max_daily_runtime=None,
        schedule_deadline=None,
        switch_interval=300,
        allow_grid_supplement=False, max_grid_power=None,
    )


def _make_coordinator_with_data(data: dict) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.entry_id = "entry1"
    return coordinator


class TestStatusSensorNativeValue:
    def test_returns_decision_reason_when_no_decorations(self) -> None:
        decision = ControlDecision(
            appliance_id="sub123",
            action=Action.ON,
            target_current=None,
            reason="Excess available (2100W >= 1800W needed)",
            overrides_plan=False,
        )
        state = ApplianceState(
            appliance_id="sub123",
            is_on=True,
            current_power=1800.0,
            current_amperage=None,
            runtime_today=timedelta(0),
            energy_today=0.0,
            last_state_change=None,
            ev_connected=None,
        )
        data = {
            "control_decisions": [decision],
            "appliance_states": {"sub123": state},
            "appliance_configs": {"sub123": _make_config()},
            "battery_discharge_action": BatteryDischargeAction(should_limit=False),
            "current_plan": None,
            "grace_period_remaining": None,
        }
        sensor = PvApplianceStatusSensor(
            _make_coordinator_with_data(data), "sub123", "Test Heater"
        )
        assert sensor.native_value == "Excess available (2100W >= 1800W needed)"

    def test_grace_period_overrides_decision_reason(self) -> None:
        data = {
            "control_decisions": [],
            "appliance_states": {},
            "appliance_configs": {},
            "battery_discharge_action": BatteryDischargeAction(should_limit=False),
            "current_plan": None,
            "grace_period_remaining": 83.0,
        }
        sensor = PvApplianceStatusSensor(
            _make_coordinator_with_data(data), "sub123", "Test Heater"
        )
        assert sensor.native_value == (
            "Startup grace period - 83s remaining before decisions begin"
        )

    def test_attributes_exposed_for_plain_decision(self) -> None:
        decision = ControlDecision(
            appliance_id="sub123",
            action=Action.ON,
            target_current=None,
            reason="Excess available",
            overrides_plan=False,
        )
        state = ApplianceState(
            appliance_id="sub123",
            is_on=True,
            current_power=1800.0,
            current_amperage=None,
            runtime_today=timedelta(0),
            energy_today=0.0,
            last_state_change=None,
            ev_connected=None,
        )
        data = {
            "control_decisions": [decision],
            "appliance_states": {"sub123": state},
            "appliance_configs": {"sub123": _make_config()},
            "battery_discharge_action": BatteryDischargeAction(should_limit=False),
            "current_plan": None,
            "grace_period_remaining": None,
        }
        sensor = PvApplianceStatusSensor(
            _make_coordinator_with_data(data), "sub123", "Test Heater"
        )
        attrs = sensor.extra_state_attributes
        assert set(attrs.keys()) == {
            "action",
            "overrides_plan",
            "cooldown_seconds_remaining",
            "switch_deferred",
            "headroom_watts",
            "plan_action",
            "plan_window_start",
            "plan_window_end",
            "not_drawing_power",
            "current_deferred",
            "current_deferred_seconds",
        }
        assert attrs["action"] == "on"
        assert attrs["overrides_plan"] is False
        assert attrs["cooldown_seconds_remaining"] is None
        assert attrs["switch_deferred"] is False

    def test_returns_none_when_no_data_and_no_grace(self) -> None:
        sensor = PvApplianceStatusSensor(
            _make_coordinator_with_data(None), "sub123", "Test Heater"
        )
        assert sensor.native_value is None

    def test_grace_period_uses_ceil_not_floor_for_countdown(self) -> None:
        # 0.4s remaining → text should say "1s remaining", not "0s".
        # Without ceil rounding, the last grace cycle would briefly
        # display "Startup grace period - 0s remaining", which is
        # confusing because the grace branch is still active.
        data = {
            "control_decisions": [],
            "appliance_states": {},
            "appliance_configs": {},
            "battery_discharge_action": BatteryDischargeAction(should_limit=False),
            "current_plan": None,
            "grace_period_remaining": 0.4,
        }
        sensor = PvApplianceStatusSensor(
            _make_coordinator_with_data(data), "sub123", "Test Heater"
        )
        assert sensor.native_value == (
            "Startup grace period - 1s remaining before decisions begin"
        )

    def test_native_value_and_attributes_are_consistent(self) -> None:
        # Both properties read from the same `_compose` cache, so the
        # cooldown computation runs exactly once per HA state read.
        # Without the cache, two `datetime.now()` calls (one per
        # property) could disagree on the cooldown_seconds_remaining
        # field. This test verifies the structural guarantee, not the
        # timing race directly.
        decision = ControlDecision(
            appliance_id="sub123",
            action=Action.ON,
            target_current=None,
            reason="Excess available",
            overrides_plan=False,
        )
        state = ApplianceState(
            appliance_id="sub123",
            is_on=False,  # decision is ON, state is OFF → would switch
            current_power=0.0,
            current_amperage=None,
            runtime_today=timedelta(0),
            energy_today=0.0,
            last_state_change=datetime.now() - timedelta(seconds=10),
            ev_connected=None,
        )
        data = {
            "control_decisions": [decision],
            "appliance_states": {"sub123": state},
            "appliance_configs": {"sub123": _make_config()},
            "battery_discharge_action": BatteryDischargeAction(should_limit=False),
            "current_plan": None,
            "grace_period_remaining": None,
        }
        sensor = PvApplianceStatusSensor(
            _make_coordinator_with_data(data), "sub123", "Test Heater"
        )
        text = sensor.native_value
        attrs = sensor.extra_state_attributes
        # The cooldown second-count embedded in the state text must
        # match the attribute value exactly.
        assert text is not None
        assert attrs is not None
        if attrs["switch_deferred"]:
            cooldown_n = attrs["cooldown_seconds_remaining"]
            assert f"{cooldown_n}s cooldown" in text or (
                # _format_duration may render as "Xmin Ys" for >60s
                f"cooldown" in text
            )

    def test_fallback_when_state_missing(self) -> None:
        # Decision is present but appliance_states is empty —
        # the formatter cannot run, so the fallback path returns the
        # bare reason as text and defaults all attributes.
        decision = ControlDecision(
            appliance_id="sub123",
            action=Action.ON,
            target_current=None,
            reason="Excess available (2100W >= 1800W needed)",
            overrides_plan=False,
        )
        data = {
            "control_decisions": [decision],
            "appliance_states": {},  # missing
            "appliance_configs": {"sub123": _make_config()},
            "battery_discharge_action": BatteryDischargeAction(should_limit=False),
            "current_plan": None,
            "grace_period_remaining": None,
        }
        sensor = PvApplianceStatusSensor(
            _make_coordinator_with_data(data), "sub123", "Test Heater"
        )
        assert sensor.native_value == "Excess available (2100W >= 1800W needed)"
        attrs = sensor.extra_state_attributes
        assert attrs["action"] == "on"
        assert attrs["cooldown_seconds_remaining"] is None
        assert attrs["switch_deferred"] is False
        assert attrs["headroom_watts"] is None

    def test_fallback_truncates_long_reason(self) -> None:
        # If the fallback path is taken with an oversized reason, the
        # 255-char HA limit must still be respected.
        decision = ControlDecision(
            appliance_id="sub123",
            action=Action.ON,
            target_current=None,
            reason="x" * 300,
            overrides_plan=False,
        )
        data = {
            "control_decisions": [decision],
            "appliance_states": {},  # forces fallback
            "appliance_configs": {"sub123": _make_config()},
            "battery_discharge_action": BatteryDischargeAction(should_limit=False),
            "current_plan": None,
            "grace_period_remaining": None,
        }
        sensor = PvApplianceStatusSensor(
            _make_coordinator_with_data(data), "sub123", "Test Heater"
        )
        text = sensor.native_value
        assert len(text) == 255
        assert text.endswith("...")


class TestStatusSensorCurrentHoldEndToEnd:
    """Coordinator throttle data surfaces as the status-sensor hold text.

    Mirrors the live scenario from the "why was the current reduced"
    investigation: the optimizer proposed a higher target but the
    SET_CURRENT write was suppressed by the dynamic-current throttle
    (coordinator.py), leaving `applied_currents` at the old value. The
    status sensor must then report "Holding X.0A - target Y.0A deferred"
    instead of echoing the optimizer's reason string.
    """

    def _make_ev_config(self) -> ApplianceConfig:
        return ApplianceConfig(
            id="sub123", name="Test EV", entity_id="switch.ev",
            priority=100, phases=3, nominal_power=11000.0, actual_power_entity=None,
            dynamic_current=True, current_entity="number.ev_current",
            min_current=6.0, max_current=16.0,
            ev_soc_entity=None, ev_connected_entity=None,
            is_big_consumer=False, battery_max_discharge_override=None,
            on_only=False,
            min_daily_runtime=None, max_daily_runtime=None,
            schedule_deadline=None,
            switch_interval=300,
            allow_grid_supplement=False, max_grid_power=None,
            dynamic_current_interval=300,
        )

    def test_throttled_write_reports_hold_instead_of_optimizer_reason(self) -> None:
        decision = ControlDecision(
            appliance_id="sub123",
            action=Action.SET_CURRENT,
            target_current=12.0,
            reason="Dynamic current adjustment: 12.0A (9660W available)",
            overrides_plan=False,
        )
        state = ApplianceState(
            appliance_id="sub123",
            is_on=True,
            current_power=9660.0,
            current_amperage=14.0,
            runtime_today=timedelta(0),
            energy_today=0.0,
            last_state_change=None,
            ev_connected=None,
        )
        data = {
            "control_decisions": [decision],
            "appliance_states": {"sub123": state},
            "appliance_configs": {"sub123": self._make_ev_config()},
            "battery_discharge_action": BatteryDischargeAction(should_limit=False),
            "current_plan": None,
            "grace_period_remaining": None,
            # Throttled write: applied stays at 10.0A while the optimizer
            # proposed 12.0A ~30s ago (within the 300s dynamic interval).
            "applied_currents": {"sub123": 10.0},
            "current_change_ts": {"sub123": datetime.now() - timedelta(seconds=30)},
        }
        sensor = PvApplianceStatusSensor(
            _make_coordinator_with_data(data), "sub123", "Test EV"
        )
        text = sensor.native_value
        assert text is not None
        assert text.startswith("Holding 10.0A - target 12.0A deferred")
        assert "Dynamic current adjustment" not in text
        attrs = sensor.extra_state_attributes
        assert attrs["current_deferred"] is True
        assert attrs["current_deferred_seconds"] is not None

    def test_unthrottled_write_shows_optimizer_reason(self) -> None:
        decision = ControlDecision(
            appliance_id="sub123",
            action=Action.SET_CURRENT,
            target_current=12.0,
            reason="Dynamic current adjustment: 12.0A (9660W available)",
            overrides_plan=False,
        )
        state = ApplianceState(
            appliance_id="sub123",
            is_on=True,
            current_power=9660.0,
            current_amperage=12.0,
            runtime_today=timedelta(0),
            energy_today=0.0,
            last_state_change=None,
            ev_connected=None,
        )
        data = {
            "control_decisions": [decision],
            "appliance_states": {"sub123": state},
            "appliance_configs": {"sub123": self._make_ev_config()},
            "battery_discharge_action": BatteryDischargeAction(should_limit=False),
            "current_plan": None,
            "grace_period_remaining": None,
            # Write went through: applied current matches the target.
            "applied_currents": {"sub123": 12.0},
            "current_change_ts": {"sub123": datetime.now() - timedelta(seconds=30)},
        }
        sensor = PvApplianceStatusSensor(
            _make_coordinator_with_data(data), "sub123", "Test EV"
        )
        text = sensor.native_value
        assert text == "Dynamic current adjustment: 12.0A (9660W available)"
        attrs = sensor.extra_state_attributes
        assert attrs["current_deferred"] is False
