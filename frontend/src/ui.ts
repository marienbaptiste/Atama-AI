/** Page chrome that is neither the avatar, the status bar nor the settings panel (spec §8): the
 *  activity log, the live line, subtitles, the start and ended screens, the headphones hint, and
 *  her mood as white kaomoji drifting up behind her. */

/** The product's name — package.json `productName`, injected at build time (vite.config.ts). The
 *  page says it wherever it would otherwise say "her": in the title and every tooltip. */
export const APP_NAME: string = __APP_NAME__;

export const $ = <T extends HTMLElement = HTMLElement>(id: string): T => {
  const el = document.getElementById(id);
  if (!el) throw new Error(`#${id} is missing from index.html`);
  return el as T;
};

const ESC: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const esc = (s: unknown): string => String(s ?? "").replace(/[&<>"']/g, c => ESC[c]);

// ------------------------------------------------------------------ activity log
const LOG_KEEP = 80;
export type LogKind = "info" | "ok" | "err" | "you" | "her";

/** One line in the activity panel, newest first. `html` is trusted markup — escape user text. */
export function log(html: string, kind: LogKind = "info"): void {
  if (typeof document === "undefined") return;     // headless tests
  const box = document.getElementById("log");
  if (!box) return;
  const line = document.createElement("div");
  line.className = "l " + kind;
  line.innerHTML = html;
  box.prepend(line);
  while (box.childElementCount > LOG_KEEP) box.lastElementChild?.remove();
}

/** The one-line status under the talk button. */
export function live(text: string, tone: "" | "on" | "warn" = ""): void {
  if (typeof document === "undefined") return;     // headless tests
  const el = document.getElementById("live");
  if (!el) return;
  el.textContent = text;
  el.dataset.tone = tone;
}

// ------------------------------------------------------------------ subtitles (spec §8: JP / off)
let subsOn = true;
let subsTimer = 0;

export function setSubtitles(mode: unknown): void {
  subsOn = mode !== "off";
  if (!subsOn) $("subs").hidden = true;
}

/** Her sentence as it starts playing, or what you said once it was heard. */
export function subtitle(text: string, who: "her" | "you"): void {
  if (!subsOn || !text) return;
  const el = $("subs");
  el.dataset.who = who;
  el.textContent = text;
  el.hidden = false;
  clearTimeout(subsTimer);
  subsTimer = window.setTimeout(() => { el.hidden = true; }, who === "her" ? 9000 : 5000);
}

// ------------------------------------------------------------------ start / ended / hint
/** Browsers refuse to play sound until the page has been touched (autoplay policy); audio sent
 *  before that was silently lost — her whole opening (2026-09-10). The first click or key only
 *  starts the page: capture phase, so it never doubles as push-to-talk. */
export function onFirstTouch(start: () => void): void {
  let done = false;
  const go = () => {
    if (done) return;
    done = true;
    $("start").hidden = true;
    start();
  };
  $("start").addEventListener("pointerdown", go);
  addEventListener("keydown", e => {
    if (done) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    go();
  }, true);
}

export function showEnded(): void {
  if (document.getElementById("ended")) return;
  const cover = document.createElement("div");
  cover.id = "ended";
  cover.innerHTML = "<div><b>Session ended</b><br>The tutor, the server and the containers have "
    + "stopped.<br>Start again from the terminal with the run command, then reload this page.</div>";
  document.body.appendChild(cover);
}

export function hint(text: string | null): void {
  const el = $("hint");
  el.hidden = !text;
  el.textContent = text || "";
}

// ------------------------------------------------------------------ mood kaomoji
//: One pool per emotion tag (ADR-020's closed set). Faces come only when she FEELS something, in a
//: short burst that drifts off; neutral, idle or waiting shows none — a constant stream behind a
//: tutor who is just waiting reads as noise, not mood (user, 2026-09-10).
export const KAOMOJI: Record<string, string[]> = {
  neutral:     ["(・ω・)", "(´･ω･`)", "(￣ー￣)", "( ´ ▽ ` )"],
  happy:       ["(＾▽＾)", "٩(◕‿◕)۶", "(≧◡≦)", "ヽ(•‿•)ノ", "(´▽`)♪", "(^_^)"],
  thinking:    ["(・・ )?", "(ー_ー)", "(´-ω-`)", "( ˘ω˘ )", "(。-ω-)"],
  surprised:   ["Σ(°□°)", "(⊙_⊙)", "(°o°)", "w(°ｏ°)w", "ヽ(°〇°)ﾉ"],
  serious:     ["(・_・)", "(￣^￣)", "(-_-)", "(｀_´)"],
  encouraging: ["(ง •̀_•́)ง", "٩(^ᴗ^)و", "(•̀ᴗ•́)و ✧", "(9^o^)9", "ᕦ(ò_ó)ᕤ"],
  proud:       ["( •̀ ω •́ )✧", "(￣▽￣)ノ", "＼(^o^)／", "(⌐■_■)", "☆(ゝω・)v"],
  confused:    ["(・へ・)?", "¯\\_(ツ)_/¯", "(◎_◎;)", "(゜-゜)", "(・・？"],
};
//: How long a mood keeps sending faces before the flow stops and the last ones drift away.
const KAO_BURST_MS = 3000;
let kaoMood = "neutral";
let kaoTimer = 0;
let kaoPaused = false;

export function setKaoPaused(paused: boolean): void { kaoPaused = paused; }

function spawnKao(mood = kaoMood, midway = false): void {
  const layer = document.getElementById("moodbg");
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!layer || reduced || document.hidden || kaoPaused || layer.childElementCount > 18) return;
  const pool = KAOMOJI[mood] || KAOMOJI.neutral;
  const near = Math.random() < 0.35;               // a few close and crisp, most far and soft
  const dur = 10 + Math.random() * 8;
  const el = document.createElement("span");
  el.className = "kao";
  el.textContent = pool[Math.floor(Math.random() * pool.length)];
  el.style.cssText = `--x:${(4 + Math.random() * 88).toFixed(1)}%;`
    + `--s:${(near ? 30 + Math.random() * 14 : 18 + Math.random() * 10).toFixed(0)}px;`
    + `--b:${near ? 0 : 1.2}px;--o:${near ? 0.34 : 0.18};--d:${dur.toFixed(1)}s;`
    + `--dx:${(Math.random() * 120 - 60).toFixed(0)}px;`
    + `--r0:${(Math.random() * 16 - 8).toFixed(0)}deg;--r1:${(Math.random() * 16 - 8).toFixed(0)}deg`;
  // Already on its way up: a mood that just changed must not take ten seconds to show anything.
  if (midway) el.style.animationDelay = `-${(Math.random() * dur * 0.7).toFixed(1)}s`;
  el.addEventListener("animationend", () => el.remove());
  layer.appendChild(el);
}

/** Something the student just used correctly, drifting up behind her (user, 2026-09-12). The same
 *  layer as her mood faces, but larger and slower: this is praise, not decoration.
 *
 *  `kind` is which of their lists it came from, checked on the orchestrator against what they have
 *  not yet Guru'd (backend/study.py): blue for one of their words, red for a grammar point — the
 *  chat's own two colours — and gold when it is theirs but from neither list. */
export function floatWord(word: string, kind = ""): void {
  const layer = document.getElementById("moodbg");
  if (!layer || !word || kaoPaused || document.hidden) return;
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const el = document.createElement("span");
  el.className = "kao word" + (kind === "vocab" || kind === "grammar" ? " " + kind : "");
  el.textContent = word;
  const seconds = 11 + Math.random() * 4;
  el.style.cssText = `--x:${(12 + Math.random() * 62).toFixed(1)}%;--s:${(30 + Math.random() * 12).toFixed(0)}px;`
    + `--b:0px;--o:0.9;--d:${seconds.toFixed(1)}s;--dx:${(Math.random() * 80 - 40).toFixed(0)}px;`
    + "--r0:-3deg;--r1:3deg";
  el.addEventListener("animationend", () => el.remove());
  layer.appendChild(el);
}

export function setMoodBg(mood: string): void {
  mood = KAOMOJI[mood] ? mood : "neutral";
  const fresh = mood !== kaoMood;
  kaoMood = mood;
  clearInterval(kaoTimer);
  if (mood === "neutral") return;                  // the ones in flight finish and fade
  const now = fresh ? 4 : 2;
  for (let i = 0; i < now; i++) setTimeout(() => spawnKao(mood, fresh), i * 150);
  const flow = kaoTimer = window.setInterval(() => spawnKao(), 700);
  setTimeout(() => clearInterval(flow), KAO_BURST_MS);   // this burst's flow only, not a newer one
}
