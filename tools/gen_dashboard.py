"""Generate the Narwal dashboard section.

One sections-view grid: the map, on which you tap rooms in the order to clean
them (as in the Narwal app), the vacuum commands with the six clean settings
under it, and remote control. While rooms are picked the six settings edit
those rooms; with none picked they are the whole-house defaults.

It reads Home Assistant's entity registry rather than the robot, because the
registry is the only place that knows which entity_id belongs to which room:
entity_ids are minted from the room name at first registration and never
change, while unique_ids carry the stable ``map_<id>_room_<id>`` key.

Usage (from the repo root, with a copy of the registry file)::

    py tools/gen_dashboard.py --registry core.entity_registry \
        --vacuum vacuum.narwal_flow_vacuum --out build/

Outputs, all plain YAML you paste into your own dashboard / config:

- ``narwal_section.yaml``       — the section above
- ``narwal_dock_section.yaml``   — a grid with the five dock task switches
- ``narwal_drive_input_number.yaml`` / ``narwal_drive_script.yaml`` — the
  speed slider's ``input_number`` and ``script.narwal_drive``, which scales
  each pad move by it (dashboard actions cannot template)

The map is ``custom:narwal-map-card``, served by the integration itself; every
other card is built in.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

ROOM_KEY = re.compile(r"_map_(?P<map>\w+?)_room_(?P<room>\d+)_(?P<key>mode|suction|water|scrub|route|passes|selected|clean_order)$")
PROFILE_KEYS = ("mode", "suction", "water", "scrub", "route", "passes")
# Device-level clean settings, by the part of the unique_id after the device id.
GLOBAL_KEYS = {
    "mode": "mode",
    "runtime_suction": "suction",
    "runtime_water": "water",
    "scrub": "scrub",
    "route": "route",
    "passes": "passes",
}
DOCK_SUFFIXES = {
    "_empty_dustbin": "Empty dustbin",
    "_wash_mop": "Wash mop",
    "_dry_mop": "Dry mop",
    "_dry_dust_bin": "Dry dust bin",
    "_dry_dock_bag": "Dry dock bag",
}
# Device-level entities, keyed by the part of the unique_id after the device id.
VACUUM_KEYS = {
    "battery": "sensor",
    "charging_state": "sensor",
    "error": "binary_sensor",
    "map": "camera",
    "cleaning_area": "sensor",
    "cleaning_time": "sensor",
    "remaining_time": "sensor",
    "remote_control": "switch",
    "remote_control_suction": "select",
    "suction_cooldown": "sensor",
    "remote_control_suction_running": "binary_sensor",
}
# Drive pad, row by row: (name, icon, linear %, angular %) at full speed;
# + forward, + left. script.narwal_drive scales both by the speed slider, so
# the default 50 % gives forward 50, turn 40.
DRIVE_PAD = (
    ("Forward left", "mdi:arrow-top-left", 80, 70),
    ("Forward", "mdi:arrow-up-bold", 100, 0),
    ("Forward right", "mdi:arrow-top-right", 80, -70),
    ("Turn left", "mdi:rotate-left", 0, 80),
    ("Hold still", "mdi:stop-circle-outline", 0, 0),
    ("Turn right", "mdi:rotate-right", 0, -80),
    ("Back left", "mdi:arrow-bottom-left", -70, 60),
    ("Back", "mdi:arrow-down-bold", -80, 0),
    ("Back right", "mdi:arrow-bottom-right", -70, -60),
)
DRIVE_SPEED = "input_number.narwal_drive_speed"
DRIVE_SCRIPT = "script.narwal_drive"
DRIVE_TAP_SECONDS = 1.0
DRIVE_HOLD_SECONDS = 3.0
LABELS = {
    "mode": "Clean mode",
    "suction": "Suction",
    "water": "Water",
    "scrub": "Scrub",
    "route": "Route",
    "passes": "Passes",
}
SETTING_ICONS = {
    "mode": "mdi:broom",
    "suction": "mdi:fan",
    "water": "mdi:water",
    "scrub": "mdi:brush",
    "route": "mdi:map-marker-path",
    "passes": "mdi:repeat",
}


def load_registry(path: Path) -> list[dict]:
    """Accept the raw .storage file or a pre-filtered list of entries."""
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    entries = payload["data"]["entities"] if isinstance(payload, dict) else payload
    return [e for e in entries if e.get("platform") == "narwal" or "platform" not in e]


def room_name(entry: dict, key: str) -> str:
    """Prefer the user's override name, else strip the key suffix from the original."""
    name = entry.get("name") or entry.get("original_name") or entry["entity_id"]
    suffix = " cleaning order" if key == "clean_order" else " " + key
    return name[: -len(suffix)] if name.lower().endswith(suffix) else name


