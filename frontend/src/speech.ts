/** Her sentences, in order, each with its face applied when its audio STARTS (spec §8, ADR-020).
 *
 *  TalkingHead queues what speakAudio is given, so a sentence usually arrives while the previous
 *  one is still playing. Applying its emotion on arrival — what the prototype did — put the face a
 *  whole sentence ahead of the voice. The hook that fires at the start of a sentence's audio is the
 *  subtitle callback: speakAudio schedules a `subtitles` animation at the first word's time, and
 *  sets the callback when that sentence leaves the queue (talkinghead.mjs 1.4, startSpeaking and
 *  speakAudio, read 2026-09-11). We pass one word spanning the sentence at t=0, so it fires once,
 *  exactly as the audio begins.
 *
 *  Barge-in: every sentence carries its turn epoch (models.py Speak.turn). `stop(turn)` drops that
 *  epoch and all before it — including sentences still decoding, and ones that arrive later. */
import type { SpeakMsg } from "./protocol.gen";

export interface AudioLike { duration: number }

/** The part of TalkingHead this needs; a fake in the tests. */
export interface SpeakingHead {
  speakAudio(
    r: { audio: unknown; words: string[]; wtimes: number[]; wdurations: number[];
         visemes: string[]; vtimes: number[]; vdurations: number[] },
    opt?: Record<string, unknown> | null,
    onsubtitles?: ((text: string) => void) | null,
  ): void;
  stopSpeaking(): void;
}

interface Handed { msg: SpeakMsg; preview: boolean; started: boolean }

export class SpeechPlayer {
  /** The newest epoch stopped: audio of this epoch or older is dropped. */
  private stoppedUpTo = -1;
  /** The newest epoch handed to the head. */
  lastTurn = -1;
  private chain: Promise<unknown> = Promise.resolve();
  /** Sentences handed to the head whose start has not been announced yet, in order. */
  private handed: Handed[] = [];

  constructor(
    private readonly head: SpeakingHead,
    private readonly decode: (b64: string) => Promise<AudioLike>,
    /** `preview`: a rig-panel sample, not part of the conversation. `late`: announced after the
     *  fact because its own start never fired (its audio could not play) — the text still belongs
     *  in the conversation, but the face should not jump to an emotion that was never heard. */
    private readonly onStart: (msg: SpeakMsg, preview: boolean, late: boolean) => void,
  ) {}

  /** Her turn is over: announce anything handed over whose start never fired. Found 2026-09-11:
   *  TalkingHead drops a sentence it cannot play (a suspended audio context) before its callback
   *  runs, and the conversation lost the sentence with it. */
  flush(): void {
    for (const h of this.handed.splice(0)) this.announce(h, true);
  }

  /** Queue one sentence. Resolves true once handed to the head, false if it was dropped.
   *  `preview`: a rig-panel sample, outside any turn — never filtered, never counted. */
  play(msg: SpeakMsg, preview = false): Promise<boolean> {
    // Decoding is async and sentences must reach the head in the order they were sent.
    const run = this.chain.then(() => this.hand(msg, preview));
    this.chain = run.catch(() => undefined);
    return run;
  }

  /** Stop now, and drop every sentence of epoch `turn` and before (default: all received). */
  stop(turn: number = this.lastTurn): void {
    this.stoppedUpTo = Math.max(this.stoppedUpTo, turn);
    this.handed = [];                               // never played: not part of the conversation
    this.head.stopSpeaking();
  }

  /** This sentence's audio started — so everything queued before it has played, whether or not
   *  its own callback fired. Announce those first, in order. */
  private started(h: Handed): void {
    const at = this.handed.indexOf(h);
    if (at === -1) return;                          // already announced, or dropped by a stop
    for (const earlier of this.handed.splice(0, at + 1)) this.announce(earlier, earlier !== h);
  }

  private announce(h: Handed, late: boolean): void {
    if (h.started) return;
    h.started = true;
    this.onStart(h.msg, h.preview, late);
  }

  private async hand(msg: SpeakMsg, preview: boolean): Promise<boolean> {
    if (!preview && msg.turn <= this.stoppedUpTo) return false;
    const audio = await this.decode(msg.audio_b64);
    if (!preview && msg.turn <= this.stoppedUpTo) return false;   // interrupted while it decoded
    if (!preview) this.lastTurn = Math.max(this.lastTurn, msg.turn);
    const h: Handed = { msg, preview, started: false };
    this.handed.push(h);
    this.head.speakAudio(
      // `words` is REQUIRED for supplied visemes to be read at all: in speakAudio the visemes
      // branch is nested inside `if (r.words)` — omit it and you get audio with a frozen face.
      { audio, words: [msg.text || " "], wtimes: [0], wdurations: [audio.duration * 1000],
        visemes: msg.visemes, vtimes: msg.vtimes, vdurations: msg.vdurations },
      null,
      () => this.started(h),                        // fires per subtitle word; once is all we act on
    );
    return true;
  }
}
