/** The status bar (spec §5b, user request 2026-09-10): one white glass pill with the session
 *  timer, her context, the five-hour window, the microphone, every service, and GPU memory. Each
 *  gauge fills in when its source reports. The colour logic is pure and tested (ROADMAP 16). */
import type { MetersMsg, ServiceStatusMsg } from "./protocol.gen";
import { $, esc } from "./ui";

//: Spec §5b: every external dependency, working or not — not merely configured. One dot each.
export const SVC_NAMES: Record<string, string> = {
  wanikani: "WaniKani", bunpro: "Bunpro", bunpro_mcp: "Bunpro tools", brain: "Brain",
  search: "Search", voicevox: "Voice", stt: "Listening",
};
export const GREEN = "#22a355", BLUE = "#3b82f6", AMBER = "#f2a33a", RED = "#e8453c", OFF = "#c9ced6";
export const SVC_COLOR: Record<string, string> = {
  ok: GREEN, warm: GREEN, ready: GREEN, connected: GREEN, used: GREEN,
  syncing: BLUE, starting: BLUE, loading: BLUE, thinking: BLUE, restarting: BLUE,
  stale: AMBER, fallback: AMBER, rate_limited: AMBER,
  disabled: OFF, error: RED, failed: RED, down: RED,
};
export const chipColor = (state: string): string => SVC_COLOR[state] ?? OFF;
export const isService = (name: string): boolean => name in SVC_NAMES;

export type Services = Record<string, ServiceStatusMsg>;

/** Which services have reported, in a fixed order, and which of them need attention. */
export function summarize(services: Services): { known: string[]; bad: string[] } {
  const known = Object.keys(SVC_NAMES).filter(k => services[k]);
  return { known, bad: known.filter(k => [RED, AMBER].includes(chipColor(services[k].state))) };
}

/** A level (RMS, peak-held) as a bar width: quiet mics are the norm, so it is scaled up. */
export const levelPercent = (level: number): number => Math.round(Math.min(1, Math.sqrt(level * 30)) * 100);

export function elapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const two = (n: number) => String(n).padStart(2, "0");
  return h ? `${h}:${two(m)}:${two(sec)}` : `${m}:${two(sec)}`;
}

// ------------------------------------------------------------------ services
const services: Services = {};

export function showService(msg: ServiceStatusMsg): void {
  services[msg.service] = msg;
  const { known, bad } = summarize(services);
  $("m-svc").querySelector(".dots")!.innerHTML = known.map(k =>
    `<i style="background:${chipColor(services[k].state)}"></i>`).join("");
  $("m-svc").title = bad.length
    ? bad.map(k => `${SVC_NAMES[k]}: ${services[k].state}`).join(" · ") + " — click for details"
    : "All services working — click for details";
  $("svc-pop").innerHTML = known.map(k => {
    const s = services[k];
    return `<div class="row"><i style="background:${chipColor(s.state)}"></i><b>${esc(SVC_NAMES[k])}</b>`
      + `<span>${esc(s.state)}</span><span title="${esc(s.detail)}">${esc(s.detail)}</span>`
      + (s.last_error ? `<div class="err">${esc(s.last_error)}</div>` : "") + "</div>";
  }).join("") || '<div class="row"><span>No service has reported yet.</span></div>';
}

export function bindStatus(): void {
  const toggle = () => { $("svc-pop").hidden = !$("svc-pop").hidden; $("m-svc").blur(); };
  $("m-svc").onclick = toggle;
  $("m-svc").onkeydown = e => { if (e.key === "Enter") toggle(); };
}

// ------------------------------------------------------------------ gauges
const LIMIT_DOT: Record<string, string> = { allowed: GREEN, allowed_warning: AMBER, rejected: RED };
const kTok = (n: number) => (n >= 1000 ? Math.round(n / 1000) + "k" : String(n));

function gauge(id: string, fraction: number | null, text: string | null, title?: string): void {
  const chip = $(id);
  const bar = chip.querySelector<HTMLElement>(".bar");
  if (bar && fraction != null) {
    const f = Math.max(0, Math.min(1, fraction));
    bar.querySelector<HTMLElement>("u")!.style.width = Math.round(f * 100) + "%";
    bar.classList.toggle("warn", f >= 0.7 && f < 0.9);
    bar.classList.toggle("bad", f >= 0.9);
  }
  const b = chip.querySelector("b");
  if (b && text != null) b.textContent = text;
  if (title) chip.title = title;
}

export function onMeters(m: MetersMsg, vramCapGb: number): void {
  if (m.context_tokens != null && m.context_window) {
    const f = m.context_tokens / m.context_window;
    gauge("m-ctx", f, Math.round(f * 100) + "%",
      `Her memory of this conversation: ${kTok(m.context_tokens)} of ${kTok(m.context_window)} tokens. `
      + "Before it is full she moves to a fresh session with the lesson so far.");
  }
  if (m.month_turns != null || m.limit_status) {
    // No dollars: the subscription runs through `claude -p` and is not billed per turn (user,
    // 2026-09-10). The CLI reports whether the five-hour window allows requests and when it
    // resets — not how much is left — so the dot is the window and the text says so.
    const reset = m.limit_resets_at
      ? new Date(m.limit_resets_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : null;
    const turns = m.month_turns ?? 0;
    const state = ({ allowed: "OK", allowed_warning: "near limit", rejected: "limited" } as Record<string, string>)[
      m.limit_status ?? ""] || (m.limit_status || "—");
    gauge("m-use", null, `5h ${state}` + (reset ? ` · ${reset}` : ""),
      `Five-hour usage window: ${m.limit_status || "not reported yet"}`
      + (reset ? `, resets at ${reset}` : "") + ". The subscription says only whether you are inside "
      + `the window, not how much of it is left. This month: ${turns} ${turns === 1 ? "turn" : "turns"} with her.`);
    $("m-use").querySelector<HTMLElement>(".dot")!.style.background = LIMIT_DOT[m.limit_status ?? ""] || OFF;
  }
  if (m.vram_used_mib != null && m.vram_total_mib) {
    gauge("m-gpu", m.vram_used_mib / (vramCapGb * 1024), (m.vram_used_mib / 1024).toFixed(1) + " GB",
      `GPU memory: ${(m.vram_used_mib / 1024).toFixed(1)} of ${(m.vram_total_mib / 1024).toFixed(0)} GB `
      + `(cap ${vramCapGb} GB, spec §10b). Sampled every 30 s.`);
  }
}

// ------------------------------------------------------------------ microphone
//: Unplugged, missing, back, or on the default instead of the chosen one (spec §9).
const MIC_DOT: Record<string, string> = { ok: GREEN, fallback: AMBER, missing: RED, lost: RED };

export function showMic(state: string, detail: string): void {
  $("m-mic").querySelector<HTMLElement>(".dot")!.style.background = MIC_DOT[state] || OFF;
  $("m-mic").title = "Microphone: " + (detail || state);
}

export function micLevel(level: number): void {
  $("m-mic").querySelector<HTMLElement>(".bar u")!.style.width = levelPercent(level) + "%";
}

// ------------------------------------------------------------------ session timer (spec §8)
let startedAt = 0;

export function startTimer(): void {
  if (startedAt) return;
  startedAt = performance.now();
  const tick = () => { $("m-time").querySelector("b")!.textContent = elapsed(performance.now() - startedAt); };
  tick();
  window.setInterval(tick, 1000);
}
