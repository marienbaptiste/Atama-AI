/** The conversation beside the tutor (spec §8b, ADR-036): her sentences join her bubble as each
 *  starts playing, yours appear once heard, and the grammar she used is marked in red. */
import type { GrammarSpan, SpeakMsg } from "./protocol.gen";
import { esc } from "./ui";

/** One sentence as HTML with her grammar uses wrapped in clickable marks. Spans count code
 *  points, as Python does (models.py GrammarSpan), so the text is split with Array.from rather
 *  than indexed as UTF-16. Out-of-range or overlapping spans are ignored, never thrown on. */
export function renderSentence(text: string, grammar: readonly GrammarSpan[] = []): string {
  const chars = Array.from(text);
  const spans = grammar.filter(g => g.start >= 0 && g.start < g.end && g.end <= chars.length)
    .slice().sort((a, b) => a.start - b.start);
  let html = "";
  let at = 0;
  for (const g of spans) {
    if (g.start < at) continue;                     // overlaps the previous mark: keep the first
    html += esc(chars.slice(at, g.start).join(""))
      + `<mark class="gp" tabindex="0" data-point="${esc(g.point)}">${esc(chars.slice(g.start, g.end).join(""))}</mark>`;
    at = g.end;
  }
  return html + esc(chars.slice(at).join(""));
}

/** Near enough to the bottom that a new message should scroll into view — someone scrolled up to
 *  reread is left where they are. */
const FOLLOW_PX = 80;

export class Chat {
  private herTurn = -1;
  private herBody: HTMLElement | null = null;

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

  /** Her sentence, as its audio starts. The sentences of one turn share one bubble. */
  her(msg: SpeakMsg): void {
    if (!this.herBody || msg.turn !== this.herTurn) {
      this.herBody = this.bubble("her");
      this.herTurn = msg.turn;
    }
    const sentence = document.createElement("span");
    sentence.className = "s";
    sentence.innerHTML = renderSentence(msg.text, msg.grammar);
    this.append(() => this.herBody!.appendChild(sentence));
  }

  /** What you said, once heard — or, faded, what was heard and not sent. */
  you(text: string, accepted: boolean, reason = ""): void {
    this.herBody = null;                            // her next sentence opens a new bubble
    const body = this.bubble(accepted ? "you" : "you dropped");
    body.textContent = text || "…";
    if (!accepted) body.parentElement!.title = `Not sent to your tutor: ${reason}`;
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
