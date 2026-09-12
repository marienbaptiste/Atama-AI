/** Her face and body (spec §8): TalkingHead driven only through speakAudio (ADR-007) with our own
 *  visemes; the emotion → rig table (rig.ts) applied when each sentence's audio STARTS (speech.ts)
 *  and held until the next tag or the end of the turn (expression.ts, ADR-020); idle life; the
 *  "considering" look while she thinks; listening reactions while you talk.
 *
 *  Without TalkingHead — the CDN unreachable, the module late, the GLB missing — she is a voice
 *  (audio_only.ts): every sentence still plays and still lands in the chat, the rig is simply not
 *  there to move, and the stage says so. */
import { AudioOnly } from "./audio_only";
import { ExpressionClock, type Face } from "./expression";
import type { SpeakMsg } from "./protocol.gen";
import { FRAMED_POSES, REST_POSES, type Rig, draw, nextHand, rigFor } from "./rig";
import { SpeechPlayer, type SpeakingHead } from "./speech";
import { TALKINGHEAD_LOAD_TIMEOUT_MS, TALKINGHEAD_URL } from "./talkinghead_pins";
import { esc, log, setKaoPaused, setMoodBg } from "./ui";

/** One persona, from cast.json (written by backend.tools.make_preview from the persona files). */
export interface CastEntry { id: string; glb: string; own_face: boolean; body: string; voice: number; configured?: boolean }

/** What we call on TalkingHead (talkinghead.mjs 1.4; every call pinned in talkinghead_pins.ts). */
export interface Head extends SpeakingHead {
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

/** Whatever plays her sentences: TalkingHead, or the voice-only fallback. */
type Voice = SpeakingHead & { isSpeaking?: boolean; audioCtx: { resume(): Promise<void>; decodeAudioData(data: ArrayBuffer): Promise<AudioBuffer> } };

export interface Framing { zoom: number; high: number; offx: number }
//: Framing is a judgement by eye, and it cost a round trip every time. Remembered per browser.
//: A new key for the new page (2026-09-11): the prototype's saved framing put her head under the
//: status bar, and a saved value would have hidden the new default.
const FRAMING_KEY = "atama.framing.v2";
//: offx 0: the old control panel no longer covers the left of the stage, so she is centred.
//: high 0.38 -> 0.12: lower in frame, so her head clears the status bar (user, 2026-09-11). At 0.3
//: her head still touched it in a real-time screenshot (60 px from the top at 1264x705).
export const DEFAULT_FRAMING: Framing = { zoom: -0.4, high: 0.12, offx: 0 };

function loadFraming(): Framing {
  try {
    const saved = JSON.parse(localStorage.getItem(FRAMING_KEY) || "null");
    if (saved) return { zoom: Number(saved.zoom ?? DEFAULT_FRAMING.zoom), high: Number(saved.high ?? DEFAULT_FRAMING.high),
                        offx: Number(saved.offx ?? DEFAULT_FRAMING.offx) };
  } catch { /* private window: not worth failing over */ }
  return { ...DEFAULT_FRAMING };
}

//: Held while the student talks: brows lifted a touch reads as "I'm listening".
const ATTENTIVE: Record<string, number> = { browInnerUp: 0.18, eyeWideLeft: 0.08, eyeWideRight: 0.08 };
//: A closed-mouth "mm" with some nods. Silent on purpose: a sound would reach the microphone.
const MM: Record<string, number> = { mouthPressLeft: 0.35, mouthPressRight: 0.35, mouthRollLower: 0.2 };

function withTimeout<T>(p: Promise<T>, ms: number, what: string): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => reject(new Error(`${what} did not load in ${ms / 1000} s`)), ms);
    p.then(v => { clearTimeout(timer); resolve(v); }, e => { clearTimeout(timer); reject(e); });
  });
}

export class Avatar {
  readonly player: SpeechPlayer;
  /** Resolves once a model is on stage — or once it is known that none will be. TalkingHead
   *  cannot speak before showAvatar has set `this.avatar`; a sentence that arrived while the
   *  model was still loading threw and was lost (found 2026-09-12). And a sentence that waited
   *  for a model that never came was lost with the whole conversation (same day): so this also
   *  resolves when the page gives up on the face and carries on as a voice. */
  readonly loaded: Promise<void>;
  /** The face, or null when she is a voice only. */
  head: Head | null;
  private markLoaded!: () => void;
  private voice: Voice;
  private readonly clock: ExpressionClock;
  framing: Framing = loadFraming();
  /** Gesture hold in seconds, and whether the left hand gets a turn (rig panel). */
  holdS = 3;
  varyHands = true;
  frozen = false;
  lastMotion = { pose: "", gesture: "", react: "" };
  private held: string[] = [];
  private attentiveHeld: string[] = [];
  private lastRight = true;
  private shownGlb = "";

