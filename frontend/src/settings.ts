/** The settings panel (spec §11, ROADMAP 19): frosted glass over the blurred scene, generated from
 *  the server's schema (backend/settings_view.py). TABS is the friendly face of config.py — plain
 *  labels, one-line help, the right control for each setting. A key not curated here still appears
 *  under Advanced with its raw name, so a new setting is never hidden, only unpolished. Secrets
 *  arrive as {set, hint} and are never shown or sent back; a blank box means "leave it". Keys set
 *  in .env are locked, because .env wins at launch.
 *
 *  Sign-in fields are listed from the schema (`secret`), never by key: spec §0's build-time grep
 *  fails on the SRS token key names anywhere in frontend/src, and the server supplies the labels. */
import type { SettingsMsg } from "./protocol.gen";
import { $, esc } from "./ui";

export interface Field {
  key: string; group: string; type: string; default: unknown; description: string; secret: boolean;
  choices: string[]; locked: string; options: string[] | null; live: boolean; label?: string;
}
type Kind = "secret" | "toggle" | "select" | "device" | "number" | "text" | "slider" | "steps"
  | "segmented" | "cards" | "persona";
/** [value, title, subtitle, face letter] */
export type Option = [string, string, string?, string?];
interface Item {
  key?: string; label?: string; help?: string; kind?: Kind; raw?: boolean;
  action?: string; button?: string; meter?: boolean;
  min?: number; max?: number; step?: number; lo?: string; hi?: string; fmt?: (v: number) => string;
  options?: Option[];
}
interface Section { title: string; items: Item[]; secrets?: boolean }
interface Tab { id: string; label: string; sections: Section[]; advanced?: boolean }

const PAL: Record<string, string> = { account: "#e8453c", brain: "#3b82f6", voice: "#f59e0b",
  sound: "#14b8c4", display: "#8b5cf6", advanced: "#7b8494" };
const ICON: Record<string, string> = {
  account:  '<circle cx="8" cy="15" r="4"/><path d="M11 12 20 3M16 7l3 3M18.5 4.5l2 2"/>',
  brain:    '<path d="M12 3l2.2 6.8L21 12l-6.8 2.2L12 21l-2.2-6.8L3 12l6.8-2.2z"/>',
  voice:    '<path d="M4 5h16v10H9l-5 4z"/><path d="M9 9v2M12 8v4M15 9v2"/>',
  sound:    '<path d="M4 15v-3a8 8 0 0 1 16 0v3"/><rect x="3" y="14" width="4" height="6" rx="1.5"/><rect x="17" y="14" width="4" height="6" rx="1.5"/>',
  display:  '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
  advanced: '<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>',
};
const times = (v: number) => v.toFixed(2) + "×";
//: How far along its track a slider sits, for the filled part of the track (CSS --p).
const pct = (v: unknown, min: number, max: number) => (((Number(v) - min) / (max - min)) * 100).toFixed(1) + "%";

