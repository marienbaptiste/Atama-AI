"""The orchestrator's web face: static files plus one WebSocket (spec §8).

Minimal on purpose. The conversation still runs where it already worked — mic, VAD, Whisper, the
Claude subprocess, VOICEVOX — and this only moves the *output* into the browser so TalkingHead
can lip-sync it. The student still pushes to talk; they simply watch her answer instead of
listening to a terminal.

Loopback only, always (ADR-017). The socket carries every transcript, her audio and the settings
panel, so binding it anywhere else is a privacy hole rather than a convenience. Binding to
loopback is not enough on its own, though: any web page open in the same browser can open a
socket to 127.0.0.1, so the handshake also checks the `Origin` header (2026-09-12) — only the
page served from here, or by Vite's dev server, is let in.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import socket
import sys
import traceback
from collections import deque
from pathlib import Path
from typing import Any, Callable

import uvicorn
from starlette.applications import Starlette
from starlette.responses import FileResponse, HTMLResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from backend import config, models, settings_view
from backend.speaker import Speech

STATIC_DIR = config.REPO_ROOT / "frontend" / "public"
#: The built page (`npm --prefix frontend run build`, ADR-009). Only its index and /assets come
#: from here; the avatar, the cast and the preview audio stay in public/ and are served from there,
#: so regenerating them never needs a rebuild. `.\run` (backend/tools/up.py) builds it first.
DIST_DIR = config.REPO_ROOT / "frontend" / "dist"
#: What / answers when the page was never built — e.g. the orchestrator started by hand. A bare
#: 404 would look like a broken server; this says what to do.
NOT_BUILT = ("<!doctype html><meta charset=utf-8><title>Page not built</title>"
             "<body style='font:15px system-ui;margin:3em;line-height:1.6'>"
             "<h1>The page is not built yet</h1>"
             "<p>Start with <code>.\\run</code> (Windows) or <code>make run</code>, which builds it, "
             "or build it yourself: <code>cd frontend</code>, <code>npm ci</code>, "
             "<code>npm run build</code> — then reload.</p>")
#: The page reconnects when it has heard nothing for 3 heartbeats (WATCHDOG_MS in src/ws.ts).
#: Levels and audio usually arrive far more often; this covers a quiet link, e.g. no microphone.
HEARTBEAT_S = 4.0
#: How long her voice waits for a connected page to be started (clicked) before it is dropped.
READY_WAIT_S = 600.0
#: The orchestrator's port when nothing is configured (config.py's schema default).
DEFAULT_PORT = int(next(s.default for s in config.SCHEMA if s.key == "PORT"))
#: Vite's dev server (frontend/vite.config.ts `server.port`; `npm run dev`). It proxies /ws to
#: the orchestrator and forwards the browser's Origin unchanged, so the page it serves arrives
#: with this port in its origin. A test asserts the two files agree.
DEV_PORT = 5173
#: Message types a slow page may lose: only the newest of each is kept in its outbox, because a
#: microphone level from a second ago is worthless once a newer one exists. Everything else — a
#: sentence, a state, a barge-in, a settings echo — is delivered, in order, however slow the page.
COALESCED = frozenset({"mic_level"})
#: A page whose outbox holds this much has stopped reading. It is closed so it reconnects, the
#: same recovery as a failed send; a real browser on loopback drains thousands a second.
OUTBOX_MAX = 1000


def allowed_origins(port: int) -> frozenset[str]:
    """Where the page may be served from: this port and Vite's, on the three loopback names."""
    return frozenset(f"http://{host}:{p}" for host in ("127.0.0.1", "localhost", "[::1]")
                     for p in {int(port), DEV_PORT})


def is_loopback(host: str | None) -> bool:
    try:
        return ipaddress.ip_address(str(host).strip("[]")).is_loopback
    except ValueError:
        return False


def origin_ok(origin: str | None, client_host: str | None, allowed: frozenset[str]) -> bool:
    """The handshake's Origin, compared whole (scheme, host and port) against `allowed`.

    A browser always sends one, so a header that is not on the list — another site's, or the
    `null` of a sandboxed frame — is refused. No header at all is not a browser: a test client
    or a script. Those are admitted only from the loopback address itself.
    """
    if origin is None:
        return is_loopback(client_host)
    return origin.strip().lower() in allowed


