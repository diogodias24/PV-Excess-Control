"""Tests for following the EV target SoC from a live entity.

Covers the resolution done in PvExcessCoordinator._get_appliance_configs:
when CONF_EV_FOLLOW_TARGET_SOC is on and the referenced entity is readable,
its live value is used as ev_target_soc; otherwise the static slider value
(CONF_EV_TARGET_SOC) is the fallback.
"""
from types import SimpleNamespace

from custom_components.pv_excess_control.const import (
    CONF_APPLIANCE_ENTITY,
    CONF_APPLIANCE_NAME,
    CONF_EV_FOLLOW_TARGET_SOC,
    CONF_EV_TARGET_SOC,
    CONF_EV_TARGET_SOC_ENTITY,
)

LIMIT_ENTITY = "number.tesla_charge_limit"


def _state(value):
    return SimpleNamespace(state=value, attributes={})


def _setup(coord, sub_data, entity_states):
    coord.config_entry.subentries = {
        "ev": SimpleNamespace(data=sub_data),
    }
    coord.hass.states.get = lambda eid: entity_states.get(eid)


def _base_sub(**extra):
    data = {
        CONF_APPLIANCE_NAME: "Tesla",
        CONF_APPLIANCE_ENTITY: "switch.tesla_charge",
        CONF_EV_TARGET_SOC: 100,
    }
    data.update(extra)
    return data


def _target(coord):
    configs = coord._get_appliance_configs()
    assert len(configs) == 1
    return configs[0].ev_target_soc


def test_follows_live_entity_value(coordinator_factory):
    coord = coordinator_factory()
    _setup(
        coord,
        _base_sub(
            **{
                CONF_EV_FOLLOW_TARGET_SOC: True,
                CONF_EV_TARGET_SOC_ENTITY: LIMIT_ENTITY,
            }
        ),
        {LIMIT_ENTITY: _state("70")},
    )
    assert _target(coord) == 70.0


def test_falls_back_to_slider_when_entity_unavailable(coordinator_factory):
    coord = coordinator_factory()
    _setup(
        coord,
        _base_sub(
            **{
                CONF_EV_FOLLOW_TARGET_SOC: True,
                CONF_EV_TARGET_SOC_ENTITY: LIMIT_ENTITY,
            }
        ),
        {LIMIT_ENTITY: _state("unavailable")},
    )
    assert _target(coord) == 100


def test_falls_back_to_slider_when_entity_missing(coordinator_factory):
    coord = coordinator_factory()
    _setup(
        coord,
        _base_sub(
            **{
                CONF_EV_FOLLOW_TARGET_SOC: True,
                CONF_EV_TARGET_SOC_ENTITY: LIMIT_ENTITY,
            }
        ),
        {},  # entity not present
    )
    assert _target(coord) == 100


def test_toggle_off_uses_slider_even_if_entity_set(coordinator_factory):
    coord = coordinator_factory()
    _setup(
        coord,
        _base_sub(
            **{
                CONF_EV_FOLLOW_TARGET_SOC: False,
                CONF_EV_TARGET_SOC_ENTITY: LIMIT_ENTITY,
            }
        ),
        {LIMIT_ENTITY: _state("70")},
    )
    assert _target(coord) == 100
