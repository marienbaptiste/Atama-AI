/** The avatar page (spec §8): the link, her face, the key, the status bar and the settings panel,
 *  wired together. Every server message has exactly one handler below, and `Handlers` makes `tsc`
 *  fail while one is missing (gate M3a). */
import "./style.css";
import { Avatar, type CastEntry } from "./avatar";
import { Chat } from "./chat";
import { Talk } from "./mic";
import type { ExplanationMsg, ServiceStatusMsg, SettingsMsg, SpeakMsg, TimingMsg } from "./protocol.gen";
import { Backchannel } from "./rig";
import { loadSamples, mountRigPanel } from "./rigpanel";
import * as settings from "./settings";
import * as status from "./status";
import { $, APP_NAME, esc, floatWord, hint, live, log, onFirstTouch, setMoodBg, setSubtitles, showEnded, subtitle } from "./ui";
import { Link, type Handlers } from "./ws";

//: Plain names for the cast. cast.json comes from the persona files themselves (make_preview).
const PERSONA_LABELS: Record<string, string> = {
  minami: "みなみ先生 — female teacher", hayashi: "はやし先生 — younger man",
  mori: "ゆい — teenager", tanaka: "たなか先生 — older man",
};
//: Spec §8: "headphones recommended" until the first successful barge-in.
const BARGED_KEY = "atama.bargein-ok";

let cast: CastEntry[] = [];
let avatar: Avatar | null = null;
let state: "listening" | "thinking" | "speaking" = "listening";
let turn = 0;
let unlocked = false;
let mode = "ptt";
const back = new Backchannel();
//: The persona whose face is showing. Chosen in Settings → Voice, never on the main screen.
let persona = "";
//: What she has asked the student to use (a `[target:]` mark, ADR-036) — the hint. Cleared when
//: the student answers, i.e. when her next turn starts.
let goal = "";
const chat = new Chat($("chat-list"), (point, mark) => showPoint(point, mark),
                      sentence => askExplain("sentence", sentence));
//: EXPLAIN_LANGUAGE: the language a grammar explanation comes back in (a translation is English).
let explainLang: "en" | "ja" = "en";

const avatarReady: Promise<Avatar> = Avatar.create($("stage"), onSentence);
avatarReady.then(a => { avatar = a; mountRigPanel(a); },
                 err => log(`the avatar could not load: ${esc(err)}`, "err"));

/** Her sentence, the moment its audio starts: the face has just been set (speech.ts). */
function onSentence(msg: SpeakMsg, preview: boolean): void {
  subtitle(msg.text, "her");
  if (preview) return;                              // a rig-panel sample, not the conversation
  chat.her(msg);
  if (msg.target) setGoal(msg.target);
  if (msg.used) {                                   // you used it, and she noticed
    floatWord(msg.used, msg.used_kind);
    const whose = msg.used_kind === "vocab" ? " — one of your words"
      : msg.used_kind === "grammar" ? " — one of your grammar points" : "";
    log(`<b>you used ${esc(msg.used)}</b>${whose}`, "ok");
  }
  log(`<b>her:</b> ${esc(msg.text)}` + (msg.emotion ? ` <i>${esc(msg.emotion)}</i>` : ""), "her");
}

// ------------------------------------------------------------------ every server message
const handlers: Handlers = {
  state: m => onState(m.state, m.turn),
  stt_partial: () => { /* reserved: never sent (models.py SttPartial) */ },
  stt_final: m => {
    if (m.accepted) subtitle(m.text, "you");
    chat.you(m.text, m.accepted, m.reason, m.readings, m.vocab);
    log(`<b>you:</b> ${esc(m.text)}` + (m.accepted ? "" : ` <i>(discarded: ${esc(m.reason)})</i>`),
        m.accepted ? "you" : "err");
  },
  // Not sent today: `speak` carries each sentence, shown as its audio starts.
  assistant_text: m => log(`<b>her:</b> ${esc(m.text)}`, "her"),
  speak: async m => {
    const a = await avatarReady;
    await a.loaded;                                 // she cannot speak before her model is on stage
    await a.player.play(m);
  },
  emotion: async m => (await avatarReady).emote(m.emotion),
  bargein: async m => {
    (await avatarReady).stop(m.turn);
    bargedIn();
    if (mode !== "ptt") log("she heard you and stopped");
  },
  srs_profile: m => { $("profile").textContent = m.text; },
  service_status: m => onService(m),
  settings: m => onSettings(m),
  mic_level: m => onLevel(m.level, m.speech),
  meters: m => status.onMeters(m, Number(settings.values().VRAM_WARN_GB) || 10),
  explanation: m => onExplanation(m),
  timing: m => showTiming(m),
  error: m => log(esc(m.message), "err"),
};

