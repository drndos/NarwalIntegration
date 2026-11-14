"""Remote control (manual drive) entities and helpers.

The robot is driven with telecontrol/set_manual_control_mode and a 200 ms
telecontrol/velocity_control stream owned by NarwalClient. Home Assistant has
no joystick entity, so driving is done through the narwal.remote_control_*
services; these entities expose the session state and the suction choice.

Verified only on the Flow 2. Other
models get the entities disabled by default until someone confirms them.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.exceptions import HomeAssistantError

from .const import CONF_PRODUCT_KEY
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import (
    CommandResult,
    ManualControlMode,
    NarwalCommandError,
    NarwalConnectionError,
)

# Product keys where remote control was verified live.
REMOTE_CONTROL_VERIFIED_KEYS = frozenset({"QxMSPG6VSO", "iSuVlI1If2", "mkbqaprvrb"})

# Suction choices in the app's order: off (joystick only), quiet ... ultra.
RC_SUCTION_OPTIONS: dict[str, ManualControlMode] = {
    "off": ManualControlMode.JOYSTICK,
    "quiet": ManualControlMode.SWEEP_MUTE,
    "standard": ManualControlMode.SWEEP_NORMAL,
    "strong": ManualControlMode.SWEEP_STRONG,
    "super": ManualControlMode.SWEEP_DEEP,
    "ultra": ManualControlMode.SWEEP_SUPER,
}
_RC_SUCTION_LABELS = {mode: label for label, mode in RC_SUCTION_OPTIONS.items()}


def remote_control_verified(entry: ConfigEntry) -> bool:
    """True when remote control was verified on this entry's model."""
    return entry.data.get(CONF_PRODUCT_KEY, "") in REMOTE_CONTROL_VERIFIED_KEYS


def _suction_label(mode: int | None) -> str | None:
    if mode is None:
        return None
    try:
        return _RC_SUCTION_LABELS.get(ManualControlMode(mode), "off")
    except ValueError:
        return None


async def async_start_remote_control(
    coordinator: NarwalCoordinator, suction: str | None = None
) -> None:
    """Enter remote control with the given (or remembered) suction choice."""
    if suction is not None:
        coordinator.remote_control_suction = suction
    mode = RC_SUCTION_OPTIONS[coordinator.remote_control_suction]
    client = coordinator.client
    try:
        if not client.robot_awake:
            # The wake burst carries notify_app_event, which is harmless
            # before remote control starts and fatal during it.
            await client.wake(timeout=10.0)
        resp = await client.remote_control_start(mode)
    except (NarwalCommandError, NarwalConnectionError) as err:
        raise HomeAssistantError(f"Narwal remote control failed: {err}") from err
    if resp.result_code == CommandResult.NOT_APPLICABLE and not client.remote_control_active:
        raise HomeAssistantError(
            "Narwal remote control is not possible now (pause or stop the clean first)"
        )
    coordinator.async_update_listeners()


async def async_stop_remote_control(coordinator: NarwalCoordinator) -> None:
    """Leave remote control."""
    try:
        await coordinator.client.remote_control_stop()
    except (NarwalCommandError, NarwalConnectionError) as err:
        raise HomeAssistantError(f"Narwal remote control stop failed: {err}") from err
    coordinator.async_update_listeners()


class _RemoteControlEntity(NarwalEntity):
    """Common setup for remote-control entities."""

    def __init__(self, coordinator: NarwalCoordinator, key: str) -> None:
        super().__init__(coordinator)
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_{key}"
        self._attr_translation_key = key
        if not remote_control_verified(coordinator.config_entry):
            self._attr_entity_registry_enabled_default = False


class NarwalRemoteControlSwitch(_RemoteControlEntity, SwitchEntity):
    """On while the robot is in remote control (working status 21)."""

    _attr_icon = "mdi:gamepad-variant"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator, "remote_control")

    @property
    def is_on(self) -> bool:
        state = self.coordinator.data
        return self.coordinator.client.remote_control_active or bool(
            state is not None and state.is_remote_control
        )

    async def async_turn_on(self, **kwargs) -> None:
        await async_start_remote_control(self.coordinator)

    async def async_turn_off(self, **kwargs) -> None:
        await async_stop_remote_control(self.coordinator)


class NarwalRemoteControlSuctionSelect(_RemoteControlEntity, SelectEntity):
    """Suction while driving; remembered for the next session when idle."""

    _attr_icon = "mdi:fan"
    _attr_options = list(RC_SUCTION_OPTIONS)

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator, "remote_control_suction")

    @property
    def current_option(self) -> str | None:
        client = self.coordinator.client
        if client.remote_control_active:
            return _suction_label(client.remote_control_mode)
        return self.coordinator.remote_control_suction

    async def async_select_option(self, option: str) -> None:
        self.coordinator.remote_control_suction = option
        client = self.coordinator.client
        if client.remote_control_active:
            try:
                await client.remote_control_set_suction(RC_SUCTION_OPTIONS[option])
            except (NarwalCommandError, NarwalConnectionError) as err:
                raise HomeAssistantError(
                    f"Narwal remote control suction failed: {err}"
                ) from err
        self.async_write_ha_state()


class NarwalFanCooldownSensor(_RemoteControlEntity, SensorEntity):
    """Suction cool-down the robot imposes in remote control (field 37)."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator, "suction_cooldown")

    @property
    def native_value(self) -> int | None:
        state = self.coordinator.data
        return None if state is None else state.fan_cooldown_seconds


class NarwalRemoteSuctionBinarySensor(_RemoteControlEntity, BinarySensorEntity):
    """On while suction runs in remote control (field 3.19 = 2)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:fan"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator, "remote_control_suction_running")

    @property
    def is_on(self) -> bool:
        state = self.coordinator.data
        return bool(state is not None and state.remote_control_suction_running)
