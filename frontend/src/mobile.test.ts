import { describe, expect, it } from "vitest";
import { forcedMode, wantsMobile } from "./mobile";

describe("mobile mode is for phones, never the desktop page (ADR-041)", () => {
  it("wants a coarse pointer AND a narrow screen", () => {
    expect(wantsMobile(390, true, null)).toBe(true);        // a phone
    expect(wantsMobile(1280, true, null)).toBe(false);      // a touch laptop or a tablet in landscape
    expect(wantsMobile(390, false, null)).toBe(false);      // a narrow desktop window keeps its layout
  });

  it("can be forced either way from the URL hash, for testing", () => {
    expect(forcedMode("#m=1")).toBe(true);
    expect(forcedMode("#m=0")).toBe(false);
    expect(forcedMode("#settings/sound")).toBe(null);
    expect(forcedMode("#mood=happy&m=1")).toBe(true);
    expect(forcedMode("")).toBe(null);
    expect(wantsMobile(1920, false, true)).toBe(true);
    expect(wantsMobile(390, true, false)).toBe(false);
  });
});