//: What the chat says while she is still coming up (user, 2026-09-12). The chips already carry
//: the detail; this turns the loudest of them into one sentence, because a blank panel during a
//: 40-second launch looks broken. Her first sentence removes it (chat.her).
const LOADING: Record<string, (m: ServiceStatusMsg) => string | null> = {
  brain: m => m.state === "starting"
    ? (/lesson/i.test(m.detail) ? "reading back your last lesson…" : "waking your tutor…")
    : m.state === "ready" ? "she is thinking of how to start…" : null,
  stt: m => (m.state === "loading" ? "loading speech recognition…" : null),
  voicevox: m => (m.state === "loading" ? "warming her voice…" : null),
};

function waitingFor(m: ServiceStatusMsg): void {
  const caption = LOADING[m.service]?.(m);
  if (caption) chat.loading(caption);
}

function onState(s: typeof state, t: number): void {
  state = s;
  turn = t;
  if (s === "thinking") { avatar?.thinking(); setGoal(""); }   // the student answered
  else if (s === "listening") { avatar?.listening(); avatar?.player.flush(); }   // her turn is over
  const ptt = mode === "ptt";
  live(s === "thinking" ? "she is thinking…"
    : s === "speaking" ? (ptt ? "she is speaking — hold SPACE to interrupt" : "she is speaking")
    : (ptt ? "hold ALT GR to cancel what you are saying" : "your turn — just speak"),
    s === "listening" ? "on" : "");
}

function onService(m: ServiceStatusMsg): void {
  switch (m.service) {
    case "orchestrator": return;                    // the heartbeat: arriving is all it has to do
    case "ptt": talk.ack(m); return;                // what the last press actually did
    case "resync":                                  // the Refresh button's progress (spec §5b)
      settings.resyncProgress(m.state);
      if (m.state !== "syncing") log(esc(m.detail), m.state === "done" ? "ok" : "err");
      return;
    case "compaction":                              // Claude condensing her memory (spec §6b)
      if (m.state === "running") { live("tidying her notes — one moment…", "warn"); return; }
      log(esc(m.detail), m.state === "failed" ? "err" : "info");
      return;
    case "gpu":                                     // spec §10b: only worth a word over the cap
      if (m.state === "over") log(`GPU memory over the cap — ${esc(m.detail)}`, "err");
      return;
    case "tutor":                                   // a live tutor change in progress
      // On the main line, not only in the Activity card: a switch that failed silently left the
      // old tutor talking while the panel said the new one was chosen (2026-09-11).
      live(m.detail, m.state === "failed" ? "warn" : "on");
      log(esc(m.detail), m.state === "failed" ? "err" : "ok");
      return;
    case "microphone":                              // unplugged, missing, back (spec §9)
      status.showMic(m.state, m.detail);
      if (m.state !== "ok") log(`microphone ${esc(m.state)} — ${esc(m.detail)}`, "err");
      return;
    default:
      if (status.isService(m.service)) status.showService(m);
      waitingFor(m);
  }
}

function onSettings(m: SettingsMsg): void {
  settings.onSettings(m);
  const v = m.values as Record<string, unknown>;
  // The conversation panel replaces the subtitles; with it off, subtitles as configured.
  const study = v.STUDY_PANEL !== false;
  document.body.classList.toggle("study", study);
  setSubtitles(study ? "off" : v.SUBTITLES);
  chat.setFurigana(String(v.FURIGANA ?? "unknown"));
  explainLang = v.EXPLAIN_LANGUAGE === "ja" ? "ja" : "en";
  mode = String(v.TURN_MODE || "ptt");
  talk.render();
  headphonesHint();
  const tutor = String(v.TUTOR_PERSONA || "");
  if (tutor && tutor !== persona && cast.some(c => c.id === tutor)) void showPersona(tutor);
  setupCard();
}

//: First run (user, 2026-09-12): nothing is configured, and the tutor would quietly teach as if
//: you were an early beginner. What is missing comes from the schema the server sent — secrets
//: arrive only as {set, hint} (ADR-022) — because this page may not name the study services
//: (spec §0). Dismissed once, it stays dismissed; the Services chip still shows the gap.
const SETUP_KEY = "atama.setup.dismissed";

function setupCard(): void {
  let dismissed = false;
  try { dismissed = localStorage.getItem(SETUP_KEY) === "1"; } catch { /* private window */ }
  const show = settings.nothingConfigured() && !dismissed;
  $("setup").hidden = !show;
  if (!show) return;
  $("setup-why").textContent =
    `${APP_NAME} teaches from your own reviews — your level, your leeches, the grammar you keep `
    + "forgetting. Without a sign-in the lesson still works, but as if you were starting from "
    + "scratch. It takes a minute: " + settings.unsetSecrets().join(", ") + ".";
}

