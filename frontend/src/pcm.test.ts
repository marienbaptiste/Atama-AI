import { describe, expect, it } from "vitest";
import { FRAME, Pcm16k, RATE } from "./pcm";

const sine = (n: number, rate: number, hz = 440) =>
  Float32Array.from({ length: n }, (_, i) => Math.sin((2 * Math.PI * hz * i) / rate));
const expected = (k: number, hz = 440) => Math.sin((2 * Math.PI * hz * k) / RATE) * 32767;

describe("the microphone's samples as the server wants them (ADR-040)", () => {
  it("passes 16 kHz through unchanged, in 512-sample frames", () => {
    const frames = new Pcm16k(RATE).push(sine(FRAME * 2 + 1, RATE));
    expect(frames.length).toBe(2);
    expect(frames[0].length).toBe(FRAME);
    expect(frames[0][37]).toBe(Math.round(expected(37)));
    expect(frames[1][3]).toBe(Math.round(expected(FRAME + 3)));
  });

  it("resamples 48 kHz to 16 kHz: a 440 Hz tone is still a 440 Hz tone", () => {
    const frames = new Pcm16k(48_000).push(sine(4800, 48_000));   // 100 ms -> 1600 samples
    expect(frames.length).toBe(3);                                  // 3 x 512, 64 left waiting
    for (let k = 0; k < FRAME; k++) expect(Math.abs(frames[0][k] - expected(k))).toBeLessThan(700);
  });

  it("gives the same output however the input is chunked (the read position is carried)", () => {
    const signal = sine(44_100, 44_100, 300);
    const whole = new Pcm16k(44_100).push(signal).flatMap(f => Array.from(f));
    const chunked = new Pcm16k(44_100);
    const parts: number[] = [];
    for (let i = 0; i < signal.length; i += 128) {
      for (const f of chunked.push(signal.subarray(i, Math.min(i + 128, signal.length)))) parts.push(...f);
    }
    expect(parts.length).toBe(whole.length);
    for (let k = 0; k < whole.length; k++) expect(Math.abs(parts[k] - whole[k])).toBeLessThanOrEqual(1);
  });

  it("clamps what the microphone should never send", () => {
    const input = new Float32Array(FRAME + 1);
    input[0] = 2; input[1] = -2; input[2] = 0.5;
    const [frame] = new Pcm16k(RATE).push(input);
    expect([frame[0], frame[1], frame[2]]).toEqual([32767, -32768, 16384]);
  });

  it("refuses a rate it cannot work with", () => {
    expect(() => new Pcm16k(0)).toThrow(RangeError);
    expect(() => new Pcm16k(NaN)).toThrow(RangeError);
  });
});