  private constructor(head: Head | null, private readonly stage: HTMLElement,
                      onSentence: (msg: SpeakMsg, preview: boolean) => void, why = "") {
    this.head = head;
    this.voice = head ?? new AudioOnly(new AudioContext());
    this.loaded = new Promise(resolve => { this.markLoaded = resolve; });
    const face: Face = {
      hold: rig => this.hold(rig),
      release: () => this.clearShapes(),
      settle: () => this.settle(),
      rest: () => { const pose = draw("rest", REST_POSES); if (pose) this.setPose(pose, 2500); },
    };
    this.clock = new ExpressionClock(face);
    // The player talks to whichever voice is current: the face can still be swapped for the
    // fallback after construction (a GLB that fails to load).
    const relay: SpeakingHead = {
      speakAudio: (r, opt, on) => this.voice.speakAudio(r, opt, on),
      stopSpeaking: () => this.voice.stopSpeaking(),
    };
    this.player = new SpeechPlayer(relay, b64 => this.decode(b64), (msg, preview, late) => {
      if (!late) this.emote(msg.emotion || "neutral");   // a face for audio that was never heard is wrong
      onSentence(msg, preview);
    });
    if (head === null) this.faceless(why);
    // Keep finding the camera again between turns, or she drifts off during long silences.
    window.setInterval(() => { if (Math.random() < 0.7) this.meetEye(900); }, 4000);
  }

  get available(): boolean { return this.head !== null; }

  static async create(stage: HTMLElement, onSentence: (msg: SpeakMsg, preview: boolean) => void): Promise<Avatar> {
    let head: Head | null = null;
    let why = "";
    try {
      const mod = await withTimeout(import(/* @vite-ignore */ TALKINGHEAD_URL), TALKINGHEAD_LOAD_TIMEOUT_MS, "TalkingHead");
      head = new mod.TalkingHead(stage, {
        // The constructor THROWS on a falsy ttsEndpoint (talkinghead.mjs:813) though we never call
        // its TTS — VOICEVOX makes every sound. A placeholder satisfies it; nothing requests it.
        ttsEndpoint: "unused://voicevox-makes-the-audio",
        lipsyncModules: [],
        cameraView: "upper",
        // No dragging the camera around her (user, 2026-09-11): a stray drag left her off-centre,
        // and nothing in a lesson needs it. Zoom and pan are already off by default
        // (talkinghead.mjs 1.4 lines 146-148, applied to OrbitControls at 849-851).
        cameraRotateEnable: false,
        avatarMood: "neutral",
        modelPixelRatio: 1,          // it multiplies by devicePixelRatio itself; passing it squares it
        // TalkingHead defaults to 0.2 idle / 0.5 speaking, which reads as evasive on a teacher. 0.9
        // holds the student's gaze; the residual 10% keeps it from becoming a stare (spec §8).
        avatarIdleEyeContact: 0.9,
        avatarSpeakingEyeContact: 0.9,
      }) as Head;
    } catch (err) {
      why = err instanceof Error ? err.message : String(err);
    }
    return new Avatar(head, stage, onSentence, why);
  }

  async show(c: CastEntry): Promise<boolean> {
    if (this.head === null) return false;
    if (this.shownGlb === c.glb) return true;
    try {
      await this.head.showAvatar({ url: c.glb, body: c.body, avatarMood: "neutral", lipsyncLang: "en" });
    } catch {
      log(`${esc(c.glb)} failed to load — make one at readyplayer.me with ARKit and Oculus visemes, `
        + "then run <code>check_avatar</code>", "err");
      // No model yet: she cannot speak through TalkingHead at all, so carry on as a voice
      // rather than hold every sentence for a face that is not coming.
      if (!this.shownGlb) this.faceless(`${c.glb} failed to load`);
      return false;
    }
    this.shownGlb = c.glb;
    this.markLoaded();
    this.reframe();
    this.meetEye(600);
    return true;
  }

  /** Give up on the face for this session: voice only, and say so on the stage. */
  private faceless(why: string): void {
    const head = this.head;
    this.head = null;
    if (head) { try { head.stopSpeaking(); head.stop(); } catch { /* it never ran */ } }
    this.voice = new AudioOnly(new AudioContext());
    const note = document.createElement("div");
    note.className = "noavatar";
    note.innerHTML = "<b>Her face could not load</b><br>" + esc(why || "TalkingHead unavailable")
      + "<br>You still hear her, and the conversation still shows. Check the network and reload for the avatar.";
    this.stage.appendChild(note);
    log(`the avatar could not load (${esc(why)}) — carrying on with her voice only`, "err");
    this.markLoaded();
  }

  /** The page was touched: the browser now lets the audio context play. */
  async unlock(): Promise<void> {
    try { await this.voice.audioCtx.resume(); } catch { /* resumed on the first sentence instead */ }
  }

  isTalking(): boolean { return !!this.voice.isSpeaking; }

