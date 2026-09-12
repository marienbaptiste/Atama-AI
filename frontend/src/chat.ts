/** The conversation beside the tutor (spec §8b, ADR-036): each of her sentences becomes its own
 *  bubble as its audio starts, yours appear once heard, and the grammar she used is in red. */
import type { GrammarSpan, Reading, SpeakMsg, VocabSpan } from "./protocol.gen";
import { esc } from "./ui";

/** Settings → Display: furigana over every kanji, only over kanji not yet passed on WaniKani
 *  (the default), or none at all. */
export type Furigana = "all" | "unknown" | "off";

/** Keep the spans that make sense, in order, none overlapping the one before it. */
function usable<T extends { start: number; end: number }>(spans: readonly T[], length: number): T[] {
  const out: T[] = [];
  let at = 0;
  for (const s of [...spans].sort((a, b) => a.start - b.start)) {
    if (s.start < at || s.start < 0 || s.start >= s.end || s.end > length) continue;
    out.push(s);
    at = s.end;
  }
  return out;
}

/** One sentence as HTML: her grammar uses wrapped in clickable red marks, the student's own words
 *  in blue (user, 2026-09-12), furigana over the kanji the setting asks for. Positions count code
 *  points, as Python does (models.py), so the text is split with Array.from rather than indexed as
 *  UTF-16. Anything out of range is ignored, never thrown on.
 *
 *  Where a word sits inside a grammar point the grammar wins: red is the teaching colour, and two
 *  nested marks would be a box inside a box for no gain. */
export function renderSentence(text: string, grammar: readonly GrammarSpan[] = [],
                               readings: readonly Reading[] = [], furigana: Furigana = "off",
                               vocab: readonly VocabSpan[] = []): string {
  const chars = Array.from(text);
  const spans = usable(grammar, chars.length);
  const words = usable(vocab, chars.length);
  const ruby = new Map<number, Reading>();
  if (furigana !== "off") {
    for (const r of usable(readings, chars.length)) {
      if (furigana === "unknown" && r.known) continue;
      ruby.set(r.start, r);
    }
  }
  let html = "";
  let open: GrammarSpan | VocabSpan | undefined;
  const tagFor = (s: GrammarSpan | VocabSpan) =>
    "point" in s ? `<mark class="gp" tabindex="0" data-point="${esc(s.point)}">`
                 : `<mark class="vw" tabindex="0" data-word="${esc(s.word)}"`
                   + ` data-reading="${esc(s.reading)}" data-meaning="${esc(s.meaning)}"`
                   + ` data-stage="${esc(s.stage)}">`;
  for (let i = 0; i < chars.length;) {
    const r = ruby.get(i);
    const end = r ? r.end : i + 1;
    const at = (list: readonly (GrammarSpan | VocabSpan)[]) =>
      list.find(s => s.start <= i && i < s.end);
    const span = at(spans) ?? at(words);
    if (span !== open) {
      if (open) html += "</mark>";
      if (span) html += tagFor(span);
      open = span;
    }
    const base = esc(chars.slice(i, end).join(""));
    html += r ? `<ruby>${base}<rt>${esc(r.reading)}</rt></ruby>` : base;
    i = end;
  }
  return html + (open ? "</mark>" : "");
}

/** Near enough to the bottom that a new message should scroll into view — someone scrolled up to
 *  reread is left where they are. */
const FOLLOW_PX = 80;

interface Line {
  el: HTMLElement; text: string;
  grammar: readonly GrammarSpan[]; readings: readonly Reading[]; vocab: readonly VocabSpan[];
}

/** The translate icon on her bubbles (spec §8b): 訳 = "translation". */
const TRANSLATE = "訳";

export class Chat {
  private furigana: Furigana = "unknown";
  /** Whether new lines scroll into view: false once the student scrolls up to reread. */
  private following = true;
  /** When we last scrolled the list ourselves, so our own scroll events do not read as the
   *  student scrolling away from the bottom. */
  private jumped = 0;
  /** Every line shown, so a change of the furigana setting re-reads the conversation so far. */
  private lines: Line[] = [];

  constructor(private readonly list: HTMLElement,
              private readonly onPoint: (point: string, mark: HTMLElement) => void,
              private readonly onTranslate: (sentence: string) => void = () => {},
              private readonly onWord: (word: VocabSpan, mark: HTMLElement) => void = () => {}) {
    // A click lands on a grammar point (red) or on one of their own words (blue). Both open a
    // card; the word's card is already in the message (user, 2026-09-12).
    const open = (target: EventTarget | null) => {
      const el = target as HTMLElement | null;
      const point = el?.closest?.<HTMLElement>("mark.gp");
      if (point) {
        this.onPoint(point.dataset.point || "", point);
        return true;
      }
      const word = el?.closest?.<HTMLElement>("mark.vw");
      if (word) {
        this.onWord(this.wordOf(word), word);
        return true;
      }
      return false;
    };
    list.addEventListener("scroll", () => {
      if (Date.now() - this.jumped < 400) return;        // our own jump, still settling
      this.following = this.atBottom();
    }, { passive: true });
    list.addEventListener("click", e => {
      const button = (e.target as HTMLElement | null)?.closest?.<HTMLButtonElement>("button.tr");
      if (button) { e.stopPropagation(); this.translate(button); return; }
      if (open(e.target)) e.stopPropagation();
    });
    list.addEventListener("keydown", e => { if (e.key === "Enter" && open(e.target)) e.preventDefault(); });
  }

  /** Settings → Display changed: re-read every line with the new furigana. */
  setFurigana(mode: string): void {
    const wanted: Furigana = mode === "all" || mode === "off" ? mode : "unknown";
    if (wanted === this.furigana) return;
    this.furigana = wanted;
    for (const line of this.lines) line.el.innerHTML = this.html(line);
  }

