/** The student's side of the turn (spec §8/§9): push-to-talk, and barge-in.
 *
 *  The microphone itself is captured by the orchestrator (backend/audio.py: device choice and
 *  unplug recovery live there, spec §9), so this module owns no audio — it owns the key. Holding
 *  SPACE (or the button) is `control: start`, releasing it `stop`: the release IS the end of the
 *  turn, no silence window involved.
 *
 *  Barge-in, push-to-talk: a press while she talks is unambiguous, so the page stops her HERE,
 *  before the server has even heard of it, and the server's `bargein` then confirms and closes the
 *  turn (M3b wants the voice gone in < 300 ms; this is one function call after the key event).
 *  Hands-free, the server's VAD decides and the page stops on its `bargein`.
 *
 *  ALT GR during a hold CANCELS the recording (the user, 2026-09-12; the right-hand ALT, because
 *  Chrome takes SPACE with the left one): the captured audio is
 *  dropped server-side, and because the hold is over as far as this module is concerned, releasing
 *  the talk key sends nothing. Press again to retry. */
import type { ServiceStatusMsg } from "./protocol.gen";
import { live, log } from "./ui";

//: Only controls where SPACE TYPES something keep it. A focused button is no reason to lose
//: push-to-talk: after clicking the cog, SPACE used to reopen the settings panel (2026-09-10).
export const TYPES_SPACE = new Set(["text", "password", "number", "search", "email", "url"]);

export function spaceIsOurs(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null;
  if (!el || !el.tagName) return true;
  return !(el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable
    || (el.tagName === "INPUT" && TYPES_SPACE.has((el as HTMLInputElement).type)));
}

/** The server answers every press (service_status "ptt"). Once it has shown it does, a press left
 *  unanswered this long means the link is dead: reconnect rather than let presses vanish. */
export const ACK_MS = 2500;
const TROUBLE = ["silent", "empty", "short", "busy", "off"];

export interface TalkDeps {
  send(action: "start" | "stop" | "cancel"): boolean;
  connected(): boolean;
  /** Talking is off while the settings panel is up. */
  blocked(): boolean;
  /** TURN_MODE: "ptt" or "vad". */
  mode(): string;
  /** Stop her now if she is talking or about to; true if there was anything to stop. */
  interrupt(): boolean;
  deadLink(): void;
}

export class Talk {
  talking = false;
  /** Key-to-silence for every local barge-in this session, in ms (gate M3b). */
  readonly stops: number[] = [];
  private ackTimer = 0;
  private serverAcks = false;

  constructor(private readonly d: TalkDeps, private readonly button: HTMLButtonElement) {}

  bind(): void {
    // keydown's `repeat` matters: a held key fires continuously, and every repeat would restart
    // the turn. A browser sees keyup, which a terminal cannot, so this is a real hold.
    addEventListener("keydown", e => {
      if (e.code !== "Space" || e.repeat || this.d.blocked() || !spaceIsOurs(e.target)) return;
      e.preventDefault();
      this.press(true, e.timeStamp);
    });
    // ALT GR while holding: drop it. Checked before the release handler ever runs, so the talk key
    // can be let go in peace. On Windows AltGr also reports a left CONTROL, which is ignored here.
    addEventListener("keydown", e => {
      if (e.code !== "AltRight" || e.repeat || !this.talking) return;
      e.preventDefault();
      this.cancel();
    });
    // Release ALWAYS ends the turn, even if focus moved mid-hold, or the mic stays open.
    addEventListener("keyup", e => {
      if (e.code === "Space" && this.talking) { e.preventDefault(); this.press(false); }
    });
    addEventListener("blur", () => this.press(false));
    this.button.addEventListener("pointerdown", e => { e.preventDefault(); this.press(true, e.timeStamp); });
    addEventListener("pointerup", () => this.press(false));
  }

  press(on: boolean, at: number = performance.now()): void {
    if (!this.d.connected()) {
      if (on) log("not connected to the tutor — SPACE does nothing until it reconnects", "err");
      this.talking = false;
      this.render();
      return;
    }
    if (on === this.talking) return;
    if (on && this.d.mode() === "ptt" && this.d.interrupt()) {
      const ms = performance.now() - at;
      this.stops.push(ms);
      const worst = Math.max(...this.stops);
      log(`interrupted her — voice stopped <b>${ms.toFixed(0)} ms</b> after the key `
        + `(this session: ${this.stops.length}, worst ${worst.toFixed(0)} ms; gate M3b: under 300)`, "ok");
    }
    this.talking = on;
    this.d.send(on ? "start" : "stop");
    clearTimeout(this.ackTimer);
    if (on && this.serverAcks) this.ackTimer = window.setTimeout(() => {
      log("that press never reached the tutor — reconnecting", "err");
      this.d.deadLink();
    }, ACK_MS);
    this.render();
  }

  /** Drop the recording in progress: nothing is sent, and the release is a no-op. */
  cancel(): void {
    if (!this.talking) return;
    this.talking = false;
    clearTimeout(this.ackTimer);
    this.d.send("cancel");
    live("dropped - press SPACE to start again");
    this.render();
  }

  /** What the last press actually did (service_status "ptt"). */
  ack(msg: ServiceStatusMsg): void {
    clearTimeout(this.ackTimer);
    this.serverAcks = true;
    live(msg.detail, TROUBLE.includes(msg.state) ? "warn" : "on");
    if (TROUBLE.includes(msg.state)) log(msg.detail, "err");
  }

  reset(): void {
    clearTimeout(this.ackTimer);
    this.talking = false;
    this.render();
  }

  render(): void {
    const vad = this.d.mode() === "vad";
    this.button.classList.toggle("hot", this.talking);
    this.button.disabled = !this.d.connected() || vad;
    this.button.querySelector("span")!.textContent = vad ? "Hands-free — just speak"
      : this.talking ? "Listening — release to send" : "Hold SPACE to talk";
  }
}