const TABS: Tab[] = [
  { id: "account", label: "Account", sections: [
    { title: "Sign-ins", secrets: true, items: [] },
    { title: "Study data", items: [
      { action: "resync", label: "Refresh from WaniKani and Bunpro", button: "Refresh now",
        help: "Fetches your latest progress (read-only) and gives it to her — she carries on with it from her next answer." },
    ] },
  ] },
  { id: "brain", label: "Brain", sections: [
    { title: "Her mind", items: [
      { key: "CLAUDE_MODEL", label: "Model", kind: "cards",
        help: "Always the newest version of the tier your account can use — checked once a week.",
        options: [["sonnet", "Sonnet", "Balanced · recommended"], ["opus", "Opus", "Smartest · slower replies"],
                  ["haiku", "Haiku", "Fastest · lighter"]] },
      { key: "CLAUDE_EFFORT", label: "Thinking", kind: "steps", lo: "Quicker", hi: "Deeper",
        help: "How long she thinks before answering. Medium measured fastest; higher is slower to reply." },
    ] },
    { title: "Memory", items: [
      { key: "MEMORY_ENABLED", label: "Remember past lessons", kind: "toggle",
        help: "She recalls last time, avoids repeating topics, and keeps notes about you that you can edit." },
    ] },
  ] },
  { id: "voice", label: "Voice", sections: [
    { title: "Your tutor", items: [
      { key: "TUTOR_PERSONA", label: "Tutor", kind: "persona",
        help: "Who teaches you — voice and character switch at once, and they introduce themselves." },
    ] },
    { title: "How she sounds", items: [
      { key: "VOICEVOX_SPEED_SCALE", label: "Speaking speed", kind: "slider",
        min: 0.6, max: 1.4, step: 0.05, lo: "Slower", hi: "Faster", fmt: times },
      { key: "VOICEVOX_PITCH_SCALE", label: "Pitch", kind: "slider",
        min: -0.15, max: 0.15, step: 0.01, lo: "Lower", hi: "Higher", fmt: v => (v > 0 ? "+" : "") + v.toFixed(2) },
      { key: "VOICEVOX_INTONATION_SCALE", label: "Expressiveness", kind: "slider",
        min: 0.5, max: 1.6, step: 0.05, lo: "Flatter", hi: "Livelier", fmt: times },
      { key: "VOICEVOX_PAUSE_SCALE", label: "Pauses", kind: "slider",
        min: 0.5, max: 1.6, step: 0.05, lo: "Shorter", hi: "Longer", fmt: times, help: "The breaks at 、 and 。" },
    ] },
  ] },
  { id: "sound", label: "Sound", sections: [
    { title: "Devices", items: [
      { key: "AUDIO_INPUT_DEVICE", label: "Microphone", kind: "device", meter: true,
        help: "Unplug it any time — she switches to the system default, and back when it returns." },
      { key: "AUDIO_OUTPUT_DEVICE", label: "Speakers", kind: "device",
        help: "For the terminal voice only. On this page she plays through your browser's sound output." },
    ] },
    { title: "Talking", items: [
      { key: "TURN_MODE", label: "How you talk", kind: "segmented", options: [["ptt", "Hold SPACE"], ["vad", "Hands-free"]],
        help: "Hold SPACE is the reliable one: her own voice can never end your turn." },
      { key: "VAD_SILENCE_MS", label: "Pause before she answers", kind: "slider",
        min: 400, max: 2000, step: 50, lo: "Snappier", hi: "More patient", fmt: v => (v / 1000).toFixed(2) + " s",
        help: "Hands-free only: how long a silence means you have finished." },
    ] },
  ] },
  { id: "display", label: "Display", sections: [
    { title: "On screen", items: [
      { key: "STUDY_PANEL", label: "Conversation beside the tutor", kind: "toggle",
        help: "Her sentences in a chat on the right, grammar points in red, and a hint of what she wants you to use. Off: the tutor full-width, with subtitles." },
      { key: "EXPLAIN_LANGUAGE", label: "Explain grammar in", kind: "segmented",
        options: [["en", "English"], ["ja", "Japanese"]],
        help: "When you click a red grammar point. Sentence translations are always English." },
      { key: "FURIGANA", label: "Furigana in the chat", kind: "segmented",
        options: [["all", "All kanji"], ["unknown", "Not yet Guru"], ["off", "None"]],
        help: "Readings above kanji. “Not yet Guru” hides them on every kanji you have already reached Guru on in WaniKani." },
      { key: "SUBTITLES", label: "Subtitles", kind: "segmented", options: [["jp", "Japanese"], ["off", "Off"]],
        help: "When the conversation panel is off." },
    ] },
  ] },
  { id: "advanced", label: "Advanced", advanced: true, sections: [] },
];
const ITEM: Record<string, Item> = {};
TABS.forEach(t => t.sections.forEach(sec => sec.items.forEach(it => { if (it.key) ITEM[it.key] = it; })));
//: Advanced lists every key not curated above, under the config group it belongs to.
const RAW_GROUPS: [string, string][] = [["model", "Brain"], ["voice", "Voice"], ["speech", "Listening"],
  ["audio", "Audio"], ["display", "Display"], ["advanced", "System"], ["account", "Account"]];
