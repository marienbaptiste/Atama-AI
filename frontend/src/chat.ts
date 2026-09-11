/** The conversation beside the tutor (spec §8b, ADR-036): her sentences join her bubble as each
 *  starts playing, yours appear once heard, and the grammar she used is marked in red. */
import type { GrammarSpan, Reading, SpeakMsg } from "./protocol.gen";
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

/** One sentence as HTML: her grammar uses wrapped in clickable marks, furigana over the kanji the
 *  setting asks for. Positions count code points, as Python does (models.py), so the text is split
 *  with Array.from rather than indexed as UTF-16. Anything out of range is ignored, never thrown on. */
export function renderSentence(text: string, grammar: readonly GrammarSpan[] = [],
                               readings: readonly Reading[] = [], furigana: Furigana = "off"): string {
  const chars = Array.from(text);
  const spans = usable(grammar, chars.length);
  const ruby = new Map<number, Reading>();
  if (furigana !== "off") {
    for (const r of usable(readings, chars.length)) {
      if (furigana === "unknown" && r.known) continue;
      ruby.set(r.start, r);
    }
  }
  let html = "";
  let open: GrammarSpan | undefined;
  for (let i = 0; i < chars.length;) {
    const r = ruby.get(i);
    const end = r ? r.end : i + 1;
    const span = spans.find(g => g.start <= i && i < g.end);
    if (span !== open) {
      if (open) html += "</mark>";
      if (span) html += `<mark class="gp" tabindex="0" data-point="${esc(span.point)}">`;
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

interface Line { el: HTMLElement; text: string; grammar: readonly GrammarSpan[]; readings: readonly Reading[] }

export class Chat {
  private herTurn = -1;
  private herBody: HTMLElement | null = null;
  private furigana: Furigana = "unknown";
  /** Every line shown, so a change of the furigana setting re-reads the conversation so far. */
  private lines: Line[] = [];

  constructor(private readonly list: HTMLElement,
              private readonly onPoint: (point: string, mark: HTMLElement) => void) {
    const open = (target: EventTarget | null) => {
      const mark = (target as HTMLElement | null)?.closest?.<HTMLElement>("mark.gp");
      if (mark) this.onPoint(mark.dataset.point || "", mark);
      return !!mark;
    };
    list.addEventListener("click", e => { if (open(e.target)) e.stopPropagation(); });
    list.addEventListener("keydown", e => { if (e.key === "Enter" && open(e.target)) e.preventDefault(); });
  }

  /** Settings → Display changed: re-read every line with the new furigana. */
  setFurigana(mode: string): void {
    const wanted: Furigana = mode === "all" || mode === "off" ? mode : "unknown";
    if (wanted === this.furigana) return;
    this.furigana = wanted;
    for (const line of this.lines) line.el.innerHTML = this.html(line);
  }

  /** Her sentence, as its audio starts. The sentences of one turn share one bubble. */
  her(msg: SpeakMsg): void {
    if (!this.herBody || msg.turn !== this.herTurn) {
      this.herBody = this.bubble("her");
      this.herTurn = msg.turn;
    }
    const sentence = document.createElement("span");
    sentence.className = "s";
    this.show(sentence, msg.text, msg.grammar, msg.readings);
    this.append(() => this.herBody!.appendChild(sentence));
  }

  /** What you said, once heard — or, faded, what was heard and not sent. */
  you(text: string, accepted: boolean, reason = "", readings: readonly Reading[] = []): void {
    this.herBody = null;                            // her next sentence opens a new bubble
    const body = this.bubble(accepted ? "you" : "you dropped");
    this.show(body, text || "…", [], readings);
    if (!accepted) body.parentElement!.title = `Not sent to your tutor: ${reason}`;
  }

  private show(el: HTMLElement, text: string, grammar: readonly GrammarSpan[],
               readings: readonly Reading[]): void {
    const line: Line = { el, text, grammar, readings };
    el.innerHTML = this.html(line);
    this.lines.push(line);
  }

  private html(line: Line): string {
    return renderSentence(line.text, line.grammar, line.readings, this.furigana);
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
    const near = this.list.scrollHeight - this.list.scrollTop - this.list.clientHeight < FOLLOW_PX;
    add();
    if (near) this.list.scrollTop = this.list.scrollHeight;
  }
}
