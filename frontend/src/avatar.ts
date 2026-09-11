/** Her face and body (spec §8): TalkingHead driven only through speakAudio (ADR-007) with our own
 *  visemes; the emotion → rig table (rig.ts) applied when each sentence's audio STARTS (speech.ts);
 *  idle life; the "considering" look while she thinks; listening reactions while you talk. */
import type { SpeakMsg } from "./protocol.gen";
import { FRAMED_POSES, REST_POSES, draw, nextHand, rigFor } from "./rig";
import { SpeechPlayer, type SpeakingHead } from "./speech";
import { esc, log, setKaoPaused, setMoodBg } from "./ui";

/** TalkingHead 1.4, loaded by URL — the build the prototype ran and V0.4 read (constants.py
 *  TALKINGHEAD_*). It imports "three" by bare name, which index.html's import map resolves. */
const TALKINGHEAD_URL = "https://cdn.jsdelivr.net/gh/met4citizen/TalkingHead@1.4/modules/talkinghead.mjs";

/** One persona, from cast.json (written by backend.tools.make_preview from the persona files). */
export interface CastEntry { id: string; glb: string; own_face: boolean; body: string; voice: number; configured?: boolean }

/** What we call on TalkingHead (talkinghead.mjs 1.4, read 2026-09-10 and 2026-09-11). */
interface Head extends SpeakingHead {
  audioCtx: AudioContext;
  isSpeaking: boolean;
  poseTemplates?: Record<string, unknown>;
  showAvatar(avatar: { url: string; body: string; avatarMood: string; lipsyncLang: string }): Promise<void>;
  setMood(mood: string): void;
  setFixedValue(shape: string, value: number | null): void;
  playGesture(name: string, dur?: number, mirror?: boolean, ms?: number): void;
  stopGesture?(ms?: number): void;
  lookAt(x: number, y: number, ms: number): void;
  lookAtCamera(ms: number): void;
  setView(view: string, opt: Record<string, number>): void;
  setPoseFromTemplate(template: unknown, ms?: number): void;
  start(): void;
  stop(): void;
}

export interface Framing { zoom: number; high: number; offx: number }
//: Framing is a judgement by eye, and it cost a round trip every time. Remembered per browser.
const FRAMING_KEY = "atama.framing";
//: offx 0: the old control panel no longer covers the left of the stage, so she is centred.
export const DEFAULT_FRAMING: Framing = { zoom: -0.4, high: 0.38, offx: 0 };

function loadFraming(): Framing {
  try {
    const saved = JSON.parse(localStorage.getItem(FRAMING_KEY) || "null");
    if (saved) return { zoom: Number(saved.zoom ?? DEFAULT_FRAMING.zoom), high: Number(saved.high ?? DEFAULT_FRAMING.high),
                        offx: Number(saved.offx ?? DEFAULT_FRAMING.offx) };
  } catch { /* private window: not worth failing over */ }
  return { ...DEFAULT_FRAMING };
}

//: Expressions are events, not states: without a settle she keeps whatever face the last sentence
//: gave her — a `serious` turn left the brows down for the rest of the session (2026-09-10).
const SETTLE_AFTER_MS = 4000;
const REST_AFTER_MS = 9000;
//: Held while the student talks: brows lifted a touch reads as "I'm listening".
const ATTENTIVE: Record<string, number> = { browInnerUp: 0.18, eyeWideLeft: 0.08, eyeWideRight: 0.08 };
//: A closed-mouth "mm" with some nods. Silent on purpose: a sound would reach the microphone.
const MM: Record<string, number> = { mouthPressLeft: 0.35, mouthPressRight: 0.35, mouthRollLower: 0.2 };

export class Avatar {
  readonly player: SpeechPlayer;
  framing: Framing = loadFraming();
  /** Gesture hold in seconds, and whether the left hand gets a turn (rig panel). */
  holdS = 3;
  varyHands = true;
  frozen = false;
  lastMotion = { pose: "", gesture: "", react: "" };
  private held: string[] = [];
  private attentiveHeld: string[] = [];
  private settleTimer = 0;
  private restTimer = 0;
  private lastRight = true;
  private shownGlb = "";

