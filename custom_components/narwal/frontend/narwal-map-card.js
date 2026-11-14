/*
 * narwal-map-card - the Narwal map with tap-to-select rooms.
 *
 * Tap rooms in the order they should be cleaned, as in the Narwal app. The map
 * camera draws each label with a ring, or with the room's number once picked,
 * and fades the rooms that are not picked. Tapping a picked room drops it and
 * the rest close up.
 *
 *   type: custom:narwal-map-card
 *   camera: camera.narwal_map
 *   rooms:
 *     - id: 3                                   # room id on the robot's map
 *       selected: switch.narwal_kitchen_selected
 *
 * A tap is one call to the room's "selected" switch: the integration appends a
 * newly picked room to the cleaning order itself. The card lays invisible tap
 * targets over the labels, placed from the camera's `rooms` (grid centroids)
 * and `calibration_points`, so map rotation and zoom are honoured; it reads
 * the order from the camera's `selected_rooms`. Served by the integration; no
 * resource registration needed.
 */

const VERSION = "1.2.0";
// Map frames while nothing was tapped are fetched at most this often.
const REFRESH_MS = 2000;
// After a tap, fetch every new frame at once for this long.
const TAP_FAST_MS = 3000;
// A tap off every label picks the nearest room within this share of the image.
const NEAREST_LIMIT = 0.15;
// Must match map_renderer: ROOM_LABEL_FONT_SCALE and the label padding.
const LABEL_FONT_SCALE = 7;

const STYLE = `
  ha-card { overflow: hidden; }
  .wrap { position: relative; line-height: 0; user-select: none; }
  img { width: 100%; height: auto; display: block; cursor: pointer; }
  .empty { padding: 24px; text-align: center; line-height: 1.4; color: var(--secondary-text-color); }
  .hit {
    position: absolute; transform: translate(-50%, -50%); padding: 0; margin: 0;
    border: none; border-radius: 999px; background: transparent;
    cursor: pointer; -webkit-tap-highlight-color: transparent;
  }
  .hit:focus-visible { outline: 2px solid var(--primary-color, #03a9f4); outline-offset: 2px; }
  .hit.locked { cursor: not-allowed; }
  .bar {
    display: flex; align-items: center; gap: 8px; padding: 8px 12px;
    font-size: 14px; color: var(--secondary-text-color);
  }
  .bar .text { flex: 1; }
  .bar button {
    border: none; background: none; cursor: pointer; padding: 6px 10px; border-radius: 16px;
    font: 500 14px/1 var(--ha-font-family-body, sans-serif); color: var(--primary-color, #03a9f4);
  }
  .bar button:hover { background: rgba(127, 127, 127, .15); }
  .bar button[hidden] { display: none; }
`;

class NarwalMapCard extends HTMLElement {
  constructor() {
    super();
    this._lastLoad = 0;
    this._loadTimer = null;
    this._tapAt = 0;
  }

  setConfig(config) {
    if (!config || !config.camera) throw new Error("narwal-map-card: 'camera' is required");
    if (config.rooms && !Array.isArray(config.rooms)) throw new Error("narwal-map-card: 'rooms' must be a list");
    this._config = {
      ...config,
      rooms: (config.rooms || []).map((r) => ({ ...r, id: Number(r.id) })),
    };
    this._built = false;
  }

  static getStubConfig() {
    return { camera: "", rooms: [] };
  }

