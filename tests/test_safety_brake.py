"""Tests for the safety brake (grid cap and PV drop detection) functionality."""
from __future__ import annotations


from datetime import datetime, timezone
from unittest.mock import MagicMock

from custom_components.pv_excess_control.models import (
    Action,
    ApplianceConfig,
    ControlDecision,
    OptimizerResult,
    PowerState,
    BatteryDischargeAction,
)
from custom_components.pv_excess_control.const import (
    CONF_GRID_IMPORT_CAP_W,
    CONF_PV_DROP_THRESHOLD_W,
)


def _make_power_state(
    pv_production: float | None = None,
    grid_import: float | None = None,
    grid_export: float | None = None,
    load_power: float | None = None,
    battery_soc: float | None = None,
    battery_power: float | None = None,
    excess_power: float | None = None,
) -> PowerState:
    """Create a PowerState for testing."""
    return PowerState(
        pv_production=pv_production,
        grid_import=grid_import,
        grid_export=grid_export,
        load_power=load_power,
        battery_soc=battery_soc,
        battery_power=battery_power,
        ev_soc=None,
        excess_power=excess_power,
        timestamp=datetime.now(timezone.utc),
    )


def _make_appliance_config(
    appliance_id: str = "tesla",
    dynamic_current: bool = True,
    min_current: float = 6.0,
    max_current: float = 32.0,
    override_active: bool = False,
) -> ApplianceConfig:
    """Create an ApplianceConfig for testing."""
    return ApplianceConfig(
        id=appliance_id,
        name="Tesla",
        entity_id="switch.tesla_charge",
        priority=5,
        phases=1,
        nominal_power=7360.0,
        actual_power_entity=None,
        dynamic_current=dynamic_current,
        current_entity="number.tesla_charge_current",
        min_current=min_current,
        max_current=max_current,
        ev_soc_entity=None,
        ev_connected_entity=None,
        ev_charging_entity=None,
        is_big_consumer=True,
        battery_max_discharge_override=None,
        on_only=False,
        min_daily_runtime=None,
        max_daily_runtime=None,
        schedule_deadline=None,
        switch_interval=300,
        allow_grid_supplement=False,
        max_grid_power=None,
        requires_appliance=None,
        helper_only=False,
        current_step=1.0,
        override_active=override_active,
    )


def _make_decision(
    appliance_id: str = "tesla",
    action: Action = Action.SET_CURRENT,
    target_current: float | None = 32.0,
    reason: str = "Dynamic current adjustment",
    bypasses_cooldown: bool = False,
) -> ControlDecision:
    """Create a ControlDecision for testing."""
    return ControlDecision(
        appliance_id=appliance_id,
        action=action,
        target_current=target_current,
        reason=reason,
        overrides_plan=False,
        bypasses_cooldown=bypasses_cooldown,
    )


def _make_optimizer_result(decisions: list[ControlDecision]) -> OptimizerResult:
    """Create an OptimizerResult for testing."""
    return OptimizerResult(
        decisions=decisions,
        battery_discharge_action=BatteryDischargeAction(
            should_limit=False,
            max_discharge_watts=None,
            soc_protection_active=False,
        ),
    )


