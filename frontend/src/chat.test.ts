// Spec §8b / ADR-036: her grammar uses become clickable marks on exactly their words, the student's
// own words too, each in the colour of its SRS level (user, 2026-09-12).
import { describe, expect, it } from "vitest";
import { renderSentence } from "./chat";
import type { GrammarSpan, VocabSpan } from "./protocol.gen";

const gp = (start: number, end: number, point: string, level = ""): GrammarSpan => ({ start, end, point, level });
const vw = (start: number, end: number, word: string, stage = "", leech = false, reading = "", meaning = ""): VocabSpan =>
  ({ start, end, word, reading, meaning, stage, leech });

describe("renderSentence", () => {
  it("wraps each grammar use in a mark naming its point", () => {
    const html = renderSentence("雨が降ったら行きません。", [gp(2, 6, "〜たら")]);
    expect(html).toBe('雨が<mark class="gp" tabindex="0" data-point="〜たら">降ったら</mark>行きません。');
  });

  it("counts code points like the server, so a character outside the BMP does not shift marks", () => {
    const html = renderSentence("𠮷野で食べたい。", [gp(3, 6, "〜たい")]);
    expect(html).toContain(">食べた</mark>");
  });

  it("escapes everything — her text is never trusted markup", () => {
    const html = renderSentence("<b>x</b>", [gp(0, 3, '"><img>')]);
    expect(html).not.toContain("<b>");
    expect(html).not.toContain('"><img>');
  });

  it("ignores spans that are out of range or overlap", () => {
    const html = renderSentence("はい。", [gp(2, 9, "x"), gp(0, 2, "a"), gp(1, 3, "b")]);
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

  it("hides per kanji when the server split a half-known compound", () => {
    // 日本語 with 日 and 本 passed, 語 not (backend/annotate.py): furigana over 語 alone.
    const html = renderSentence("日本語です。", [], [
      { start: 0, end: 1, reading: "に", known: true }, { start: 1, end: 2, reading: "ほん", known: true },
      { start: 2, end: 3, reading: "ご", known: false }], "unknown");
    expect(html).toBe("日本<ruby>語<rt>ご</rt></ruby>です。");
  });

  it("puts furigana inside a grammar mark", () => {
    const html = renderSentence("雨が降ったら。", [gp(2, 6, "〜たら")],
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
  it("marks a word from their lessons, and lets grammar win where they overlap", () => {
    const html = renderSentence("公園で勉強します。", [gp(3, 8, "〜ます")], [], "off",
                                [vw(0, 2, "公園", "Apprentice 2", false, "こうえん", "park"), vw(3, 5, "勉強", "")]);
    expect(html).toContain('data-word="公園"');
    expect(html).toContain('data-reading="こうえん"');       // the card rides with the span
    expect(html).toContain('data-stage="Apprentice 2"');
    expect(html).toContain('data-point="〜ます"');
    expect(html).not.toContain('data-word="勉強"');      // inside the grammar span: stays grammar
  });

  it("puts furigana over one of their words like any other", () => {
    const html = renderSentence("公園です。", [], [{ start: 0, end: 2, reading: "こうえん", known: false }],
                                "unknown", [vw(0, 2, "公園", "Apprentice 2", false, "こうえん", "park")]);
    expect(html).toContain("<ruby>公園<rt>こうえん</rt></ruby></mark>です。");
  });

  it("ignores a span that runs off the end", () => {
    expect(renderSentence("はい。", [], [], "off", [vw(1, 99, "x")])).toBe("はい。");
  });
});

describe("the colour of a mark is its level (user, 2026-09-12)", () => {
  it("gives a grammar mark the class of its Bunpro level, and none when it is not on their list", () => {
    expect(renderSentence("雨が降ったら。", [gp(2, 6, "〜たら", "beginner")]))
      .toBe('雨が<mark class="gp lv-beginner" tabindex="0" data-point="〜たら" data-level="beginner">降ったら</mark>。');
    expect(renderSentence("雨が降ったら。", [gp(2, 6, "〜たら", "ghost")])).toContain('class="gp lv-ghost"');
    expect(renderSentence("雨が降ったら。", [gp(2, 6, "〜たら", "Seasoned")])).toContain('class="gp lv-seasoned"');
    expect(renderSentence("雨が降ったら。", [gp(2, 6, "〜たら", "")])).toContain('class="gp" ');
    expect(renderSentence("雨が降ったら。", [gp(2, 6, "〜たら", "legendary")])).toContain('class="gp" ');
  });

  it("maps a word's WaniKani stage onto the same scale, and a leech onto ghost", () => {
    const at = (stage: string, leech = false) => renderSentence("公園", [], [], "off", [vw(0, 2, "公園", stage, leech)]);
    expect(at("Apprentice 1")).toContain('class="vw lv-beginner"');
    expect(at("Apprentice 4")).toContain('class="vw lv-beginner"');
    expect(at("Guru 1")).toContain('class="vw lv-adept"');
    expect(at("Master")).toContain('class="vw lv-seasoned"');
    expect(at("Enlightened")).toContain('class="vw lv-expert"');
    expect(at("Burned")).toContain('class="vw lv-master"');
    expect(at("Apprentice 2", true)).toContain('class="vw lv-ghost"');
    expect(at("Apprentice 2", true)).toContain('data-leech="1"');
    expect(at("")).toContain('class="vw" ');
    expect(at("")).not.toContain("data-leech");
  });
});
