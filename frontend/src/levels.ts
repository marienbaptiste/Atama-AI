/** The SRS level colours (user, 2026-09-12): every grammar mark and every word span in the chat is
 *  painted the colour of ITS level, on the one scale Bunpro uses for progress. Grammar carries its
 *  Bunpro level as-is; a word's WaniKani stage is mapped onto the same scale, and a leech is
 *  painted like a ghost. The colours themselves are tokens in style.css (--lv-*); this module only
 *  decides which one a span gets, and draws the legend and the cards. Pure: no DOM. */
import type { VocabSpan } from "./protocol.gen";
import { esc } from "./ui";

/** The scale, weakest first — the order the legend shows. */
export const LEVELS = ["ghost", "beginner", "adept", "seasoned", "expert", "master", "self_study"] as const;
export type Level = (typeof LEVELS)[number];

const NAMES: Record<Level, string> = {
  ghost: "Ghost", beginner: "Beginner", adept: "Adept", seasoned: "Seasoned",
  expert: "Expert", master: "Master", self_study: "Self-Study",
};

/** A Bunpro level as the server sends it ("beginner"), or "" for one not on the scale. */
export function level(raw: string): Level | "" {
  const key = raw.trim().toLowerCase().replace("-", "_") as Level;
  return (LEVELS as readonly string[]).includes(key) ? key : "";
}

/** A WaniKani stage name ("Apprentice 2", "Guru 1", "Master", "Enlightened", "Burned") on the
 *  Bunpro scale: Apprentice is Beginner, Guru is Adept, Master is Seasoned, Enlightened is Expert
 *  and Burned is Master — the same five steps, one scale for both. A leech is a ghost. */
export function levelOfStage(stage: string, leech = false): Level | "" {
  if (leech) return "ghost";
  const s = stage.trim().toLowerCase();
  if (s.startsWith("apprentice")) return "beginner";
  if (s.startsWith("guru")) return "adept";
  if (s.startsWith("master")) return "seasoned";
  if (s.startsWith("enlightened")) return "expert";
  if (s.startsWith("burned")) return "master";
  return "";
}

/** The level's name for a card: "Beginner", "Ghost" — "" when there is none. */
export function levelName(lv: Level | ""): string {
  return lv ? NAMES[lv] : "";
}

/** The class a mark gets for its level, "" when it has none (the neutral style). */
export function levelClass(lv: Level | ""): string {
  return lv ? ` lv-${lv}` : "";
}

/** A small filled swatch, as in the legend and on the cards. */
export function swatch(lv: Level | ""): string {
  return lv ? `<i class="sw lv-${lv}" aria-hidden="true"></i>` : "";
}

/** The seven swatches with their names, for the study panel's header. Compact on purpose: it is
 *  a key, not a chart. */
export function legendHtml(): string {
  return `<span class="legend" aria-label="Level colours">`
    + LEVELS.map(lv => `<span class="lv" title="${NAMES[lv]}">${swatch(lv)}${NAMES[lv]}</span>`).join("")
    + "</span>";
}

/** The word card (user, 2026-09-12): reading, meaning, and the level of mastery with its colour.
 *  Everything came with the span, so the card costs nothing. */
export function wordCardHtml(word: VocabSpan): string {
  const lv = levelOfStage(word.stage, word.leech);
  const where = word.stage ? `${esc(word.stage)} on WaniKani${word.leech ? " · leech" : ""}` : "";
  const bits = [word.reading && `<p class="rd">${esc(word.reading)}</p>`,
                word.meaning && `<p>${esc(word.meaning)}</p>`,
                where && `<p class="stage">${chip(lv)}${where}</p>`];
  return `<small>Your vocabulary</small><b>${esc(word.word)}</b>`
    + (bits.filter(Boolean).join("") || "<p>No reading stored for this one.</p>");
}

/** The grammar card's head, before the explanation arrives: the point and its Bunpro level. */
export function pointCardHtml(point: string, rawLevel: string): string {
  const lv = level(rawLevel);
  return `<small>Grammar point</small><b>${esc(point)}</b>`
    + (lv ? `<p class="stage">${chip(lv)}${levelName(lv)} on Bunpro</p>` : "")
    + '<p class="wait ans">Looking it up…</p>';
}

function chip(lv: Level | ""): string {
  return lv ? `<i class="chip lv-${lv}">${levelName(lv)}</i>` : "";
}
