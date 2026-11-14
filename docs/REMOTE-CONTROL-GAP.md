# Remote control: gap analysis

> **Status:** §2 (client + Home Assistant remote control), §3 (status parsing) and
> §4 (PROTOCOL.md) are implemented on `feat/remote-control`, untested.

Source: Narwal app 2.9.90 (blutter decode) and live tests on a Flow 2.
Target: `narwal_client/` (and its embedded copy)
plus `custom_components/narwal/`. Nothing here is implemented yet; this is the write-up to plan from.

## 1. Summary

| Finding | In the integration today | Gap |
|---|---|---|
| `telecontrol/set_manual_control_mode` + `telecontrol/velocity_control` (manual drive, drive with suction) | Absent: no topic, no client method, no entity, no service | **New feature** (main item) |
| Working status `21` = remote control | Not in `WorkingStatus`; parsed as `UNKNOWN` with a "please report" warning | Add enum value + activity mapping |
| Working status `5` = normal cleaning on Flow 2 | `CLEANING_ALT`, documented as "seen when stuck" | Correct the docs/comment; mapping to CLEANING is already right |
| Status field `3.19` (1 joystick, 2 suction running) | Not parsed | Parse, expose |
| Status field `31` (remote-control mode + 1) | Not parsed | Parse, expose |
| Status field `37` (suction cool-down, s) | Not parsed | Parse, expose as sensor |
| `common/notify_app_event` ends remote control | Wake burst always sends it | Must be suppressed while in remote control |
| `velocity_control` gets no reply | `UNACKNOWLEDGED_TOPICS` only has the heartbeat | Add it, or every later reply is misattributed |
| Unexplored telecontrol topics: `point_navi`, `stop_point_navi`, `navi_to_room`, `navi_to_region`, `get_two_rooms_crossing_point`, `perform_choreography` | Absent | Probe only; feeds the ROADMAP "send the robot to a point or room" item |
| mDNS `_narwal_sweeper._tcp`, `_app_wss_server_<6hex>` | Already used (manifest zeroconf, config flow) | None |
| Dock light 0–3 via `supply/ambient_light_ctrl`, field 50 | Already implemented (`light.py`, models field 50) | None |
| `common/yell`, `supply/recall`, `task/force_end`, `pause`/`resume`, `dust_gathering`, `wash_mop`, `dry_mop`, `set_fan_level`, `set_mop_humidity` | Already implemented | None (re-confirmed on Flow 2) |
| Flow 2 refuses `clean/easy_clean/start` and `clean/plan/start` | `start()` already enumerates rooms and sends `clean/start_clean` | None |
| `Header` field 5 = `Properties {1: responseUrl, 2: correlationData}` on responses | Responses matched by FIFO order only | Optional: check whether `correlationData` is ever populated; would remove the FIFO fragility (see §6) |

## 2. Main feature: remote control (manual drive)

### Protocol (app-decompiled and live-verified)

| Topic | Payload | Reply |
|---|---|---|
| `telecontrol/set_manual_control_mode` | `{1: mode}`; mode 0 OFF (send empty payload), 1 JOYSTICK, 2 CLEAN_DEFAULT, 3–7 suction quiet / standard / strong / super / ultra | `{1: result, 2: unifiedErrorCode}` |
| `telecontrol/velocity_control` | `{1: linear % int32, 2: angular % int32}`; + forward / + left; field 3 type left unset | **none** |

Negative int32 must be encoded as 10-byte sign-extended varints. Check `protocol.py` / the client's
`_encode_varint_field` handles negatives; it has only ever encoded positive values.

Robot behaviour that the implementation has to respect:

- Mode set → state 3.1 becomes 21 within ~0.5 s.
- The robot needs a steady `velocity_control` stream (zero velocity included) or it drops out; the app sends every 200 ms, the ESP32 every 100 ms.
- If it falls back to standby (3.1 = 1), re-send the mode; `2` (not possible now) during the transition → retry about once a second.
- Suction runs only a few seconds per request. Re-send the same suction mode once a second while suction is wanted, except while field 37 (cool-down) is present.
- Near the dock the robot may dock itself (3.1 = 10) and leave remote control. Do not fight it.
- `common/notify_app_event` throws the robot out of remote control.
- App preconditions: refuse while a clean runs and is not paused; cancel a pending return to dock with `task/force_end` first.
- Leaving: mode 0 (empty payload). `task/force_end` also ends it.

