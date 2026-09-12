"""Console rendering for the tutor's terminal: colours, the level meter, timing lines, the p90
report, and the push-to-talk key pump. Nothing here talks to the brain, the page or the audio
stack — it turns what they report into lines on the console (split out of repl.py, 2026-09-12).
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Callable

from backend.tools.latency_run import percentile

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
CR = "\r"


def emotion_tag(emotion: str) -> str:
    return f"[{emotion}]".ljust(11) if emotion else " " * 11


def compaction_text(ev) -> str:
    """One line for a provider compaction (spec §6b), for the terminal and the page."""
    if ev.active:
        return "Claude is condensing the conversation…"
    if ev.error:
        return f"condensing the conversation failed: {ev.error}"
    who = {"auto": " on its own", "manual": " on request"}.get(ev.trigger, "")
    sizes = f" {ev.pre_tokens:,} → {ev.post_tokens:,} tokens" if ev.pre_tokens and ev.post_tokens else ""
    took = f" in {ev.duration_ms / 1000:.1f} s" if ev.duration_ms else ""
    return f"Claude condensed the conversation{who}:{sizes}{took}"


def say(text: str, *, end: str = "\n") -> None:
    print(text, end=end, flush=True)


def note(tag: str, text: str, *, bold: bool = False, pad: int = 0, end: str = "\n") -> None:
    """A tagged line that overwrites the meter's line: `[tag] text`."""
    say(CR + (BOLD if bold else DIM) + "[" + tag + "] " + text + RESET + " " * pad, end=end)


class LevelMeter:
    """A live meter on the prompt line: the difference between "it is not hearing me" and
    "it heard me and decided that was not speech" should never be a guess.

    `update()` is called per frame; it redraws at 20 fps (responsive, not flickery) and returns
    the peak-held level for anyone else who wants it (the page's meter, page_control.LevelSender).
    """

    #: 20 fps: responsive, not flickery.
    REDRAW_S = 0.05

    def __init__(self, out: Callable[[str], None] | None = None) -> None:
        self.out = out or (lambda s: print(s, end="", flush=True))
        self.last = 0.0
        self.peak = 0.0

    def update(self, level: float, prob: float, *, hot: bool, ptt: str | None) -> float:
        """`hot`: the VAD calls this speech. `ptt`: None hands-free, else "open" | "closed"."""
        self.peak = max(self.peak, level)                       # peak-hold between redraws
        now = time.monotonic()
        if now - self.last < self.REDRAW_S:
            return self.peak
        self.last = now
        peak = self.peak
        self.peak = peak * 0.45                                 # ~decays to nothing in 0.2 s
        self.out(self.render(peak, prob, hot=hot, ptt=ptt))
        return peak

    @staticmethod
    def render(peak: float, prob: float, *, hot: bool, ptt: str | None) -> str:
        # Quiet mics are the norm here (this one peaks around 0.02 on speech), so scale by the
        # square root: a linear bar on a 0-1 range barely twitches and reads as "not hearing you".
        bars = int(min(1.0, (peak * 30) ** 0.5) * 28)
        if ptt is not None:
            label = "RECORDING   " if ptt == "open" else "SPACE to talk"
            colour = BOLD if ptt == "open" else DIM
        else:
            label = "HEARING YOU" if hot else "listening   "
            colour = BOLD if hot else DIM
        return f"\r{colour}{label}{RESET} |{('#' * bars):<28}| {DIM}{prob:.2f}{RESET}  "


def timing_line(t, done_ms: list[float], warn_s: float) -> str:
    """Spec §10 / ROADMAP 10: one turn's breakdown with the Claude stage taken apart and the
    session's rolling p50/p90 (`done_ms`: completed turns' voice→voice so far), with a warning
    over LATENCY_WARN_S. `first_audio_ms` is synthesis done; `first_play_ms` is playback start
    (2026-09-12) — voice→voice is the play time when it was observed."""
    v2v = t.voice_to_voice_ms()
    slow = bool(v2v) and v2v > float(warn_s) * 1000
    return (f"  {BOLD if slow else DIM}stt {t.stt_ms:.0f}ms · first sentence {t.first_chunk_ms:.0f}ms "
            f"(ttft {t.ttft_ms or 0:.0f}ms" + (f", thinking {t.thinking_chars} chars" if t.thinking_chars else "")
            + f") · audio {t.first_audio_ms:.0f}ms · play {t.first_play_ms:.0f}ms"
            + f" · voice→voice {v2v:.0f}ms · turn {t.total_ms:.0f}ms"
            + (f" · session p50 {t.session_p50_ms / 1000:.2f}s p90 {t.session_p90_ms / 1000:.2f}s "
               f"({len(done_ms)} turns)" if t.session_p50_ms else "")
            + (f" · of which condensing {t.compaction_ms / 1000:.1f}s" if t.compaction_ms else "")
            + (f"  OVER {float(warn_s):.1f}s" if slow else "") + RESET)


def targets_line(vocab: list[str], grammar: list[str], opener: str, subject: str = "") -> str:
    """One line at launch: today's targets and how the lesson opens (spec §6c)."""
    bits = []
    if vocab:
        bits.append("vocab " + "、".join(vocab))
    if grammar:
        bits.append("grammar " + "、".join(grammar))
    bits.append(f"opener {opener}" + (f"（{subject}）" if subject else ""))
    return f"{DIM}targets: {' · '.join(bits)}{RESET}"


def session_report(timings, budget_s: float = 5.0) -> str | None:
    """The end-of-session line: turns, voice→voice median and p90 against the budget. None when
    no turn produced a voice. Nearest-rank percentiles, the same rule as latency_run."""
    v2v = [t.voice_to_voice_ms() for t in timings if t.voice_to_voice_ms()]
    if not v2v:
        return None
    return (f"\n{DIM}{len(v2v)} turns · voice→voice median {percentile(v2v, 50) / 1000:.2f}s · "
            f"p90 {percentile(v2v, 90) / 1000:.2f}s (budget {budget_s:.1f}s){RESET}")


def ptt_keys(loop, aloop):
    """SPACE opens and closes the turn, pumped from a daemon thread.

    A terminal cannot see key RELEASE, only presses, so this is a toggle rather than a true
    hold. In the browser (M3) it becomes a real hold on mousedown/mouseup — the loop API is the
    same either way, which is why ptt_begin/ptt_end are two calls and not one.
    """
    stop = threading.Event()

    def toggle():
        loop.ptt_end() if loop._ptt_open else loop.ptt_begin()

    def pump():
        try:
            import msvcrt
        except ImportError:                      # POSIX: no raw keys, ENTER toggles instead
            while not stop.is_set():
                if sys.stdin.readline() == "":
                    return
                aloop.call_soon_threadsafe(toggle)
            return
        while not stop.is_set():
            if msvcrt.kbhit():
                if msvcrt.getch() in (b" ", b"\r"):
                    aloop.call_soon_threadsafe(toggle)
            else:
                time.sleep(0.01)

    threading.Thread(target=pump, name="ptt-keys", daemon=True).start()
    return stop
