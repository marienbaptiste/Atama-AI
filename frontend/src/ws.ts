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
/** Reconnects a stop press may try, while the server has not had it yet, before the page says it
 *  could not reach the tutor (about 10 s at RETRY_MS). */
export const STOP_ATTEMPTS = 5;
/** Microphone frames still unsent on the socket beyond which new ones are dropped instead of
 *  queued (about 8 s of audio): a socket that far behind is dead in all but name, and live audio
 *  is worth nothing late. The watchdog above closes it. */
export const BACKLOG_BYTES = 256 * 1024;

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
  /** `retrying`: false once the tutor has stopped on request — the end, not a fault. */
  close(retrying: boolean): void;
  stale?(): void;
  /** Stop was pressed and the tutor could not be reached to be told. */
  unreachable?(): void;
}

export function defaultUrl(): string {
  return (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";
}

export class Link {
  private socket: WebSocket | null = null;
  private lastHeard = 0;
  /** Stop was pressed. It stays pressed until the server has had it: a press that met a dropped
   *  or reconnecting socket used to send nothing and still show "Session ended", while the tutor
   *  and the containers ran on (user, 2026-09-14). */
  private stopping = false;
  /** The stop went out on an open socket at least once. */
  private stopDelivered = false;
  private failedConnects = 0;

  constructor(private readonly handlers: Handlers, private readonly events: LinkEvents,
              private readonly url: string = defaultUrl()) {}

  get open(): boolean { return this.socket?.readyState === WebSocket.OPEN; }

  get quitting(): boolean { return this.stopping; }

  /** The page's stop button. Sent now if the socket is open, and again on every reconnect until the
   *  server goes away — which is how the page knows it was heard. */
  requestStop(): void {
    this.stopping = true;
    this.deliverStop();
  }

  private deliverStop(): void {
    if (this.stopping && this.send({ type: "control", action: "quit" })) this.stopDelivered = true;
  }

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

  /** One microphone frame (ADR-040), the socket's one binary message. False — and the frame is
   *  dropped — when there is no open socket or it has stopped draining (BACKLOG_BYTES). */
  sendBytes(frame: ArrayBuffer): boolean {
    if (!this.open || !this.socket || (this.socket.bufferedAmount ?? 0) > BACKLOG_BYTES) return false;
    this.socket.send(frame);
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
    let opened = false;
    socket.onopen = () => {
      opened = true;
      this.failedConnects = 0;
      this.socket = socket;
      this.lastHeard = performance.now();
      if (this.stopping) { this.deliverStop(); return; }   // a reconnect made only to be told to stop
      this.events.open();
    };
    socket.onclose = () => {
      if (this.socket === socket) this.socket = null;
      if (this.stopping && !opened) {
        // A reconnect that found nobody. After the server had the stop, that IS the stop; before
        // it, the tutor is unreachable, and the page must say so rather than claim it stopped.
        if (this.stopDelivered) { this.events.close(false); return; }
        if (++this.failedConnects >= STOP_ATTEMPTS) { this.events.unreachable?.(); return; }
      }
      this.events.close(true);
      window.setTimeout(() => this.connect(), RETRY_MS);   // the tutor may not be up yet, or not gone yet
    };
    socket.onmessage = event => {
      this.lastHeard = performance.now();
      void dispatch(this.handlers, String(event.data));
    };
  }
}
