// Gate M3a, the page's half: every server type has a handler (a missing one fails `tsc` on the
// `Handlers` type), and the dispatcher routes known messages and survives anything else.
import { describe, expect, it } from "vitest";
import { SERVER_TYPES, type ServerType } from "./protocol.gen";
import { dispatch, type Handlers } from "./ws";

function recorder() {
  const seen: string[] = [];
  const handlers = Object.fromEntries(SERVER_TYPES.map(t => [t, () => { seen.push(t); }])) as unknown as Handlers;
  return { seen, handlers };
}

describe("dispatch", () => {
  it("routes every server message type to its own handler", async () => {
    const { seen, handlers } = recorder();
    for (const type of SERVER_TYPES) expect(await dispatch(handlers, JSON.stringify({ type }))).toBe(true);
    expect(seen).toEqual([...SERVER_TYPES]);
  });

  it("ignores an unknown type rather than throwing", async () => {
    const { seen, handlers } = recorder();
    expect(await dispatch(handlers, JSON.stringify({ type: "shout" }))).toBe(false);
    expect(seen).toEqual([]);
  });

  it("ignores a frame that is not JSON, or has no type", async () => {
    const { handlers } = recorder();
    expect(await dispatch(handlers, "{not json")).toBe(false);
    expect(await dispatch(handlers, "null")).toBe(false);
    expect(await dispatch(handlers, "[1,2]")).toBe(false);
  });

  it("covers the whole protocol, reserved type included", () => {
    const expected: ServerType[] = ["state", "stt_partial", "speak", "bargein", "service_status", "settings"];
    for (const t of expected) expect(SERVER_TYPES).toContain(t);
  });
});

// ------------------------------------------------------------------ the stop button (2026-09-14)
// A press that met a dropped or reconnecting socket used to send nothing and still show "Session
// ended", while the tutor and the containers ran on. Now it is re-sent until the server goes away.
import { afterEach, beforeEach, vi } from "vitest";
import { Link, RETRY_MS, STOP_ATTEMPTS } from "./ws";

class FakeSocket {
  static OPEN = 1;
  static all: FakeSocket[] = [];
  readyState = 0;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  constructor(readonly url: string) { FakeSocket.all.push(this); }
  send(data: string) { this.sent.push(data); }
  close() { this.readyState = 3; this.onclose?.(); }
  accept() { this.readyState = 1; this.onopen?.(); }       // the server answers
  refuse() { this.readyState = 3; this.onclose?.(); }      // nobody listening
}

const quits = (s: FakeSocket) => s.sent.filter(d => JSON.parse(d).action === "quit").length;

describe("the stop button", () => {
  let events: { open: number; closes: boolean[]; unreachable: number };
  let link: Link;
  const latest = () => FakeSocket.all[FakeSocket.all.length - 1];

  beforeEach(() => {
    vi.useFakeTimers();
    FakeSocket.all = [];
    vi.stubGlobal("WebSocket", FakeSocket);
    vi.stubGlobal("window", globalThis);
    events = { open: 0, closes: [], unreachable: 0 };
    link = new Link({} as Handlers, {
      open: () => { events.open++; },
      close: retrying => { events.closes.push(retrying); },
      unreachable: () => { events.unreachable++; },
    }, "ws://test/ws");
    link.start();
  });
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

  it("sends at once on an open socket, and ends only when the server has gone", () => {
    latest().accept();
    link.requestStop();
    expect(quits(latest())).toBe(1);
    latest().close();                                     // the server shuts down
    expect(events.closes).toEqual([true]);                // not "ended" yet: it checks the server is gone
    vi.advanceTimersByTime(RETRY_MS);
    latest().refuse();                                    // nobody there any more
    expect(events.closes).toEqual([true, false]);         // now it is the end
  });

  it("a press while reconnecting is delivered on the next open, not dropped", () => {
    latest().accept();
    latest().close();                                     // the link drops (a missed heartbeat)
    link.requestStop();                                   // pressed in the gap
    expect(events.closes.at(-1)).toBe(true);
    vi.advanceTimersByTime(RETRY_MS);
    latest().accept();                                    // the server is still there
    expect(quits(latest())).toBe(1);
    expect(events.open).toBe(1);                          // the reconnect is not shown as "connected"
  });

  it("re-sends on reconnect if the server is still alive after the first one", () => {
    latest().accept();
    link.requestStop();
    latest().close();                                     // dropped before the server acted on it
    vi.advanceTimersByTime(RETRY_MS);
    latest().accept();                                    // still alive: tell it again
    expect(quits(latest())).toBe(1);
  });

  it("says the tutor is unreachable instead of claiming it stopped", () => {
    latest().accept();
    latest().close();
    link.requestStop();
    for (let i = 0; i < STOP_ATTEMPTS; i++) {
      vi.advanceTimersByTime(RETRY_MS);
      latest().refuse();
    }
    expect(events.unreachable).toBe(1);
    expect(events.closes).not.toContain(false);           // never "Session ended"
  });
});
