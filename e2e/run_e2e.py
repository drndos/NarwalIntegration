"""End-to-end test: fresh Home Assistant in Docker + a real Narwal robot.

Starts e2e/docker-compose.yml, onboards a brand-new HA instance over its REST API,
adds the Narwal integration through the real config flow, then exercises the
remote-control (manual drive) feature against the robot.

Standard library only. Usage (from the repo root):

    py e2e/run_e2e.py --host <robot ip> --device-id <32-hex id>
    py e2e/run_e2e.py --host ... --device-id ... --fresh       # wipe HA config first
    py e2e/run_e2e.py --host ... --device-id ... --no-drive    # never move the robot
    py e2e/run_e2e.py --down                                   # stop the container

The robot WILL move unless --no-drive is given: it drives ~2 m straight out of the
dock (--speed / --out-seconds), turns, reverses, runs quiet suction briefly, then
docks again. It must start docked; clear ~2.5 m in front of the dock.

The HA login is generated on first run and kept in e2e/.ha-config/e2e-credentials.json
(gitignored, like the rest of .ha-config).
"""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_DIR = HERE / ".ha-config"
CREDS_FILE = CONFIG_DIR / "e2e-credentials.json"
COMPOSE = ["docker", "compose", "-f", str(HERE / "docker-compose.yml")]
AUTO_DETECT = "Other / Auto-detect"


class HAError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


