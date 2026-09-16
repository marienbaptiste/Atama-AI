/** The microphone's samples as the server wants them (spec §8, ADR-040): 16 kHz mono PCM16 in
 *  frames of exactly 512 samples — Silero's window. The VAD is stateful and a different size
 *  degrades its judgement rather than erroring (backend/voice_loop.py `feed_pcm16` reframes
 *  anyway, but sending whole frames keeps the server's partial buffer empty).
 *
 *  Pure: the worklet (capture_worklet.ts) runs it on the audio thread, the tests run it in node.
 *
 *  Resampling is done properly or not at all (2026-09-16, after the user heard Whisper do badly
 *  on the browser path): the browser is asked for a 16 kHz context first (capture_web.ts), in
 *  which case this is a pass-through; when it cannot give one, a windowed-sinc low-pass at 0.45 x
 *  16 kHz runs at the input rate BEFORE linear interpolation, so what lies above 8 kHz — the hiss
 *  and fricative energy a phone microphone has plenty of — is removed instead of folding down into
 *  the speech band as aliasing. The filter's state and the read position are carried across
 *  calls, so chunking does not change the output. */
export const RATE = 16_000;
export const FRAME = 512;
/** The AudioWorkletProcessor's registered name — here, not in the worklet file, because importing
 *  the worklet module on the main thread would run `registerProcessor` where it does not exist. */
export const PROCESSOR = "atama-capture-16k";
/** Low-pass taps for the fallback resampler: Hamming-windowed sinc, ~53 dB stopband. */
export const TAPS = 33;

function lowpass(from: number, taps: number): Float32Array {
  const fc = (0.45 * RATE) / from;           // cycles per input sample: 7.2 kHz at 48 k
  const h = new Float32Array(taps);
  const m = (taps - 1) / 2;
  let sum = 0;
  for (let n = 0; n < taps; n++) {
    const k = n - m;
    const sinc = k === 0 ? 2 * fc : Math.sin(2 * Math.PI * fc * k) / (Math.PI * k);
    const w = 0.54 - 0.46 * Math.cos((2 * Math.PI * n) / (taps - 1));
    h[n] = sinc * w;
    sum += h[n];
  }
  for (let n = 0; n < taps; n++) h[n] /= sum;   // unity gain at DC
  return h;
}

export class Pcm16k {
  /** The fallback resampler's own delay, in seconds (0 when passing 16 kHz through). */
  readonly delaySeconds: number;
  private readonly h: Float32Array | null;
  private hist: Float32Array;                // the last TAPS-1 input samples, for the filter
  private carry = new Float32Array(0);       // filtered samples not yet consumed
  private phase = 0;                         // fractional read position into `carry`
  private frame = new Int16Array(FRAME);
  private filled = 0;

  constructor(readonly from: number) {
    if (!(from > 0)) throw new RangeError(`bad sample rate ${from}`);
    this.h = from === RATE ? null : lowpass(from, TAPS);
    this.hist = new Float32Array(this.h ? TAPS - 1 : 0);
    this.delaySeconds = this.h ? (TAPS - 1) / 2 / from : 0;
  }

  /** Feed samples at `from` Hz; get every 512-sample frame they complete, oldest first. */
  push(input: Float32Array): Int16Array[] {
    const clean = this.h ? this.filter(input) : input;
    const buf = new Float32Array(this.carry.length + clean.length);
    buf.set(this.carry);
    buf.set(clean, this.carry.length);
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

  /** The low-pass, causal, at the input rate, with its history carried across calls. */
  private filter(input: Float32Array): Float32Array {
    const h = this.h!, n = h.length, keep = n - 1;
    const ext = new Float32Array(keep + input.length);
    ext.set(this.hist);
    ext.set(input, keep);
    const out = new Float32Array(input.length);
    for (let i = 0; i < input.length; i++) {
      let acc = 0;
      const base = i + keep;
      for (let k = 0; k < n; k++) acc += h[k] * ext[base - k];
      out[i] = acc;
    }
    this.hist = ext.slice(ext.length - keep);
    return out;
  }
}
