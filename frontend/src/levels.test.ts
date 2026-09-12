// The level scale (user, 2026-09-12): one palette for grammar and words, the legend, the cards.
import { describe, expect, it } from "vitest";
import { LEVELS, legendHtml, level, levelName, levelOfStage, pointCardHtml, wordCardHtml } from "./levels";

describe("the scale", () => {
  it("has the seven Bunpro levels, weakest first", () => {
    expect([...LEVELS]).toEqual(["ghost", "beginner", "adept", "seasoned", "expert", "master", "self_study"]);
  });

  it("reads a Bunpro level as sent, and refuses anything else", () => {
    expect(level("beginner")).toBe("beginner");
    expect(level(" Expert ")).toBe("expert");
    expect(level("self-study")).toBe("self_study");
    expect(level("")).toBe("");
    expect(level("guru")).toBe("");
  });

  it("maps every WaniKani stage onto it", () => {
    expect(["Apprentice 1", "Guru 2", "Master", "Enlightened", "Burned", "?"].map(s => levelOfStage(s)))
      .toEqual(["beginner", "adept", "seasoned", "expert", "master", ""]);
    expect(levelOfStage("Burned", true)).toBe("ghost");
    expect(levelName("self_study")).toBe("Self-Study");
    expect(levelName("")).toBe("");
  });
});

describe("the legend", () => {
  it("shows the seven swatches with their names", () => {
    const html = legendHtml();
    for (const lv of LEVELS) expect(html).toContain(`<i class="sw lv-${lv}"`);
    expect(html.match(/class="sw /g)).toHaveLength(7);
    for (const name of ["Ghost", "Beginner", "Adept", "Seasoned", "Expert", "Master", "Self-Study"]) expect(html).toContain(name);
  });
});

describe("the cards (user, 2026-09-12: the level of mastery on the card)", () => {
  it("shows a word's stage with its chip, and marks a leech", () => {
    const html = wordCardHtml({ start: 0, end: 2, word: "公園", reading: "こうえん", meaning: "park",
                                stage: "Apprentice 2", leech: false });
    expect(html).toContain("<b>公園</b>");
    expect(html).toContain('<p class="rd">こうえん</p>');
    expect(html).toContain('<i class="chip lv-beginner">Beginner</i>Apprentice 2 on WaniKani</p>');
    const leech = wordCardHtml({ start: 0, end: 2, word: "公園", reading: "", meaning: "", stage: "Guru 1", leech: true });
    expect(leech).toContain('<i class="chip lv-ghost">Ghost</i>Guru 1 on WaniKani · leech</p>');
    expect(wordCardHtml({ start: 0, end: 1, word: "x", reading: "", meaning: "", stage: "", leech: false }))
      .toContain("No reading stored");
  });

  it("shows a grammar point's Bunpro level above the explanation it waits for", () => {
    const html = pointCardHtml("〜たら", "adept");
    expect(html).toContain("<b>〜たら</b>");
    expect(html).toContain('<p class="stage"><i class="chip lv-adept">Adept</i>Adept on Bunpro</p>');
    expect(html.indexOf("Adept on Bunpro")).toBeLessThan(html.indexOf('class="wait ans"'));
    expect(pointCardHtml("〜たら", "")).not.toContain("on Bunpro");   // not on their list: no level
    expect(pointCardHtml("<x>", "")).not.toContain("<x>");
  });
});