$("setup-go").onclick = () => { $("setup").hidden = true; settings.openSettings(true, "account"); };
$("setup-skip").onclick = () => {
  $("setup").hidden = true;
  try { localStorage.setItem(SETUP_KEY, "1"); } catch { /* private window */ }
  log("no study data yet — add your keys any time in Settings → Account", "err");
};

function onLevel(level: number, speech: number): void {
  status.micLevel(level);
  settings.showLevel(status.levelPercent(level), speech);
  const floor = talk.talking || (mode === "vad" && state === "listening");
  for (const reaction of back.feed(performance.now(), speech, floor)) avatar?.[reaction]();
}

//: Spec §10: the last turn's breakdown, and where this session's p90 stands against the gate.
function showTiming(m: TimingMsg): void {
  if (m.barged_in) return;                          // an interrupted turn was not slow
  const s = (v: number | null) => (v == null ? "-" : (v / 1000).toFixed(1) + " s");
  log(`last turn: <b>${s(m.first_audio_ms)}</b> until her voice (listening ${s(m.stt_ms)}, `
    + `first sentence ${s(m.first_chunk_ms)}` + (m.thinking_chars ? `, thought ${m.thinking_chars} chars` : "")
    + ")" + (m.p90_ms != null ? ` · session p90 ${s(m.p90_ms)} of 5.0 s over ${m.turns}` : ""));
}

// ------------------------------------------------------------------ study panel: hint and grammar
function setGoal(target: string): void {
  goal = target;
  const button = $<HTMLButtonElement>("goal");
  button.disabled = !target;
  button.classList.toggle("has", !!target);
  if (!target) $("goal-pop").hidden = true;
}

$("goal").onclick = e => {
  e.stopPropagation();
  if (!goal) return;
  $("goal").classList.remove("has");
  showPop($("goal-pop"), $("goal"), "<small>Your tutor is waiting for you to use</small>"
    + `<b>${esc(goal)}</b><p>Try it in your answer — hold SPACE and speak.</p>`);
  $("goal").blur();                                 // or the next SPACE would press it
};

function askExplain(kind: "grammar" | "sentence", text: string, context = ""): void {
  if (!link.send({ type: "explain", kind, text, context, lang: explainLang })) {
    log("not connected — nothing to ask", "err");
  }
}

function showPoint(point: string, mark: HTMLElement): void {
  const pop = $("gp-pop");
  pop.dataset.point = point;
  showPop(pop, mark, `<small>Grammar point</small><b>${esc(point)}</b>`
    + '<p class="wait">Looking it up…</p>');
  askExplain("grammar", point, chat.sentenceOf(mark));   // the sentence she used it in
}

/** The answer to a click: into the open grammar card, or under the sentence it translates. */
function onExplanation(m: ExplanationMsg): void {
  if (m.kind === "sentence") { chat.setTranslation(m.text, m.answer, m.error); return; }
  const pop = $("gp-pop");
  const line = pop.querySelector("p");
  if (pop.hidden || pop.dataset.point !== m.text || !line) return;
  line.textContent = m.answer || m.error || "no answer";
  line.className = m.answer ? "" : "bad";
}

/** A small card beside what was clicked: above it if there is room, else below; on screen. */
function showPop(pop: HTMLElement, anchor: HTMLElement, html: string): void {
  for (const other of document.querySelectorAll<HTMLElement>(".pop")) other.hidden = true;
  pop.innerHTML = html;
  pop.hidden = false;
  const r = anchor.getBoundingClientRect();
  const w = pop.offsetWidth, h = pop.offsetHeight;
  pop.style.left = Math.max(8, Math.min(innerWidth - w - 8, r.left + r.width / 2 - w / 2)) + "px";
  pop.style.top = (r.top - h - 10 >= 8 ? r.top - h - 10 : r.bottom + 10) + "px";
}

addEventListener("click", e => {
  if (!(e.target as HTMLElement | null)?.closest?.(".pop")) {
    for (const pop of document.querySelectorAll<HTMLElement>(".pop")) pop.hidden = true;
  }
});
addEventListener("keydown", e => {
  if (e.key === "Escape") for (const pop of document.querySelectorAll<HTMLElement>(".pop")) pop.hidden = true;
});

function headphonesHint(): void {
  let done = false;
  try { done = localStorage.getItem(BARGED_KEY) === "1"; } catch { /* private window */ }
  hint(mode === "vad" && !done ? "Headphones recommended — on speakers her own voice can sound like you talking." : null);
}

function bargedIn(): void {
  try { localStorage.setItem(BARGED_KEY, "1"); } catch { /* private window */ }
  hint(null);
}