  /** Stop now and drop what is left of `turn` (barge-in). */
  stop(turn?: number): void {
    this.player.stop(turn);
    this.clock.stop();
  }

  end(): void {
    try { this.voice.stopSpeaking(); } catch { /* already quiet */ }
    try { this.head?.stop(); } catch { /* nothing left to animate */ }
  }

  // ---------------------------------------------------------------- emotion → rig (spec §8)
  /** A sentence's tag, as its audio starts. The rig holds until the next tag or the end of the
   *  turn (ADR-020); the clock owns every timer, so nothing stale can wipe this face. */
  emote(tag: string): void {
    const name = tag || "neutral";
    const r = rigFor(name);
    this.clock.apply(r);
    setMoodBg(name);
    if (this.head === null) return;

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
  }

  private hold(r: Rig): void {
    this.head?.setMood(r.mood);
    this.held = Object.keys(r.shapes);
    this.held.forEach(s => this.head?.setFixedValue(s, r.shapes[s]));
  }

  private settle(): void {
    this.clearShapes();
    this.head?.setMood("neutral");
    setMoodBg("neutral");
  }

  /** Spec §8: a "considering" look while she thinks — gaze up and away. lookAt(x, y, ms) takes
   *  viewport pixels; her first sentence looks back at the student (emote → meetEye). */
  thinking(): void {
    this.clock.settleNow();         // the last turn's face is over; a new one is being made
    setMoodBg("thinking");
    try { this.head?.lookAt(innerWidth * (Math.random() < 0.5 ? 0.3 : 0.7), innerHeight * 0.12, 900); } catch { /* not loaded */ }
  }

  /** Her turn is over: the last face lingers a moment, then relaxes (expression.ts). */
  listening(): void {
    this.clock.turnEnded();
  }

  // ---------------------------------------------------------------- listening reactions (あいづち)
  attentive(): void {
    this.meetEye(400);
    this.attentiveHeld = Object.keys(ATTENTIVE);
    this.attentiveHeld.forEach(s => this.head?.setFixedValue(s, ATTENTIVE[s]));
  }

  nod(): void {
    if (this.head === null) return;
    this.head.playGesture("yes", 1, false, 250);
    if (Math.random() < 0.4) {
      const shapes = Object.keys(MM);
      shapes.forEach(s => this.head?.setFixedValue(s, MM[s]));
      window.setTimeout(() => shapes.forEach(s => this.head?.setFixedValue(s, null)), 380);
    }
  }

  rest(): void {
    this.attentiveHeld.forEach(s => this.head?.setFixedValue(s, null));
    this.attentiveHeld = [];
  }

  // ---------------------------------------------------------------- body and camera
  /** Actively aim the gaze at the camera. avatarIdleEyeContact only COMPENSATES head rotation;
   *  lookAtCamera is the call that points her at you (verified 2026-09-10). */
  meetEye(ms = 600): void {
    try { this.head?.lookAtCamera(ms); } catch { /* not loaded yet */ }
  }

  /** setView computes a STATIC frame, so a pose that shifts weight walks her out of shot; this
   *  re-asserts it. A negative cameraDistance zooms in; a positive cameraY LIFTS her in frame
   *  (it lowers the look-at point) — the opposite of what the name suggests (2026-09-10). */
  reframe(): void {
    const f = this.framing;
    try { this.head?.setView("upper", { cameraDistance: f.zoom, cameraY: f.high, cameraX: f.offx }); } catch { /* not loaded */ }
  }

  setFraming(f: Partial<Framing>): void {
    this.framing = { ...this.framing, ...f };
    this.reframe();
    try { localStorage.setItem(FRAMING_KEY, JSON.stringify(this.framing)); } catch { /* private window */ }
  }

  /** setPoseFromTemplate takes the TEMPLATE OBJECT, not its name — passing the string throws
   *  inside poseFactory, silently under a try/catch (2026-09-10). */
  setPose(name: string, ms = 1200): boolean {
    if (this.head === null) return false;
    const template = this.head.poseTemplates?.[name];
    if (!template) { log(`unknown pose: ${esc(name)}`, "err"); return false; }
    this.head.setPoseFromTemplate(template, ms);
    window.setTimeout(() => this.reframe(), ms * 0.6);   // recentre as the pose settles
    return true;
  }

  freeze(on: boolean): void {
    this.frozen = on;
    setKaoPaused(on);
    if (on) this.head?.stop(); else this.head?.start();
  }

  reset(): void {
    this.clock.settleNow();
    this.setPose("straight", 800);
    this.meetEye(400);
    this.reframe();
  }

  private clearShapes(): void {
    this.held.forEach(s => this.head?.setFixedValue(s, null));
    this.held = [];
  }

  private async decode(b64: string): Promise<AudioBuffer> {
    const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
    return this.voice.audioCtx.decodeAudioData(bytes.buffer);
  }
}
