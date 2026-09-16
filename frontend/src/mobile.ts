/** Mobile mode (ADR-041, user request 2026-09-16): the same page on a phone.
 *
 *  Decided by `wantsMobile` — a coarse pointer on a narrow screen, or `#m=1` / `#m=0` to force it
 *  for a test — and never on the desktop page, which is unchanged (user, 2026-09-16). In mobile
 *  mode the status bar, the New topic and Stop buttons and the Activity card move into a
 *  hamburger drawer; the drawer also opens the conversation as an overlay and the settings. The
 *  elements are MOVED, not copied: their handlers, ids and state come with them, and leaving the
 *  mode puts each back exactly where it was. */
import { $ } from "./ui";

export function wantsMobile(width: number, coarse: boolean, forced: boolean | null): boolean {
  if (forced !== null) return forced;
  return coarse && width < 900;
}

/** `#m=1` forces mobile mode, `#m=0` forces desktop; anything else decides by the screen. */
export function forcedMode(hash: string): boolean | null {
  const m = /(?:^#|[#&])m=([01])(?:&|$)/.exec(hash);
  return m ? m[1] === "1" : null;
}

export interface MobileDeps {
  onChange(on: boolean): void;
  openSettings(): void;
  /** The conversation overlay just opened: show its newest lines. */
  chatOpened(): void;
}

interface Home { parent: HTMLElement; next: Node | null }

export class Mobile {
  active = false;
  private readonly homes = new Map<HTMLElement, Home>();

  constructor(private readonly d: MobileDeps) {}

  start(): void {
    const coarse = matchMedia("(pointer: coarse)");
    const decide = () => this.apply(wantsMobile(innerWidth, coarse.matches, forcedMode(location.hash)));
    $("menu-btn").onclick = () => this.menu(true);
    $("menu-close").onclick = () => this.menu(false);
    $("menu-scrim").onclick = () => this.menu(false);
    $("menu-chat").onclick = () => { this.chat(true); this.menu(false); };
    $("menu-settings").onclick = () => { this.menu(false); this.d.openSettings(); };
    $("chat-close").onclick = () => this.chat(false);
    decide();
    addEventListener("resize", decide);
    coarse.addEventListener?.("change", decide);
    addEventListener("hashchange", decide);
  }

  get chatOpen(): boolean { return document.body.classList.contains("chat-open"); }

  chat(on: boolean): void {
    document.body.classList.toggle("chat-open", on && this.active);
    this.d.onChange(this.active);
    if (on && this.active) this.d.chatOpened();
  }

  menu(on: boolean): void {
    $("menu").hidden = !on;
    $("menu-scrim").hidden = !on;
    document.body.classList.toggle("menu-open", on);
  }

  private apply(on: boolean): void {
    if (on === this.active) return;
    this.active = on;
    document.body.classList.toggle("mobile", on);
    if (on) this.enter(); else this.leave();
    this.d.onChange(on);
  }

  private move(el: HTMLElement, into: HTMLElement): void {
    if (!this.homes.has(el)) this.homes.set(el, { parent: el.parentElement!, next: el.nextSibling });
    into.appendChild(el);
  }

  private enter(): void {
    this.move($("meters"), $("menu-status"));
    this.move($("topic"), $("menu-lesson"));
    this.move($("quit"), $("menu-lesson"));
    this.move($("activity"), $("menu-activity"));
    $("menu-btn").hidden = false;
    const start = $("start").querySelector("b");
    if (start) start.textContent = "Tap to start";     // there is no key to press on a phone
  }

  private leave(): void {
    this.menu(false);
    document.body.classList.remove("chat-open");
    for (const [el, home] of this.homes) {
      const next = home.next && home.next.parentNode === home.parent ? home.next : null;
      home.parent.insertBefore(el, next);
    }
    this.homes.clear();
    $("menu-btn").hidden = true;
  }
}
