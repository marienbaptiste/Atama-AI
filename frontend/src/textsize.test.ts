// − / + for the chat's text (user, 2026-09-12): whole steps, hard floor and ceiling, remembered.
import { describe, expect, it } from "vitest";
import { DEFAULT_PX, MAX_PX, MIN_PX, STEP_PX, remembered, step } from "./textsize";

describe("text size", () => {
  it("moves one step at a time and stops at the ends", () => {
    expect(step(16, 1)).toBe(16 + STEP_PX);
    expect(step(16, -1)).toBe(16 - STEP_PX);
    expect(step(MAX_PX, 1)).toBe(MAX_PX);
    expect(step(MIN_PX, -1)).toBe(MIN_PX);
    expect(step(15, 1)).toBe(18);                  // an odd size lands on the grid first
  });
  it("remembers a sensible size and ignores nonsense", () => {
    expect(remembered("20")).toBe(20);
    expect(remembered(null)).toBe(DEFAULT_PX);
    expect(remembered("abc")).toBe(DEFAULT_PX);
    expect(remembered("2")).toBe(DEFAULT_PX);
    expect(remembered("99")).toBe(DEFAULT_PX);
  });
});