def collect(entries: list[dict], map_id: str | None):
    rooms: dict[int, dict] = {}
    globals_: dict[str, str] = {}
    dock: dict[str, str] = {}
    maps: set[str] = set()
    for e in entries:
        uid, eid = e["unique_id"], e["entity_id"]
        if m := ROOM_KEY.search(uid):
            if map_id is not None and m["map"] != map_id:
                continue
            if e.get("disabled_by") is not None:
                if m["key"] == "selected":
                    sys.exit(
                        f"{eid} is disabled; re-enable it and clear any stale "
                        "selection before generating the dashboard"
                    )
                continue
            maps.add(m["map"])
            room = rooms.setdefault(int(m["room"]), {"entities": {}})
            room["entities"][m["key"]] = eid
            room.setdefault("name", room_name(e, m["key"]))
            continue
        device_key = uid.split("_", 1)[1] if "_" in uid else ""
        if (
            device_key in GLOBAL_KEYS
            and eid.startswith("select.")
            and e.get("disabled_by") is None
        ):
            globals_[GLOBAL_KEYS[device_key]] = eid
        for suffix, label in DOCK_SUFFIXES.items():
            if uid.endswith(suffix) and eid.startswith("switch."):
                dock[label] = eid
    if map_id is None and len(maps) > 1:
        sys.exit(f"registry holds rooms for several maps {sorted(maps)}; pass --map-id")
    complete = {rid: r for rid, r in rooms.items() if "selected" in r["entities"]}
    return dict(sorted(complete.items(), key=lambda kv: kv[1]["name"].lower())), globals_, dock


def collect_vacuum(entries: list[dict]) -> dict[str, str]:
    """Device-level entities by key; disabled ones are left out."""
    found: dict[str, str] = {}
    for e in entries:
        uid, eid = e["unique_id"], e["entity_id"]
        if "_" not in uid or e.get("disabled_by") is not None:
            continue
        key = uid.split("_", 1)[1]  # device ids are hex, no underscores
        if VACUUM_KEYS.get(key) == eid.split(".", 1)[0]:
            found[key] = eid
    return found


def tile(entity: str, name: str, feature: str | None = None, **extra) -> dict:
    card = {"type": "tile", "entity": entity, "name": name}
    if feature:
        card["features"] = [{"type": feature}]
    card.update(extra)
    return card


def button(name: str, icon: str, action: str, *, target: dict | None = None, data: dict | None = None) -> dict:
    tap = {"action": "perform-action", "perform_action": action}
    if target:
        tap["target"] = target
    if data:
        tap["data"] = data
    return {"type": "button", "name": name, "icon": icon, "show_state": False, "tap_action": tap}


def map_card(camera: str, rooms) -> dict:
    """The integration's map card: tap rooms to select them in cleaning order."""
    return {
        "type": "custom:narwal-map-card",
        "camera": camera,
        "rooms": [{"id": rid, "selected": room["entities"]["selected"]} for rid, room in rooms.items()],
        "grid_options": {"columns": 12},
    }


def dock_section(dock) -> dict:
    return {
        "type": "grid",
        "cards": [
            {"type": "heading", "heading": "Dock tasks", "heading_style": "title", "icon": "mdi:home-import-outline"},
            {
                "type": "markdown",
                "content": "A switch is on while that task runs and shows time left. It is only "
                "available when the task can be started or safely stopped right now.",
            },
            *[
                tile(eid, label, "toggle", state_content=["state", "time_left"])
                for label, eid in dock.items()
            ],
        ],
    }


def _when(entity: str, state: str, *, negate: bool = False) -> list[dict]:
    return [{"condition": "state", "entity": entity, "state_not" if negate else "state": state}]