class _Client:
    """One page: its socket and the queue of what it has not read yet (`Hub.send`)."""

    __slots__ = ("ws", "outbox", "wake", "pump", "busy")

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.outbox: deque[dict[str, Any]] = deque()
        self.wake = asyncio.Event()
        self.pump: asyncio.Task | None = None
        self.busy = False


class Hub:
    """Every connected browser. Usually one; a second tab is not worth forbidding.

    Sends are best-effort and never block the conversation: each page has its own outbox and its
    own sender task, so a browser that has gone away — or one that is merely slow — can neither
    stall a turn nor delay another page. A failed send drops that client rather than propagating.
    The conversation is the thing that matters, and it is happening on the server.
    """

    def __init__(self) -> None:
        self._clients: dict[WebSocket, _Client] = {}
        #: Pages the student has touched, which the browser therefore lets play sound.
        self._ready: set[WebSocket] = set()
        #: Called with a client->server control action ("start" / "stop" / ...).
        self.on_control: Callable[[str], None] | None = None
        #: The page holding the talk key right now (a `control: start` not yet followed by its
        #: `stop` or `cancel`). If that page drops off the socket mid-hold, `leave` cancels the
        #: hold for it: the voice loop refuses a new press while one is open, so an unfinished
        #: hold would otherwise wedge the microphone until a restart (found 2026-09-12).
        self._holder: WebSocket | None = None
        #: The last status per service, replayed to a page that connects later: a microphone
        #: that went missing before the tab opened would otherwise show nothing at all.
        self.last_status: dict[str, dict[str, Any]] = {}
        #: The last `state` sent, replayed to a page that connects later so its own interrupt
        #: logic reasons on the real state rather than on "listening" by default.
        self.last_state: str | None = None
        #: Called with the keys a settings save wrote, so live keys can take effect at once.
        self.on_settings: Callable[[list[str]], None] | None = None
        #: The status bar's latest gauges, merged, replayed to a page that connects later.
        self.last_meters: dict[str, Any] = {}
        #: The turn epoch (spec §8 barge-in). Up by one when a turn starts and when she is
        #: interrupted; every `state` and `speak` carries it, and `bargein` closes it. The page
        #: then drops a sentence of an interrupted turn however late it arrives — without this
        #: every sentence said turn 0 and nothing could tell stale audio from fresh.
        self.epoch = 0
        #: (kind, text, context, lang) -> (answer, error): a click on a grammar point or a
        #: translate icon (backend/explain.py). None until the REPL wires it.
        self.explain: Callable[[str, str, str, str], Any] | None = None
        #: Background jobs started for a page; kept referenced so none is collected mid-flight.
        self._jobs: set[asyncio.Task] = set()
        #: text -> furigana for the chat (backend/annotate.py `Annotator.readings`), or None.
        self.readings: Callable[[str], list[dict[str, Any]]] | None = None

        #: (text, marks) -> the marks that are really grammar, red being for grammar and not for a
        #: word the tutor liked (`Annotator.grammar_only`). None until the REPL wires it.
        self.grammar: Callable[[str, list[dict[str, Any]]], list[dict[str, Any]]] | None = None

        #: True once the page's stop button has been pressed: the REPL then takes the containers
        #: down as well, which is what that button means (user, 2026-09-12).
        self.quit_requested = False

        #: The student's own study list (backend/study.py): their words in a sentence, blue on the
        #: page, and what kind of thing a `[used:…]` names. Replaced on a Refresh.
        self.study: Any = None

    def _grammar(self, text: str, speech: Any) -> list[dict[str, Any]]:
        """Her grammar marks for the page, minus any the guard reads as plain vocabulary."""
        marks = [{"start": g.start, "end": g.end, "point": g.point}
                 for g in getattr(speech, "grammar", ())]
        try:
            return self.grammar(text, marks) if self.grammar is not None else marks
        except Exception:  # noqa: BLE001 - same contract as the readings hook
            return marks

    def _vocab(self, text: str) -> list[dict[str, Any]]:
        """Words the student is still learning, wherever they appear. Never raises."""
        try:
            return self.study.spans(text) if self.study is not None else []
        except Exception:  # noqa: BLE001 - a colour is never worth a lost sentence
            return []

    def _used_kind(self, used: str) -> str:
        try:
            return self.study.kind_of(used) if (self.study is not None and used) else ""
        except Exception:  # noqa: BLE001
            return ""

    def _readings(self, text: str) -> list[dict[str, Any]]:
        try:
            return self.readings(text) if self.readings is not None else []
        except Exception:  # noqa: BLE001 - furigana must never cost a sentence
            return []

    # ---------------------------------------------------------------- clients
    def attach(self, ws: WebSocket) -> None:
        """Register an accepted socket. Its sender starts with its first message."""
        self._clients.setdefault(ws, _Client(ws))

    async def join(self, ws: WebSocket) -> None:
        await ws.accept()
        self.attach(ws)

    def leave(self, ws: WebSocket) -> None:
        client = self._clients.pop(ws, None)
        self._ready.discard(ws)
        if client is not None and client.pump is not None and client.pump is not _current_task():
            client.pump.cancel()
        if self._holder is ws:
            # The key was down when the page went: end the hold as the page's own ALT GR would,
            # so the next press starts clean instead of being refused.
            self._holder = None
            self._relay("cancel")

    def mark_ready(self, ws: WebSocket) -> None:
        if ws in self._clients:
            self._ready.add(ws)

    def control(self, ws: WebSocket, action: str) -> None:
        """A page's `control` message: turn-taking edges and session actions (spec §8)."""
        if action == "ready":
            self.mark_ready(ws)
            return
        if action == "start":
            self._holder = ws
        elif action in ("stop", "cancel") and self._holder is ws:
            self._holder = None
        self._relay(action)

    def _relay(self, action: str) -> None:
        if self.on_control is not None:
            self.on_control(action)

    # ---------------------------------------------------------------- delivery
    async def send(self, message: dict[str, Any], to: set[WebSocket] | None = None) -> None:
        """Queue `message` for every page (or the pages in `to`). Never waits on a page."""
        targets = list(self._clients.values()) if to is None else \
            [c for ws in to if (c := self._clients.get(ws)) is not None]
        for client in targets:
            self._enqueue(client, message)

    def _enqueue(self, client: _Client, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind in COALESCED and any(m.get("type") == kind for m in client.outbox):
            client.outbox = deque(m for m in client.outbox if m.get("type") != kind)
        client.outbox.append(message)
        if len(client.outbox) > OUTBOX_MAX:
            self.spawn(self._close(client, f"{len(client.outbox)} messages unread"))
            return
        client.wake.set()
        if client.pump is None:
            client.pump = asyncio.get_running_loop().create_task(self._pump(client))

    async def _pump(self, client: _Client) -> None:
        """One page's sender: its messages, in order, as fast as it takes them."""
        while True:
            if not client.outbox:
                client.wake.clear()
                await client.wake.wait()
                continue
            message = client.outbox.popleft()
            client.busy = True
            try:
                await client.ws.send_json(message)
            except Exception as exc:     # noqa: BLE001 - a dead socket is not a turn's problem
                # Say why, and CLOSE the socket. This used to drop the page from the list but
                # leave its socket open: a zombie. The page believed it was connected, never
                # reconnected, and received nothing more — after a mic replug its status line
                # stayed on "disconnected" and her replies never arrived, while the backend had
                # recovered (2026-09-10). Closed, the page's onclose fires and it reconnects.
                await self._close(client, f"a send failed ({type(exc).__name__}: {exc})")
                return
            finally:
                client.busy = False

    async def _close(self, client: _Client, why: str) -> None:
        print(f"\n[page] {why} - closing that page's socket so it reconnects", flush=True)
        self.leave(client.ws)
        with contextlib.suppress(Exception):
            await client.ws.close(code=1011)

    async def drain(self) -> None:
        """Wait until every page has been sent everything queued so far (tests, shutdown)."""
        while any(c.outbox or c.busy for c in self._clients.values()):
            await asyncio.sleep(0.001)

    async def heartbeat(self) -> None:
        """Proof of life for the page's watchdog (src/ws.ts). Not kept in `last_status`."""
        beat = models.ServiceStatus(service="orchestrator", state="ok", detail="heartbeat").model_dump()
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            await self.send(beat)

    # ---------------------------------------------------------------- messages
    async def state(self, name: str) -> None:
        if name == "thinking":
            self.epoch += 1          # a new turn: its sentences outrank anything stopped before
        self.last_state = name
        await self.send(models.State(state=name, turn=self.epoch).model_dump())

    async def transcript(self, text: str, accepted: bool = True, reason: str = "") -> None:
        await self.send(models.SttFinal(text=text, accepted=accepted, reason=reason,
                                        readings=self._readings(text),
                                        vocab=self._vocab(text)).model_dump())

    async def speak(self, speech: Speech, turn: int | None = None) -> float:
        """Send one sentence for the browser to play. Returns its duration in seconds.

        The viseme timeline has already been fitted to the real WAV (`tts_voicevox.say`), and the
        times are milliseconds — which is what TalkingHead's `speakAudio` wants, verified in V0.4.
        Nothing is converted here; that is the point of having fitted it upstream.

        `turn` defaults to the epoch at the moment the sentence was handed over, not when it is
        finally sent: a sentence held for an unstarted page still belongs to the turn it came from.
        """
        turn = self.epoch if turn is None else turn
        # A page cannot play sound until the student has clicked or pressed a key on it (browser
        # autoplay policy). Audio sent before that was lost — her whole opening greeting went
        # unheard (2026-09-10). So while a page is connected but not yet started, wait for it;
        # with no page at all there is nobody to wait for, exactly as before.
        waited = 0.0
        while self._clients and not self._ready and waited < READY_WAIT_S:
            if waited == 0.0:
                print("\n[page] her voice is waiting for you to click the page (browsers only "
                      "play sound after you have touched it)", flush=True)
            await asyncio.sleep(0.1)
            waited += 0.1
        timeline = speech.timeline.as_message()
        await self.send(models.Speak(
            audio_b64=base64.b64encode(speech.wav).decode(),
            visemes=timeline["visemes"],
            vtimes=timeline["vtimes"],
            vdurations=timeline["vdurations"],
            text=speech.text,
            emotion=speech.emotion,
            turn=turn,
            grammar=self._grammar(speech.text, speech),
            target=getattr(speech, "target", ""),
            used=getattr(speech, "used", ""),
            used_kind=self._used_kind(getattr(speech, "used", "")),
            readings=self._readings(speech.text),
            vocab=self._vocab(speech.text),
        ).model_dump(), to=self._ready)
        return speech.duration_ms / 1000.0

    async def level(self, level: float, speech: float) -> None:
        if self._clients:            # nobody watching: do not even build the message
            await self.send(models.MicLevel(level=round(level, 5), speech=round(speech, 3)).model_dump())

    async def timing(self, **fields: Any) -> None:
        """One turn's stage breakdown, plus the session's rolling p50/p90 (spec §10)."""
        await self.send(models.Timing(**fields).model_dump())

    def follow(self, registry, loop: asyncio.AbstractEventLoop) -> None:
        """Forward every service change (spec §5b) to the pages, starting with what is known now.

        The registry reports from whichever thread noticed — the brain's reader thread, a fetch
        worker — so each change is handed to the event loop rather than sent from that thread.
        """
        def on_change(st) -> None:
            try:
                loop.call_soon_threadsafe(lambda: loop.create_task(
                    self.status(st.service, st.state, st.detail, st.last_error)))
            except RuntimeError:
                pass                                  # the loop has closed: shutting down

        registry.subscribe(on_change)
        for msg in registry.snapshot().values():
            loop.create_task(self.status(msg["service"], msg["state"], msg["detail"], msg["last_error"]))

    async def status_heartbeat(self, registry, every_s: float) -> None:
        """Every service at least every `every_s`, changed or not (spec §5b: "at least every 30 s")."""
        while True:
            await asyncio.sleep(every_s)
            for msg in registry.snapshot().values():
                await self.status(msg["service"], msg["state"], msg["detail"], msg["last_error"])

    async def meters(self, **fields: Any) -> None:
        """Update some gauges and send them all: sources report at different times."""
        self.last_meters.update({k: v for k, v in fields.items() if v is not None})
        await self.send(models.Meters(**self.last_meters).model_dump())

    async def push_settings(self) -> None:
        """Send every page a fresh settings echo — e.g. the device list just changed."""
        echo = await asyncio.to_thread(settings_view.snapshot)
        await self.send(models.Settings(**echo).model_dump())

    def spawn(self, coro) -> None:
        """Run a job for one page without blocking the socket loop — an explanation takes seconds."""
        task = asyncio.get_running_loop().create_task(coro)
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)

    async def answer(self, ws: WebSocket, ask: models.Explain) -> None:
        """One click's explanation or translation (spec §8b). The lesson carries on meanwhile, and
        a failure comes back as a message rather than a spinner that never stops."""
        answer, error = "", "explanations are not available in this session"
        if self.explain is not None:
            answer, error = await self.explain(ask.kind, ask.text, ask.context, ask.lang)
        await self.send(models.Explanation(kind=ask.kind, text=ask.text, answer=answer,
                                           error=error).model_dump(), to={ws})

    async def bargein(self) -> None:
        """She was interrupted (spec §8): every page stops now and drops what is left of this
        turn. Closes the epoch, so whatever she says next is never mistaken for it. Without this
        the page played on to the end of the sentence it had (found 2026-09-11)."""
        turn, self.epoch = self.epoch, self.epoch + 1
        await self.send(models.BargeIn(turn=turn).model_dump())

    async def status(self, service: str, state: str, detail: str = "", last_error: str = "",
                     remember: bool = True) -> None:
        """`remember=False` for events rather than states (a push-to-talk acknowledgement):
        replaying those to a page that connects later would describe a press it never made."""
        message = models.ServiceStatus(service=service, state=state, detail=detail,
                                       last_error=last_error).model_dump()
        if remember:
            self.last_status[service] = message
        await self.send(message)

    async def welcome(self, ws: WebSocket) -> None:
        """What a page needs the moment it connects: the settings panel, every service's last
        status, the gauges, and the state she is in — so a page that reconnects mid-turn knows
        whether a press is an interruption."""
        # The panel is built from this; secrets are already {set, hint} (ADR-022).
        await self.send(models.Settings(**settings_view.snapshot()).model_dump(), to={ws})
        for status in list(self.last_status.values()):
            await self.send(status, to={ws})
        if self.last_meters:
            await self.send(models.Meters(**self.last_meters).model_dump(), to={ws})
        if self.last_state is not None:
            await self.send(models.State(state=self.last_state, turn=self.epoch).model_dump(), to={ws})


