/** Emotion → rig (spec §8), and her listening reactions. Pure: no DOM, no TalkingHead — tested
 *  headless (ROADMAP 9). avatar.ts applies what this decides.
 *
 *  Mood names are TalkingHead's CLOSED set (V0.4, constants.py TALKINGHEAD_MOODS): an unknown mood
 *  is a silent no-op, so thinking / surprised / serious ride on `neutral` plus ARKit blendshapes. */

export const MOODS = ["neutral", "happy", "angry", "sad", "fear", "disgust", "love", "sleep"] as const;
export type Mood = (typeof MOODS)[number];
// Read out of talkinghead.mjs on 2026-09-10, not the README: the README lists seven gestures and
// omits `namaste`; `yes` / `no` are its animated emoji (a nod and a head shake), also played
// through playGesture (talkinghead.mjs 1.4, playGesture falls through to animEmojis).
export const GESTURES = ["handup", "index", "ok", "thumbup", "thumbdown", "side", "shrug", "namaste"] as const;
export const POSES = ["side", "hip", "turn", "bend", "back", "straight", "wide", "oneknee", "kneel", "sitting"] as const;
//: Safe for an upper-body frame: bend/back walk her out of shot, kneel/sitting/oneknee drop her
//: below it. She is teaching, standing.
export const FRAMED_POSES: readonly string[] = ["side", "hip", "turn", "straight", "wide"];
export const REST_POSES: readonly string[] = ["straight", "side"];

export interface Rig {
  mood: Mood;
  /** ARKit blendshapes pinned while the expression lasts. */
  shapes: Record<string, number>;
  /** Every expression releases: one with no timer is one she is stuck in. */
  release: number;
  gestures: string[];
  gestureChance: number;
  poses: string[];
  poseChance: number;
  /** A nod or a head shake — the answer arriving before the sentence does. */
  react?: "yes" | "no";
  reactChance?: number;
}

// Gestures and poses are POOLS, not single values: one emotion mapped to one fixed gesture is what
// makes an avatar read as a machine. Each turn draws from the pool, never repeats the previous
// draw, and often does nothing — a person punctuating every sentence with their hands is as wrong
// as one who never moves. Posture changes far more rarely than gesture.
export const RIG: Record<string, Rig> = {
  neutral:     { mood: "neutral", shapes: {}, release: 1200,
                 gestures: ["side"], gestureChance: 0.10,
                 poses: ["side", "straight", "hip"], poseChance: 0.20 },
  happy:       { mood: "happy", shapes: {}, release: 2600, react: "yes", reactChance: 0.75,
                 gestures: ["ok", "thumbup", "handup", "namaste"], gestureChance: 0.65,
                 poses: ["straight", "side"], poseChance: 0.30 },
  // A pinned gaze beats avatarIdleEyeContact outright, so `thinking` must release too, or she
  // looks away for the rest of the session. Considering is momentary; evasive is not.
  thinking:    { mood: "neutral", release: 2000,
                 gestures: ["shrug", "side", "index"], gestureChance: 0.55,
                 poses: ["hip", "side", "turn"], poseChance: 0.35,
                 shapes: { eyesRotateX: -0.35, eyesRotateY: 0.45, browDownLeft: 0.25,
                           browDownRight: 0.25, mouthPucker: 0.15 } },
  surprised:   { mood: "neutral", release: 1200,
                 gestures: ["handup", "index", "shrug"], gestureChance: 0.85,
                 poses: ["straight", "side"], poseChance: 0.40,
                 shapes: { browInnerUp: 0.9, browOuterUpLeft: 0.7, browOuterUpRight: 0.7,
                           eyeWideLeft: 0.6, eyeWideRight: 0.6, jawOpen: 0.18 } },
  serious:     { mood: "neutral", release: 2600, react: "no", reactChance: 0.70,
                 gestures: ["index", "side", "handup"], gestureChance: 0.70,
                 poses: ["straight", "wide"], poseChance: 0.25,
                 shapes: { browDownLeft: 0.55, browDownRight: 0.55, mouthPressLeft: 0.3,
                           mouthPressRight: 0.3, eyeSquintLeft: 0.2, eyeSquintRight: 0.2 } },
  // "go on, try it": an open palm invites; a raised finger corrects — which is why encouraging and
  // serious must not share a gesture pool.
  encouraging: { mood: "happy", release: 2400,
                 gestures: ["handup", "ok", "side"], gestureChance: 0.75,
                 poses: ["straight", "side"], poseChance: 0.30,
                 shapes: { browInnerUp: 0.45, browOuterUpLeft: 0.3, browOuterUpRight: 0.3,
                           mouthSmileLeft: 0.25, mouthSmileRight: 0.25, eyeWideLeft: 0.2,
                           eyeWideRight: 0.2 } },
  // Praise with weight: held longest, because a compliment that vanishes in a second reads as reflex.
  proud:       { mood: "happy", release: 3000, react: "yes", reactChance: 0.85,
                 gestures: ["thumbup", "ok", "namaste"], gestureChance: 0.80,
                 poses: ["straight"], poseChance: 0.20,
                 shapes: { mouthSmileLeft: 0.5, mouthSmileRight: 0.5, cheekSquintLeft: 0.3,
                           cheekSquintRight: 0.3, browInnerUp: 0.2 } },
  // Puzzlement, NOT disapproval: no head shake. "I did not catch that", not "you were wrong".
  confused:    { mood: "neutral", release: 2200,
                 gestures: ["shrug", "side"], gestureChance: 0.70,
                 poses: ["hip", "side"], poseChance: 0.30,
                 shapes: { browInnerUp: 0.5, browDownRight: 0.3, eyeSquintLeft: 0.25,
                           mouthLeft: 0.35, mouthPucker: 0.2 } },
};

