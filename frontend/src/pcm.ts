/** The microphone's samples as the server wants them (spec §8, ADR-040): 16 kHz mono PCM16 in
 *  frames of exactly 512 samples — Silero's window. The VAD is stateful and a different size
 *  degrades its judgement rather than erroring (backend/voice_loop.py `feed_pcm16` reframes
 *  anyway, but sending whole frames keeps the server's partial buffer empty).
 *
 *  Pure: the worklet (capture_worklet.ts) runs it on the audio thread, the tests run it in node.
 *  Linear interpolation is enough for speech at 16 kHz from 44.1 or 48 k: what the VAD and Whisper
 *  need is the band under 4 kHz, and the aliasing a proper low-pass would remove sits above the
 *  speech they listen to. The read position is carried across calls, so chunking does not change
 *  the output. */
export const RATE = 16_000;
export const FRAME = 512;
/** The AudioWorkletProcessor's registered name — here, not in the worklet file, because importing
 *  the worklet module on the main thread would run `registerProcessor` where it does not exist. */
export const PROCESSOR = "atama-capture-16k";

export class Pcm16k {
  private carry = new Float32Array(0);   // input samples not yet consumed
  private phase = 0;                     // fractional read position into `carry`
  private frame = new Int16Array(FRAME);
  private filled = 0;

  constructor(readonly from: number) {
    if (!(from > 0)) throw new RangeError(`bad sample rate ${from}`);
  }

  /** Feed samples at `from` Hz; get every 512-sample frame they complete, oldest first. */
  push(input: Float32Array): Int16Array[] {
    const buf = new Float32Array(this.carry.length + input.length);
    buf.set(this.carry);
    buf.set(input, this.carry.length);
    const ratio = this.from / RATE;
    const out: Int16Array[] = [];
    let pos = this.phase;
    while (pos + 1 < buf.length) {          // the sample after `pos` is needed to interpolate
      const i = Math.floor(pos), t = pos - i;
      const s = buf[i] + (buf[i + 1] - buf[i]) * t;
      this.frame[this.filled++] = s >= 1 ? 32767 : s <= -1 ? -32768 : Math.round(s * 32767);
      if (this.filled === FRAME) {
        out.push(this.frame);
        this.frame = new Int16Array(FRAME);
        this.filled = 0;
      }
      pos += ratio;
    }
    // The next read position may lie past the end of this buffer (a ratio of 3 can step over
    // two samples at once): then nothing is carried, and the phase is where to start in the next
    // input — possibly a whole sample or more in. Clamping the consumed count is what keeps the
    // output identical however the input is chunked (pcm.test.ts).
    const consumed = Math.min(Math.floor(pos), buf.length);
    this.carry = buf.slice(consumed);
    this.phase = pos - consumed;
    return out;
  }
}