def _full(card: dict) -> dict:
    return {**card, "features_position": "inline", "grid_options": {"columns": 12}}


def robot_cards(ents: dict[str, str], vacuum: str, rooms=None, globals_=None) -> list[dict]:
    """Map, native commands, the six clean settings and live status."""
    badges = [
        {"type": "entity", "entity": ents[key], "show_icon": True, "show_state": True}
        for key in ("battery", "charging_state")
        if key in ents
    ]
    if "error" in ents:
        badges.append({"type": "entity", "entity": ents["error"], "color": "red",
                       "visibility": _when(ents["error"], "on")})
    cards: list[dict] = [{"type": "heading", "heading": "Narwal", "icon": "mdi:robot-vacuum", "badges": badges}]
    if "map" in ents and rooms:
        cards.append(map_card(ents["map"], rooms))
    elif "map" in ents:
        cards.append({"type": "picture-entity", "entity": ents["map"], "camera_view": "auto",
                      "show_name": False, "show_state": False})
    cards.append({
        "type": "tile", "entity": vacuum, "name": "Vacuum",
        "state_content": ["state", "status_summary"],
        "features": [{"type": "vacuum-commands",
                      "commands": ["start_pause", "stop", "return_home", "locate"]}],
        "features_position": "bottom",
        "grid_options": {"columns": 12},
    })
    globals_ = globals_ or {}
    for key in PROFILE_KEYS:
        if key in globals_:
            cards.append(_full(tile(globals_[key], LABELS[key], "select-options", icon=SETTING_ICONS[key])))
    for key, name in (("cleaning_area", "Area"), ("cleaning_time", "Time"), ("remaining_time", "Left")):
        if key in ents:
            cards.append({**tile(ents[key], name), "vertical": True,
                          "grid_options": {"columns": 4}, "visibility": _when(vacuum, "cleaning")})
    return cards


def drive_button(name: str, icon: str, linear: int, angular: int, vacuum: str) -> dict:
    def move(seconds: float) -> dict:
        if not (linear or angular):
            # Stop now; no speed to scale.
            return {
                "action": "perform-action",
                "perform_action": "narwal.remote_control_move",
                "target": {"entity_id": vacuum},
                "data": {"linear": 0, "angular": 0, "duration": 0},
            }
        return {
            "action": "perform-action",
            "perform_action": DRIVE_SCRIPT,
            "data": {"linear": linear, "angular": angular, "duration": seconds},
        }

    return {
        "type": "button", "name": name, "icon": icon, "show_name": False, "show_state": False,
        "tap_action": move(DRIVE_TAP_SECONDS),
        "hold_action": move(DRIVE_HOLD_SECONDS),
        "grid_options": {"columns": 4, "rows": 1},
    }


def remote_cards(ents: dict[str, str], vacuum: str) -> list[dict]:
    """Remote control: on/off, suction, and a drive pad shown only while driving."""
    switch = ents.get("remote_control")
    if switch is None:
        return []
    cards: list[dict] = [
        {"type": "heading", "heading": "Remote control", "heading_style": "subtitle",
         "icon": "mdi:gamepad-variant"},
        _full(tile(switch, "Remote control", "toggle", icon="mdi:gamepad-variant")),
    ]
    if "remote_control_suction" in ents:
        cards.append(_full(tile(ents["remote_control_suction"], "Suction while driving", "select-options", icon="mdi:fan")))
    cards.append({
        **_full(tile(DRIVE_SPEED, "Drive speed", icon="mdi:speedometer")),
        "features": [{"type": "numeric-input", "style": "slider"}],
    })
    cards.append({
        "type": "markdown",
        "content": (
            f"Turn on remote control to drive. Tap an arrow for a {DRIVE_TAP_SECONDS:g} s nudge, "
            f"hold it for {DRIVE_HOLD_SECONDS:g} s. Manual drive ends after 30 s without a move "
            "and the robot stays where it is: send it home with the return button above."
        ),
        "visibility": _when(switch, "on", negate=True),
    })
    cards += [{**drive_button(*pad, vacuum), "visibility": _when(switch, "on")} for pad in DRIVE_PAD]
    for key, name in (("suction_cooldown", "Suction cool-down"), ("remote_control_suction_running", "Suction running")):
        if key in ents:
            cards.append({**tile(ents[key], name), "grid_options": {"columns": 6}, "visibility": _when(switch, "on")})
    return cards