  /** She is not here yet: one bubble on her side saying what is still loading, so the wait has a
   *  face (user, 2026-09-12 — the launch went quiet for half a minute and looked hung). Calling it
   *  again only changes the words; her first real sentence removes it. */
  loading(caption: string): void {
    let el = this.list.querySelector<HTMLElement>(".msg.loading");
    if (!el) {
      el = document.createElement("div");
      el.className = "msg her loading";
      el.innerHTML = '<i class="dots" aria-hidden="true"><u></u><u></u><u></u></i><span></span>';
      const made = el;
      this.append(() => this.list.appendChild(made));
    }
    el.querySelector("span")!.textContent = caption;
  }

  /** Whatever was loading is done, or she is about to speak. */
  ready(): void {
    this.list.querySelector<HTMLElement>(".msg.loading")?.remove();
  }

  /** Her sentence, as its audio starts — one bubble each (user, 2026-09-12), with the icon that
   *  asks for its English. Nothing is translated until it is clicked (ADR-036). */
  her(msg: SpeakMsg): void {
    this.ready();
    const body = this.bubble("her");
    this.show(body, msg.text, msg.grammar, msg.readings, msg.vocab);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "tr";
    button.title = "Translate this sentence";
    button.textContent = TRANSLATE;
    body.parentElement!.appendChild(button);
  }

  /** What the page knows about a blue word: its own dataset, filled when the span was rendered. */
  private wordOf(mark: HTMLElement): VocabSpan {
    return { start: 0, end: 0, word: mark.dataset.word || mark.textContent || "",
             reading: mark.dataset.reading || "", meaning: mark.dataset.meaning || "",
             stage: mark.dataset.stage || "" };
  }

  /** The plain sentence an element sits in — not what is on screen, which carries furigana. */
  sentenceOf(el: HTMLElement): string {
    // From inside the sentence (a grammar mark) or from beside it (the translate button, which is
    // the bubble's child, not the body's — it returned nothing at first try, 2026-09-12).
    const body = el.closest<HTMLElement>(".body")
      ?? el.closest<HTMLElement>(".msg")?.querySelector<HTMLElement>(".body")
      ?? null;
    return this.lines.find(line => line.el === body)?.text ?? "";
  }

  /** The answer to one translate click, or why there is none. */
  setTranslation(sentence: string, answer: string, error = ""): void {
    for (const line of this.lines) {
      if (line.text !== sentence) continue;
      const en = line.el.parentElement?.querySelector<HTMLElement>(".en");
      if (!en) continue;
      en.textContent = answer || error || "no answer";
      en.classList.toggle("bad", !answer);
      this.stick();                      // the English grows the bubble after it was scrolled to
    }
  }

  private translate(button: HTMLButtonElement): void {
    const bubble = button.parentElement!;
    const shown = bubble.querySelector<HTMLElement>(".en");
    if (shown) {                                    // clicking again puts it away, and back
      shown.hidden = !shown.hidden;
      button.classList.toggle("on", !shown.hidden);
      return;
    }
    const sentence = this.sentenceOf(button);
    if (!sentence) return;
    const en = document.createElement("div");
    en.className = "en";
    en.textContent = "…";
    bubble.insertBefore(en, button);
    button.classList.add("on");
    this.onTranslate(sentence);
  }

  /** What you said, once heard — or, faded, what was heard and not sent. */
  you(text: string, accepted: boolean, reason = "", readings: readonly Reading[] = [],
      vocab: readonly VocabSpan[] = []): void {
    const body = this.bubble(accepted ? "you" : "you dropped");
    this.show(body, text || "…", [], readings, vocab);
    if (!accepted) body.parentElement!.title = `Not sent to your tutor: ${reason}`;
  }

  private show(el: HTMLElement, text: string, grammar: readonly GrammarSpan[],
               readings: readonly Reading[], vocab: readonly VocabSpan[] = []): void {
    const line: Line = { el, text, grammar, readings, vocab };
    el.innerHTML = this.html(line);
    this.lines.push(line);
  }

  private html(line: Line): string {
    return renderSentence(line.text, line.grammar, line.readings, this.furigana, line.vocab);
  }

  private bubble(kind: string): HTMLElement {
    const msg = document.createElement("div");
    msg.className = "msg " + kind;
    const body = document.createElement("div");
    body.className = "body";
    msg.appendChild(body);
    this.append(() => this.list.appendChild(msg));
    return body;
  }

  private append(add: () => void): void {
    if (this.atBottom()) this.following = true;
    add();
    if (this.following) this.stick();
  }

  private atBottom(): boolean {
    return this.list.scrollHeight - this.list.scrollTop - this.list.clientHeight < FOLLOW_PX;
  }

  /** Ride the bottom until the line has its final height.
   *
   *  One jump was not enough and the chat kept stopping a line short (the user, 2026-09-12):
   *  furigana adds a row above the text, the Japanese webfont swaps in after the bubble is in the
   *  DOM, and a translation appears under a sentence later still — each one grows the content after
   *  the scroll already happened. So jump now, on the next frame, and once the fonts have settled,
   *  and keep the flag so anything that grows later (`setTranslation`) rides down too. */
  private stick(): void {
    const jump = () => {
      if (!this.following) return;
      this.jumped = Date.now();
      this.list.scrollTop = this.list.scrollHeight;
    };
    jump();
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(jump);
    if (typeof setTimeout === "function") setTimeout(jump, 150);
    document?.fonts?.ready?.then?.(jump);
  }
}