  getCardSize() {
    return 7;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6 };
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._config) return;
    if (!this._built) this._build();
    this._refreshImage();
    this._renderTargets();
  }

  _build() {
    const root = this.shadowRoot || this.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>${STYLE}</style>
      <ha-card>
        <div class="wrap"><img alt="Narwal map"><div class="targets"></div></div>
        <div class="empty" hidden>Map not available yet</div>
        <div class="bar"><span class="text"></span><button class="clear" hidden>Clear</button></div>
      </ha-card>`;
    this._img = root.querySelector("img");
    this._wrap = root.querySelector(".wrap");
    this._targets = root.querySelector(".targets");
    this._emptyEl = root.querySelector(".empty");
    this._text = root.querySelector(".text");
    this._clear = root.querySelector(".clear");
    this._img.addEventListener("load", () => this._renderTargets());
    this._img.addEventListener("click", (ev) => this._tapImage(ev));
    this._clear.addEventListener("click", () => this._clearAll());
    this._built = true;
  }

  // ----- state helpers -----

  _state(entityId) {
    return entityId ? this._hass.states[entityId] : undefined;
  }

  _camera() {
    return this._state(this._config.camera);
  }

  _usable(entityId) {
    const st = this._state(entityId);
    return !!st && st.state !== "unavailable" && st.state !== "unknown";
  }

  _rooms() {
    const cam = this._camera();
    const located = new Map(((cam && cam.attributes.rooms) || []).map((r) => [Number(r.id), r]));
    return this._config.rooms
      .filter((r) => located.has(r.id) && this._state(r.selected))
      .map((r) => ({ ...r, point: located.get(r.id) }));
  }

  /** Picked room ids in cleaning order, as the integration reports them. */
  _order(rooms) {
    const cam = this._camera();
    const reported = cam && cam.attributes.selected_rooms;
    if (Array.isArray(reported)) return reported.map(Number).filter((id) => rooms.some((r) => r.id === id));
    return rooms.filter((r) => (this._state(r.selected) || {}).state === "on").map((r) => r.id);
  }

  // ----- image -----

  _refreshImage() {
    const cam = this._camera();
    const pic = cam && cam.attributes.entity_picture;
    this._wrap.hidden = !pic;
    this._emptyEl.hidden = !!pic;
    if (!pic) return;
    const count = cam.attributes.render_count;
    const key = `${pic}|${count}`;
    if (key === this._imgKey) return;
    const fast = Date.now() - this._tapAt < TAP_FAST_MS;
    const wait = REFRESH_MS - (Date.now() - this._lastLoad);
    if (wait > 0 && this._imgKey && !fast) {
      if (!this._loadTimer) this._loadTimer = setTimeout(() => { this._loadTimer = null; this._refreshImage(); }, wait);
      return;
    }
    this._imgKey = key;
    this._lastLoad = Date.now();
    const sep = pic.includes("?") ? "&" : "?";
    const url = this._hass.hassUrl(`${pic}${sep}r=${count ?? 0}`);
    // Swap only once loaded, so the map never blanks between frames.
    const next = new Image();
    next.onload = () => { if (this._imgKey === key) this._img.src = url; };
    next.src = url;
  }

  /** Grid point -> percent of the displayed image, or null. */
  _toPercent(x, y) {
    const cam = this._camera();
    const cal = cam && cam.attributes.calibration_points;
    const w = this._img.naturalWidth;
    const h = this._img.naturalHeight;
    if (!cal || cal.length < 3 || !w || !h) return null;
    const [p0, p1, p2] = cal;
    const gw = p1.vacuum.x - p0.vacuum.x;
    const gh = p2.vacuum.y - p0.vacuum.y;
    if (!gw || !gh) return null;
    const u = (x - p0.vacuum.x) / gw;
    const v = (y - p0.vacuum.y) / gh;
    const px = p0.map.x + u * (p1.map.x - p0.map.x) + v * (p2.map.x - p0.map.x);
    const py = p0.map.y + u * (p1.map.y - p0.map.y) + v * (p2.map.y - p0.map.y);
    if (px < 0 || py < 0 || px > w || py > h) return null;
    return { left: (px / w) * 100, top: (py / h) * 100 };
  }

  /** Size of the label pill the camera draws for `name`, in percent of the image. */
  _labelBox(name) {
    const cam = this._camera();
    const cal = cam && cam.attributes.calibration_points;
    const w = this._img.naturalWidth;
    const h = this._img.naturalHeight;
    if (!cal || cal.length < 3 || !w || !h) return { w: 8, h: 5 };
    const [p0, p1] = cal;
    const scale = Math.hypot(p1.map.x - p0.map.x, p1.map.y - p0.map.y) / ((p1.vacuum.x - p0.vacuum.x) || 1);
    const s = Math.max(1, Math.round(scale));
    const font = LABEL_FONT_SCALE * s;
    const pad = Math.max(4, s);
    if (!this._ctx) this._ctx = document.createElement("canvas").getContext("2d");
    this._ctx.font = `${font}px sans-serif`;
    const textW = this._ctx.measureText(name).width;
    const diameter = font * 0.75 + pad;
    // A little larger than the pill: easier to hit.
    const pillW = (pad + diameter + pad + textW + pad * 2) * 1.1 + pad * 2;
    const pillH = diameter + pad * 4;
    return { w: (pillW / w) * 100, h: (pillH / h) * 100 };
  }

  // ----- tap targets -----

  _renderTargets() {
    if (!this._built) return;
    const rooms = this._rooms();
    const order = this._order(rooms);
    const frag = document.createDocumentFragment();
    this._placed = [];
    for (const room of rooms) {
      const at = this._toPercent(room.point.x, room.point.y);
      if (!at) continue;
      const rank = order.indexOf(room.id) + 1;
      const box = this._labelBox(room.point.name);
      const el = document.createElement("button");
      el.className = `hit${this._usable(room.selected) ? "" : " locked"}`;
      el.style.left = `${at.left}%`;
      el.style.top = `${at.top}%`;
      el.style.width = `${box.w}%`;
      el.style.height = `${box.h}%`;
      el.title = rank ? `${room.point.name}: #${rank} - tap to remove` : `${room.point.name} - tap to add`;
      el.setAttribute("aria-label", el.title);
      el.addEventListener("click", (ev) => { ev.stopPropagation(); this._toggle(room); });
      frag.appendChild(el);
      this._placed.push({ room, at });
    }
    this._targets.replaceChildren(frag);

    const names = order.map((id) => rooms.find((r) => r.id === id)).filter(Boolean).map((r) => r.point.name);
    this._text.textContent = names.length
      ? `${names.length === 1 ? "1 room" : `${names.length} rooms`}: ${names.join(" → ")}`
      : rooms.length
        ? "Tap rooms in the order to clean them. None selected = whole house."
        : "No rooms on this map yet.";
    this._clear.hidden = !names.length;
  }

  _tapImage(ev) {
    if (!this._placed || !this._placed.length) return;
    const box = this._img.getBoundingClientRect();
    const left = ((ev.clientX - box.left) / box.width) * 100;
    const top = ((ev.clientY - box.top) / box.height) * 100;
    let best = null;
    let bestDist = Infinity;
    for (const p of this._placed) {
      const d = Math.hypot(p.at.left - left, p.at.top - top);
      if (d < bestDist) { best = p; bestDist = d; }
    }
    if (best && bestDist <= NEAREST_LIMIT * 100) this._toggle(best.room);
  }

  // ----- actions -----

  _notify(message) {
    this.dispatchEvent(new CustomEvent("hass-notification", { detail: { message }, bubbles: true, composed: true }));
  }

  async _call(domain, service, data) {
    this._tapAt = Date.now();
    try {
      await this._hass.callService(domain, service, data);
    } catch (err) {
      this._notify(`Narwal: ${err && err.message ? err.message : err}`);
    }
  }

  _toggle(room) {
    if (!this._usable(room.selected)) {
      this._notify("Room selection can't be changed right now (robot busy?)");
      return;
    }
    const on = (this._state(room.selected) || {}).state === "on";
    this._call("switch", on ? "turn_off" : "turn_on", { entity_id: room.selected });
  }

  _clearAll() {
    const ids = this._rooms()
      .filter((r) => (this._state(r.selected) || {}).state === "on" && this._usable(r.selected))
      .map((r) => r.selected);
    if (ids.length) this._call("switch", "turn_off", { entity_id: ids });
  }
}

if (!customElements.get("narwal-map-card")) {
  customElements.define("narwal-map-card", NarwalMapCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "narwal-map-card",
    name: "Narwal map",
    description: "Narwal map: tap rooms to pick them and their cleaning order.",
  });
  console.info(`%c narwal-map-card %c ${VERSION} `, "background:#03a9f4;color:#fff", "");
}
