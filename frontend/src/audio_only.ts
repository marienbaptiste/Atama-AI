/** Her voice without her face. When TalkingHead cannot be loaded — the CDN is down, the page is
 *  offline, the GLB is missing — the lesson must still be a lesson: every sentence plays, and lands
 *  in the chat as its audio starts, exactly as it would with the avatar (found 2026-09-12: the chat
 *  stayed empty for ever, because `speak` waited for a model that was never coming).
 *
 *  Same contract as TalkingHead's queue (speech.ts `SpeakingHead`): sentences play in the order
 *  given, one after the other; the subtitle callback fires as each one starts; stopSpeaking drops
 *  the rest. Pure Web Audio, injectable, so it is tested with a fake context. */
import type { SpeakingHead } from "./speech";

export interface SourceLike {
  buffer: AudioBuffer | null;
  onended: ((ev: Event) => unknown) | null;
  connect(destination: unknown): void;
  start(): void;
  stop(): void;
}

export interface ContextLike {
  readonly destination: unknown;
  resume(): Promise<void>;
  createBufferSource(): SourceLike;
  decodeAudioData(data: ArrayBuffer): Promise<AudioBuffer>;
}

interface Queued { audio: AudioBuffer; start: () => void }

export class AudioOnly implements SpeakingHead {
  isSpeaking = false;
  private queue: Queued[] = [];
  private current: SourceLike | null = null;

  constructor(readonly audioCtx: ContextLike) {}

  speakAudio(r: { audio: unknown; words: string[] }, _opt?: unknown,
             onsubtitles?: ((text: string) => void) | null): void {
    this.queue.push({ audio: r.audio as AudioBuffer, start: () => onsubtitles?.(r.words[0] ?? "") });
    if (!this.current) this.next();
  }

  stopSpeaking(): void {
    this.queue = [];
    const playing = this.current;
    this.current = null;
    this.isSpeaking = false;
    if (playing) {
      playing.onended = null;
      try { playing.stop(); } catch { /* already over */ }
    }
  }

  private next(): void {
    const item = this.queue.shift();
    if (!item) {
      this.current = null;
      this.isSpeaking = false;
      return;
    }
    void this.audioCtx.resume().catch(() => { /* not yet touched: it plays once it is */ });
    const source = this.audioCtx.createBufferSource();
    source.buffer = item.audio;
    source.connect(this.audioCtx.destination);
    source.onended = () => { if (this.current === source) this.next(); };
    this.current = source;
    this.isSpeaking = true;
    source.start();
    item.start();
  }
}
