// ROADMAP 9: the emotion → mood/blendshape table, and the listening reactions, headless.
import { describe, expect, it } from "vitest";
import { Backchannel, FRAMED_POSES, MOODS, RIG, draw, nextHand, rigFor } from "./rig";

describe("the emotion → rig table (spec §8)", () => {
  it("has every tag the spec names", () => {
    for (const tag of ["neutral", "happy", "thinking", "surprised", "serious"]) expect(RIG[tag]).toBeDefined();
  });

  it("uses only TalkingHead's real moods — an unknown mood is a silent no-op", () => {
    for (const rig of Object.values(RIG)) expect(MOODS).toContain(rig.mood);
  });

  it("drives the moods TalkingHead lacks with blendshapes on neutral", () => {
    for (const tag of ["thinking", "surprised", "serious"]) {
      expect(RIG[tag].mood).toBe("neutral");
      expect(Object.keys(RIG[tag].shapes).length).toBeGreaterThan(0);
    }
  });

  it("releases every expression", () => {
    for (const rig of Object.values(RIG)) expect(rig.release).toBeGreaterThan(0);
  });

  it("falls back to neutral for an unknown or missing tag", () => {
    expect(rigFor("furious")).toBe(RIG.neutral);
    expect(rigFor("")).toBe(RIG.neutral);
    expect(rigFor(undefined)).toBe(RIG.neutral);
  });

  it("only draws poses that keep her in an upper-body frame", () => {
    const memo: Record<string, string> = {};
    for (let i = 0; i < 50; i++) {
      const pose = draw("t", RIG.thinking.poses.filter(p => FRAMED_POSES.includes(p)), memo);
      expect(FRAMED_POSES).toContain(pose);
    }
  });
});

describe("variety", () => {
  it("never draws the same gesture twice running", () => {
    const memo: Record<string, string> = {};
    let last = draw("g", RIG.happy.gestures, memo);
    for (let i = 0; i < 100; i++) {
      const next = draw("g", RIG.happy.gestures, memo);
      expect(next).not.toBe(last);
      last = next;
    }
  });

  it("never uses the left hand twice running", () => {
    expect(nextHand(false, () => 0.99)).toBe(true);
    expect(nextHand(true, () => 0.99)).toBe(false);
    expect(nextHand(true, () => 0.1)).toBe(true);
  });
});

describe("listening reactions — あいづち (spec §8)", () => {
  const run = (b: Backchannel, samples: [number, number, boolean][]) =>
    samples.flatMap(([t, p, active]) => b.feed(t, p, active).map(r => `${t}:${r}`));

  it("turns attentive once the student is really talking, then nods at a pause of 300 ms", () => {
    const b = new Backchannel();
    const out = run(b, [[0, 0.9, true], [100, 0.9, true], [250, 0.9, true], [300, 0.9, true],
                        [400, 0.1, true], [600, 0.1, true], [700, 0.1, true]]);
    expect(out).toEqual(["250:attentive", "700:nod"]);
  });

  it("does not react to a cough", () => {
    const b = new Backchannel();
    expect(run(b, [[0, 0.9, true], [100, 0.1, true], [500, 0.1, true], [900, 0.1, true]])).toEqual([]);
  });

  it("nods at most once per pause and no more often than the gap", () => {
    const b = new Backchannel();
    const talk = (from: number) => [[from, 0.9, true], [from + 300, 0.9, true]] as [number, number, boolean][];
    const quiet = (from: number) => [[from, 0.1, true], [from + 350, 0.1, true], [from + 700, 0.1, true]] as [number, number, boolean][];
    const out = run(b, [...talk(0), ...quiet(400), ...talk(1200), ...quiet(1600), ...talk(2400), ...quiet(3200)]);
    const nods = out.filter(r => r.endsWith("nod")).map(r => Number(r.split(":")[0]));
    // One per pause — the second waits out the gap (1950 was too soon) and still lands in its pause.
    expect(nods).toEqual([750, 2300, 3900]);
    nods.slice(1).forEach((t, i) => expect(t - nods[i]).toBeGreaterThanOrEqual(b.opt.gapMs));
  });

  it("rests when the floor goes back to her", () => {
    const b = new Backchannel();
    run(b, [[0, 0.9, true], [300, 0.9, true]]);
    expect(b.feed(400, 0.9, false)).toEqual(["rest"]);
    expect(b.feed(500, 0.9, false)).toEqual([]);
  });
});
