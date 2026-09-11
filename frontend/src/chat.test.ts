// Spec §8b / ADR-036: her grammar uses become red, clickable marks on exactly their words.
import { describe, expect, it } from "vitest";
import { renderSentence } from "./chat";

describe("renderSentence", () => {
  it("wraps each grammar use in a mark naming its point", () => {
    const html = renderSentence("雨が降ったら行きません。", [{ start: 2, end: 6, point: "〜たら" }]);
    expect(html).toBe('雨が<mark class="gp" tabindex="0" data-point="〜たら">降ったら</mark>行きません。');
  });

  it("counts code points like the server, so a character outside the BMP does not shift marks", () => {
    const html = renderSentence("𠮷野で食べたい。", [{ start: 3, end: 6, point: "〜たい" }]);
    expect(html).toContain(">食べた</mark>");
  });

  it("escapes everything — her text is never trusted markup", () => {
    const html = renderSentence("<b>x</b>", [{ start: 0, end: 3, point: '"><img>' }]);
    expect(html).not.toContain("<b>");
    expect(html).not.toContain('"><img>');
  });

  it("ignores spans that are out of range or overlap", () => {
    const html = renderSentence("はい。", [{ start: 2, end: 9, point: "x" }, { start: 0, end: 2, point: "a" },
                                          { start: 1, end: 3, point: "b" }]);
    expect(html).toBe('<mark class="gp" tabindex="0" data-point="a">はい</mark>。');
  });

  it("renders a sentence without marks as plain text", () => {
    expect(renderSentence("こんにちは。")).toBe("こんにちは。");
  });
});
