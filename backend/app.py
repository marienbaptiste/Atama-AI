"""The orchestrator's web face: static files plus one WebSocket (spec §8).

Minimal on purpose. The conversation still runs where it already worked — mic, VAD, Whisper, the
Claude subprocess, VOICEVOX — and this only moves the *output* into the browser so TalkingHead
can lip-sync it. The student still pushes to talk; they simply watch her answer instead of
listening to a terminal.

Loopback only, always (ADR-017). The socket carries the raw microphone stream and the student's
SRS profile, so binding it anywhere else is a privacy hole rather than a convenience.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import traceback
from pathlib import Path
from typing import Any, Callable

import uvicorn
from starlette.applications import Starlette
from starlette.responses import RedirectResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from backend import config, models, settings_view
from backend.speaker import Speech

STATIC_DIR = config.REPO_ROOT / "frontend" / "public"
#: The page reconnects when it has heard nothing for 3 heartbeats (WATCHDOG_MS in preview.html).
#: Levels and audio usually arrive far more often; this covers a quiet link, e.g. no microphone.
HEARTBEAT_S = 4.0
#: How long her voice waits for a connected page to be started (clicked) before it is dropped.
READY_WAIT_S = 600.0


class Hub:
    """Every connected browser. Usually one; a second tab is not worth forbidding.

    Sends are best-effort: a browser that has gone away must never be able to stall a turn, so a
    failed send drops that client rather than propagating. The conversation is the thing that
    matters, and it is happening on the server.
    """

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        #: Pages the student has touched, which the browser therefore lets play sound.
        self._ready: set[WebSocket] = set()
        #: Set once a browser has ever connected, so the REPL can say whether anyone is watching.
        self.seen_client = False
        #: Called with a client->server control action ("start" / "stop" / ...).
        self.on_control: Callable[[str], None] | None = None
        #: The last status per service, replayed to a page that connects later: a microphone
        #: that went missing before the tab opened would otherwise show nothing at all.
        self.last_status: dict[str, dict[str, Any]] = {}
        #: Called with the keys a settings save wrote, so live keys can take effect at once.
        self.on_settings: Callable[[list[str]], None] | None = None

    async def join(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)
        self.seen_client = True

    def leave(self, ws: WebSocket) -> None:
        self._clients.discard(ws)
        self._ready.discard(ws)

    def mark_ready(self, ws: WebSocket) -> None:
        if ws in self._clients:
            self._ready.add(ws)

    @property
    def watching(self) -> int:
        return len(self._clients)

    async def send(self, message: dict[str, Any], to: set[WebSocket] | None = None) -> None:
        for ws in list(self._clients if to is None else to):
            try:
                await ws.send_json(message)
            except Exception as exc:     # noqa: BLE001 - a dead socket is not a turn's problem
                # Say why, and CLOSE the socket. This used to drop the page from the list but
                # leave its socket open: a zombie. The page believed it was connected, never
                # reconnected, and received nothing more — after a mic replug its status line
                # stayed on "disconnected" and her replies never arrived, while the backend had
                # recovered (2026-09-10). Closed, the page's onclose fires and it reconnects.
                print(f"\n[page] a send failed ({type(exc).__name__}: {exc}) - closing that "
                      f"page's socket so it reconnects", flush=True)
                self.leave(ws)
                with contextlib.suppress(Exception):
                    await ws.close(code=1011)

    async def heartbeat(self) -> None:
        """Proof of life for the page's watchdog (preview.html). Not kept in `last_status`."""
        beat = models.ServiceStatus(service="orchestrator", state="ok", detail="heartbeat").model_dump()
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            await self.send(beat)

    # ---------------------------------------------------------------- messages
    async def state(self, name: str) -> None:
        await self.send(models.State(state=name).model_dump())

    async def transcript(self, text: str, accepted: bool = True, reason: str = "") -> None:
        await self.send(models.SttFinal(text=text, accepted=accepted, reason=reason).model_dump())

    async def speak(self, speech: Speech, turn: int = 0) -> float:
        """Send one sentence for the browser to play. Returns its duration in seconds.

        The viseme timeline has already been fitted to the real WAV (`tts_voicevox.say`), and the
        times are milliseconds — which is what TalkingHead's `speakAudio` wants, verified in V0.4.
        Nothing is converted here; that is the point of having fitted it upstream.
        """
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
        ).model_dump(), to=self._ready)
        return speech.duration_ms / 1000.0

    async def level(self, level: float, speech: float) -> None:
        if self._clients:            # nobody watching: do not even build the message
            await self.send(models.MicLevel(level=round(level, 5), speech=round(speech, 3)).model_dump())

    async def timing(self, **fields: Any) -> None:
        """One turn's stage breakdown, plus the session's rolling p50/p90 (spec §10)."""
        await self.send(models.Timing(**fields).model_dump())

    async def push_settings(self) -> None:
        """Send every page a fresh settings echo — e.g. the device list just changed."""
        echo = await asyncio.to_thread(settings_view.snapshot)
        await self.send(models.Settings(**echo).model_dump())

    async def bargein(self, turn: int = 0) -> None:
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


