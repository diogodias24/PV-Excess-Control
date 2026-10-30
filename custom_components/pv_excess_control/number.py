"""Number platform for PV Excess Control."""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    BATTERY_ASSIST_SEED_ENTITY,
    CONF_APPLIANCE_NAME,
    CONF_BATTERY_ASSIST_MAX_SHARE,
    CONF_BATTERY_ASSIST_MIN_SOC,
    CONF_DYNAMIC_CURRENT,
    DEFAULT_BATTERY_ASSIST_MAX_SHARE,
    DEFAULT_BATTERY_ASSIST_MIN_SOC,
    MAX_BATTERY_ASSIST_SHARE,
    CONF_MIN_DAILY_RUNTIME,
    CONF_MAX_DAILY_RUNTIME,
    CONF_RESTART_INTERVAL,
    CONF_SWITCH_INTERVAL,
    DOMAIN,
    MANUFACTURER,
    MAX_PRIORITY,
    MIN_PRIORITY,
    DEFAULT_SWITCH_INTERVAL,
)
from .coordinator import PvExcessCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PV Excess Control number entities."""
    coordinator: PvExcessCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    entities: list[NumberEntity] = []

    # Per-appliance number entities: priority, min/max daily runtime, switch interval
    subentries = getattr(config_entry, "subentries", {})
    for subentry_id, subentry in subentries.items():
        appliance_name = subentry.data.get(CONF_APPLIANCE_NAME, f"Appliance {subentry_id}")
        entities.append(AppliancePriorityNumber(coordinator, subentry_id, appliance_name))
        entities.append(ApplianceMinDailyRuntimeNumber(coordinator, subentry_id, appliance_name))
        entities.append(ApplianceMaxDailyRuntimeNumber(coordinator, subentry_id, appliance_name))
        entities.append(ApplianceSwitchIntervalNumber(coordinator, subentry_id, appliance_name))
        entities.append(ApplianceRestartIntervalNumber(coordinator, subentry_id, appliance_name))
        if subentry.data.get(CONF_DYNAMIC_CURRENT, False):
            entities.append(BatteryAssistMaxShareNumber(coordinator, subentry_id, appliance_name))
            entities.append(BatteryAssistMinSocNumber(coordinator, subentry_id, appliance_name))
    async_add_entities(entities)


class ApplianceSwitchIntervalNumber(CoordinatorEntity[PvExcessCoordinator], NumberEntity):
    """Per-appliance switch interval number entity."""

    _attr_has_entity_name = True
    _attr_native_min_value = 5.0
    _attr_native_max_value = 3600.0
    _attr_native_step = 1.0
    _attr_native_unit_of_measurement = "s"
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:skip-next"

    def __init__(
        self,
        coordinator: PvExcessCoordinator,
        appliance_id: str,
        appliance_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._appliance_id = appliance_id
        self._appliance_name = appliance_name
        self._attr_name = f"{appliance_name} Switch Interval"
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{appliance_id}_switch_interval"
        )

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.config_entry.entry_id)},
            name="PV Excess Control",
            manufacturer=MANUFACTURER,
        )

    @property
    def native_value(self) -> float:
        return float(
            self.coordinator.appliance_switch_intervals.get(self._appliance_id, DEFAULT_SWITCH_INTERVAL)
        )

    async def async_set_native_value(self, value: float) -> None:
        """Persist the switch interval to the coordinator and subentry.

        Clamped to the 5s floor so a write below the protected minimum
        (matching the coordinator's load-time clamp for legacy configs)
        can never re-introduce a 0s interval.
        """
        new_value = int(max(5, int(value)))
        self.coordinator.appliance_switch_intervals[self._appliance_id] = new_value
        try:
            subentries = getattr(self.coordinator.config_entry, "subentries", {})
            subentry = subentries.get(self._appliance_id)
            if subentry is not None:
                new_data = dict(subentry.data)
                new_data[CONF_SWITCH_INTERVAL] = new_value
                await self.hass.config_entries.async_update_subentry(
                    self.coordinator.config_entry, subentry, data=new_data
                )
        except Exception:
            _LOGGER.debug(
                "Could not persist switch_interval for %s (HA version may not support subentry updates)",
                self._appliance_id,
            )
        self.async_write_ha_state()


class ApplianceRestartIntervalNumber(CoordinatorEntity[PvExcessCoordinator], NumberEntity):
    """Per-appliance restart interval number entity.

    Minimum delay before an OFF appliance may be re-enabled. When unset
    (None) the effective value shown is the appliance's switch_interval;
    writing a value persists an explicit restart_interval override.
    """

    _attr_has_entity_name = True
    _attr_native_min_value = 5.0
    _attr_native_max_value = 3600.0
    _attr_native_step = 1.0
    _attr_native_unit_of_measurement = "s"
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:restart"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: PvExcessCoordinator,
        appliance_id: str,
        appliance_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._appliance_id = appliance_id
        self._appliance_name = appliance_name
        self._attr_name = f"{appliance_name} Restart Interval"
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{appliance_id}_restart_interval"
        )

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.config_entry.entry_id)},
            name="PV Excess Control",
            manufacturer=MANUFACTURER,
        )

    @property
    def native_value(self) -> float:
        raw = self.coordinator.appliance_restart_intervals.get(self._appliance_id)
        if raw is None:
            raw = self.coordinator.appliance_switch_intervals.get(
                self._appliance_id, DEFAULT_SWITCH_INTERVAL
            )
        return float(raw)

    async def async_set_native_value(self, value: float) -> None:
        """Persist the restart interval to the coordinator and subentry.

        Clamped to the 5s floor, matching the switch-interval entity and the
        coordinator's load-time clamp.
        """
        new_value = int(max(5, int(value)))
        self.coordinator.appliance_restart_intervals[self._appliance_id] = new_value
        try:
            subentries = getattr(self.coordinator.config_entry, "subentries", {})
            subentry = subentries.get(self._appliance_id)
            if subentry is not None:
                new_data = dict(subentry.data)
                new_data[CONF_RESTART_INTERVAL] = new_value
                await self.hass.config_entries.async_update_subentry(
                    self.coordinator.config_entry, subentry, data=new_data
                )
        except Exception:
            _LOGGER.debug(
                "Could not persist restart_interval for %s (HA version may not support subentry updates)",
                self._appliance_id,
            )
        self.async_write_ha_state()


class AppliancePriorityNumber(CoordinatorEntity[PvExcessCoordinator], NumberEntity):
    """Per-appliance priority number entity."""

    _attr_has_entity_name = True
    _attr_native_min_value = float(MIN_PRIORITY)
    _attr_native_max_value = float(MAX_PRIORITY)
    _attr_native_step = 1.0
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:priority-high"

    def __init__(
        self,
        coordinator: PvExcessCoordinator,
        appliance_id: str,
        appliance_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._appliance_id = appliance_id
        self._appliance_name = appliance_name
        self._attr_name = f"{appliance_name} Priority"
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{appliance_id}_priority"
        )

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.config_entry.entry_id)},
            name="PV Excess Control",
            manufacturer=MANUFACTURER,
        )

    @property
    def native_value(self) -> float:
        return float(
            self.coordinator.appliance_priorities.get(self._appliance_id, 500)
        )

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.appliance_priorities[self._appliance_id] = int(value)
        # Persist to config entry subentry data so value survives restarts
        try:
            subentries = getattr(self.coordinator.config_entry, "subentries", {})
            subentry = subentries.get(self._appliance_id)
            if subentry is not None:
                new_data = dict(subentry.data)
                new_data["appliance_priority"] = int(value)
                await self.hass.config_entries.async_update_subentry(
                    self.coordinator.config_entry, subentry, data=new_data
                )
        except Exception:
            # async_update_subentry may not exist in older HA versions;
            # runtime override still works until restart
            _LOGGER.debug(
                "Could not persist priority for %s (HA version may not support subentry updates)",
                self._appliance_id,
            )
        self.async_write_ha_state()


class ApplianceMinDailyRuntimeNumber(CoordinatorEntity[PvExcessCoordinator], NumberEntity):
    """Per-appliance minimum daily runtime (minutes).

    0 means "disabled" (no minimum). Writes persist to the subentry so
    the value survives restarts. If the user writes a value above the
    current max, it is silently clamped down to max to maintain
    min <= max. The Max entity handles the symmetric case.
    """

    _attr_has_entity_name = True
    _attr_native_min_value = 0.0
    _attr_native_max_value = 1440.0
    _attr_native_step = 1.0
    _attr_native_unit_of_measurement = "min"
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:timer-sand"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: PvExcessCoordinator,
        appliance_id: str,
        appliance_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._appliance_id = appliance_id
        self._appliance_name = appliance_name
        self._attr_name = f"{appliance_name} Min Daily Runtime"
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{appliance_id}_min_daily_runtime"
        )

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.config_entry.entry_id)},
            name="PV Excess Control",
            manufacturer=MANUFACTURER,
        )

    @property
    def native_value(self) -> float:
        raw = self.coordinator.appliance_min_daily_runtime.get(self._appliance_id)
        return 0.0 if raw is None else float(raw)

    async def async_set_native_value(self, value: float) -> None:
        new_value: int | None = None if int(value) == 0 else int(value)
        if new_value is not None:
            max_effective = self._effective_max()
            if max_effective is not None and new_value > max_effective:
                new_value = max_effective
        self.coordinator.appliance_min_daily_runtime[self._appliance_id] = new_value
        await self._persist_to_subentry(new_value)
        self.async_write_ha_state()

    def _effective_max(self) -> int | None:
        """Read current max from the runtime dict, falling through to subentry.data."""
        runtime = self.coordinator.appliance_max_daily_runtime
        if self._appliance_id in runtime:
            return runtime[self._appliance_id]
        subentries = getattr(self.coordinator.config_entry, "subentries", {})
        subentry = subentries.get(self._appliance_id) if subentries else None
        if subentry is None:
            return None
        return subentry.data.get(CONF_MAX_DAILY_RUNTIME)

    async def _persist_to_subentry(self, new_value: int | None) -> None:
        try:
            subentries = getattr(self.coordinator.config_entry, "subentries", {})
            subentry = subentries.get(self._appliance_id)
            if subentry is not None:
                new_data = dict(subentry.data)
                if new_value is None:
                    new_data.pop(CONF_MIN_DAILY_RUNTIME, None)
                else:
                    new_data[CONF_MIN_DAILY_RUNTIME] = new_value
                await self.hass.config_entries.async_update_subentry(
                    self.coordinator.config_entry, subentry, data=new_data
                )
        except Exception:
            _LOGGER.debug(
                "Could not persist min_daily_runtime for %s (HA version may not support subentry updates)",
                self._appliance_id,
            )


class ApplianceMaxDailyRuntimeNumber(CoordinatorEntity[PvExcessCoordinator], NumberEntity):
    """Per-appliance maximum daily runtime (minutes).

    0 means "disabled" (no maximum). Writes persist to the subentry so
    the value survives restarts. If the user writes a value below the
    current min, the min is silently lowered to match before the max
    is written, so min <= max always holds. The Min entity's state
    refreshes on its next scheduled update.
    """

    _attr_has_entity_name = True
    _attr_native_min_value = 0.0
    _attr_native_max_value = 1440.0
    _attr_native_step = 1.0
    _attr_native_unit_of_measurement = "min"
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:timer-alert-outline"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: PvExcessCoordinator,
        appliance_id: str,
        appliance_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._appliance_id = appliance_id
        self._appliance_name = appliance_name
        self._attr_name = f"{appliance_name} Max Daily Runtime"
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{appliance_id}_max_daily_runtime"
        )

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.config_entry.entry_id)},
            name="PV Excess Control",
            manufacturer=MANUFACTURER,
        )

    @property
    def native_value(self) -> float:
        raw = self.coordinator.appliance_max_daily_runtime.get(self._appliance_id)
        return 0.0 if raw is None else float(raw)

    async def async_set_native_value(self, value: float) -> None:
        new_value: int | None = None if int(value) == 0 else int(value)
        if new_value is not None:
            min_effective = self._effective_min()
            if min_effective is not None and new_value < min_effective:
                # Lower the sibling (min) to match the new max before writing self.
                self.coordinator.appliance_min_daily_runtime[self._appliance_id] = new_value
                await self._persist_min_to_subentry(new_value)
        self.coordinator.appliance_max_daily_runtime[self._appliance_id] = new_value
        await self._persist_to_subentry(new_value)
        self.async_write_ha_state()

    def _effective_min(self) -> int | None:
        """Read current min from the runtime dict, falling through to subentry.data."""
        runtime = self.coordinator.appliance_min_daily_runtime
        if self._appliance_id in runtime:
            return runtime[self._appliance_id]
        subentries = getattr(self.coordinator.config_entry, "subentries", {})
        subentry = subentries.get(self._appliance_id) if subentries else None
        if subentry is None:
            return None
        return subentry.data.get(CONF_MIN_DAILY_RUNTIME)

    async def _persist_min_to_subentry(self, new_value: int | None) -> None:
        """Write the sibling min_daily_runtime into subentry.data."""
        try:
            subentries = getattr(self.coordinator.config_entry, "subentries", {})
            subentry = subentries.get(self._appliance_id)
            if subentry is not None:
                new_data = dict(subentry.data)
                if new_value is None:
                    new_data.pop(CONF_MIN_DAILY_RUNTIME, None)
                else:
                    new_data[CONF_MIN_DAILY_RUNTIME] = new_value
                await self.hass.config_entries.async_update_subentry(
                    self.coordinator.config_entry, subentry, data=new_data
                )
        except Exception:
            _LOGGER.debug(
                "Could not persist sibling min_daily_runtime for %s (HA version may not support subentry updates)",
                self._appliance_id,
            )

    async def _persist_to_subentry(self, new_value: int | None) -> None:
        try:
            subentries = getattr(self.coordinator.config_entry, "subentries", {})
            subentry = subentries.get(self._appliance_id)
            if subentry is not None:
                new_data = dict(subentry.data)
                if new_value is None:
                    new_data.pop(CONF_MAX_DAILY_RUNTIME, None)
                else:
                    new_data[CONF_MAX_DAILY_RUNTIME] = new_value
                await self.hass.config_entries.async_update_subentry(
                    self.coordinator.config_entry, subentry, data=new_data
                )
        except Exception:
            _LOGGER.debug(
                "Could not persist max_daily_runtime for %s (HA version may not support subentry updates)",
                self._appliance_id,
            )


class _BatteryAssistNumber(CoordinatorEntity[PvExcessCoordinator], NumberEntity):
    """Base for the per-appliance Battery Assist knobs.

    Values live in coordinator dicts (read each cycle, no reload) and persist to
    the appliance subentry like priority / switch interval.
    """

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.BOX
    _conf_key: str
    _store_attr: str
    _default: float
    _name_suffix: str
    _unique_suffix: str

    def __init__(
        self,
        coordinator: PvExcessCoordinator,
        appliance_id: str,
        appliance_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._appliance_id = appliance_id
        self._appliance_name = appliance_name
        self._attr_name = f"{appliance_name} {self._name_suffix}"
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{appliance_id}_{self._unique_suffix}"
        )

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.config_entry.entry_id)},
            name="PV Excess Control",
            manufacturer=MANUFACTURER,
        )

    @property
    def _store(self) -> dict[str, float]:
        return getattr(self.coordinator, self._store_attr)

    @property
    def native_value(self) -> float:
        return float(self._store.get(self._appliance_id, self._default))

    def _clamp(self, value: float) -> float:
        return max(self._attr_native_min_value, min(float(value), self._attr_native_max_value))

    async def _persist_to_subentry(self, value: float) -> None:
        try:
            subentries = getattr(self.coordinator.config_entry, "subentries", {})
            subentry = subentries.get(self._appliance_id)
            if subentry is not None:
                new_data = dict(subentry.data)
                new_data[self._conf_key] = value
                self.hass.config_entries.async_update_subentry(
                    self.coordinator.config_entry, subentry, data=new_data
                )
        except Exception:
            _LOGGER.debug(
                "Could not persist %s for %s (HA version may not support subentry updates)",
                self._conf_key, self._appliance_id,
            )

    async def async_set_native_value(self, value: float) -> None:
        new_value = self._clamp(value)
        self._store[self._appliance_id] = new_value
        await self._persist_to_subentry(new_value)
        self.async_write_ha_state()


class BatteryAssistMaxShareNumber(_BatteryAssistNumber):
    """Max share (%) of the appliance's draw the home battery may cover."""

    _attr_native_min_value = 0.0
    _attr_native_max_value = MAX_BATTERY_ASSIST_SHARE
    _attr_native_step = 5.0
    _attr_icon = "mdi:battery-arrow-down"
    _conf_key = CONF_BATTERY_ASSIST_MAX_SHARE
    _store_attr = "appliance_battery_assist_max_share"
    _default = DEFAULT_BATTERY_ASSIST_MAX_SHARE
    _name_suffix = "Battery Assist Max Share"
    _unique_suffix = "battery_assist_max_share"


class BatteryAssistMinSocNumber(_BatteryAssistNumber):
    """Battery SoC floor (%) below which Battery Assist stops using the battery.

    Seeded ONCE from input_number.battery_assist_threshold (if present and
    numeric) when the appliance has no stored value, else DEFAULT (61).
    """

    _attr_native_min_value = 20.0
    _attr_native_max_value = 95.0
    _attr_native_step = 1.0
    _attr_icon = "mdi:battery-lock"
    _conf_key = CONF_BATTERY_ASSIST_MIN_SOC
    _store_attr = "appliance_battery_assist_min_soc"
    _default = DEFAULT_BATTERY_ASSIST_MIN_SOC
    _name_suffix = "Battery Assist Min SoC"
    _unique_suffix = "battery_assist_min_soc"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        subentries = getattr(self.coordinator.config_entry, "subentries", {})
        subentry = subentries.get(self._appliance_id)
        if subentry is None or CONF_BATTERY_ASSIST_MIN_SOC in subentry.data:
            return
        seed = self._default
        state = self.hass.states.get(BATTERY_ASSIST_SEED_ENTITY)
        if state is not None:
            try:
                seed = float(state.state)
            except (TypeError, ValueError):
                pass
        seed = self._clamp(seed)
        self._store[self._appliance_id] = seed
        await self._persist_to_subentry(seed)
