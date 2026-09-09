"""VOICEVOX TTS client (spec §7, ADR-005).

Sentence in, WAV plus viseme timeline out. Synthesis happens sentence by sentence as the chunker
closes them (ADR-008), never for a whole reply — the first sentence must reach the speakers while
the rest is still being generated.

Verified live against VOICEVOX 0.25.2 on 2026-09-09 (ROADMAP V0.3):
  POST /audio_query?speaker=<id>&text=<text>  -> the query object (mora timings)
  POST /synthesis?speaker=<id>  body: that object -> audio/wav, 24 kHz mono
  GET  /speakers -> [{name, speaker_uuid, styles: [{id, name, type}]}]
  GET  /version  -> "0.25.2"
The engine stays on CPU (ADR-005): the GPU belongs to Whisper.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from backend import emotions as emotions_mod
from backend import visemes as visemes_mod
from backend.chunker import NEUTRAL


@dataclass
class Speech:
    """One synthesised sentence, ready for the browser (spec §8 `speak`)."""

    text: str
    emotion: str
    wav: bytes
    timeline: visemes_mod.VisemeTimeline
    style_id: int
    synth_ms: float = 0.0

    @property
    def duration_ms(self) -> float:
        return self.timeline.duration_ms


class VoicevoxError(RuntimeError):
    pass


@dataclass
class VoicevoxClient:
    """Talks to the local engine. One instance per session."""

    base_url: str
    speaker: int = 29
    speed: float = 0.9
    intonation: float = 1.0
    pitch: float = 0.0
    pre_phoneme: float = 0.0
    post_phoneme: float = 0.08
    pause_scale: float = 1.0
    timeout_s: float = 30.0
    table: dict[str, emotions_mod.VoiceParams] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    version: str = ""
    _client: httpx.Client | None = None

    # ------------------------------------------------------------------ setup
    @classmethod
    def from_config(cls, cfg, *, transport: httpx.BaseTransport | None = None) -> "VoicevoxClient":
        # -1 means "whatever the persona was written for": character and voice are one choice,
        # and a male persona in a female voice is jarring (ADR-026).
        speaker = int(cfg.VOICEVOX_SPEAKER)
        if speaker < 0:
            from backend import prompt as prompt_mod
            speaker = prompt_mod.declared_voice(cfg.TUTOR_PERSONA) or 29
        self = cls(base_url=str(cfg.VOICEVOX_URL).rstrip("/"), speaker=speaker,
                   speed=float(cfg.VOICEVOX_SPEED_SCALE), intonation=float(cfg.VOICEVOX_INTONATION_SCALE),
                   pitch=float(cfg.VOICEVOX_PITCH_SCALE),
                   pre_phoneme=float(cfg.VOICEVOX_PRE_PHONEME), post_phoneme=float(cfg.VOICEVOX_POST_PHONEME),
                   pause_scale=float(cfg.VOICEVOX_PAUSE_SCALE))
        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout_s,
                                    follow_redirects=False, transport=transport)
        self.table, self.warnings = emotions_mod.resolve(cfg, self.speakers())
        return self

    @property
    def http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout_s, follow_redirects=False)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ------------------------------------------------------------------ engine
    def speakers(self) -> list[dict[str, Any]]:
        """Live style catalogue; empty (not an exception) when the engine is unreachable."""
        try:
            resp = self.http.get("/speakers")
            resp.raise_for_status()
            self.version = self._version()
            return list(resp.json())
        except (httpx.HTTPError, ValueError):
            return []

    def _version(self) -> str:
        try:
            return str(self.http.get("/version").json())
        except (httpx.HTTPError, ValueError):
            return ""

    def is_up(self) -> bool:
        return bool(self._version())

    # --------------------------------------------------------------- synthesis
    def params_for(self, emotion: str) -> emotions_mod.VoiceParams:
        return self.table.get(emotion) or self.table.get(NEUTRAL) or emotions_mod.VoiceParams(self.speaker)

    def say(self, text: str, emotion: str = NEUTRAL) -> Speech:
        """Synthesise one sentence with the voice that matches its emotion."""
        if not text.strip():
            raise VoicevoxError("refusing to synthesise empty text")
        params = self.params_for(emotion)
        started = time.monotonic()
        try:
            q = self.http.post("/audio_query", params={"speaker": params.style_id, "text": text})
            q.raise_for_status()
            query = params.apply(q.json(), self.speed, self.intonation,
                                 self.pre_phoneme, self.post_phoneme, self.pause_scale, self.pitch)
            wav = self.http.post("/synthesis", params={"speaker": params.style_id}, json=query)
            wav.raise_for_status()
        except httpx.HTTPError as exc:
            raise VoicevoxError(f"VOICEVOX unreachable or refused: {type(exc).__name__}: {exc}") from None
        except ValueError as exc:
            raise VoicevoxError(f"VOICEVOX returned a non-JSON audio_query: {exc}") from None
        return Speech(text=text, emotion=emotion, wav=wav.content, timeline=visemes_mod.build(query),
                      style_id=params.style_id, synth_ms=(time.monotonic() - started) * 1000.0)
