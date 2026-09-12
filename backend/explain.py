"""Explanations and translations on click (spec §8b, ADR-036 point 2).

Nothing is generated until the student asks — a click on a red grammar point, or on a sentence's
translate icon. Every answer is cached on disk, so the second click on the same thing is free and
instant. One worker does the asking: `claude -p` on the haiku tier, no tools, under §4's rules
(ADR-001 — the CLI, never the API), kept between requests and closed with the app. It is never the
tutor's session: an explanation must not land in her transcript (ADR-027).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from backend import brain as brain_api
from backend import model_tiers
from backend.brain import TextDelta, TurnComplete

LANGUAGES = {"en": "English", "ja": "Japanese"}
#: An answer nobody has after this long is not worth the wait; the page says so and stays usable.
TIMEOUT_S = 60.0
#: The worker starts again after this many answers, so one session cannot grow all evening.
MAX_TURNS = 30
SYSTEM = ("You explain Japanese to a learner, briefly and plainly. No markdown, no lists, no "
          "preamble, no greeting — two or three sentences at most.")


def prompt_for(kind: str, text: str, context: str, lang: str) -> str:
    """What to ask. `grammar` gets the sentence it appeared in; `sentence` is a translation."""
    if kind == "sentence":
        return "Translate this Japanese sentence into natural English. Reply with the translation only.\n\n" + text
    where = f"\n\nAs used in: {context}" if context else ""
    return (f"Explain the Japanese grammar point {text} in {LANGUAGES.get(lang, 'English')}: what it "
            f"means, when it is used, and what it attaches to. Two or three sentences.{where}")


def cache_key(kind: str, text: str, context: str, lang: str) -> str:
    """One answer per (kind, language, thing). A translation ignores context — it IS the sentence."""
    raw = "|".join((kind, lang, text, context if kind == "grammar" else ""))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class Explainer:
    def __init__(self, cfg, cache_dir: Path | None = None,
                 ask: Callable[[str], Awaitable[str]] | None = None) -> None:
        self.cfg = cfg
        self.dir = Path(cache_dir) if cache_dir is not None else cfg.path("CACHE_DIR") / "explain"
        self.asked = 0                       # answers that cost a model call this session
        self.served = 0                      # answers that came from the cache
        self._ask = ask                      # injected by the tests; None means the real worker
        self._worker: Any = None
        self._turns = 0
        self._lock = asyncio.Lock()

    async def explain(self, kind: str, text: str, context: str = "", lang: str = "en") -> tuple[str, str]:
        """(answer, error). A failure is a message the page can show, never a raised exception."""
        text = (text or "").strip()
        if not text:
            return "", "nothing to explain"
        key = cache_key(kind, text, context, lang)
        cached = self._read(key)
        if cached:
            self.served += 1
            return cached, ""
        try:
            answer = await asyncio.wait_for(self._answer(prompt_for(kind, text, context, lang)), TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:             # noqa: BLE001 - the lesson carries on without it
            return "", f"{type(exc).__name__}: {exc}"[:200]
        answer = (answer or "").strip()
        if not answer:
            return "", "no answer came back"
        self.asked += 1
        self._write(key, kind, text, lang, answer)
        return answer, ""

    async def aclose(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            await worker.aclose()

    # ----------------------------------------------------------------- private
    async def _answer(self, prompt: str) -> str:
        if self._ask is not None:
            return await self._ask(prompt)
        async with self._lock:               # one question at a time: it is one process
            worker = await self._ready()
            out: list[str] = []
            async for event in worker.turn(prompt):
                if isinstance(event, TextDelta):
                    out.append(event.text)
                elif isinstance(event, TurnComplete):
                    break
            self._turns += 1
            return "".join(out)

    async def _ready(self) -> Any:
        if self._worker is not None and self._turns < MAX_TURNS:
            return self._worker
        await self.aclose()
        self._turns = 0
        model = await asyncio.to_thread(model_tiers.Resolver.from_config(self.cfg).resolve,
                                        str(self.cfg.EXPLAIN_MODEL))
        worker = brain_api.create(self.cfg, registry=None, allowed_tools=(), model=model, effort="low",
                                  system_prompt=SYSTEM)
        await worker.start()
        self._worker = worker
        return worker

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def _read(self, key: str) -> str:
        try:
            return str(json.loads(self._path(key).read_text(encoding="utf-8")).get("text") or "")
        except (OSError, ValueError):
            return ""

    def _write(self, key: str, kind: str, text: str, lang: str, answer: str) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._path(key).write_text(json.dumps(
                {"kind": kind, "asked": text, "lang": lang, "text": answer, "at": int(time.time())},
                ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass                             # a cache that cannot be written is still an answer
