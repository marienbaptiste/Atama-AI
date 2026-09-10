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
from pathlib import Path
from typing import Any, Callable

import uvicorn
from starlette.applications import Starlette
from starlette.responses import RedirectResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from backend import config, models
from backend.speaker import Speech

STATIC_DIR = config.REPO_ROOT / "frontend" / "public"


class Hub:
    """Every connected browser. Usually one; a second tab is not worth forbidding.

    Sends are best-effort: a browser that has gone away must never be able to stall a turn, so a
    failed send drops that client rather than propagating. The conversation is the thing that
    matters, and it is happening on the server.
    """

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        #: Set once a browser has ever connected, so the REPL can say whether anyone is watching.
        self.seen_client = False
        #: Called with a client->server control action ("start" / "stop" / ...).
        self.on_control: Callable[[str], None] | None = None

    async def join(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)
        self.seen_client = True

    def leave(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    @property
    def watching(self) -> int:
        return len(self._clients)

    async def send(self, message: dict[str, Any]) -> None:
        for ws in list(self._clients):
            try:
                await ws.send_json(message)
            except Exception:            # noqa: BLE001 - a dead socket is not a turn's problem
                self.leave(ws)

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
        timeline = speech.timeline.as_message()
        await self.send(models.Speak(
            audio_b64=base64.b64encode(speech.wav).decode(),
            visemes=timeline["visemes"],
            vtimes=timeline["vtimes"],
            vdurations=timeline["vdurations"],
            text=speech.text,
            emotion=speech.emotion,
            turn=turn,
        ).model_dump())
        return speech.duration_ms / 1000.0

    async def bargein(self, turn: int = 0) -> None:
        await self.send(models.BargeIn(turn=turn).model_dump())

    async def status(self, service: str, state: str, detail: str = "", last_error: str = "") -> None:
        await self.send(models.ServiceStatus(service=service, state=state, detail=detail,
                                             last_error=last_error).model_dump())


def build(hub: Hub) -> Starlette:
    async def socket(ws: WebSocket) -> None:
        await hub.join(ws)
        try:
            while True:
                raw = await ws.receive_json()
                try:
                    message = models.ClientMessageAdapter.validate_python(raw)
                except Exception as exc:      # noqa: BLE001 - a bad frame must not kill the socket
                    await ws.send_json(models.Error(message=f"unrecognised message: {exc}")
                                       .model_dump())
                    continue
                if isinstance(message, models.Control) and hub.on_control is not None:
                    hub.on_control(message.action)
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
