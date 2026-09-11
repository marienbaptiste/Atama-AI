// ROADMAP 9/16: the status chips' colour logic, headless.
import { describe, expect, it } from "vitest";
import type { ServiceStatusMsg } from "./protocol.gen";
import { AMBER, GREEN, OFF, RED, chipColor, elapsed, levelPercent, summarize } from "./status";

const st = (service: string, state: string): ServiceStatusMsg =>
  ({ type: "service_status", service, state, detail: "", last_error: "" });

describe("service chips (spec §5b)", () => {
  it("colours working, degraded, failed and switched-off states", () => {
    expect(chipColor("ready")).toBe(GREEN);
    expect(chipColor("stale")).toBe(AMBER);
    expect(chipColor("rate_limited")).toBe(AMBER);
    expect(chipColor("down")).toBe(RED);
    expect(chipColor("disabled")).toBe(OFF);
    expect(chipColor("something-new")).toBe(OFF);   // unknown is grey, never green
  });

  it("lists reported services in a fixed order and names the ones needing attention", () => {
    const s = { voicevox: st("voicevox", "down"), wanikani: st("wanikani", "ok"), bunpro: st("bunpro", "disabled") };
    expect(summarize(s)).toEqual({ known: ["wanikani", "bunpro", "voicevox"], bad: ["voicevox"] });
  });

  it("ignores statuses that are not services (the push-to-talk acknowledgement, the heartbeat)", () => {
    expect(summarize({ ptt: st("ptt", "failed"), orchestrator: st("orchestrator", "ok") })).toEqual({ known: [], bad: [] });
  });
});

describe("gauges", () => {
  it("formats the session timer", () => {
    expect(elapsed(0)).toBe("0:00");
    expect(elapsed(65_000)).toBe("1:05");
    expect(elapsed(3_725_000)).toBe("1:02:05");
  });

  it("scales a quiet microphone up without overflowing", () => {
    expect(levelPercent(0)).toBe(0);
    expect(levelPercent(0.02)).toBeGreaterThan(70);    // a quiet headset's speech still moves the bar
    expect(levelPercent(1)).toBe(100);
  });
});