def _current_task() -> asyncio.Task | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def build(hub: Hub, port: int = DEFAULT_PORT) -> Starlette:
    allowed = allowed_origins(port)

    async def socket(ws: WebSocket) -> None:
        # Before accept: starlette turns a close here into a refused handshake (HTTP 403), so a
        # foreign page never gets a socket at all. 1008 is "policy violation".
        if not origin_ok(ws.headers.get("origin"), ws.client.host if ws.client else None, allowed):
            await ws.close(code=1008)
            return
        await hub.join(ws)
        try:
            await hub.welcome(ws)
            while True:
                try:
                    raw = await ws.receive_json()
                except (ValueError, KeyError):   # not JSON, or not a text frame: say so, carry on
                    await hub.send(models.Error(message="unrecognised message: not JSON").model_dump(),
                                   to={ws})
                    continue
                try:
                    message = models.ClientMessageAdapter.validate_python(raw)
                except Exception as exc:      # noqa: BLE001 - a bad frame must not kill the socket
                    await hub.send(models.Error(message=f"unrecognised message: {exc}").model_dump(),
                                   to={ws})
                    continue
                try:
                    if isinstance(message, models.Control):
                        hub.control(ws, message.action)
                    elif isinstance(message, models.Explain):
                        hub.spawn(hub.answer(ws, message))
                    elif isinstance(message, models.SettingsUpdate):
                        # A file write, off the event loop: a turn in flight must not wait on it.
                        echo = await asyncio.to_thread(settings_view.apply, message.values)
                        await hub.send(models.Settings(**echo).model_dump(), to={ws})
                        if echo["saved"] and hub.on_settings is not None:
                            hub.on_settings(echo["saved"])
                except WebSocketDisconnect:
                    raise
                except Exception as exc:  # noqa: BLE001 - a handler bug must not end the socket
                    # Uncaught, this ended the handler, and with it the page's link, over one
                    # bad press. Say what broke, on both screens, and keep listening.
                    traceback.print_exc()
                    await hub.send(models.Error(message=f"{type(exc).__name__}: {exc}"[:300])
                                   .model_dump(), to={ws})
        except WebSocketDisconnect:
            pass
        finally:
            hub.leave(ws)

    async def index(_request):
        page = DIST_DIR / "index.html"
        if page.exists():
            # no-cache: a rebuilt page must never be shadowed by yesterday's copy in the browser.
            return FileResponse(page, headers={"Cache-Control": "no-cache"})
        return HTMLResponse(NOT_BUILT, status_code=503)

    return Starlette(routes=[
        Route("/", index),
        WebSocketRoute("/ws", socket),
        Mount("/assets", StaticFiles(directory=str(DIST_DIR / "assets"), check_dir=False)),
        Mount("/", StaticFiles(directory=str(STATIC_DIR), html=True)),
    ])


