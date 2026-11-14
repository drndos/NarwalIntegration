"""States around remote control, from live Flow 2 payloads (fw 01.10.30.02).

Captured by e2e/run_e2e.py: after manual drive ended ~2 m from the dock the
robot reported 19 and sat still; a recall was accepted and it drove home
reporting 10 with 3.10 = 2, then 19 + 18 = 4 once seated.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

# Install HA stubs before any custom_components import
import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from homeassistant.components.vacuum import VacuumActivity, VacuumEntityFeature  # noqa: E402

from custom_components.narwal.coordinator import can_return_home  # noqa: E402
from custom_components.narwal.remote_control import async_start_remote_control  # noqa: E402
from custom_components.narwal.narwal_client import (  # noqa: E402
    CommandResponse,
    CommandResult,
    NarwalCommandError,
)
from narwal_client.const import WorkingStatus  # noqa: E402
from narwal_client.models import NarwalState  # noqa: E402
from tests.test_vacuum_segments import _make_vacuum  # noqa: E402

DRIVING = {"3": {"1": 21, "19": 1}, "11": 1, "47": 2, "31": 2}
IDLE_AFTER_DRIVE = {"3": {"1": 19, "18": 4}, "11": 1, "47": 2}
RECALLED = {"3": {"1": 10, "10": 2}, "11": 1, "47": 2}
SEATED = {"3": {"1": 19, "18": 4}, "11": 2, "47": 3}


def _state(*payloads: dict) -> NarwalState:
    state = NarwalState()
    for payload in payloads:
        state.update_from_base_status(payload)
    return state


class TestIdleAfterRemoteControl:
    def test_status_19_after_drive_is_idle_not_returning(self) -> None:
        state = _state(DRIVING, IDLE_AFTER_DRIVE)
        assert state.working_status == WorkingStatus.TASK_COMPLETED
        assert state.is_idle_after_remote_control
        assert not state.is_returning
        vac = _make_vacuum(state)
        assert vac.activity == VacuumActivity.IDLE
        assert vac.extra_state_attributes["task_status"] == "idle"

    def test_return_home_offered_after_drive(self) -> None:
        state = _state(DRIVING, IDLE_AFTER_DRIVE)
        assert can_return_home(state)
        vac = _make_vacuum(state)
        assert vac.supported_features & VacuumEntityFeature.RETURN_HOME

    def test_status_19_without_drive_keeps_old_meaning(self) -> None:
        state = _state(IDLE_AFTER_DRIVE)
        assert not state.is_idle_after_remote_control
        assert not can_return_home(state)

    def test_flag_cleared_once_robot_moves_on(self) -> None:
        state = _state(DRIVING, IDLE_AFTER_DRIVE, RECALLED)
        assert not state.after_remote_control
        state.update_from_base_status(IDLE_AFTER_DRIVE)
        assert not state.is_idle_after_remote_control

    def test_seated_after_drive_is_docked(self) -> None:
        state = _state(DRIVING, IDLE_AFTER_DRIVE, SEATED)
        assert not state.is_idle_after_remote_control
        assert state.is_docked
        assert _make_vacuum(state).activity == VacuumActivity.DOCKED


class TestRecallFromIdle:
    def test_status_10_off_dock_with_3_10_is_returning(self) -> None:
        state = _state(RECALLED)
        assert state.is_returning
        assert not state.is_docked
        vac = _make_vacuum(state)
        assert vac.activity == VacuumActivity.RETURNING
        assert vac.extra_state_attributes["task_status"] == "returning"

    def test_status_10_seated_is_docked(self) -> None:
        state = _state({"3": {"1": 10}, "11": 2, "47": 3})
        assert not state.is_returning
        assert state.is_docked


def _coordinator(client: MagicMock) -> MagicMock:
    coordinator = MagicMock()
    coordinator.remote_control_suction = "off"
    coordinator.client = client
    return coordinator


class TestStartRetry:
    def test_unanswered_start_wakes_and_retries_once(self) -> None:
        client = MagicMock()
        client.robot_awake = True  # stale: the robot had dozed off
        client.remote_control_active = True
        client.wake = AsyncMock(return_value=True)
        client.remote_control_start = AsyncMock(
            side_effect=[
                NarwalCommandError("No response within 3.0s"),
                CommandResponse(result_code=CommandResult.SUCCESS),
            ]
        )
        asyncio.run(async_start_remote_control(_coordinator(client)))
        assert client.remote_control_start.await_count == 2
        client.wake.assert_awaited_once()

    def test_second_failure_is_reported(self) -> None:
        from homeassistant.exceptions import HomeAssistantError

        client = MagicMock()
        client.robot_awake = True
        client.wake = AsyncMock(return_value=True)
        client.remote_control_start = AsyncMock(side_effect=NarwalCommandError("timeout"))
        try:
            asyncio.run(async_start_remote_control(_coordinator(client)))
        except HomeAssistantError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected HomeAssistantError")
        assert client.remote_control_start.await_count == 2


