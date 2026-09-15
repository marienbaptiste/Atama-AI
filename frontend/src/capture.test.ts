/** The page's microphone (ADR-040): the states of spec §9, against a fake browser. */
import { describe, expect, it } from "vitest";
import { Capture, type MicSource, type MicState } from "./capture";

class FakeSource implements MicSource {
  frame: ((f: ArrayBuffer) => void) | null = null;
  ended: (() => void) | null = null;
  stopped = false;
  constructor(readonly label: string) {}
  onFrame(cb: (f: ArrayBuffer) => void) { this.frame = cb; }
  onEnded(cb: () => void) { this.ended = cb; }
  stop() { this.stopped = true; }
  emit(n = 1) { for (let i = 0; i < n; i++) this.frame?.(new ArrayBuffer(1024)); }
  unplug() { this.ended?.(); }
}

const flush = () => new Promise(r => setTimeout(r, 0));

function harness(devices: Record<string, string> = { "": "Default mic" }) {
  const sent: ArrayBuffer[] = [];
  const reports: [MicState, string][] = [];
  const timers = new Map<number, () => void>();
  const opened: FakeSource[] = [];
  let denied = false, busy = false, id = 0;
  const cap = new Capture({
    open: async deviceId => {
      if (denied) { const e = new Error("no"); e.name = "NotAllowedError"; throw e; }
      const label = devices[deviceId];
      if (label === undefined) { const e = new Error("none"); e.name = deviceId ? "OverconstrainedError" : "NotFoundError"; throw e; }
      const s = new FakeSource(label);
      opened.push(s);
      return s;
    },
    send: f => { sent.push(f); return true; },
    report: (s, d) => reports.push([s, d]),
    setTimeout: fn => { timers.set(++id, fn); return id; },
    clearTimeout: t => { timers.delete(t); },
    busy: () => busy,
  });
  const tick = async () => { const fns = [...timers.values()]; timers.clear(); fns.forEach(f => f()); await flush(); };
  const last = () => reports[reports.length - 1];
  return { cap, sent, reports, timers, opened, devices, tick, last,
           deny: (v: boolean) => { denied = v; }, setBusy: (v: boolean) => { busy = v; } };
}

describe("the page's microphone", () => {
  it("opens the default microphone and streams its frames", async () => {
    const h = harness();
    await h.cap.start();
    expect(h.last()).toEqual(["ok", "Default mic"]);
    h.opened[0].emit(3);
    expect(h.sent.length).toBe(3);
  });

  it("a refused permission is reported, not retried on a timer, and asked again on a press", async () => {
    const h = harness();
    h.deny(true);
    await h.cap.start();
    expect(h.last()[0]).toBe("denied");
    expect(h.timers.size).toBe(0);
    h.deny(false);
    await h.cap.retryIfDenied();
    expect(h.last()).toEqual(["ok", "Default mic"]);
  });

  it("a chosen microphone that is not connected falls back to the default, and returns on devicechange", async () => {
    const h = harness();
    h.cap.chosen = "usb";
    await h.cap.start();
    expect(h.last()[0]).toBe("fallback");
    expect(h.last()[1]).toContain("Default mic");
    h.devices.usb = "USB mic";
    await h.cap.devicesChanged();
    expect(h.last()).toEqual(["ok", "USB mic"]);
    expect(h.opened[0].stopped).toBe(true);           // the fallback source was closed
    h.opened[0].emit();                               // and a late frame from it is ignored
    expect(h.sent.length).toBe(0);
  });

  it("no microphone at all is missing, and picked up when one appears", async () => {
    const h = harness({});
    await h.cap.start();
    expect(h.last()[0]).toBe("missing");
    expect(h.timers.size).toBe(1);
    await h.tick();
    expect(h.last()[0]).toBe("missing");              // still none: tried, and waiting again
    expect(h.timers.size).toBe(1);
    h.devices[""] = "Headset";
    await h.tick();
    expect(h.last()).toEqual(["ok", "Headset"]);
    expect(h.timers.size).toBe(0);
  });

  it("an unplugged microphone is lost and reopened", async () => {
    const h = harness();
    await h.cap.start();
    h.opened[0].unplug();
    expect(h.last()[0]).toBe("lost");
    await h.tick();
    expect(h.last()).toEqual(["ok", "Default mic"]);
    expect(h.opened.length).toBe(2);
    h.opened[0].emit();                               // the dead one has nothing to say any more
    h.opened[1].emit();
    expect(h.sent.length).toBe(1);
  });

  it("stop turns it off, and an open that resolves late is thrown away", async () => {
    const h = harness();
    const starting = h.cap.start();
    h.cap.stop();
    await starting;
    expect(h.cap.state).toBe("off");
    expect(h.opened[0].stopped).toBe(true);
    h.opened[0].emit();
    expect(h.sent.length).toBe(0);
  });

  it("never swaps devices while the talk key is held", async () => {
    const h = harness();
    h.cap.chosen = "usb";
    await h.cap.start();                               // fallback
    h.devices.usb = "USB mic";
    h.setBusy(true);
    await h.cap.devicesChanged();
    expect(h.last()[0]).toBe("fallback");             // deferred, not done under the sentence
    expect(h.timers.size).toBe(1);
    h.setBusy(false);
    await h.tick();
    expect(h.last()).toEqual(["ok", "USB mic"]);
  });

  it("a new choice from the panel applies at once", async () => {
    const h = harness({ "": "Default mic", usb: "USB mic" });
    await h.cap.start();
    await h.cap.setDevice("usb");
    expect(h.last()).toEqual(["ok", "USB mic"]);
    expect(h.opened[0].stopped).toBe(true);
  });
});
