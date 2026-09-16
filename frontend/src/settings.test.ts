import { describe, expect, it } from "vitest";
import { scalable } from "./settings";

describe("the QR card's SVG scales to its box", () => {
  it("adds a viewBox from width and height when the server sent none", () => {
    const fixed = '<svg width="222" height="222" class="segno"><path d="M0 0"/></svg>';
    expect(scalable(fixed)).toBe('<svg viewBox="0 0 222 222" width="222" height="222" class="segno"><path d="M0 0"/></svg>');
  });
  it("leaves one that already has a viewBox alone", () => {
    const ok = '<svg viewBox="0 0 10 10" width="10" height="10"></svg>';
    expect(scalable(ok)).toBe(ok);
  });
});