class TestSafetyBrake:
    """Test the safety brake functionality."""

    def test_grid_cap_exceeded_reduces_current(self, coordinator_factory) -> None:
        """When grid import exceeds cap, EV current is reduced to minimum."""
        coordinator = coordinator_factory(
            config_data={
                CONF_GRID_IMPORT_CAP_W: 5000,  # 5 kW cap
                CONF_PV_DROP_THRESHOLD_W: 0,  # Disable PV drop detection
            }
        )

        # Create appliance config with dynamic current
        app_config = _make_appliance_config()

        # Mock _get_appliance_config_by_id to return our config
        coordinator._get_appliance_config_by_id = MagicMock(return_value=app_config)

        # Create optimizer result with high current
        decision = _make_decision(target_current=32.0)
        result = _make_optimizer_result([decision])

        # Create power state with high grid import
        power_state = _make_power_state(
            pv_production=3000.0,
            grid_import=6000.0,  # Exceeds 5 kW cap
            load_power=9000.0,
        )

        # Apply safety brake
        coordinator._apply_safety_brake(result, power_state)

        # Verify current was reduced to minimum
        assert result.decisions[0].target_current == 6.0
        assert "Safety brake" in result.decisions[0].reason
        assert "grid cap exceeded" in result.decisions[0].reason

    def test_pv_drop_detected_reduces_current(self, coordinator_factory) -> None:
        """A PV drop exceeding threshold vs N samples ago reduces current to minimum.

        PV drop detection only fires if grid import is positive (importing from grid)
        AND grid cap is enabled. This prevents false positives when PV drops but
        there's still plenty of solar (e.g. 9.7kW → 7.1kW with 0W grid import).
        """
        coordinator = coordinator_factory(
            config_data={
                CONF_GRID_IMPORT_CAP_W: 5000,  # Enable grid cap at 5kW
                CONF_PV_DROP_THRESHOLD_W: 2000,  # 2 kW drop threshold
            }
        )

        # Create appliance config with dynamic current
        app_config = _make_appliance_config()

        # Mock _get_appliance_config_by_id to return our config
        coordinator._get_appliance_config_by_id = MagicMock(return_value=app_config)

        # Create optimizer result with high current
        decision = _make_decision(target_current=32.0)
        result = _make_optimizer_result([decision])

        # Seed history with a high-PV baseline (N samples ago)
        coordinator.power_history.append(_make_power_state(pv_production=8000.0))

        # Cycle 1: drop of 3 kW (8000 -> 5000). Not enough history yet (need > N samples).
        c1 = _make_power_state(pv_production=5000.0, grid_import=1000.0)
        coordinator.power_history.append(c1)
        coordinator._apply_safety_brake(result, c1)
        assert result.decisions[0].target_current == 32.0
        assert "Safety brake" not in result.decisions[0].reason

        # Cycle 2: drop of 2.5 kW more (5000 -> 2500). Now compare to N samples ago (8000).
        # Drop from 8000 to 2500 = 5500W > 2000W threshold, brake fires.
        # Grid import is positive (1500W), so PV drop detection triggers.
        c2 = _make_power_state(pv_production=2500.0, grid_import=1500.0)
        coordinator.power_history.append(c2)
        coordinator._apply_safety_brake(result, c2)

        # Verify current was reduced to minimum
        assert result.decisions[0].target_current == 6.0
        assert "Safety brake" in result.decisions[0].reason
        assert "PV drop detected" in result.decisions[0].reason

    def test_pv_drop_transient_single_sample_does_not_fire(self, coordinator_factory) -> None:
        """A single-sample PV blip (cloud) must NOT trip the brake."""
        coordinator = coordinator_factory(
            config_data={
                CONF_GRID_IMPORT_CAP_W: 5000,  # Enable grid cap
                CONF_PV_DROP_THRESHOLD_W: 2000,  # 2 kW drop threshold
            }
        )

        app_config = _make_appliance_config()
        coordinator._get_appliance_config_by_id = MagicMock(return_value=app_config)

        decision = _make_decision(target_current=32.0)
        result = _make_optimizer_result([decision])

        coordinator.power_history.append(_make_power_state(pv_production=8000.0))

        # Cycle 1: drop of 3 kW -> not enough history yet (need > N samples).
        c1 = _make_power_state(pv_production=5000.0, grid_import=1000.0)
        coordinator.power_history.append(c1)
        coordinator._apply_safety_brake(result, c1)

        # Cycle 2: PV recovers (transient cloud). Compare to N samples ago (8000).
        # Drop from 8000 to 6000 = 2000W, NOT > 2000W threshold, so no fire.
        c2 = _make_power_state(pv_production=6000.0, grid_import=500.0)
        coordinator.power_history.append(c2)
        coordinator._apply_safety_brake(result, c2)

        assert result.decisions[0].target_current == 32.0
        assert "Safety brake" not in result.decisions[0].reason

    def test_pv_drop_single_step_collapse_fires(self, coordinator_factory) -> None:
        """A sudden PV collapse (e.g. inverter restart) must fire the brake.

        The primary use case: PV drops from 8000W to 0W in a single sample.
        The old streak-based logic required consecutive drops, so this case
        never fired (next sample 0→0 resets the streak). The new lookback
        logic compares to N samples ago, catching both gradual and sudden
        collapses.
        """
        coordinator = coordinator_factory(
            config_data={
                CONF_GRID_IMPORT_CAP_W: 5000,  # Enable grid cap
                CONF_PV_DROP_THRESHOLD_W: 2000,  # 2 kW drop threshold
            }
        )

        app_config = _make_appliance_config()
        coordinator._get_appliance_config_by_id = MagicMock(return_value=app_config)

        decision = _make_decision(target_current=32.0)
        result = _make_optimizer_result([decision])

        # Seed history with high-PV samples (need > N samples for lookback)
        coordinator.power_history.append(_make_power_state(pv_production=8000.0))
        coordinator.power_history.append(_make_power_state(pv_production=8000.0))

        # Sudden collapse: 8000W → 0W in one sample (inverter restart).
        # Compare to N samples ago (8000W), drop = 8000W > 2000W, fires immediately.
        # Grid import is positive (3000W), so PV drop detection triggers.
        collapse = _make_power_state(pv_production=0.0, grid_import=3000.0)
        coordinator.power_history.append(collapse)
        coordinator._apply_safety_brake(result, collapse)

        assert result.decisions[0].target_current == 6.0
        assert "Safety brake" in result.decisions[0].reason
        assert "PV drop detected" in result.decisions[0].reason

    def test_pv_drop_with_zero_grid_import_does_not_fire(self, coordinator_factory) -> None:
        """PV drop with 0W grid import (exporting/balanced) must NOT fire.

        This is the key fix for false positives: if PV drops from 9.7kW to 7.1kW
        but grid import is 0W, the house is still balanced — no emergency.
        The optimizer will naturally adjust EV current on the next cycle.
        """
        coordinator = coordinator_factory(
            config_data={
                CONF_GRID_IMPORT_CAP_W: 5000,  # Enable grid cap
                CONF_PV_DROP_THRESHOLD_W: 2000,  # 2 kW drop threshold
            }
        )

        app_config = _make_appliance_config()
        coordinator._get_appliance_config_by_id = MagicMock(return_value=app_config)

        decision = _make_decision(target_current=32.0)
        result = _make_optimizer_result([decision])

        # Seed history with high-PV samples
        coordinator.power_history.append(_make_power_state(pv_production=9700.0))
        coordinator.power_history.append(_make_power_state(pv_production=9700.0))

        # PV drops from 9700W to 7100W (2600W drop > 2000W threshold)
        # But grid import is 0W (house is balanced, all PV consumed locally)
        # Safety brake should NOT fire — no risk of grid overload
        drop = _make_power_state(pv_production=7100.0, grid_import=0.0)
        coordinator.power_history.append(drop)
        coordinator._apply_safety_brake(result, drop)

        assert result.decisions[0].target_current == 32.0
        assert "Safety brake" not in result.decisions[0].reason

    def test_pv_drop_skips_overridden_appliance(self, coordinator_factory) -> None:
        """The PV-drop trigger must respect a manual override."""
        coordinator = coordinator_factory(
            config_data={
                CONF_GRID_IMPORT_CAP_W: 5000,  # Enable grid cap
                CONF_PV_DROP_THRESHOLD_W: 2000,  # 2 kW drop threshold
            }
        )

        # Override is active: the user explicitly commands this current.
        app_config = _make_appliance_config(override_active=True)
        coordinator._get_appliance_config_by_id = MagicMock(return_value=app_config)

        decision = _make_decision(target_current=32.0)
        result = _make_optimizer_result([decision])

        coordinator.power_history.append(_make_power_state(pv_production=8000.0))

        # Two consecutive drops: compare to N samples ago (8000) would fire the brake.
        # Grid import is positive, so PV drop detection triggers.
        c1 = _make_power_state(pv_production=5000.0, grid_import=1000.0)
        coordinator.power_history.append(c1)
        coordinator._apply_safety_brake(result, c1)

        c2 = _make_power_state(pv_production=2500.0, grid_import=1500.0)
        coordinator.power_history.append(c2)
        coordinator._apply_safety_brake(result, c2)

        # Override wins: current untouched.
        assert result.decisions[0].target_current == 32.0
        assert "Safety brake" not in result.decisions[0].reason

    def test_grid_cap_applies_to_overridden_appliance(self, coordinator_factory) -> None:
        """The grid-import-cap trigger is a physical limit: it overrides manual override."""
        coordinator = coordinator_factory(
            config_data={
                CONF_GRID_IMPORT_CAP_W: 5000,  # 5 kW cap
                CONF_PV_DROP_THRESHOLD_W: 0,  # Disable PV drop detection
            }
        )

        # Override is active — grid cap must still win.
        app_config = _make_appliance_config(override_active=True)
        coordinator._get_appliance_config_by_id = MagicMock(return_value=app_config)

        decision = _make_decision(target_current=32.0)
        result = _make_optimizer_result([decision])

        power_state = _make_power_state(
            pv_production=3000.0,
            grid_import=6000.0,  # Exceeds 5 kW cap
            load_power=9000.0,
        )

        coordinator._apply_safety_brake(result, power_state)

        assert result.decisions[0].target_current == 6.0
        assert "Safety brake" in result.decisions[0].reason
        assert "grid cap exceeded" in result.decisions[0].reason

    def test_no_override_when_within_limits(self, coordinator_factory) -> None:
        """When within limits, optimizer decisions are not overridden."""
        coordinator = coordinator_factory(
            config_data={
                CONF_GRID_IMPORT_CAP_W: 5000,
                CONF_PV_DROP_THRESHOLD_W: 2000,
            }
        )

        # Create appliance config with dynamic current
        app_config = _make_appliance_config()

        # Mock _get_appliance_config_by_id to return our config
        coordinator._get_appliance_config_by_id = MagicMock(return_value=app_config)

        # Create optimizer result with high current
        decision = _make_decision(target_current=32.0)
        result = _make_optimizer_result([decision])

        # Add previous power state to history (stable PV)
        prev_state = _make_power_state(pv_production=8000.0)
        coordinator.power_history.append(prev_state)

        # Create power state within limits
        power_state = _make_power_state(
            pv_production=7500.0,  # Only 500W drop (below 2 kW threshold)
            grid_import=3000.0,  # Below 5 kW cap
            load_power=4500.0,
        )

        # Add current state to history (simulating what the coordinator does)
        coordinator.power_history.append(power_state)

        # Apply safety brake
        coordinator._apply_safety_brake(result, power_state)

        # Verify current was NOT reduced
        assert result.decisions[0].target_current == 32.0
        assert "Safety brake" not in result.decisions[0].reason

    def test_both_triggers_disabled(self, coordinator_factory) -> None:
        """When both triggers are disabled (set to 0), no override occurs."""
        coordinator = coordinator_factory(
            config_data={
                CONF_GRID_IMPORT_CAP_W: 0,  # Disabled
                CONF_PV_DROP_THRESHOLD_W: 0,  # Disabled
            }
        )

        # Create appliance config with dynamic current
        app_config = _make_appliance_config()

        # Mock _get_appliance_config_by_id to return our config
        coordinator._get_appliance_config_by_id = MagicMock(return_value=app_config)

        # Create optimizer result with high current
        decision = _make_decision(target_current=32.0)
        result = _make_optimizer_result([decision])

        # Create power state with extreme values
        power_state = _make_power_state(
            pv_production=1000.0,
            grid_import=10000.0,  # Very high
            load_power=11000.0,
        )

        # Apply safety brake
        coordinator._apply_safety_brake(result, power_state)

        # Verify current was NOT reduced (both triggers disabled)
        assert result.decisions[0].target_current == 32.0

    def test_grid_cap_remaps_on_decision_to_min_current(self, coordinator_factory) -> None:
        """An Action.ON turn-on decided during a grid-cap emergency must be
        converted to a SET_CURRENT(min) so the emergency actually limits
        power instead of turning the appliance on unthrottled.
        """
        coordinator = coordinator_factory(
            config_data={
                CONF_GRID_IMPORT_CAP_W: 5000,
                CONF_PV_DROP_THRESHOLD_W: 0,
            }
        )

        app_config = _make_appliance_config()
        coordinator._get_appliance_config_by_id = MagicMock(return_value=app_config)

        # Optimizer decided to turn the appliance ON (target_current=None).
        on_decision = _make_decision(action=Action.ON, target_current=None)
        result = _make_optimizer_result([on_decision])

        power_state = _make_power_state(
            pv_production=3000.0,
            grid_import=6000.0,  # Exceeds 5 kW cap
            load_power=9000.0,
        )

        coordinator._apply_safety_brake(result, power_state)

        assert result.decisions[0].action == Action.SET_CURRENT
        assert result.decisions[0].target_current == 6.0
        assert "Safety brake" in result.decisions[0].reason

    def test_grid_cap_keeps_set_current_bypass_flag(self, coordinator_factory) -> None:
        """The rewritten SET_CURRENT must keep bypasses_cooldown=True so the
        emergency reduction is not held back by the dynamic-current throttle
        in the apply phase.
        """
        coordinator = coordinator_factory(
            config_data={
                CONF_GRID_IMPORT_CAP_W: 5000,
                CONF_PV_DROP_THRESHOLD_W: 0,
            }
        )

        app_config = _make_appliance_config()
        coordinator._get_appliance_config_by_id = MagicMock(return_value=app_config)

        decision = _make_decision(target_current=32.0)
        result = _make_optimizer_result([decision])

        power_state = _make_power_state(
            pv_production=3000.0,
            grid_import=6000.0,
            load_power=9000.0,
        )

        coordinator._apply_safety_brake(result, power_state)

        assert result.decisions[0].target_current == 6.0
        # The brake's reduction must reach the device this cycle.
        assert result.decisions[0].bypasses_cooldown is True