### Client layer (`narwal_client/`, mirrored to `custom_components/narwal/narwal_client/`)

`const.py`

- `TOPIC_CMD_SET_MANUAL_CONTROL_MODE = "telecontrol/set_manual_control_mode"`, `TOPIC_CMD_VELOCITY_CONTROL = "telecontrol/velocity_control"`.
- `class ManualControlMode(IntEnum)`: OFF 0, JOYSTICK 1, CLEAN_DEFAULT 2, SWEEP_MUTE 3, SWEEP_NORMAL 4, SWEEP_STRONG 5, SWEEP_DEEP 6, SWEEP_SUPER 7.
- `WorkingStatus.REMOTE_CONTROL = 21`. Keep it out of `ACTIVE_CLEANING_STATUSES`.
- Add `TOPIC_CMD_VELOCITY_CONTROL` to `UNACKNOWLEDGED_TOPICS`.
- `BaseStatusField`: `MANUAL_CONTROL_MODE = 31`, `FAN_COOLDOWN = 37`.
- Fix the `CLEANING_ALT = 5` comment: on Flow 2 it is the plain cleaning state.

`models.py` (`NarwalState` / base-status parse)

- `remote_control_active: bool` (3.1 == 21).
- `remote_control_suction_running: bool | None` from 3.19 (2 = running).
- `manual_control_mode: ManualControlMode | None` from field 31 − 1; absent → None.
- `fan_cooldown_seconds: int | None` from field 37; absent → 0/None.

`client.py`: a small remote-control session owned by the client, because the 100–200 ms stream
cannot go through HA's service-call path.

- `async def remote_control_start(mode=ManualControlMode.JOYSTICK)`: precondition checks (clean running and not paused → refuse; returning → `force_end` first), send mode, start the stream task.
- `async def remote_control_drive(linear: int, angular: int, ttl: float = 0.5)`: clamp to −100..100 and set the *target* velocity with an expiry. The stream task sends the target every 200 ms and falls back to zero when the TTL lapses (dead-man), so a lost HA/frontend connection stops the robot.
- `async def remote_control_set_suction(mode)`: switch between JOYSTICK and 3–7.
- `async def remote_control_stop()`: zero velocity, mode 0, cancel the stream task.
- Stream task (`_remote_control_loop`): every 200 ms send velocity; re-send mode when state left 21 for standby or the last mode reply was not 1; re-send suction mode every 1 s unless field 37 is present; stop the session when the robot goes to another state (cleaning, docking: 10, 19, 5…) or after 30 s without a non-zero drive.
- `_keepalive_loop` / `wake()`: while a remote-control session is active, never send the wake burst (it contains `notify_app_event`). If broadcasts lapse, only renew the subscription (`_renew_topic_subscription`). `velocity_control` itself counts as command traffic, so the 60 s idle timeout is not a concern during driving.
- `disconnect()` and reconnect: end the session first (best-effort mode 0).

### Home Assistant layer (`custom_components/narwal/`)

HA has no joystick entity, so the drive surface is services plus a few entities. Proposed:

Services (`services.yaml`, `strings.json`, registered next to `clean_rooms`):

| Service | Fields | Action |
|---|---|---|
| `narwal.remote_control_start` | `suction` (off, quiet, standard, strong, super, ultra; default off) | Enter remote control |
| `narwal.remote_control_move` | `linear` (−100..100), `angular` (−100..100), `duration` (s, default 0.5, max ~5) | Set target velocity for `duration`, then zero |
| `narwal.remote_control_stop` | — | Zero velocity, leave remote control |

A dashboard joystick/D-pad card (or a script per button: "forward 1 s", "turn left 0.5 s") calls
`remote_control_move` repeatedly; the TTL keeps a dropped browser from leaving the robot driving.

Entities:

| Entity | Platform | Source |
|---|---|---|
| Remote control (on/off) | `switch` | 3.1 == 21; on → `remote_control_start`, off → `remote_control_stop` |
| Remote-control suction | `select` (off/quiet/standard/strong/super/ultra) | field 31; sets mode 1 or 3–7. Available only while remote control is on |
| Suction cool-down | `sensor` (s, diagnostic) | field 37 |
| Suction running | `binary_sensor` (diagnostic) | 3.19 == 2 |