  private constructor(readonly head: Head, onSentence: (msg: SpeakMsg) => void) {
    this.player = new SpeechPlayer(head, b64 => this.decode(b64), msg => {
      this.emote(msg.emotion || "neutral");
      onSentence(msg);
    });
    // Keep finding the camera again between turns, or she drifts off during long silences.
    window.setInterval(() => { if (Math.random() < 0.7) this.meetEye(900); }, 4000);
  }

  static async create(stage: HTMLElement, onSentence: (msg: SpeakMsg) => void): Promise<Avatar> {
    const mod = await import(/* @vite-ignore */ TALKINGHEAD_URL);
    const head = new mod.TalkingHead(stage, {
      // The constructor THROWS on a falsy ttsEndpoint (talkinghead.mjs:813) though we never call
      // its TTS — VOICEVOX makes every sound. A placeholder satisfies it; nothing requests it.
      ttsEndpoint: "unused://voicevox-makes-the-audio",
      lipsyncModules: [],
      cameraView: "upper",
      avatarMood: "neutral",
      modelPixelRatio: 1,          // it multiplies by devicePixelRatio itself; passing it squares it
      // TalkingHead defaults to 0.2 idle / 0.5 speaking, which reads as evasive on a teacher. 0.9
      // holds the student's gaze; the residual 10% keeps it from becoming a stare (spec §8).
      avatarIdleEyeContact: 0.9,
      avatarSpeakingEyeContact: 0.9,
    }) as Head;
    return new Avatar(head, onSentence);
  }

  async show(c: CastEntry): Promise<boolean> {
    if (this.shownGlb === c.glb) return true;
    try {
      await this.head.showAvatar({ url: c.glb, body: c.body, avatarMood: "neutral", lipsyncLang: "en" });
    } catch {
      log(`${esc(c.glb)} failed to load — make one at readyplayer.me with ARKit and Oculus visemes, `
        + "then run <code>check_avatar</code>", "err");
      return false;
    }
    this.shownGlb = c.glb;
    this.reframe();
    this.meetEye(600);
    return true;
  }

  /** The page was touched: the browser now lets TalkingHead's audio context play. */
  async unlock(): Promise<void> {
    try { await this.head.audioCtx.resume(); } catch { /* resumed on the first sentence instead */ }
  }

  isTalking(): boolean { return !!this.head.isSpeaking; }

  /** Stop now and drop what is left of `turn` (barge-in). */
  stop(turn?: number): void {
    this.player.stop(turn);
    this.clearShapes();
  }

  end(): void {
    try { this.head.stopSpeaking(); } catch { /* already quiet */ }
    try { this.head.stop(); } catch { /* nothing left to animate */ }
  }

  // ---------------------------------------------------------------- emotion → rig (spec §8)
  emote(tag: string): void {
    const name = tag || "neutral";
    const r = rigFor(name);
    this.clearShapes();
    this.head.setMood(r.mood);
    setMoodBg(name);
    this.held = Object.keys(r.shapes);
    this.held.forEach(s => this.head.setFixedValue(s, r.shapes[s]));
    // Every emotion releases. An expression with no timer is an expression she is stuck in.
    window.setTimeout(() => this.clearShapes(), r.release);

    this.lastMotion = { pose: "", gesture: "", react: "" };
    this.meetEye(500);              // look at the student first, then react
    this.reframe();
    if (r.react && Math.random() < (r.reactChance ?? 0)) {
      this.head.playGesture(r.react, 2, false, 400);
      this.lastMotion.react = r.react === "yes" ? "nod" : "shake";
    }
    if (Math.random() < r.poseChance) {
      const pose = draw(name + ":pose", r.poses.filter(p => FRAMED_POSES.includes(p)));
      if (pose && this.setPose(pose, 1200)) this.lastMotion.pose = pose;
    }
    if (Math.random() < r.gestureChance) {
      const gesture = draw(name + ":gesture", r.gestures);
      if (gesture) {
        // Jitter the hold and the hand too: identical timing is its own kind of robotic.
        const hold = Math.max(1, this.holdS + (Math.random() * 1.4 - 0.7));
        const right = this.varyHands ? (this.lastRight = nextHand(this.lastRight)) : false;
        this.head.playGesture(gesture, hold, right, 600 + Math.random() * 500);
        this.lastMotion.gesture = gesture + (right ? " (right)" : " (left)");
      }
    }
    this.settleSoon();
  }

