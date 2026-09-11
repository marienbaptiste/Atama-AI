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