def drive_speed_config() -> dict:
    """input_number helper behind the drive-speed slider."""
    return {
        DRIVE_SPEED.split(".", 1)[1]: {
            "name": "Narwal drive speed", "icon": "mdi:speedometer",
            "min": 10, "max": 100, "step": 10, "initial": 50,
            "unit_of_measurement": "%", "mode": "slider",
        }
    }


def drive_script_config(vacuum: str) -> dict:
    """script.narwal_drive: one pad move scaled by the drive-speed slider."""
    def scaled(field: str) -> str:
        return (
            f"{{{{ ({field} | float(0) * states('{DRIVE_SPEED}') | float(50) / 100)"
            " | round(0) | int }}"
        )

    def field(name: str) -> dict:  # fresh dicts: shared ones dump as YAML anchors
        return {"name": name, "required": True, "selector": {"number": {"min": -100, "max": 100}}}

    return {
        DRIVE_SCRIPT.split(".", 1)[1]: {
            "alias": "Narwal: drive",
            "description": "Remote-control move scaled by the drive-speed slider.",
            "icon": "mdi:gamepad-variant",
            # A new tap replaces the move in flight.
            "mode": "restart",
            "fields": {
                "linear": field("Linear at full speed"),
                "angular": field("Turn at full speed"),
                "duration": {"name": "Duration", "default": DRIVE_TAP_SECONDS,
                             "selector": {"number": {"min": 0, "max": 5, "step": 0.1}}},
            },
            "sequence": [{
                "action": "narwal.remote_control_move",
                "target": {"entity_id": vacuum},
                "data": {"linear": scaled("linear"), "angular": scaled("angular"),
                         "duration": "{{ duration | float(1) }}"},
            }],
        }
    }


def narwal_section(ents: dict[str, str], vacuum: str, rooms=None, globals_=None) -> dict:
    """One grid: map with tap-to-select rooms, vacuum and clean settings, remote control."""
    return {"type": "grid", "cards": robot_cards(ents, vacuum, rooms, globals_) + remote_cards(ents, vacuum)}


def dump(obj) -> str:
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, width=100)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--registry", required=True, type=Path, help="core.entity_registry (or a JSON list of narwal entries)")
    parser.add_argument("--vacuum", required=True, help="vacuum entity_id, e.g. vacuum.narwal_flow_vacuum")
    parser.add_argument("--map-id", help="restrict to one map when the registry knows several")
    parser.add_argument("--out", type=Path, default=Path("build"))
    args = parser.parse_args()

    entries = load_registry(args.registry)
    args.out.mkdir(parents=True, exist_ok=True)
    vacuum_ents = collect_vacuum(entries)
    rooms, globals_, dock = collect(entries, args.map_id)
    if not rooms:
        print(
            "warning: no enabled room controls found; the map will not select rooms. "
            "Enable the Narwal room entities you want, then run this tool again",
            file=sys.stderr,
        )
    missing = [k for k in PROFILE_KEYS if k not in globals_]
    if missing:
        print(f"warning: clean setting selects missing for {missing}", file=sys.stderr)

    (args.out / "narwal_section.yaml").write_text(
        dump([narwal_section(vacuum_ents, args.vacuum, rooms, globals_)]),
        encoding="utf-8",
    )
    if "remote_control" in vacuum_ents:
        (args.out / "narwal_drive_input_number.yaml").write_text(dump(drive_speed_config()), encoding="utf-8")
        (args.out / "narwal_drive_script.yaml").write_text(
            dump(drive_script_config(args.vacuum)), encoding="utf-8"
        )
    (args.out / "narwal_dock_section.yaml").write_text(dump([dock_section(dock)]) if dock else "", encoding="utf-8")
    print(
        f"{len(vacuum_ents)} vacuum entities, {len(rooms)} rooms, {len(globals_)} global selects, "
        f"{len(dock)} dock switches -> {args.out}/narwal_section.yaml"
    )
    for rid, room in rooms.items():
        print(f"  room {rid:>3}  {room['name']}")


if __name__ == "__main__":
    main()
