/** The student's microphone, captured by the page (spec §8/§9, ADR-040).
 *
 *  This is the state machine; the browser is behind `CaptureDeps.open` (capture_web.ts), so it
 *  is tested with a fake. The states are spec §9's, the same words the terminal's sounddevice
 *  path reports, because the student sees them in the same chip:
 *
 *    ok        the chosen microphone (or the default) is live
 *    fallback  the chosen one is not connected; the default is live; tried again on devicechange
 *    missing   no microphone at all; tried again every RETRY_MS and on devicechange
 *    lost      the track ended mid-lesson (unplugged); reopened
 *    denied    the browser refused permission; asked again on the next talk press (a gesture)
 *    off       stopped, or not started yet
 *
 *  Capture runs for the whole lesson, not only while the key is held: the server's VAD wants the
 *  room between turns for the level meter, the listening reactions and hands-free mode, exactly
 *  as the sounddevice thread gave it. The socket is not this module's problem: `send` returns
 *  false while the link is down and the frame is simply dropped — live audio has no use for a
 *  queue — and the page re-reports its state when the link comes back (main.ts). */
export type MicState = "ok" | "fallback" | "missing" | "lost" | "denied" | "off";

export interface MicSource {
  label: string;
  onFrame(cb: (frame: ArrayBuffer) => void): void;
  onEnded(cb: () => void): void;
  stop(): void;
  /** Call `cb` once every frame captured so far has been handed to `onFrame` (a marker round trip
   *  through the audio thread). Optional: a source without it is flushed already. */
  flush?(cb: () => void): void;
}

export interface CaptureDeps {
  /** Open the microphone; `deviceId` "" for the default. Rejects with the browser's own error
   *  (`name`: NotAllowedError, NotFoundError, OverconstrainedError, …). */
  open(deviceId: string): Promise<MicSource>;
  send(frame: ArrayBuffer): boolean;
  report(state: MicState, detail: string): void;
  setTimeout(fn: () => void, ms: number): number;
  clearTimeout(id: number): void;
  /** The talk key is held: never swap devices under a sentence (spec §9). */
  busy?(): boolean;
}

export const RETRY_MS = 2000;
export const FLUSH_MS = 600;
const DENIED = new Set(["NotAllowedError", "SecurityError", "PermissionDeniedError", "NotSupportedError"]);
const errorName = (e: unknown) => (e as { name?: string } | null)?.name || "";

export class Capture {
  /** The chosen device id; "" is the default. Remembered per browser by main.ts. */
  chosen = "";
  state: MicState = "off";
  detail = "";
  private live: MicSource | null = null;
  /** Bumped by every open and stop: a source or timer from an older generation is discarded. */
  private gen = 0;
  private timer = 0;
  private wanted = false;

  constructor(private readonly d: CaptureDeps) {}

  start(): Promise<void> {
    this.wanted = true;
    return this.open();
  }

  stop(): void {
    this.wanted = false;
    this.close();
    this.set("off", "microphone off");
  }

  setDevice(id: string): Promise<void> {
    this.chosen = id;
    return this.wanted ? this.reopenWhenIdle() : Promise.resolve();
  }

  /** The device list changed: a missing microphone may be back, the chosen one may have returned. */
  devicesChanged(): Promise<void> {
    if (this.wanted && (this.state === "missing" || this.state === "fallback" || this.state === "lost")) {
      return this.reopenWhenIdle();
    }
    return Promise.resolve();
  }

  /** Everything captured up to now has reached `send` — then `cb`. On a phone the main thread can
   *  lag the audio thread by hundreds of milliseconds (the avatar renders there), so a talk key
   *  released "now" must not be reported before the audio of the last syllable has gone out
   *  (2026-09-16: utterances from the phone lost their tails). Bounded: a source that never
   *  answers, or none at all, releases `cb` after FLUSH_MS. */
  flush(cb: () => void): void {
    const live = this.live;
    if (!live?.flush) { cb(); return; }
    let done = false;
    const once = () => { if (!done) { done = true; this.d.clearTimeout(timer); cb(); } };
    const timer = this.d.setTimeout(once, FLUSH_MS);
    live.flush(once);
  }

  /** A talk press is a gesture: a refused permission can be asked for again. */
  retryIfDenied(): Promise<void> {
    return this.wanted && this.state === "denied" ? this.open() : Promise.resolve();
  }

  private reopenWhenIdle(): Promise<void> {
    if (this.d.busy?.()) {
      this.later(() => void this.reopenWhenIdle());
      return Promise.resolve();
    }
    return this.open();
  }

  private set(state: MicState, detail: string): void {
    this.state = state;
    this.detail = detail;
    this.d.report(state, detail);
  }

  private close(): void {
    this.d.clearTimeout(this.timer);
    this.gen++;
    this.live?.stop();
    this.live = null;
  }

  private later(fn: () => void): void {
    this.d.clearTimeout(this.timer);
    this.timer = this.d.setTimeout(fn, RETRY_MS);
  }

  private async open(): Promise<void> {
    this.close();
    const gen = this.gen;
    let src: MicSource | null = null;
    let fell = false;
    try {
      src = await this.d.open(this.chosen);
    } catch (e) {
      if (DENIED.has(errorName(e))) return this.denied(gen, e);
      if (this.chosen) {
        try {
          src = await this.d.open("");
          fell = true;
        } catch (e2) {
          if (DENIED.has(errorName(e2))) return this.denied(gen, e2);
        }
      }
    }
    if (gen !== this.gen) {                 // superseded by a newer open, or stopped meanwhile
      src?.stop();
      return;
    }
    if (!src) {
      this.set("missing", "no microphone found - plug one in and it will be picked up");
      this.later(() => { if (gen === this.gen && this.wanted) void this.open(); });
      return;
    }
    const source = src;
    this.live = source;
    source.onFrame(frame => { if (gen === this.gen) this.d.send(frame); });
    source.onEnded(() => {
      if (gen !== this.gen) return;
      this.live = null;
      this.set("lost", `microphone disconnected (${source.label}) - reconnecting`);
      this.later(() => { if (gen === this.gen && this.wanted) void this.open(); });
    });
    this.set(fell ? "fallback" : "ok",
             fell ? `the chosen microphone is not connected - using the default (${source.label})` : source.label);
  }

  private denied(gen: number, e?: unknown): void {
    if (gen !== this.gen) return;
    // A browser that cannot ask at all (no secure context) says why; a refusal says what to do.
    const why = errorName(e) === "NotSupportedError" ? String((e as Error).message) : "";
    this.set("denied", why || "the browser refused the microphone - allow it for this page, then hold to talk again");
  }
}
