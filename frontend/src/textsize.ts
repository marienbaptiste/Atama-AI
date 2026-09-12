/** − / + for the conversation's text (user, 2026-09-12): in place of the level legend in the chat
 *  header. The size is a CSS variable on the chat (--chat-size), so every bubble, ruby and mark
 *  follows it; it is remembered per browser and clamped to what still fits a bubble. Pure apart
 *  from the two DOM calls in mount()/apply(), so the arithmetic is testable. */

export const MIN_PX = 12;
export const MAX_PX = 26;
export const STEP_PX = 2;
export const DEFAULT_PX = 16;
const KEY = "atama.chat-size";

/** The next size after one press, never outside [MIN_PX, MAX_PX] and always a whole step. */
export function step(current: number, delta: -1 | 1): number {
  const next = Math.round(current / STEP_PX) * STEP_PX + delta * STEP_PX;
  return Math.min(MAX_PX, Math.max(MIN_PX, next));
}

/** What was remembered, or the default when nothing (or nonsense) was. */
export function remembered(raw: string | null): number {
  const n = Number(raw);
  return Number.isFinite(n) && n >= MIN_PX && n <= MAX_PX ? Math.round(n / STEP_PX) * STEP_PX : DEFAULT_PX;
}

export class TextSize {
  px = DEFAULT_PX;
  private minus?: HTMLButtonElement;
  private plus?: HTMLButtonElement;

  constructor(private readonly host: HTMLElement) {
    let raw: string | null = null;
    try { raw = localStorage.getItem(KEY); } catch { /* private window: the default, every time */ }
    this.px = remembered(raw);
  }

  /** The two buttons, appended to the header. */
  mount(header: HTMLElement): void {
    const box = document.createElement("span");
    box.className = "tsize";
    box.setAttribute("aria-label", "Text size");
    this.minus = this.button("−", "Smaller text", -1);
    this.plus = this.button("+", "Larger text", 1);
    box.append(this.minus, this.plus);
    header.append(box);
    this.apply();
  }

  set(px: number): void {
    this.px = Math.min(MAX_PX, Math.max(MIN_PX, px));
    try { localStorage.setItem(KEY, String(this.px)); } catch { /* private window */ }
    this.apply();
  }

  private button(label: string, title: string, delta: -1 | 1): HTMLButtonElement {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    b.title = title;
    b.setAttribute("aria-label", title);
    b.addEventListener("click", () => this.set(step(this.px, delta)));
    return b;
  }

  private apply(): void {
    this.host.style.setProperty("--chat-size", `${this.px}px`);
    if (this.minus) this.minus.disabled = this.px <= MIN_PX;
    if (this.plus) this.plus.disabled = this.px >= MAX_PX;
  }
}
