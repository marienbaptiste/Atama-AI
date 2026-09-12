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

describe("furigana (spec §8b)", () => {
  const text = "雨の日に勉強する。";
  const readings = [{ start: 0, end: 1, reading: "あめ", known: true },
                    { start: 2, end: 3, reading: "ひ", known: false },
                    { start: 4, end: 6, reading: "べんきょう", known: false }];

  it("shows none at all when it is off", () => {
    expect(renderSentence(text, [], readings, "off")).toBe(text);
  });

  it("shows every kanji's reading on 'all'", () => {
    const html = renderSentence(text, [], readings, "all");
    expect(html).toBe("<ruby>雨<rt>あめ</rt></ruby>の<ruby>日<rt>ひ</rt></ruby>に"
      + "<ruby>勉強<rt>べんきょう</rt></ruby>する。");
  });

  it("hides the readings of kanji already passed on WaniKani by default", () => {
    const html = renderSentence(text, [], readings, "unknown");
    expect(html).not.toContain("あめ");
    expect(html).toContain("<ruby>日<rt>ひ</rt></ruby>");
    expect(html).toContain("<ruby>勉強<rt>べんきょう</rt></ruby>");
  });

  it("puts furigana inside a red grammar mark", () => {
    const html = renderSentence("雨が降ったら。", [{ start: 2, end: 6, point: "〜たら" }],
                                [{ start: 2, end: 3, reading: "ふ", known: false }], "unknown");
    expect(html).toBe('雨が<mark class="gp" tabindex="0" data-point="〜たら">'
      + "<ruby>降<rt>ふ</rt></ruby>ったら</mark>。");
  });

  it("ignores readings that overlap or fall outside the sentence", () => {
    const html = renderSentence("雨。", [], [{ start: 0, end: 9, reading: "x", known: false },
                                            { start: 0, end: 1, reading: "あめ", known: false },
                                            { start: 0, end: 1, reading: "again", known: false }], "all");
    expect(html).toBe("<ruby>雨<rt>あめ</rt></ruby>。");
  });
});

describe("their own words", () => {
  it("marks a word from their lessons in blue, and lets grammar win where they overlap", () => {
    const html = renderSentence("公園で勉強します。", [{ start: 3, end: 8, point: "〜ます" }], [], "off",
                                [{ start: 0, end: 2, word: "公園" }, { start: 3, end: 5, word: "勉強" }]);
    expect(html).toContain('<mark class="vw" data-word="公園">公園</mark>');
    expect(html).toContain('data-point="〜ます"');
    expect(html).not.toContain('data-word="勉強"');      // inside the grammar span: stays red
  });

  it("puts furigana over a blue word like any other", () => {
    const html = renderSentence("公園です。", [], [{ start: 0, end: 2, reading: "こうえん", known: false }],
                                "unknown", [{ start: 0, end: 2, word: "公園" }]);
    expect(html).toBe('<mark class="vw" data-word="公園"><ruby>公園<rt>こうえん</rt></ruby></mark>です。');
  });

  it("ignores a span that runs off the end", () => {
    expect(renderSentence("はい。", [], [], "off", [{ start: 1, end: 99, word: "x" }])).toBe("はい。");
  });
});
