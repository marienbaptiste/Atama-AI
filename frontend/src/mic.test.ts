// Push-to-talk and the local half of barge-in (spec §8/§9, ROADMAP 8).
import { describe, expect, it } from "vitest";
import { Talk, spaceIsOurs, type TalkDeps } from "./mic";

const el = (tagName: string, extra: Record<string, unknown> = {}) => ({ tagName, isContentEditable: false, ...extra }) as unknown as EventTarget;

describe("which focused controls keep SPACE", () => {
  it("keeps it for anything SPACE types into", () => {
    expect(spaceIsOurs(el("INPUT", { type: "text" }))).toBe(false);
    expect(spaceIsOurs(el("TEXTAREA"))).toBe(false);
    expect(spaceIsOurs(el("SELECT"))).toBe(false);
  });

  it("takes it from buttons, sliders, checkboxes and the page itself", () => {
    expect(spaceIsOurs(el("BUTTON"))).toBe(true);
    expect(spaceIsOurs(el("INPUT", { type: "range" }))).toBe(true);
    expect(spaceIsOurs(el("INPUT", { type: "checkbox" }))).toBe(true);
    expect(spaceIsOurs(el("BODY"))).toBe(true);
    expect(spaceIsOurs(null)).toBe(true);
  });
});

function talk(over: Partial<TalkDeps> = {}) {
  const sent: string[] = [];
  let interrupts = 0;
  const deps: TalkDeps = {
    send: a => { sent.push(a); return true; }, connected: () => true, blocked: () => false,
    mode: () => "ptt", interrupt: () => { interrupts++; return true; }, deadLink: () => {}, ...over,
  };
  const button = { classList: { toggle() {} }, disabled: false, querySelector: () => ({ textContent: "" }) };
  const t = new Talk(deps, button as unknown as HTMLButtonElement);
  return { t, sent, interrupts: () => interrupts };
}

describe("the talk key", () => {
  it("sends the press and the release, once each however often the key repeats", () => {
    const { t, sent } = talk();
    t.press(true); t.press(true); t.press(false); t.press(false);
    expect(sent).toEqual(["start", "stop"]);
  });

  it("drops the recording on ALT GR, and the release then sends nothing", () => {
    const { t, sent } = talk();
    t.press(true);
    t.cancel();
    expect(sent).toEqual(["start", "cancel"]);
    expect(t.talking).toBe(false);
    t.press(false);                              // the key comes up after the cancel
    expect(sent).toEqual(["start", "cancel"]);
    t.press(true);                               // and the next press is a clean one
    t.press(false);
    expect(sent).toEqual(["start", "cancel", "start", "stop"]);
  });

  it("has nothing to cancel when no key is down", () => {
    const { t, sent } = talk();
    t.cancel();
    expect(sent).toEqual([]);
  });

  it("stops her locally on the press, before the server hears of it, and times it", () => {
    const { t, interrupts } = talk();
    t.press(true, performance.now());
    expect(interrupts()).toBe(1);
    expect(t.stops.length).toBe(1);
    expect(t.stops[0]).toBeLessThan(300);            // gate M3b
  });

  it("leaves interrupting to the server when hands-free", () => {
    const { t, interrupts } = talk({ mode: () => "vad" });
    t.press(true);
    expect(interrupts()).toBe(0);
  });

  it("does nothing while disconnected", () => {
    const { t, sent } = talk({ connected: () => false });
    t.press(true);
    expect(sent).toEqual([]);
    expect(t.talking).toBe(false);
  });
});
