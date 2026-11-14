"""Tests for the optional Narwal dashboard generator."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("yaml", MagicMock())

from tools.gen_dashboard import (  # noqa: E402
    collect,
    collect_vacuum,
    drive_script_config,
    narwal_section,
)


def test_collect_ignores_disabled_room_controls() -> None:
    """Generated dashboards only target entities that HA actually creates."""
    entries = [
        {
            "unique_id": "dev_map_200_room_4_mode",
            "entity_id": "select.narwal_kitchen_mode",
            "original_name": "Kitchen mode",
            "disabled_by": "integration",
        },
        {
            "unique_id": "dev_map_100_room_5_selected",
            "entity_id": "switch.narwal_hallway_selected",
            "original_name": "Hallway selected",
            "disabled_by": None,
        },
    ]

    rooms, _, _ = collect(entries, map_id=None)

    assert list(rooms) == [5]


def test_collect_rejects_disabled_selection_switch() -> None:
    """A possibly selected room cannot disappear from generated clear actions."""
    entries = [
        {
            "unique_id": "dev_map_100_room_4_selected",
            "entity_id": "switch.narwal_kitchen_selected",
            "original_name": "Kitchen selected",
            "disabled_by": "user",
        }
    ]

    with pytest.raises(SystemExit, match="re-enable it and clear"):
        collect(entries, map_id="100")


def test_requested_map_ignores_disabled_selection_on_other_map() -> None:
    """A stale disabled switch cannot block an explicitly requested map."""
    entries = [
        {
            "unique_id": "dev_map_200_room_4_selected",
            "entity_id": "switch.narwal_old_kitchen_selected",
            "disabled_by": "user",
        },
        {
            "unique_id": "dev_map_100_room_5_selected",
            "entity_id": "switch.narwal_hallway_selected",
            "original_name": "Hallway selected",
            "disabled_by": None,
        },
    ]

    rooms, _, _ = collect(entries, map_id="100")

    assert list(rooms) == [5]


def test_collects_room_order_without_corrupting_room_name() -> None:
    """The order entity belongs to the room and strips its longer suffix."""
    entries = [
        {
            "unique_id": "dev_map_100_room_4_clean_order",
            "entity_id": "number.narwal_kitchen_cleaning_order",
            "original_name": "Kitchen cleaning order",
        },
        {
            "unique_id": "dev_map_100_room_4_selected",
            "entity_id": "switch.narwal_kitchen_selected",
            "original_name": "Kitchen selected",
        },
    ]

    rooms, _, _ = collect(entries, map_id="100")

    assert rooms[4]["name"] == "Kitchen"
    assert rooms[4]["entities"]["clean_order"] == ("number.narwal_kitchen_cleaning_order")


KITCHEN = {
    4: {
        "name": "Kitchen",
        "entities": {
            "selected": "switch.narwal_kitchen_selected",
            "clean_order": "number.narwal_kitchen_cleaning_order",
            "mode": "select.narwal_kitchen_mode",
        },
    },
    5: {"name": "Hall", "entities": {"selected": "switch.narwal_hall_selected"}},
}


def test_one_section_holds_map_vacuum_settings_and_remote() -> None:
    """Map, then vacuum with all six clean settings under it, then remote control."""
    ents = {"map": "camera.n_map", "remote_control": "switch.n_rc"}
    settings = {k: f"select.n_{k}" for k in ("mode", "suction", "water", "scrub", "route", "passes")}

    cards = narwal_section(ents, "vacuum.n", KITCHEN, settings)["cards"]

    assert cards[1] == {
        "type": "custom:narwal-map-card",
        "camera": "camera.n_map",
        "rooms": [
            {"id": 4, "selected": "switch.narwal_kitchen_selected"},
            {"id": 5, "selected": "switch.narwal_hall_selected"},
        ],
        "grid_options": {"columns": 12},
    }
    assert cards[2]["entity"] == "vacuum.n"
    assert [c["entity"] for c in cards[3:9]] == list(settings.values())
    remote_at = next(i for i, c in enumerate(cards) if c.get("heading") == "Remote control")
    assert remote_at > 8
    # No per-room settings panel and nothing beyond built-in cards and the map card.
    assert not any("narwal_kitchen" in str(c.get("entity")) for c in cards)
    assert all(not c["type"].startswith("custom:") for c in cards[2:])


def test_device_settings_match_exact_keys() -> None:
    """``work_mode`` must not be taken for the ``mode`` select."""
    entries = [
        {"unique_id": "dev_mode", "entity_id": "select.n_mode", "disabled_by": None},
        {"unique_id": "dev_work_mode", "entity_id": "select.n_clean_mode", "disabled_by": None},
        {"unique_id": "dev_route", "entity_id": "select.n_route", "disabled_by": "user"},
    ]

    _, globals_, _ = collect(entries, map_id=None)

    assert globals_ == {"mode": "select.n_mode"}


def test_section_without_rooms_falls_back_to_plain_map() -> None:
    """No enabled room switches: a plain camera card."""
    cards = narwal_section({"map": "camera.n_map"}, "vacuum.n")["cards"]

    assert cards[1]["type"] == "picture-entity"


DEV = "0123456789abcdef0123456789abcdef"


def _entry(key: str, entity_id: str, disabled: bool = False) -> dict:
    return {"unique_id": f"{DEV}_{key}", "entity_id": entity_id,
            "disabled_by": "integration" if disabled else None}


def test_vacuum_sections_drive_pad_targets_vacuum_and_hides_when_off() -> None:
    """The pad only shows while driving and every button moves the vacuum."""
    ents = collect_vacuum([
        _entry("map", "camera.n_map"),
        _entry("carpet_map", "camera.n_carpet_map"),
        _entry("remote_control", "switch.n_remote_control"),
        _entry("remote_control_suction", "select.n_rc_suction"),
        _entry("suction_cooldown", "sensor.n_cooldown", disabled=True),
        _entry("battery", "sensor.n_battery"),
    ])
    assert ents["map"] == "camera.n_map"
    assert "suction_cooldown" not in ents

    cards = narwal_section(ents, "vacuum.n")["cards"]
    assert cards[1]["entity"] == "camera.n_map"
    pad = [c for c in cards if c["type"] == "button"]
    assert len(pad) == 9
    for button in pad:
        assert button["visibility"] == [
            {"condition": "state", "entity": "switch.n_remote_control", "state": "on"}
        ]
    moving = [b for b in pad if b["name"] != "Hold still"]
    for button in moving:
        assert button["tap_action"]["perform_action"] == "script.narwal_drive"
        assert button["tap_action"]["data"]["duration"] == 1.0
        assert button["hold_action"]["data"]["duration"] == 3.0
    still = next(b for b in pad if b["name"] == "Hold still")
    assert still["tap_action"]["perform_action"] == "narwal.remote_control_move"
    assert still["tap_action"]["target"] == {"entity_id": "vacuum.n"}
    assert still["tap_action"]["data"] == {"linear": 0, "angular": 0, "duration": 0}
    slider = next(c for c in cards if c.get("entity") == "input_number.narwal_drive_speed")
    assert slider["features"] == [{"type": "numeric-input", "style": "slider"}]


def test_drive_script_scales_by_speed_slider() -> None:
    """The script targets the vacuum and reads the slider in both axes."""
    script = drive_script_config("vacuum.n")["narwal_drive"]
    assert script["mode"] == "restart"
    step = script["sequence"][0]
    assert step["action"] == "narwal.remote_control_move"
    assert step["target"] == {"entity_id": "vacuum.n"}
    for axis in ("linear", "angular"):
        assert "input_number.narwal_drive_speed" in step["data"][axis]
        assert step["data"][axis].startswith("{{ (" + axis)


def test_section_without_remote_control_switch() -> None:
    """Models with remote control disabled get no remote-control cards."""
    cards = narwal_section({}, "vacuum.n")["cards"]
    assert not any(c.get("heading") == "Remote control" for c in cards)
