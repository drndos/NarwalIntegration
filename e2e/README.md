# End-to-end tests (real robot, fresh Home Assistant)

`run_e2e.py` starts a throwaway Home Assistant in Docker with the integration from this
working tree mounted in, onboards it, adds the robot through the real config flow and
exercises remote control. Needs Docker and a robot reachable from the Docker host.

```bash
py e2e/run_e2e.py --host <robot ip> --device-id <device id> --fresh
```

- HA listens on <http://localhost:8124>; the generated login is in
  `e2e/.ha-config/e2e-credentials.json` (gitignored).
- **The robot moves**: it must start docked, drives ~2 m straight out (`--speed 50
  --out-seconds 6`, tune to your floor), turns both ways, reverses, holds still to test the
  dead-man stop, creeps with quiet suction, then docks again. Clear ~2.5 m in front of the
  dock, or pass `--no-drive`. Leaving manual drive right next to the dock is avoided on
  purpose: the robot then tries to dock itself and can end up rotated, not seated.
- Rerun without `--fresh` to reuse the instance and entry (after editing code, restart the
  container: `docker restart narwal-e2e-ha`). `--ha-version 2026.2.3` pins an HA release.
- `py e2e/run_e2e.py --down` stops the container. Exit code is non-zero if any check fails.

Checks: onboarding, config flow (with the device-id step when the robot does not broadcast),
entry loaded, vacuum / remote-control switch / suction select present and available, suction
select remembers its choice, `remote_control_move` refused before start, start → switch on and
`task_status: remote_control`, moves, suction change while driving, stop → switch off, and the
same start/stop through the switch, and the vacuum reading `docked` again at the end.

Not covered: the 30 s idle exit, the diagnostic cool-down / suction-running entities
(disabled by default), and driving from two clients at once.