const FACES = ["#f59e0b", "#3b82f6", "#e8453c", "#22a355", "#8b5cf6"];
const WIDE = new Set<Kind>(["slider", "steps", "cards", "persona"]);

export interface SettingsDeps {
  send(values: Record<string, unknown>): boolean;
  action(name: "resync"): boolean;
  connected(): boolean;
  personas(): Option[];
  opened(open: boolean): void;
}

let deps: SettingsDeps;
let SETTINGS: SettingsMsg | null = null;
let FIELD: Record<string, Field> = {};
let pending: Record<string, unknown> = {};
let fieldErrors: Record<string, string> = {};
let activeGroup = "voice";
let open = false;

const human = (k: string) => { const s = k.toLowerCase().replace(/_/g, " "); return s[0].toUpperCase() + s.slice(1); };
const cap = (s: unknown) => String(s).charAt(0).toUpperCase() + String(s).slice(1);
const icon = (paths: string, color: string) => `<svg viewBox="0 0 24 24" fill="none" stroke="${color}" stroke-width="2" `
  + `stroke-linecap="round" stroke-linejoin="round">${paths}</svg>`;
const dis = (off: boolean) => (off ? " disabled" : "");

export const isOpen = (): boolean => open;
export const values = (): Record<string, unknown> => (SETTINGS?.values ?? {}) as Record<string, unknown>;

export function initSettings(d: SettingsDeps): void {
  deps = d;
  $("cog").onclick = () => openSettings(!open);
  $("scrim").onclick = () => openSettings(false);
  $("sclose").onclick = () => openSettings(false);
  addEventListener("keydown", e => { if (e.key === "Escape" && open) openSettings(false); });
  $("ssave").onclick = () => {
    if (!Object.keys(pending).length || !deps.send(pending)) return;
    ($("ssave") as HTMLButtonElement).disabled = true;
    $("sstate").textContent = "saving…";
  };
}

export function openSettings(on: boolean, tab?: string): void {
  if (tab && TABS.some(t => t.id === tab)) activeGroup = tab;
  open = on;
  deps.opened(on);
  $("scrim").classList.toggle("open", on);
  $("settings").classList.toggle("open", on);
  $("settings").setAttribute("aria-hidden", on ? "false" : "true");
  if (on) render();
  // Closing leaves focus on whatever was clicked last. Let go of it, so the next SPACE is
  // push-to-talk and not a keypress for a control nobody can see.
  else (document.activeElement as HTMLElement | null)?.blur?.();
}

/** A fresh echo from the server: the schema, masked values, and — after a save — what happened. */
export function onSettings(msg: SettingsMsg): void {
  SETTINGS = msg;
  FIELD = Object.fromEntries((msg.fields as unknown as Field[]).map(f => [f.key, f]));
  const saved = msg.saved || [], errors = msg.errors || {};
  const reply = saved.length || Object.keys(errors).length;
  if (reply) {
    for (const k of saved) delete pending[k];
    fieldErrors = errors;
  }
  if (open) render();
  if (!reply) return;
  const e = Object.keys(errors).length;
  const live = saved.filter(k => FIELD[k]?.live).length;
  const later = saved.length - live;
  footer([live ? "Applied now." : "", later ? `${later} saved — applies the next time you start.` : "",
          e ? `${e} not saved — see the red notes.` : ""].filter(Boolean).join(" "));
  if (saved.length) {
    const btn = $("ssave");
    btn.classList.add("done");
    btn.textContent = "Saved ✓";
    setTimeout(() => { btn.classList.remove("done"); btn.textContent = "Save"; }, 1600);
  }
}

