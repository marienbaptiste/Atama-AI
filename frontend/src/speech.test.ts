// Spec §8 / ROADMAP 9: the emotion is applied by the audio-start callback, not on message receipt
// (tested with a delayed start), and barge-in drops stale sentences however late they arrive.
import { describe, expect, it } from "vitest";
import type { SpeakMsg } from "./protocol.gen";
import { SpeechPlayer, type SpeakingHead } from "./speech";

class FakeHead implements SpeakingHead {
  queued: { text: string; start: () => void }[] = [];
  stops = 0;
  speakAudio(r: { words: string[] }, _opt?: unknown, onsubtitles?: ((t: string) => void) | null) {
    this.queued.push({ text: r.words[0], start: () => onsubtitles?.(" " + r.words[0]) });
  }
  stopSpeaking() { this.stops++; this.queued = []; }
}

const say = (text: string, turn: number, emotion = ""): SpeakMsg => ({
  type: "speak", audio_b64: "", visemes: ["aa"], vtimes: [100], vdurations: [80], text, emotion, turn,
  grammar: [], target: "", readings: [],
});
const decodeNow = async () => ({ duration: 1 });

describe("SpeechPlayer", () => {
  it("applies a sentence's emotion when its audio starts, not when it arrives", async () => {
    const head = new FakeHead(), started: string[] = [];
    const player = new SpeechPlayer(head, decodeNow, m => started.push(m.emotion));
    await player.play(say("一。", 1, "happy"));
    await player.play(say("二。", 1, "serious"));
    expect(head.queued.map(q => q.text)).toEqual(["一。", "二。"]);
    expect(started).toEqual([]);                    // both received, neither playing yet
    head.queued[0].start();
    expect(started).toEqual(["happy"]);             // the second face waits for its own audio
    head.queued[1].start();
    head.queued[1].start();                         // a second subtitle tick must not re-apply
    expect(started).toEqual(["happy", "serious"]);
  });

  it("keeps the order sentences were sent in, however long each takes to decode", async () => {
    const head = new FakeHead();
    const slow = (b64: string) => new Promise<{ duration: number }>(r => setTimeout(() => r({ duration: 1 }), b64 === "slow" ? 30 : 0));
    const player = new SpeechPlayer(head, slow, () => {});
    const first = player.play({ ...say("一。", 1), audio_b64: "slow" });
    const second = player.play(say("二。", 1));
    await Promise.all([first, second]);
    expect(head.queued.map(q => q.text)).toEqual(["一。", "二。"]);
  });

  it("drops the interrupted turn, including sentences that arrive after the stop", async () => {
    const head = new FakeHead();
    const player = new SpeechPlayer(head, decodeNow, () => {});
    await player.play(say("一。", 3));
    player.stop();                                  // the student pressed SPACE
    expect(head.stops).toBe(1);
    expect(await player.play(say("二。", 3))).toBe(false);   // a straggler of turn 3
    expect(await player.play(say("次。", 4))).toBe(true);    // the next turn plays
    expect(head.queued.map(q => q.text)).toEqual(["次。"]);
  });

  it("drops a sentence that was still decoding when she was interrupted", async () => {
    const head = new FakeHead();
    let release!: () => void;
    const gate = () => new Promise<{ duration: number }>(r => { release = () => r({ duration: 1 }); });
    const player = new SpeechPlayer(head, gate, () => {});
    const pending = player.play(say("一。", 5));
    await Promise.resolve();
    player.stop(5);                                 // the server's `bargein` for turn 5
    release();
    expect(await pending).toBe(false);
    expect(head.queued).toEqual([]);
  });

  it("stops ahead of any audio when interrupted while she is still thinking", async () => {
    const head = new FakeHead();
    const player = new SpeechPlayer(head, decodeNow, () => {});
    player.stop(7);                                 // pressed during turn 7's thinking
    expect(await player.play(say("一。", 7))).toBe(false);
  });

  it("still puts a sentence in the conversation when its audio never started, at the turn's end", async () => {
    const head = new FakeHead(), seen: string[] = [];
    const player = new SpeechPlayer(head, decodeNow, (m, _p, late) => seen.push(m.text + (late ? " (late)" : "")));
    await player.play(say("一。", 1));
    await player.play(say("二。", 1));
    player.flush();                                 // her turn ended; neither start ever fired
    expect(seen).toEqual(["一。 (late)", "二。 (late)"]);
    player.flush();
    expect(seen.length).toBe(2);                    // each once
  });

  it("announces an earlier sentence whose start was lost before the one that started", async () => {
    const head = new FakeHead(), seen: string[] = [];
    const player = new SpeechPlayer(head, decodeNow, (m, _p, late) => seen.push(m.text + (late ? " (late)" : "")));
    await player.play(say("一。", 1));
    await player.play(say("二。", 1));
    head.queued[1].start();
    expect(seen).toEqual(["一。 (late)", "二。"]);
    head.queued[0].start();                         // a stale callback changes nothing
    expect(seen.length).toBe(2);
  });

  it("drops what was never said when she is interrupted", async () => {
    const head = new FakeHead(), seen: string[] = [];
    const player = new SpeechPlayer(head, decodeNow, m => seen.push(m.text));
    await player.play(say("一。", 1));
    player.stop();
    player.flush();
    expect(seen).toEqual([]);
  });

  it("plays a rig-panel sample outside any turn without disturbing the turn filter", async () => {
    const head = new FakeHead();
    const player = new SpeechPlayer(head, decodeNow, () => {});
    player.stop(9);
    expect(await player.play(say("見本。", 0), true)).toBe(true);
    expect(player.lastTurn).toBe(-1);
    expect(await player.play(say("一。", 9))).toBe(false);
  });
});