/** The rig for a tag; anything unknown (or none) is neutral, never an error mid-sentence. */
export function rigFor(tag: string | null | undefined): Rig {
  return (tag && RIG[tag]) || RIG.neutral;
}

type Rand = () => number;

/** One draw from a pool that never repeats the previous draw for the same key. */
export function draw(key: string, pool: readonly string[] | undefined, memo: Record<string, string> = LAST,
                     rand: Rand = Math.random): string | null {
  if (!pool || !pool.length) return null;
  const fresh = pool.length > 1 ? pool.filter(x => x !== memo[key]) : [...pool];
  const value = fresh[Math.floor(rand() * fresh.length)];
  memo[key] = value;
  return value;
}
const LAST: Record<string, string> = {};

/** Right hand 80% of the time, and never the left twice running: a plain 80/20 draw produces
 *  runs, and two left-handed gestures in a row on a right-handed person read as a glitch.
 *  TalkingHead's `mirror` is true for the RIGHT hand. */
export function nextHand(lastRight: boolean, rand: Rand = Math.random): boolean {
  return lastRight ? rand() < 0.8 : true;
}

// ------------------------------------------------------------------ listening reactions
export type Reaction = "attentive" | "nod" | "rest";

export interface BackchannelOptions {
  /** VAD speech probability that counts as talking (the server's `mic_level.speech`). */
  threshold: number;
  /** Speech this long before she turns attentive — a cough is not the student talking. */
  minSpeechMs: number;
  /** A pause this long after speech earns a nod (spec §8: ≥ 300 ms). */
  pauseMs: number;
  /** At most one nod per this long: a nod on every breath is a bobblehead. */
  gapMs: number;
}

/** あいづち (spec §8): while the student has the floor, turn attentive once they are really
 *  talking and nod at each pause, then rest when the floor goes back to her. Fed ~10 Hz from
 *  `mic_level`; returns what to do, and avatar.ts does it. */
export class Backchannel {
  private speechSince: number | null = null;
  private quietSince: number | null = null;
  private heardSinceNod = false;
  private attentive = false;
  private lastNod = -Infinity;

  constructor(readonly opt: BackchannelOptions = { threshold: 0.5, minSpeechMs: 250, pauseMs: 300, gapMs: 1500 }) {}

  /** One sample. `active`: the student has the floor (holding the key, or hands-free listening). */
  feed(now: number, speech: number, active: boolean): Reaction[] {
    const out: Reaction[] = [];
    if (!active) {
      if (this.attentive) out.push("rest");
      this.speechSince = this.quietSince = null;
      this.heardSinceNod = this.attentive = false;
      return out;
    }
    if (speech >= this.opt.threshold) {
      this.quietSince = null;
      this.speechSince ??= now;
      if (now - this.speechSince >= this.opt.minSpeechMs) {
        this.heardSinceNod = true;
        if (!this.attentive) {
          this.attentive = true;
          out.push("attentive");
        }
      }
    } else {
      this.speechSince = null;
      this.quietSince ??= now;
      if (this.heardSinceNod && now - this.quietSince >= this.opt.pauseMs && now - this.lastNod >= this.opt.gapMs) {
        this.heardSinceNod = false;
        this.lastNod = now;
        out.push("nod");
      }
    }
    return out;
  }
}