def build(hub: Hub) -> Starlette:
    async def socket(ws: WebSocket) -> None:
        await hub.join(ws)
        try:
            # The panel is built from this; secrets are already {set, hint} (ADR-022).
            await ws.send_json(models.Settings(**settings_view.snapshot()).model_dump())
            for status in list(hub.last_status.values()):
                await ws.send_json(status)
            while True:
                raw = await ws.receive_json()
                try:
                    message = models.ClientMessageAdapter.validate_python(raw)
                except Exception as exc:      # noqa: BLE001 - a bad frame must not kill the socket
                    await ws.send_json(models.Error(message=f"unrecognised message: {exc}")
                                       .model_dump())
                    continue
                try:
                    if isinstance(message, models.Control) and message.action == "ready":
                        hub.mark_ready(ws)
                    elif isinstance(message, models.Control) and hub.on_control is not None:
                        hub.on_control(message.action)
                    elif isinstance(message, models.SettingsUpdate):
                        # A file write, off the event loop: a turn in flight must not wait on it.
                        echo = await asyncio.to_thread(settings_view.apply, message.values)
                        await ws.send_json(models.Settings(**echo).model_dump())
                        if echo["saved"] and hub.on_settings is not None:
                            hub.on_settings(echo["saved"])
                except WebSocketDisconnect:
                    raise
                except Exception as exc:  # noqa: BLE001 - a handler bug must not end the socket
                    # Uncaught, this ended the handler, and with it the page's link, over one
                    # bad press. Say what broke, on both screens, and keep listening.
                    traceback.print_exc()
                    await ws.send_json(models.Error(message=f"{type(exc).__name__}: {exc}"[:300])
                                       .model_dump())
        except WebSocketDisconnect:
            pass
        finally:
            hub.leave(ws)

    async def index(_request) -> RedirectResponse:
        return RedirectResponse("/preview.html")

    return Starlette(routes=[
        Route("/", index),
        WebSocketRoute("/ws", socket),
        Mount("/", StaticFiles(directory=str(STATIC_DIR), html=True)),
    ])


async def serve(hub: Hub, cfg) -> tuple[asyncio.Task, str]:
    """Run the server on the configured loopback address. Returns (task, url)."""
    host, port = str(cfg.HOST), int(cfg.PORT)
    # timeout_graceful_shutdown matters: uvicorn otherwise waits indefinitely for open
    # connections to close, and the browser's WebSocket is exactly such a connection.
    server = uvicorn.Server(uvicorn.Config(build(hub), host=host, port=port,
                                           log_level="warning", ws="websockets",
                                           timeout_graceful_shutdown=2))
    task = asyncio.create_task(server.serve())
    task.server = server  # type: ignore[attr-defined]  - shutdown() needs it to exit gracefully
    task.beat = asyncio.create_task(hub.heartbeat())  # type: ignore[attr-defined]
    # uvicorn sets `started` once the socket is bound; waiting on it means the URL we print is
    # true rather than hopeful, and a port clash surfaces here instead of as a blank browser tab.
    for _ in range(200):
        if getattr(server, "started", False):
            break
        await asyncio.sleep(0.05)
    return task, f"http://{host}:{port}/preview.html"


async def shutdown(task: asyncio.Task) -> None:
    """Graceful first, cancel only as a fallback.

    Cancelling the server task outright tears it down mid-await and prints a CancelledError
    traceback. Now that the page has a stop button, shutdown is the normal path rather than the
    exception, so every stop would end in a traceback. Asking uvicorn to exit closes sockets and
    finishes handlers instead; cancellation remains for a server that will not go quietly.
    """
    beat = getattr(task, "beat", None)
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
