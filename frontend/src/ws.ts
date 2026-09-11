/** The page ↔ server link (spec §8): one WebSocket, auto-reconnect, a watchdog, typed messages.
 *
 *  Every message type comes from protocol.gen.ts, generated from backend/models.py. `Handlers`
 *  demands one handler per server message type, so a type added on the server fails `tsc` here
 *  until the page decides what to do with it (gate M3a). */
import type { ClientMessage, ServerMessage, ServerType } from "./protocol.gen";
import { SERVER_TYPES } from "./protocol.gen";

export type Handlers = {
  [K in ServerType]: (msg: Extract<ServerMessage, { type: K }>) => void | Promise<void>;
};

/** The server beats every 4 s (backend/app.py HEARTBEAT_S). Three missed is a dead link in all
 *  but name, even while the socket still reads "open" — close it and reconnect. */
export const WATCHDOG_MS = 12_000;
export const RETRY_MS = 2_000;

const KNOWN = new Set<string>(SERVER_TYPES);

/** Route one frame to its handler. False for anything that is not a known server message. */
export async function dispatch(handlers: Handlers, raw: string): Promise<boolean> {
  let msg: unknown;
  try { msg = JSON.parse(raw); } catch { return false; }
  const type = (msg as { type?: unknown } | null)?.type;
  if (typeof type !== "string" || !KNOWN.has(type)) {
    console.warn("unknown message from the server", msg);
    return false;
  }
  const handler = handlers[type as ServerType] as (m: ServerMessage) => void | Promise<void>;
  await handler(msg as ServerMessage);
  return true;
}

export interface LinkEvents {
  open(): void;
  /** `retrying`: false once the student has asked the tutor to stop. */
  close(retrying: boolean): void;
  stale?(): void;
}

export function defaultUrl(): string {
  return (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";
}

export class Link {
  private socket: WebSocket | null = null;
  private lastHeard = 0;
  /** Set when stop was pressed: the close that follows is the end, not a fault to recover from. */
  quitting = false;

  constructor(private readonly handlers: Handlers, private readonly events: LinkEvents,
              private readonly url: string = defaultUrl()) {}

  get open(): boolean { return this.socket?.readyState === WebSocket.OPEN; }

  start(): void {
    this.connect();
    window.setInterval(() => {
      if (this.open && performance.now() - this.lastHeard > WATCHDOG_MS) {
        this.events.stale?.();
        this.drop();
      }
    }, 2000);
  }

  send(msg: ClientMessage): boolean {
    if (!this.open || !this.socket) return false;
    this.socket.send(JSON.stringify(msg));
    return true;
  }

  /** Close the socket and let onclose reconnect — for a link that is dead in all but name. */
  drop(): void {
    try { this.socket?.close(); } catch { /* already gone */ }
  }

  private connect(): void {
    let socket: WebSocket;
    try { socket = new WebSocket(this.url); } catch {
      window.setTimeout(() => this.connect(), RETRY_MS);
      return;
    }
    socket.onopen = () => {
      this.socket = socket;
      this.lastHeard = performance.now();
      this.events.open();
    };
    socket.onclose = () => {
      if (this.socket === socket) this.socket = null;
      const retrying = !this.quitting;
      this.events.close(retrying);
      if (retrying) window.setTimeout(() => this.connect(), RETRY_MS);   // the tutor may not be up yet
    };
    socket.onmessage = event => {
      this.lastHeard = performance.now();
      void dispatch(this.handlers, String(event.data));
    };
  }
}