def page_url(host: str, port: int) -> str:
    """The page to open. There is one page (the prototype was removed, 2026-09-11)."""
    return f"http://{host}:{port}/"


def listen(host: str, port: int) -> socket.socket:
    """Bind the server's socket here, so a port already in use is one clear error.

    uvicorn binds inside its own task and answers a clash with `sys.exit(1)`, which asyncio
    re-raises through the event loop: the whole tutor died with a bare "address already in use"
    and no hint (2026-09-12). Bound here, the failure has a message; the socket is handed to
    uvicorn, which listens on it and closes it at shutdown.
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    # SO_REUSEADDR lets a restart reuse a port still in TIME_WAIT — but on Windows it also lets
    # two servers bind the same port at once, which is exactly the clash this exists to catch.
    if sys.platform != "win32":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as exc:
        sock.close()
        raise RuntimeError(
            f"cannot listen on {page_url(host, port)} - {exc.strerror or exc}. Another copy of the "
            f"tutor is probably still running: stop it, or change PORT in Settings -> Advanced."
        ) from exc
    sock.set_inheritable(True)
    return sock


async def serve(hub: Hub, cfg, registry=None) -> tuple[asyncio.Task, str]:
    """Run the server on the configured loopback address. Returns (task, url).

    With a `registry`, every service's status reaches the pages on each change and on the
    STATUS_HEARTBEAT_S heartbeat (spec §5b, ROADMAP 16). Raises RuntimeError, with the reason,
    when the server cannot start — never a URL that nothing answers."""
    host, port = str(cfg.HOST), int(cfg.PORT)
    sock = listen(host, port)
    # timeout_graceful_shutdown matters: uvicorn otherwise waits indefinitely for open
    # connections to close, and the browser's WebSocket is exactly such a connection.
    # ws: "websockets-sansio" is the implementation uvicorn 0.52 keeps; the old "websockets" one
    # printed a deprecation warning at every launch (user, 2026-09-12). Verified present in
    # uvicorn.config.WS_PROTOCOLS for uvicorn 0.52.4 / websockets 17.1, the pinned pair.
    server = uvicorn.Server(uvicorn.Config(build(hub, port), host=host, port=port,
                                           log_level="warning", ws="websockets-sansio",
                                           timeout_graceful_shutdown=2))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    task.server = server  # type: ignore[attr-defined]  - shutdown() needs it to exit gracefully
    task.beat = asyncio.create_task(hub.heartbeat())  # type: ignore[attr-defined]
    if registry is not None:
        hub.follow(registry, asyncio.get_running_loop())
        task.status_beat = asyncio.create_task(  # type: ignore[attr-defined]
            hub.status_heartbeat(registry, float(cfg.STATUS_HEARTBEAT_S)))
    # uvicorn sets `started` once it is listening; waiting on it means the URL we print is true
    # rather than hopeful, and a server that died on the way up surfaces here, with its reason.
    for _ in range(200):
        if task.done() or getattr(server, "started", False):
            break
        await asyncio.sleep(0.05)
    if task.done() or not getattr(server, "started", False):
        await shutdown(task)
        exc = task.exception() if task.done() and not task.cancelled() else None
        raise RuntimeError(f"the page server did not start on {page_url(host, port)}"
                           + (f": {type(exc).__name__}: {exc}" if exc else "")) from exc
    return task, page_url(host, port)


async def shutdown(task: asyncio.Task) -> None:
    """Graceful first, cancel only as a fallback.

    Cancelling the server task outright tears it down mid-await and prints a CancelledError
    traceback. Now that the page has a stop button, shutdown is the normal path rather than the
    exception, so every stop would end in a traceback. Asking uvicorn to exit closes sockets and
    finishes handlers instead; cancellation remains for a server that will not go quietly.
    """
    for name in ("beat", "status_beat"):
        beat = getattr(task, name, None)
        if beat is not None:
            beat.cancel()
    server = getattr(task, "server", None)
    if server is not None:
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(task), 5)
            return
        except Exception:  # noqa: BLE001 - fall through to cancellation
            pass
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