// ------------------------------------------------------------------ the link and the key
const link = new Link(handlers, {
  open() {
    for (const id of ["topic", "quit"]) $<HTMLButtonElement>(id).disabled = false;
    // A reconnect is a new socket: tell the server again that this page can play sound.
    if (unlocked) link.send({ type: "control", action: "ready" });
    live("connected — hold SPACE, or the button, and speak", "on");
    chat.loading("getting everything ready…");        // until her first sentence lands
    talk.render();
    settings.refresh();
  },
  close(retrying) {
    for (const id of ["topic", "quit"]) $<HTMLButtonElement>(id).disabled = true;
    talk.reset();
    if (!retrying) { avatar?.end(); showEnded(); return; }  // stopped on purpose: no retry loop
    live("disconnected — reconnecting…", "warn");
  },
  stale() { log("no word from the server for 12 s — reconnecting", "err"); },
});

const talk = new Talk({
  send: action => link.send({ type: "control", action }),
  connected: () => link.open,
  blocked: () => settings.isOpen(),
  mode: () => mode,
  interrupt: () => {
    // She is talking, or about to (thinking): stop her here, and drop the rest of this turn
    // however late it arrives. The server's `bargein` then confirms (spec §8).
    if (!avatar || (!avatar.isTalking() && state === "listening")) return false;
    avatar.stop(Math.max(avatar.player.lastTurn, turn));
    bargedIn();
    return true;
  },
  deadLink: () => link.drop(),
}, $<HTMLButtonElement>("talk"));

settings.initSettings({
  send: values => link.send({ type: "settings", values }),
  action: name => link.send({ type: "control", action: name }),
  connected: () => link.open,
  personas: () => cast.map(c => {
    const [name, sub = ""] = (PERSONA_LABELS[c.id] || c.id).split(" — ");
    return [c.id, name, sub + (c.own_face ? "" : " · stand-in face"), name.charAt(0)];
  }),
  opened: on => { if (on) talk.press(false); },
});

//: Same as saying 「話題を変えて」: she drops the subject, interrupting herself if need be.
//: New topic asks first (user, 2026-09-12): it interrupts her and throws away the subject you
//: were in the middle of, which is a lot to lose to a misclick on a 42px round button.
$("topic").onclick = e => {
  e.stopPropagation();                              // or the page-wide handler closes it at once
  showPop($("topic-pop"), $("topic"), "<small>Change the subject?</small>"
    + "<p>She drops what you are talking about and finds something new.</p>"
    + '<div class="row"><button type="button" id="topic-yes">New topic</button>'
    + '<button type="button" id="topic-no">Keep going</button></div>');
  $("topic-pop").querySelector<HTMLButtonElement>("#topic-yes")!.onclick = () => {
    $("topic-pop").hidden = true;
    link.send({ type: "control", action: "new_topic" });
    log("asked her for a new topic", "ok");
  };
  $("topic-pop").querySelector<HTMLButtonElement>("#topic-no")!.onclick = () => {
    $("topic-pop").hidden = true;
  };
  $("topic").blur();                                // or the next SPACE would press it again
};

$("quit").onclick = () => {
  if (!link.open) { showEnded(); return; }
  if (!confirm("Stop the tutor and shut down the server?")) return;
  link.quitting = true;
  talk.press(false);                                // never leave the mic open on the way out
  link.send({ type: "control", action: "quit" });
  live("stopping…");
  window.setTimeout(showEnded, 8000);               // the server closes the socket once clean
};

async function showPersona(id: string): Promise<void> {
  const c = cast.find(x => x.id === id);
  if (!c) return;
  persona = id;
  const a = await avatarReady;
  if (await a.show(c)) await loadSamples(id, a);
}

onFirstTouch(() => {
  unlocked = true;
  status.startTimer();
  void avatarReady.then(a => a.unlock());
  link.send({ type: "control", action: "ready" });
});

// Direct links: #settings or #settings/sound, and #mood=happy to preview the kaomoji.
if (location.hash.startsWith("#settings")) settings.openSettings(true, location.hash.split("/")[1]);
if (location.hash.startsWith("#mood=")) { $("start").hidden = true; setMoodBg(location.hash.slice(6)); }

(async function boot() {
  document.title = APP_NAME;
  $("brand-name").textContent = APP_NAME;
  $("topic").title = `Ask ${APP_NAME} to drop this subject and find a new one`;
  $("goal").title = "What your tutor wants you to use next";
  status.bindStatus();
  talk.bind();
  talk.render();
  link.start();
  try {
    cast = await (await fetch("cast.json")).json();
  } catch {
    log("no cast.json — run <code>make_preview</code>", "err");
    return;
  }
  settings.refresh();                               // the tutor cards are drawn from the cast
  const first = String(settings.values().TUTOR_PERSONA || cast[0]?.id || "");
  if (first) await showPersona(first);
})();