/** The Sound tab's meter: seeing the bar move is the fastest proof the right mic is live. */
export function showLevel(percent: number, speech: number): void {
  const bar = open ? document.getElementById("lvl") : null;
  if (!bar) return;
  bar.style.width = percent + "%";
  bar.classList.toggle("speech", speech >= 0.5);
}

/** The Refresh button's progress (spec §5b). */
export function resyncProgress(state: string): void {
  const b = document.querySelector<HTMLButtonElement>('[data-action="resync"]');
  if (!b) return;
  b.disabled = state === "syncing";
  b.textContent = state === "syncing" ? "Refreshing…" : state === "done" ? "Refreshed ✓" : "Refresh now";
}

/** Re-render if the panel is up (e.g. the cast arrived, so the tutor cards can be drawn). */
export function refresh(): void { if (open) render(); }

// ------------------------------------------------------------------ rendering
function sectionsFor(tab: Tab): Section[] {
  if (!SETTINGS) return [];
  const fields = SETTINGS.fields as unknown as Field[];
  if (tab.advanced)
    return RAW_GROUPS.map(([group, title]) => ({
      title, items: fields.filter(f => f.group === group && !f.secret && !ITEM[f.key]).map(f => ({ key: f.key, raw: true })),
    })).filter(sec => sec.items.length);
  return tab.sections.map(sec => ({
    title: sec.title,
    items: sec.secrets ? fields.filter(f => f.secret).map(f => ({ key: f.key, label: f.label || human(f.key), help: f.description }))
      : sec.items.filter(it => it.action || (it.key && FIELD[it.key])),
  })).filter(sec => sec.items.length);
}

const current = (k: string) => (k in pending ? pending[k] : values()[k]);

function kindOf(s: Field): Kind {
  if (s.secret) return "secret";
  if (s.type === "bool") return "toggle";
  if (s.options) return "device";
  if (s.choices.length) return "select";
  return s.type === "int" || s.type === "float" ? "number" : "text";
}