class HA:
    """Tiny REST client for one Home Assistant instance."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.client_id = self.base + "/"
        self.token: str | None = None

    def request(self, method: str, path: str, data=None, *, form: bool = False, timeout=30):
        headers = {}
        body = None
        if data is not None:
            if form:
                body = urllib.parse.urlencode(data).encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                body = json.dumps(data).encode()
                headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.base + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
        except urllib.error.HTTPError as err:
            raise HAError(err.code, err.read().decode(errors="replace")) from None
        return json.loads(raw) if raw else None

    def get(self, path: str):
        return self.request("GET", path)

    def post(self, path: str, data=None, **kw):
        return self.request("POST", path, data if data is not None else {}, **kw)

    # --- helpers -----------------------------------------------------------
    def template(self, tpl: str):
        return self.request("POST", "/api/template", {"template": tpl})

    def state(self, entity_id: str) -> dict | None:
        try:
            return self.get(f"/api/states/{entity_id}")
        except HAError as err:
            if err.status == 404:
                return None
            raise

    def call(self, domain: str, service: str, data: dict | None = None, timeout=60):
        return self.request("POST", f"/api/services/{domain}/{service}", data or {}, timeout=timeout)


# --- reporting ---------------------------------------------------------------
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


def log(msg: str) -> None:
    print(f"== {msg}", flush=True)


def wait_for(fn, timeout: float, interval: float = 1.0):
    """Poll fn() until it returns something truthy, or return the last value."""
    end = time.monotonic() + timeout
    value = None
    while time.monotonic() < end:
        try:
            value = fn()
        except (HAError, urllib.error.URLError, ConnectionError, OSError):
            value = None
        if value:
            return value
        time.sleep(interval)
    return value


# --- phases ------------------------------------------------------------------
def start_container(fresh: bool, ha_version: str | None) -> None:
    if fresh and CONFIG_DIR.exists():
        log(f"Wiping {CONFIG_DIR}")
        subprocess.run(COMPOSE + ["down"], check=False)
        shutil.rmtree(CONFIG_DIR)
    CONFIG_DIR.mkdir(exist_ok=True)
    env = None
    if ha_version:
        import os

        env = {**os.environ, "HA_VERSION": ha_version}
    log("Starting Home Assistant container")
    subprocess.run(COMPOSE + ["up", "-d"], check=True, env=env)


def _answers(ha: HA) -> bool:
    try:
        ha.request("GET", "/api/", timeout=5)
    except HAError:
        return True  # 401 without a token: HTTP is up
    return True


def wait_ready(ha: HA) -> None:
    log("Waiting for Home Assistant to answer")
    ok = wait_for(lambda: _answers(ha), 300, 2)
    if not ok:
        sys.exit("Home Assistant did not come up within 5 minutes (docker logs narwal-e2e-ha)")


def login(ha: HA) -> None:
    """Onboard a fresh instance, or log back in with the stored refresh token."""
    try:
        steps = {s["step"]: s["done"] for s in ha.get("/api/onboarding")}
    except HAError as err:
        if err.status != 404:
            raise
        steps = {"user": True}  # onboarding view is gone once HA restarts onboarded
    if not steps.get("user"):
        log("Onboarding a fresh instance")
        creds = {"username": "e2e", "password": secrets.token_urlsafe(18)}
        res = ha.post(
            "/api/onboarding/users",
            {"client_id": ha.client_id, "name": "E2E", "language": "en", **creds},
        )
        tok = ha.post(
            "/auth/token",
            {"grant_type": "authorization_code", "code": res["auth_code"], "client_id": ha.client_id},
            form=True,
        )
        creds["refresh_token"] = tok["refresh_token"]
        CREDS_FILE.write_text(json.dumps(creds, indent=2))
        ha.token = tok["access_token"]
        for step, payload in (
            ("core_config", {}),
            ("analytics", {}),
            ("integration", {"client_id": ha.client_id, "redirect_uri": ha.client_id}),
        ):
            if not steps.get(step):
                ha.post(f"/api/onboarding/{step}", payload)
        check("fresh HA onboarded", True)
        return
    if not CREDS_FILE.exists():
        sys.exit(f"HA is already onboarded but {CREDS_FILE} is missing; rerun with --fresh")
    creds = json.loads(CREDS_FILE.read_text())
    tok = ha.post(
        "/auth/token",
        {"grant_type": "refresh_token", "refresh_token": creds["refresh_token"], "client_id": ha.client_id},
        form=True,
    )
    ha.token = tok["access_token"]
    log("Logged in to existing instance")


def ensure_entry(ha: HA, host: str, device_id: str, model: str) -> None:
    entries = ha.get("/api/config/config_entries/entry?domain=narwal")
    if entries:
        log(f"Narwal entry already present ({entries[0]['title']}, {entries[0]['state']})")
        return
    log(f"Adding Narwal via the config flow ({host}, {model})")
    try:
        flow = ha.post("/api/config/config_entries/flow", {"handler": "narwal"})
    except HAError as err:
        check("config flow starts", False, str(err))
        raise SystemExit(finish())
    check("config flow starts", flow.get("step_id") == "user", f"step {flow.get('step_id')}")
    # Auto-detect waits up to 15 s for a broadcast before asking for the id.
    flow = ha.request(
        "POST",
        f"/api/config/config_entries/flow/{flow['flow_id']}",
        {"host": host, "port": 9002, "model": model},
        timeout=90,
    )
    if flow.get("type") == "form" and flow.get("step_id") == "device_id":
        log("Robot did not volunteer its id; entering it")
        flow = ha.request(
            "POST",
            f"/api/config/config_entries/flow/{flow['flow_id']}",
            {"device_id": device_id},
            timeout=90,
        )
    ok = check(
        "config flow creates entry",
        flow.get("type") == "create_entry",
        flow.get("title") or json.dumps(flow.get("errors") or flow)[:200],
    )
    if not ok:
        raise SystemExit(finish())


def find_entities(ha: HA) -> dict[str, str]:
    """Map role -> entity_id for the entities this test uses."""
    ids = ha.template("{{ integration_entities('narwal') | list | tojson }}")
    ids = json.loads(ids) if isinstance(ids, str) else ids
    found: dict[str, str] = {}
    for eid in ids:
        domain, obj = eid.split(".", 1)
        if domain == "vacuum":
            found["vacuum"] = eid
        elif domain == "switch" and obj.endswith("_remote_control"):
            found["switch"] = eid
        elif domain == "select" and obj.endswith("_remote_control_suction"):
            found["select"] = eid
    found["_count"] = str(len(ids))
    return found


def wait_attr(ha: HA, entity_id: str, pred, timeout: float) -> dict | None:
    return wait_for(lambda: (s := ha.state(entity_id)) and pred(s) and s, timeout, 0.5)


def test_setup(ha: HA) -> dict[str, str]:
    log("Checking the entry and its entities")
    entry = wait_for(
        lambda: (e := ha.get("/api/config/config_entries/entry?domain=narwal"))
        and e[0]["state"] in ("loaded", "setup_error", "migration_error")
        and e[0],
        120,
        2,
    )
    check("entry loaded", bool(entry) and entry["state"] == "loaded", entry and entry["state"])
    ents = wait_for(lambda: (f := find_entities(ha)) and "vacuum" in f and f, 60, 2) or {}
    check("integration entities registered", int(ents.get("_count", 0)) > 0, f"{ents.get('_count', 0)} entities")
    for role in ("vacuum", "switch", "select"):
        check(f"{role} entity present", role in ents, ents.get(role, "missing"))
    if "vacuum" in ents:
        st = wait_attr(ha, ents["vacuum"], lambda s: s["state"] != "unavailable", 120)
        check("vacuum available", bool(st), st and st["state"])
    for role in ("switch", "select"):
        if role in ents:
            st = ha.state(ents[role])
            check(f"{role} available", st is not None and st["state"] != "unavailable", st and st["state"])
    return ents


def expect_error(name: str, fn) -> None:
    try:
        fn()
    except HAError as err:
        check(name, err.status >= 400, f"HTTP {err.status}")
    else:
        check(name, False, "call succeeded")


def test_suction_select(ha: HA, ents: dict[str, str]) -> None:
    if "select" not in ents:
        return
    log("Suction select (not driving: only remembered)")
    sel = ents["select"]
    opts = (ha.state(sel) or {}).get("attributes", {}).get("options", [])
    check("suction options", opts == ["off", "quiet", "standard", "strong", "super", "ultra"], ",".join(opts))
    ha.call("select", "select_option", {"entity_id": sel, "option": "quiet"})
    check("select remembers choice", bool(wait_attr(ha, sel, lambda s: s["state"] == "quiet", 5)))
    ha.call("select", "select_option", {"entity_id": sel, "option": "off"})
    check("select back to off", bool(wait_attr(ha, sel, lambda s: s["state"] == "off", 5)))


def drive(ha: HA, target: dict, linear: int, angular: int, seconds: float) -> None:
    """Hold a velocity for `seconds`, re-sending like a joystick would.

    Each call asks for 1 s, so if this script dies the robot stops within 1 s.
    """
    end = time.monotonic() + seconds
    while (left := end - time.monotonic()) > 0:
        ha.call("narwal", "remote_control_move",
                {**target, "linear": linear, "angular": angular, "duration": min(1.0, left)})
        time.sleep(min(0.4, left))
    ha.call("narwal", "remote_control_move", {**target, "linear": 0, "angular": 0, "duration": 0})


def step(name: str, fn, settle: float = 0.5) -> bool:
    try:
        fn()
    except HAError as err:
        return check(name, False, str(err))
    time.sleep(settle)
    return check(name, True)


def test_drive(ha: HA, ents: dict[str, str], speed: int, out_seconds: float) -> None:
    vac, sw, sel = ents.get("vacuum"), ents.get("switch"), ents.get("select")
    if not (vac and sw and sel):
        return
    target = {"entity_id": vac}

    log("Move without start must be refused")
    expect_error(
        "move before start rejected",
        lambda: ha.call("narwal", "remote_control_move", {**target, "linear": 0, "angular": 0}),
    )

    start_state = ha.state(vac)["state"]
    if not check("robot docked before driving", start_state == "docked", start_state):
        log("Seat the robot on the dock and rerun; skipping the drive tests")
        return

    log("Starting remote control (suction off)")
    if not step("remote_control_start",
                lambda: ha.call("narwal", "remote_control_start", {**target, "suction": "off"}, timeout=60), 0):
        return
    check("switch on while driving", bool(wait_attr(ha, sw, lambda s: s["state"] == "on", 10)))
    st = wait_attr(ha, vac, lambda s: s["attributes"].get("task_status") == "remote_control", 15)
    check("vacuum task_status = remote_control", bool(st),
          (ha.state(vac) or {}).get("attributes", {}).get("task_status"))

    # Off the dock and well clear of it (~2 m at the defaults), so the dock's
    # pull does not end the session and the return is a real docking run.
    log(f"Driving away from the dock: {speed}% for {out_seconds:g} s")
    step("drive off the dock", lambda: drive(ha, target, speed, 0, out_seconds), 1.0)
    check("still in remote control away from dock", (ha.state(sw) or {}).get("state") == "on")

    log("Out in the open: turns, reverse, dead-man hold, suction")
    step("turn left", lambda: drive(ha, target, 0, 40, 1.5), 0.5)
    step("turn right back", lambda: drive(ha, target, 0, -40, 1.5), 0.5)
    step("reverse a little", lambda: drive(ha, target, -30, 0, 1.0), 0.5)
    step("forward a little", lambda: drive(ha, target, 30, 0, 1.0), 0.5)
    # One short move and then silence: the robot must stop by itself but stay
    # in remote control (the 30 s idle exit is not reached).
    step("single short move", lambda: ha.call(
        "narwal", "remote_control_move", {**target, "linear": 0, "angular": 30, "duration": 0.5}), 3.0)
    check("dead-man: held still, still in remote control", (ha.state(sw) or {}).get("state") == "on")
    step("suction quiet while driving",
         lambda: ha.call("select", "select_option", {"entity_id": sel, "option": "quiet"}), 0)
    check("select shows quiet", bool(wait_attr(ha, sel, lambda s: s["state"] == "quiet", 5)))
    step("creep forward with suction", lambda: drive(ha, target, 25, 0, 2.0), 0.5)
    step("suction off while driving",
         lambda: ha.call("select", "select_option", {"entity_id": sel, "option": "off"}), 0)
    check("select shows off", bool(wait_attr(ha, sel, lambda s: s["state"] == "off", 5)))

    log("Stop via the service, then start/stop through the switch")
    step("remote_control_stop", lambda: ha.call("narwal", "remote_control_stop", target), 0)
    check("switch off after stop", bool(wait_attr(ha, sw, lambda s: s["state"] == "off", 15)))
    if step("switch turn_on", lambda: ha.call("switch", "turn_on", {"entity_id": sw}, timeout=60), 0):
        check("switch turn_on enters remote control", bool(wait_attr(ha, sw, lambda s: s["state"] == "on", 10)))
        step("switch turn_off", lambda: ha.call("switch", "turn_off", {"entity_id": sw}), 0)
        check("switch turn_off leaves remote control", bool(wait_attr(ha, sw, lambda s: s["state"] == "off", 15)))

    log("Back to the dock")
    # After manual drive the Flow 2 may head home by itself (status 19, shown
    # as returning); only ask when it does not.
    st = wait_attr(ha, vac, lambda s: s["state"] in ("returning", "docked"), 10)
    if st:
        check("robot heads back to dock", True, f"on its own ({st['state']})")
    else:
        step("return_to_base", lambda: ha.call("vacuum", "return_to_base", target), 0)
    st = wait_attr(ha, vac, lambda s: s["state"] == "docked", 180)
    last = ha.state(vac) or {}
    check("vacuum reads docked again", bool(st),
          f"still {last.get('state')} / {last.get('attributes', {}).get('task_status')}")


def finish() -> int:
    failed = [r for r in RESULTS if not r[1]]
    print()
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    for name, _, detail in failed:
        print(f"  FAILED: {name}" + (f" — {detail}" if detail else ""))
    return 1 if failed else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", help="robot IP")
    p.add_argument("--device-id", default="", help="robot device id (needed when it does not broadcast)")
    p.add_argument("--model", default=AUTO_DETECT, help=f"config-flow model label (default: {AUTO_DETECT})")
    p.add_argument("--ha-url", default="http://localhost:8124")
    p.add_argument("--ha-version", help="HA image tag (default: stable)")
    p.add_argument("--fresh", action="store_true", help="wipe e2e/.ha-config and start from zero")
    p.add_argument("--no-drive", action="store_true", help="do not move the robot")
    p.add_argument("--speed", type=int, default=50, help="forward speed %% when leaving the dock (default 50)")
    p.add_argument("--out-seconds", type=float, default=6.0,
                   help="seconds to drive away from the dock (default 6, aim ~2 m)")
    p.add_argument("--no-start", action="store_true", help="use an already running container")
    p.add_argument("--down", action="store_true", help="stop the container and exit")
    args = p.parse_args()

    if args.down:
        return subprocess.run(COMPOSE + ["down"]).returncode
    if not args.host:
        p.error("--host is required")

    if not args.no_start:
        start_container(args.fresh, args.ha_version)
    ha = HA(args.ha_url)
    wait_ready(ha)
    login(ha)
    version = ha.get("/api/config").get("version")
    log(f"Home Assistant {version}")
    ensure_entry(ha, args.host, args.device_id, args.model)
    ents = test_setup(ha)
    test_suction_select(ha, ents)
    if args.no_drive:
        log("--no-drive: skipping the driving tests")
    else:
        test_drive(ha, ents, args.speed, args.out_seconds)
    return finish()


if __name__ == "__main__":
    sys.exit(main())
