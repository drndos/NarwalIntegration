"""Room centroids for the tap-to-select map card."""

from __future__ import annotations

import zlib

import tests.ha_stubs

tests.ha_stubs.install()

from custom_components.narwal.camera import NarwalMapCamera  # noqa: E402
from narwal_client.map_renderer import room_centroids, room_label_points  # noqa: E402
from narwal_client.models import MapData, RoomInfo  # noqa: E402


def _varint(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _grid(rows: list[list[int]]) -> bytes:
    """Compress rows of pixel values, row 0 first (grid y = 0)."""
    payload = b"".join(_varint(v) for row in rows for v in row)
    return zlib.compress(b"\x0a" + _varint(len(payload)) + payload)


# Room 1 floor on the left, room 2 floor on the right, a room-2 wall pixel
# (type 0x10) and unassigned floor that must not count.
ROWS = [
    [0x100, 0x100, 0x200, 0x200],
    [0x100, 0x100, 0x210, 0x200],
    [0x20, 0x20, 0x20, 0x20],
]


def test_room_centroids_average_floor_pixels_only() -> None:
    centroids = room_centroids(_grid(ROWS), 4, 3)

    assert centroids[1] == (0.5, 0.5)
    assert centroids[2] == (8 / 3, 1 / 3)


def test_room_centroids_filter_and_labels_agree() -> None:
    compressed = _grid(ROWS)

    assert set(room_centroids(compressed, 4, 3, {2})) == {2}
    assert room_label_points(compressed, 4, 3, {1: "Hall", 9: "Gone"}) == [
        ("Hall", 0.5, 0.5)
    ]
    assert room_centroids(b"", 4, 3) == {}


def test_camera_rooms_attribute_names_each_located_room() -> None:
    static_map = MapData(
        width=4,
        height=3,
        rooms=[
            RoomInfo(room_id=1, name="Hall"),
            RoomInfo(room_id=2, room_sub_type=0),
            RoomInfo(room_id=7, name="Not drawn"),
        ],
    )

    points = NarwalMapCamera._room_points_for(
        static_map, room_centroids(_grid(ROWS), 4, 3)
    )

    assert points == [
        {"id": 1, "name": "Hall", "x": 0.5, "y": 0.5},
        {"id": 2, "name": RoomInfo(room_id=2).display_name, "x": 2.7, "y": 0.3},
    ]


def test_selected_rooms_stay_coloured_and_the_rest_fade() -> None:
    from narwal_client.map_renderer import MAP_RENDER_SCALE, render_base_map

    compressed = _grid(ROWS)
    plain = render_base_map(compressed, 4, 3, show_room_labels=False, show_dock=False)
    picked = render_base_map(
        compressed, 4, 3, show_room_labels=False, show_dock=False, highlight_rooms={1}
    )
    # Grid row 0 is the image's bottom row; x=0 is room 1, x=3 is room 2.
    y = (3 - 1) * MAP_RENDER_SCALE
    assert picked.getpixel((0, y)) == plain.getpixel((0, y))
    assert picked.getpixel((3 * MAP_RENDER_SCALE, y)) != plain.getpixel((3 * MAP_RENDER_SCALE, y))


def test_numbered_label_differs_from_ring_label() -> None:
    from PIL import Image

    from narwal_client.map_renderer import render_overlay

    base = Image.new("RGBA", (300, 300), (200, 200, 200, 255))
    ring = render_overlay(base, 100, room_labels=[("Hall", 50.0, 50.0)])
    numbered = render_overlay(base, 100, room_labels=[("Hall", 50.0, 50.0, 1)])
    assert ring != numbered


def test_camera_selection_order_follows_coordinator() -> None:
    from unittest.mock import MagicMock

    camera = NarwalMapCamera.__new__(NarwalMapCamera)
    coordinator = MagicMock()
    coordinator.has_selected_clean_rooms.return_value = True
    coordinator.is_room_selected_for_clean.side_effect = lambda rid: rid in {1, 2}
    coordinator.ordered_clean_room_ids_for.side_effect = lambda ids: sorted(ids, reverse=True)
    camera.coordinator = coordinator
    static_map = MapData(rooms=[RoomInfo(room_id=1), RoomInfo(room_id=2), RoomInfo(room_id=3)])

    assert camera._selected_room_order(static_map) == (2, 1)
    coordinator.has_selected_clean_rooms.return_value = False
    assert camera._selected_room_order(static_map) == ()
