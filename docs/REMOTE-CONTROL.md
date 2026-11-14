# Remote control (manual drive)

Drive the robot by hand from Home Assistant, the same feature as the joystick screen in the
Narwal app. Verified on the **Flow 2** at the protocol level; the Home Assistant side is new and
not yet verified on hardware. On other models the entities exist but are disabled by default:
enable them and report whether it works.

## Actions

| Action | Fields | What it does |
|---|---|---|
| `narwal.remote_control_start` | `suction`: off, quiet, standard, strong, super, ultra | Puts the robot in manual drive |
| `narwal.remote_control_move` | `linear` −100…100 (+ forward), `angular` −100…100 (+ left), `duration` 0–5 s (default 0.5) | Drives at that speed for `duration`, then holds still |
| `narwal.remote_control_stop` | — | Stops and leaves manual drive |

All three target the Narwal vacuum entity. `remote_control_move` only works after
`remote_control_start`. Each move replaces the previous one; to keep driving, call it again
before `duration` runs out. If calls stop arriving (a closed dashboard, a lost connection), the
robot stops by itself when `duration` ends: that is deliberate.

Example: a script that nudges the robot forward for one second.

```yaml
sequence:
  - action: narwal.remote_control_start
    target:
      entity_id: vacuum.narwal_flow_2
  - action: narwal.remote_control_move
    target:
      entity_id: vacuum.narwal_flow_2
    data:
      linear: 60
      duration: 1
```

## Entities

| Entity | What it shows |
|---|---|
| *Remote control* switch | On while the robot is in manual drive, including when the Narwal app drives it. Turning it off ends manual drive |
| *Remote control suction* select | Suction while driving; when not driving, the choice for the next session |
| *Suction cool-down* (diagnostic, disabled by default) | Seconds until suction may run again |
| *Remote control suction running* (diagnostic, disabled by default) | On while suction actually runs |

While driving, the vacuum entity reads *idle*, or *cleaning* while suction runs; its
`task_status` attribute reads `remote_control`.

## Dashboard

`tools/gen_dashboard.py` writes `narwal_vacuum_section.yaml`: a robot section (map, start /
stop / return / locate, clean mode, suction) and a remote-control section with the switch,
the suction select, a drive-speed slider (10–100 %, default 50) and a 3 x 3 drive pad that
appears only while driving. Tap an arrow for a 1 s move, hold it for 3 s. Built-in cards only.

Dashboard buttons cannot use templates, so the pad calls `script.narwal_drive`, which scales
the move by `input_number.narwal_drive_speed`. Add both from `narwal_drive_script.yaml` and
`narwal_drive_input_number.yaml` (or create them as helpers in the UI with those ids).

```bash
py tools/gen_dashboard.py --registry core.entity_registry --vacuum vacuum.narwal_flow_2_vacuum --out build/
```

Paste the file's contents under `sections:` of a sections view.

## Limits

- Manual drive ends after **30 s without a move**, when the robot docks itself (it may do so near
  the dock), or when a clean starts. Starting a clean from Home Assistant ends it first.
- It cannot start while a clean runs; pause or stop the clean first. A return to dock is
  cancelled.
- **Suction runs in bursts.** The robot only runs suction for a few seconds per request; the
  integration repeats the request every second like the app does, so short gaps are normal. The
  robot can also impose a cool-down, shown by the *Suction cool-down* sensor.
- Home Assistant adds network latency on every move call, so driving feels less direct than in
  the app. It is meant for nudging the robot out of a corner, not for precise driving.
- Another local client (the Narwal app on the same network, an ESP32 remote) can drive at the
  same time; do not drive from two places at once.

## How it works

`telecontrol/set_manual_control_mode` with a mode (1 joystick, 3–7 suction quiet…ultra) enters
manual drive, and `telecontrol/velocity_control` is then streamed every 200 ms. The protocol,
the robot's observed behaviour and how it was decoded from the Narwal app are in
[PROTOCOL.md §5](PROTOCOL.md#remote-control-telecontrol).
