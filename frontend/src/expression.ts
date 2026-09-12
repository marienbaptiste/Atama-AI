/** When her face changes, and when it relaxes (ADR-020, spec §8). Pure timing: no DOM, no
 *  TalkingHead — avatar.ts supplies the face, this decides the moments.
 *
 *  An emotion holds until the next tag or the end of the turn (ADR-020: "it applies until the next
 *  tag or end of turn"). It used to relax on a fixed 4 s timer, which put her back to neutral in
 *  the middle of a long sentence, and each sentence's release timer was never kept, so a stale
 *  one wiped the next sentence's expression (found 2026-09-12). Now: a new sentence's rig cancels
 *  every pending timer; only the end of the turn schedules the settle. */
import type { Rig } from "./rig";

/** What the clock drives. */
export interface Face {
  /** Pin this rig: its mood and its blendshapes. */
  hold(rig: Rig): void;
  /** Let the blendshapes go. The mood stays — a face with no pinned brows is still a happy face. */
  release(): void;
  /** Back to neutral: mood, shapes and the backdrop. */
  settle(): void;
  /** A resting posture, some time into the silence after her turn. */
  rest(): void;
}

export interface Timers {
  set(fn: () => void, ms: number): number;
  clear(id: number): void;
}

//: After her turn ends the last face lingers this long before relaxing — long enough to read as
//: a face, short enough that she is not still scowling when the student starts to answer.
export const SETTLE_AFTER_MS = 1500;
export const REST_AFTER_MS = 9000;

const BROWSER: Timers = { set: (fn, ms) => window.setTimeout(fn, ms), clear: id => window.clearTimeout(id) };

export class ExpressionClock {
  private releaseTimer = 0;
  private settleTimer = 0;
  private restTimer = 0;

  constructor(private readonly face: Face, private readonly timers: Timers = BROWSER) {}

  /** A sentence's audio just started with this rig (speech.ts). */
  apply(rig: Rig): void {
    this.cancel();
    this.face.release();                            // the last sentence's shapes go first
    this.face.hold(rig);
    // The blendshapes are brief — a brow-raise held for a whole sentence is a mask (spec §8:
    // surprised "drops back after ~1 s"). The mood outlives them, until the turn ends.
    this.releaseTimer = this.timers.set(() => this.face.release(), rig.release);
  }

  /** Her turn is over (state → listening): relax soon, and rest a while after that. */
  turnEnded(): void {
    this.timers.clear(this.settleTimer);
    this.timers.clear(this.restTimer);
    this.settleTimer = this.timers.set(() => this.face.settle(), SETTLE_AFTER_MS);
    this.restTimer = this.timers.set(() => this.face.rest(), REST_AFTER_MS);
  }

  /** Neutral now, nothing pending — a reset, or the "considering" look about to be applied. */
  settleNow(): void {
    this.cancel();
    this.face.settle();
  }

  /** Interrupted: the shapes go now; the turn's end, which follows, schedules the settle. */
  stop(): void {
    this.cancel();
    this.face.release();
  }

  private cancel(): void {
    this.timers.clear(this.releaseTimer);
    this.timers.clear(this.settleTimer);
    this.timers.clear(this.restTimer);
  }
}