  /** Spec §8: a "considering" look while she thinks — gaze up and away. lookAt(x, y, ms) takes
   *  viewport pixels; her first sentence looks back at the student (emote → meetEye). */
  thinking(): void {
    setMoodBg("thinking");
    try { this.head.lookAt(innerWidth * (Math.random() < 0.5 ? 0.3 : 0.7), innerHeight * 0.12, 900); } catch { /* not loaded */ }
  }

  listening(): void { setMoodBg("neutral"); }

  // ---------------------------------------------------------------- listening reactions (あいづち)
  attentive(): void {
    this.meetEye(400);
    this.attentiveHeld = Object.keys(ATTENTIVE);
    this.attentiveHeld.forEach(s => this.head.setFixedValue(s, ATTENTIVE[s]));
  }

  nod(): void {
    this.head.playGesture("yes", 1, false, 250);
    if (Math.random() < 0.4) {
      const shapes = Object.keys(MM);
      shapes.forEach(s => this.head.setFixedValue(s, MM[s]));
      window.setTimeout(() => shapes.forEach(s => this.head.setFixedValue(s, null)), 380);
    }
  }

  rest(): void {
    this.attentiveHeld.forEach(s => this.head.setFixedValue(s, null));
    this.attentiveHeld = [];
  }

  // ---------------------------------------------------------------- body and camera
  /** Actively aim the gaze at the camera. avatarIdleEyeContact only COMPENSATES head rotation;
   *  lookAtCamera is the call that points her at you (verified 2026-09-10). */
  meetEye(ms = 600): void {
    try { this.head.lookAtCamera(ms); } catch { /* not loaded yet */ }
  }

  /** setView computes a STATIC frame, so a pose that shifts weight walks her out of shot; this
   *  re-asserts it. A negative cameraDistance zooms in; a positive cameraY LIFTS her in frame
   *  (it lowers the look-at point) — the opposite of what the name suggests (2026-09-10). */
  reframe(): void {
    const f = this.framing;
    try { this.head.setView("upper", { cameraDistance: f.zoom, cameraY: f.high, cameraX: f.offx }); } catch { /* not loaded */ }
  }

  setFraming(f: Partial<Framing>): void {
    this.framing = { ...this.framing, ...f };
    this.reframe();
    try { localStorage.setItem(FRAMING_KEY, JSON.stringify(this.framing)); } catch { /* private window */ }
  }

  /** setPoseFromTemplate takes the TEMPLATE OBJECT, not its name — passing the string throws
   *  inside poseFactory, silently under a try/catch (2026-09-10). */
  setPose(name: string, ms = 1200): boolean {
    const template = this.head.poseTemplates?.[name];
    if (!template) { log(`unknown pose: ${esc(name)}`, "err"); return false; }
    this.head.setPoseFromTemplate(template, ms);
    window.setTimeout(() => this.reframe(), ms * 0.6);   // recentre as the pose settles
    return true;
  }

  freeze(on: boolean): void {
    this.frozen = on;
    setKaoPaused(on);
    if (on) this.head.stop(); else this.head.start();
  }

  reset(): void {
    this.clearShapes();
    this.head.setMood("neutral");
    setMoodBg("neutral");
    this.setPose("straight", 800);
    this.meetEye(400);
    this.reframe();
  }

  private clearShapes(): void {
    this.held.forEach(s => this.head.setFixedValue(s, null));
    this.held = [];
  }

  private settleSoon(): void {
    clearTimeout(this.settleTimer);
    clearTimeout(this.restTimer);
    this.settleTimer = window.setTimeout(() => {
      this.clearShapes();
      this.head.setMood("neutral");
      setMoodBg("neutral");
    }, SETTLE_AFTER_MS);
    this.restTimer = window.setTimeout(() => {
      const pose = draw("rest", REST_POSES);
      if (pose) this.setPose(pose, 2500);
    }, REST_AFTER_MS);
  }

  private async decode(b64: string): Promise<AudioBuffer> {
    const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
    return this.head.audioCtx.decodeAudioData(bytes.buffer);
  }
}
