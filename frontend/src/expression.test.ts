// ADR-020 / spec §8: an emotion holds until the next tag or the end of the turn — never a fixed
// timer mid-sentence — and a stale release can never wipe the next sentence's face (2026-09-12).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ExpressionClock, REST_AFTER_MS, SETTLE_AFTER_MS, type Face } from "./expression";
import { RIG } from "./rig";

function face() {
  const calls: string[] = [];
  const f: Face = {
    hold: rig => calls.push("hold:" + rig.mood),
    release: () => calls.push("release"),
    settle: () => calls.push("settle"),
    rest: () => calls.push("rest"),
  };
  return { f, calls };
}

const timers = { set: (fn: () => void, ms: number) => setTimeout(fn, ms) as unknown as number,
                 clear: (id: number) => clearTimeout(id) };

describe("ExpressionClock", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("holds a sentence's face until the turn ends, releasing only the brief blendshapes", () => {
    const { f, calls } = face();
    const clock = new ExpressionClock(f, timers);
    clock.apply(RIG.serious);
    expect(calls).toEqual(["release", "hold:neutral"]);
    vi.advanceTimersByTime(RIG.serious.release);
    expect(calls).toEqual(["release", "hold:neutral", "release"]);
    vi.advanceTimersByTime(60_000);                 // a long sentence: no settle without a turn end
    expect(calls).not.toContain("settle");
  });

  it("settles a moment after the turn ends, and rests later", () => {
    const { f, calls } = face();
    const clock = new ExpressionClock(f, timers);
    clock.apply(RIG.happy);
    clock.turnEnded();
    vi.advanceTimersByTime(SETTLE_AFTER_MS - 1);
    expect(calls).not.toContain("settle");
    vi.advanceTimersByTime(1);
    expect(calls).toContain("settle");
    vi.advanceTimersByTime(REST_AFTER_MS - SETTLE_AFTER_MS);
    expect(calls).toContain("rest");
  });

  it("lets the next sentence's face cancel every pending timer", () => {
    const { f, calls } = face();
    const clock = new ExpressionClock(f, timers);
    clock.apply(RIG.surprised);                      // releases after 1200 ms...
    clock.turnEnded();                               // ...and would settle after 1500 ms
    vi.advanceTimersByTime(1000);
    clock.apply(RIG.happy);                          // a new sentence started
    calls.length = 0;
    vi.advanceTimersByTime(1000);                    // past both of the old deadlines
    expect(calls).toEqual([]);                       // neither the stale release nor the settle fired
    vi.advanceTimersByTime(RIG.happy.release);
    expect(calls).toEqual(["release"]);              // only its own
  });

  it("drops the shapes at once on a barge-in and leaves the settle to the turn's end", () => {
    const { f, calls } = face();
    const clock = new ExpressionClock(f, timers);
    clock.apply(RIG.serious);
    calls.length = 0;
    clock.stop();
    expect(calls).toEqual(["release"]);
    vi.advanceTimersByTime(60_000);
    expect(calls).toEqual(["release"]);
    clock.turnEnded();
    vi.advanceTimersByTime(SETTLE_AFTER_MS);
    expect(calls).toEqual(["release", "settle"]);
  });

  it("settles now for a reset or the thinking look, with nothing left pending", () => {
    const { f, calls } = face();
    const clock = new ExpressionClock(f, timers);
    clock.apply(RIG.happy);
    clock.turnEnded();
    clock.settleNow();
    expect(calls.at(-1)).toBe("settle");
    calls.length = 0;
    vi.advanceTimersByTime(60_000);
    expect(calls).toEqual([]);
  });
});