const CONTROLS: Record<Kind, (s: Field, it: Item, v: unknown, off: boolean, options?: Option[]) => string> = {
  secret(s, _it, _v, off) {
    const st = (values()[s.key] || {}) as { set?: boolean };
    const ph = off ? "kept in your .env file" : st.set ? "paste a new one to replace it" : "paste it here";
    return `<div class="secret"><span class="state${st.set ? " yes" : ""}">${st.set ? "connected" : "not set"}</span>`
      + `<input type="password" autocomplete="off" data-k="${s.key}" placeholder="${esc(ph)}" value="${esc(pending[s.key] || "")}"${dis(off)}></div>`;
  },
  toggle: (s, _it, v, off) =>
    `<label class="switch"><input type="checkbox" data-k="${s.key}"${v ? " checked" : ""}${dis(off)}><span></span></label>`,
  select: (s, _it, v, off) => `<select data-k="${s.key}"${dis(off)}>`
    + s.choices.map(c => `<option${c === v ? " selected" : ""}>${esc(c)}</option>`).join("") + "</select>",
  device(s, _it, v, off) {
    const opts: [string, string][] = [["", "System default"], ...(s.options || []).map(o => [o, o] as [string, string])];
    if (v && !(s.options || []).includes(String(v))) opts.push([String(v), v + "  (not connected)"]);
    return `<select class="device" data-k="${s.key}"${dis(off)}>` + opts.map(([val, l]) =>
      `<option value="${esc(val)}"${val === (v || "") ? " selected" : ""}>${esc(l)}</option>`).join("") + "</select>";
  },
  number: (s, _it, v, off) =>
    `<input type="number" data-k="${s.key}" step="${s.type === "int" ? 1 : "any"}" value="${esc(v)}"${dis(off)}>`,
  text: (s, _it, v, off) =>
    `<input type="text" data-k="${s.key}" value="${esc(v)}" placeholder="${esc(s.default === "" ? "empty" : s.default)}"${dis(off)}>`,
  slider: (s, it, v, off) => `<div class="slider"><span>${esc(it.lo)}</span>`
    + `<input type="range" data-k="${s.key}" min="${it.min}" max="${it.max}" step="${it.step}" value="${esc(v)}" `
    + `style="--p:${pct(v, it.min!, it.max!)}"${dis(off)}>`
    + `<span>${esc(it.hi)}</span><output>${esc(it.fmt!(Number(v)))}</output></div>`,
  steps(s, it, v, off) {
    const at = Math.max(0, s.choices.indexOf(String(v)));
    return `<div class="slider"><span>${esc(it.lo)}</span>`
      + `<input type="range" data-k="${s.key}" data-steps="${esc(JSON.stringify(s.choices))}" min="0" `
      + `max="${s.choices.length - 1}" step="1" value="${at}" style="--p:${pct(at, 0, s.choices.length - 1)}"${dis(off)}>`
      + `<span>${esc(it.hi)}</span><output>${esc(cap(s.choices[at] || v))}</output></div>`;
  },
  segmented: (s, it, v, off) => `<div class="seg">` + (it.options || []).map(([val, l]) =>
    `<button type="button" data-k="${s.key}" data-v="${esc(val)}" class="${val === v ? "on" : ""}"${dis(off)}>${esc(l)}</button>`)
    .join("") + "</div>",
  cards(s, it, v, off, options = it.options || []) {
    const opts = !v || options.some(o => o[0] === v) ? options : options.concat([[String(v), String(v), "your own setting"]]);
    return `<div class="cards">` + opts.map(([val, title, sub, face], i) =>
      `<button type="button" class="card${val === v ? " on" : ""}" data-k="${s.key}" data-v="${esc(val)}"${dis(off)}>`
      + (face ? `<span class="face" style="background:${FACES[i % FACES.length]}">${esc(face)}</span>` : "")
      + `<span><b>${esc(title)}</b><small>${esc(sub)}</small></span></button>`).join("") + "</div>";
  },
  persona(s, it, v, off) {
    const opts = deps.personas();
    return opts.length ? CONTROLS.cards(s, it, v, off, opts) : CONTROLS.text(s, it, v, off);
  },
};

function actionItem(it: Item): string {
  return `<div class="field"><div class="ftop"><div class="flabel"><div class="fname">${esc(it.label)}</div>`
    + `<div class="fdesc">${esc(it.help || "")}</div></div>`
    + `<button type="button" class="act" data-action="${esc(it.action)}">${esc(it.button || "Run")}</button></div></div>`;
}

function item(it: Item): string {
  if (it.action) return actionItem(it);
  const s = FIELD[it.key!];
  const pin = (SETTINGS!.pinned as Record<string, string>)[s.key], lock = s.locked, off = !!(pin || lock);
  const kind = it.kind || kindOf(s);
  const ctl = CONTROLS[kind](s, it, current(s.key), off);
  const badge = pin
    ? `<span class="badge pin" title="Set in ${esc(pin)}, which overrides this panel. Remove it there to edit it here.">in ${esc(pin)}</span>`
    : lock ? `<span class="badge" title="${esc(lock)}">locked</span>` : "";
  const help = it.raw ? s.description : (it.help || "");
  const wide = WIDE.has(kind);
  return `<div class="field${s.key in pending ? " dirty" : ""}${it.raw ? " raw" : ""}" data-field="${s.key}">`
    + `<div class="ftop"><div class="flabel"><div class="fname">${esc(it.label || human(s.key))}${badge}</div>`
    + (help ? `<div class="fdesc">${esc(help)}</div>` : "") + "</div>" + (wide ? "" : ctl) + "</div>"
    + (wide ? `<div class="fwide">${ctl}</div>` : "")
    + (it.meter ? '<div class="meter" title="Microphone level"><i id="lvl"></i></div>' : "")
    + (fieldErrors[s.key] ? `<div class="serr">${esc(fieldErrors[s.key])}</div>` : "") + "</div>";
}

