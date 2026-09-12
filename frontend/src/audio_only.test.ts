// Without TalkingHead (2026-09-12): sentences still play, in order, each announced as it starts,
// and a barge-in drops the rest — the same contract speech.ts relies on from the real head.
import { describe, expect, it } from "vitest";
import { AudioOnly, type ContextLike, type SourceLike } from "./audio_only";

class FakeSource implements SourceLike {
  buffer: AudioBuffer | null = null;
  onended: (() => void) | null = null;
  started = false;
  stopped = false;
  connect() {}
  start() { this.started = true; }
  stop() { this.stopped = true; }
}

class FakeContext implements ContextLike {
  destination = {};
  resumed = 0;
  sources: FakeSource[] = [];
  async resume() { this.resumed++; }
  createBufferSource() { const s = new FakeSource(); this.sources.push(s); return s; }
  async decodeAudioData() { return {} as AudioBuffer; }
}

const sentence = (text: string) => ({ audio: { text } as unknown, words: [text] });

describe("AudioOnly", () => {
  it("plays one sentence at a time and announces each as it starts", () => {
    const ctx = new FakeContext(), voice = new AudioOnly(ctx), started: string[] = [];
    voice.speakAudio(sentence("一。"), null, t => started.push(t));
    voice.speakAudio(sentence("二。"), null, t => started.push(t));
    expect(ctx.sources.map(s => s.started)).toEqual([true]);   // the second waits
    expect(started).toEqual(["一。"]);
    expect(voice.isSpeaking).toBe(true);
    ctx.sources[0].onended?.();
    expect(ctx.sources.map(s => s.started)).toEqual([true, true]);
    expect(started).toEqual(["一。", "二。"]);
    ctx.sources[1].onended?.();
    expect(voice.isSpeaking).toBe(false);
  });

  it("stops what is playing and drops what is queued on a barge-in", () => {
    const ctx = new FakeContext(), voice = new AudioOnly(ctx), started: string[] = [];
    voice.speakAudio(sentence("一。"), null, t => started.push(t));
    voice.speakAudio(sentence("二。"), null, t => started.push(t));
    voice.stopSpeaking();
    expect(ctx.sources[0].stopped).toBe(true);
    expect(voice.isSpeaking).toBe(false);
    ctx.sources[0].onended?.();                     // a late end event changes nothing
    expect(started).toEqual(["一。"]);
    voice.speakAudio(sentence("次。"), null, t => started.push(t));   // the next turn plays
    expect(started).toEqual(["一。", "次。"]);
  });

  it("asks the context to resume before each sentence, so a late unlock still plays", () => {
    const ctx = new FakeContext(), voice = new AudioOnly(ctx);
    voice.speakAudio(sentence("一。"));
    expect(ctx.resumed).toBe(1);
  });
});