`vacuum.py`: map `WorkingStatus.REMOTE_CONTROL` → `VacuumActivity.CLEANING` when 3.19 == 2, else
`VacuumActivity.IDLE` (HA has no "manual" activity). Optionally expose a `remote_control` attribute.
Consider `VacuumEntityFeature.SEND_COMMAND` only if a generic escape hatch is wanted; the services
above are clearer.

`coordinator.py`: the predicates that look at `working_status` (lines ~217, 233, 258, 377, 390,
1457) must treat 21 as "robot away from dock, not cleaning, not returning" so dock tasks, profile
application and start/stop gating don't misfire. `vacuum.start` while in remote control should
end remote control first (or refuse).

Model gating: verified only on Flow 2. Gate the entities on Flow 2 product keys
at first, or on `common/get_feature_list` if a telecontrol feature bit can be identified; other
models expose them once someone confirms a reply of `1` to `set_manual_control_mode`.

## 3. Status parsing fixes (independent of remote control)

Small, low-risk, worth shipping first:

1. `WorkingStatus.REMOTE_CONTROL = 21`, so driving with the Narwal app no longer logs "Unknown working_status value: 21" and turns the vacuum entity `unknown`.
2. Fields 31, 37 and 3.19 into `NarwalState` and `raw_base_status` docs.
3. `CLEANING_ALT` comment/docs: 5 is regular cleaning on Flow 2.

## 4. Documentation updates (`docs/PROTOCOL.md`)

- New section "Remote control (telecontrol)": topics, enums, `TelecontrolVelocity` fields, app timing and speed formulas (forward `80 + 20·s/100`, turning `30 + 20·s/100` and `17 + 17·s/100`, backward ≈ −70 %), robot behaviour list from §2 above. Credit the app 2.9.90 blutter decode.
- §7 status table: fields 31, 37, 50 (already used in code, missing from the table), sub-field 3.19; working status 5 and 21.
- Header table: field 5 `Properties {responseUrl, correlationData}`, field 6 `urlId`.
- Reverse-engineering notes: blutter workflow (`BuilderInfo` field-number pattern, `PbFieldType` constants 512 enum / 2048 int32 / 256 float / 32768 uint32) as a contributor how-to.
- `docs/ROADMAP.md`: add "Remote control" to Next; move the unexplored telecontrol topics under the camera/home-security item as the likely route for "send the robot to a point or room".

## 5. Risks and constraints

- **One connection per source IP.** HA is the connection; remote control must go through it. An ESP32 or PC script on another IP can connect at the same time, but two drivers fight. Document this.
- **Latency.** HA service calls over the frontend websocket add tens to hundreds of ms; TTL-based target velocity hides jitter but the robot will feel less direct than the ESP32. Acceptable for "nudge it out from under the sofa", not for precise driving.
- **Safety.** The dead-man TTL, the 30 s idle exit, and stop-on-HA-unload are mandatory. Never auto-start remote control from a restore.
- **Response ordering.** A single mis-queued `velocity_control` corrupts every later reply attribution; tests must cover a burst of velocity frames interleaved with acknowledged commands.
- **Suction gaps** are robot behaviour, not a bug; say so in the entity description and README.
- **Firmware variance.** Only one Flow 2 firmware verified. Unknown result codes and `unifiedErrorCode` should be logged once, not raised.

## 6. Open questions to settle before/while coding

- Does the client's varint encoder handle negative int32 (10-byte form)? Needs a unit test either way.
- Does any reply carry `Header.properties.correlationData`? If yes, matching by correlation would replace the FIFO and make unacknowledged topics harmless.
- Is there a feature bit in `common/get_feature_list` for telecontrol, so gating need not use product keys?
- Does remote control work on AX12 / Freo models? One probe each (`set_manual_control_mode` = 1, watch for 3.1 = 21).
- `navi_to_room` / `point_navi` payloads: unknown; decode with blutter the same way before any HA work.

## 7. Suggested order of work

1. Status parsing fixes (§3) + PROTOCOL.md status table, one small PR (`/gsd-quick`).
2. Client remote-control session with unit tests (encoder negatives, unacknowledged topic, TTL dead-man, wake-burst suppression), embedded copy kept identical.
3. HA services + switch/select/sensor/binary_sensor, strings/translations, vacuum activity mapping, coordinator predicates. Live test on a Flow 2 in the middle of a room before tagging.
4. Docs: PROTOCOL.md telecontrol section, README feature list, ROADMAP.
5. Later, separate: probe `navi_to_room` / `point_navi` for the home-security roadmap item.