function renderNav(): void {
  const nav = $("snav");
  nav.innerHTML = "";
  for (const tab of TABS) {
    const secs = sectionsFor(tab);
    if (SETTINGS && !secs.length) continue;
    const hasErr = secs.some(sec => sec.items.some(it => it.key && fieldErrors[it.key]));
    const b = document.createElement("button");
    b.className = "sgroup" + (tab.id === activeGroup ? " on" : "") + (hasErr ? " haserr" : "");
    b.style.setProperty("--c", PAL[tab.id]);
    b.innerHTML = `<span class="tile">${icon(ICON[tab.id], PAL[tab.id])}</span>${esc(tab.label)}`;
    b.onclick = () => { activeGroup = tab.id; render(); };
    nav.appendChild(b);
  }
}

function render(): void {
  renderNav();
  const tab = TABS.find(t => t.id === activeGroup) || TABS[0];
  $("settings").style.setProperty("--c", PAL[tab.id]);
  $("stitle").innerHTML = `<span class="tile">${icon(ICON[tab.id], PAL[tab.id])}</span>${esc(tab.label)}`;
  const body = $("sbody");
  if (!SETTINGS) {
    body.innerHTML = '<p class="shint">Connect to the tutor to see the settings.</p>';
    footer();
    return;
  }
  body.innerHTML = (tab.advanced ? '<p class="shint">For tinkering: the raw settings, with their developer notes.</p>' : "")
    + sectionsFor(tab).map(sec => `<h3 class="ssec">${esc(sec.title)}</h3>` + sec.items.map(item).join("")).join("");
  bindControls(body);
  footer();
}

function bindControls(body: HTMLElement): void {
  body.querySelectorAll<HTMLElement>("[data-k]").forEach(el => {
    const k = el.dataset.k!;
    if (el.tagName === "BUTTON") {
      el.onclick = () => {
        el.parentElement!.querySelectorAll("button").forEach(b => b.classList.toggle("on", b === el));
        change(k, el.dataset.v);
      };
      return;
    }
    const input = el as HTMLInputElement;
    const event = input.type === "checkbox" || el.tagName === "SELECT" ? "change" : "input";
    el.addEventListener(event, () => {
      let v: unknown = input.type === "checkbox" ? input.checked : input.value;
      if (input.dataset.steps) v = JSON.parse(input.dataset.steps)[Number(input.value)];
      if (input.type === "range") input.style.setProperty("--p", pct(input.value, Number(input.min), Number(input.max)));
      const out = input.type === "range" ? input.parentElement!.querySelector("output") : null;
      if (out) out.textContent = input.dataset.steps ? cap(v) : ITEM[k].fmt!(Number(v));
      change(k, v);
    });
  });
  body.querySelectorAll<HTMLElement>(".raw .fdesc").forEach(el => { el.onclick = () => el.classList.toggle("full"); });
  body.querySelectorAll<HTMLButtonElement>("[data-action]").forEach(b => {
    b.onclick = () => { if (deps.action(b.dataset.action as "resync")) resyncProgress("syncing"); };
  });
}

function change(k: string, v: unknown): void {
  const s = FIELD[k], was = values()[k];
  const numeric = s.type === "int" || s.type === "float";
  const same = s.secret ? v === "" : numeric ? Number(v) === Number(was) : String(v) === String(was);
  if (same) delete pending[k]; else pending[k] = v;
  $("sbody").querySelector(`[data-field="${k}"]`)?.classList.toggle("dirty", k in pending);
  footer();
}

function footer(text?: string): void {
  const n = Object.keys(pending).length;
  ($("ssave") as HTMLButtonElement).disabled = !n || !deps.connected();
  $("sstate").textContent = text
    || (n ? `${n} ${n === 1 ? "change" : "changes"} not saved yet`
          : "Tutor, microphone and speakers apply at once; the rest the next time you start.");
}
