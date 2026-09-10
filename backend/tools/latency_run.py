"""Scripted latency run: N turns, per-stage p50/p90 against the budget (spec §10, ROADMAP 10).

    python -m backend.tools.latency_run                    # 20 turns, current settings
    python -m backend.tools.latency_run --turns 10 --effort low --model haiku
    python -m backend.tools.latency_run --no-mcp           # without the search / Bunpro tools

ROADMAP subsystem 10, Validate: "a scripted 20-turn conversation produces the p50/p90 report".
The student side is replayed; the tutor side is the real thing:

  stt     one of your recorded utterances (logs/stt/corpus-*/utt_*.wav) through warm Whisper
  claude  a scripted student line sent to a real tutor session, until her first COMPLETE
          sentence closes (the chunker decides, as in a lesson) — plus the CLI's own ttft,
          thinking characters and tool time, so the Claude stage can be taken apart
  tts     VOICEVOX synthesis of that first sentence: the moment the first audio exists
  slack   0.15 s playback start — the spec's constant, not measured here
  vad     0 under push-to-talk: releasing the key IS the end of speech

  voice->voice = stt + claude + tts + slack

Why scripted text and not the clip's own transcript: six clips repeated twenty times is the same
six sentences over and over — not a conversation, and not the load a lesson puts on the tutor.
Each clip still pays the real STT cost; the tutor receives a natural next line.

The first turn of a session pays prompt-cache creation (constants.py, 2026-09-09), so the opening
is measured and reported separately and kept out of the p50/p90.

Writes logs/latency/<timestamp>.jsonl — one row per turn, then a summary row. Uses your Claude
subscription for N+1 real turns. The study profile comes from the local SRS cache only: this tool
never contacts WaniKani or Bunpro (ADR-024), and nothing is written to your lesson memory.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

from backend import audio as audio_mod
from backend import brain as brain_api
from backend import config, prompt
from backend import memory as memory_api
from backend.brain import BrainError, TextDelta, Thinking, TurnComplete
from backend.chunker import SentenceChunker
from backend.srs import profile as profile_api
from backend.status import registry
from backend.stt import SAMPLE_RATE, SpeechToText
from backend.tools import mcp_config
from backend.tts_voicevox import VoicevoxClient

#: Spec §10 stage budgets, ms. VAD is 0 under push-to-talk, and slack is the spec's constant.
#: The gate was 3.0 s (Claude 1.60 s) until 2026-09-10; the user chose answer quality (ADR-033).
BUDGET_MS = {"stt_ms": 350, "claude_ms": 3600, "tts_ms": 400}
TOTAL_BUDGET_MS = 5000
PLAYBACK_SLACK_MS = 150
OPENING = "（セッション開始。あいさつして、始めてください。）"

#: What a beginner-intermediate student actually says in a lesson: short, a little uneven,
#: sometimes a question back, sometimes an error worth correcting (〜てしまう, 行ったことがある).
STUDENT_LINES = (
    "こんにちは。今日はちょっと疲れています。",
    "昨日、友達と映画を見に行きました。",
    "日本の映画です。とても面白かったです。",
    "はい、でも少し難しかったです。",
    "最近、仕事がとても忙しいです。",
    "週末は料理をしたいです。",
    "カレーを作るのが好きです。",
    "先生は料理が好きですか？",
    "日本に行ったことがあります。京都に行きました。",
    "お寺がきれいでした。",
    "来年また行きたいです。",
    "東京にも行ってみたいです。",
    "漢字の勉強は大変です。",
    "毎日、WaniKaniで勉強しています。",
    "文法はBunproで練習しています。",
    "「てしまう」がまだよく分かりません。",
    "例えば、財布を忘れてしまったです。",
    "ありがとうございます。分かりました。",
    "もう一つ質問してもいいですか？",
    "今日はありがとうございました。",
)


# --------------------------------------------------------------------- the maths
def percentile(values, p: float) -> float | None:
    """Nearest-rank percentile: "p90 over 20 turns" is the 18th of the 20, sorted. The same rule
    the REPL's end-of-session line uses, so the two numbers agree. Missing values are skipped."""
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    return v[max(0, math.ceil(p / 100.0 * len(v)) - 1)]


STAGES = ("stt_ms", "claude_ms", "tts_ms", "v2v_ms", "ttft_ms", "thinking_chars")


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {s: {"p50": percentile([r.get(s) for r in rows], 50),
                               "p90": percentile([r.get(s) for r in rows], 90)} for s in STAGES}
    out["turns"] = len(rows)
    p90 = out["v2v_ms"]["p90"]
    out["pass"] = p90 is not None and p90 <= TOTAL_BUDGET_MS
    return out


def format_report(summary: dict[str, Any], opening: dict[str, Any] | None = None) -> str:
    def ms(v):
        return "   -  " if v is None else f"{v / 1000:5.2f}s"

    lines = [f"\n{summary['turns']} turns                 p50      p90     budget"]
    for key, label in (("stt_ms", "stt (whisper)"), ("claude_ms", "claude first sentence"),
                       ("tts_ms", "tts first audio"), ("v2v_ms", "VOICE->VOICE")):
        budget = TOTAL_BUDGET_MS if key == "v2v_ms" else BUDGET_MS.get(key)
        p90 = summary[key]["p90"]
        flag = "" if p90 is None or budget is None else ("  ok" if p90 <= budget else "  OVER")
        lines.append(f"  {label:<22}{ms(summary[key]['p50'])}  {ms(p90)}  {budget / 1000:5.2f}s{flag}")
    lines.append(f"  {'  of which ttft':<22}{ms(summary['ttft_ms']['p50'])}  {ms(summary['ttft_ms']['p90'])}")
    t = summary["thinking_chars"]
    lines.append(f"  {'  thinking (chars)':<22}{t['p50'] or 0:>6}  {t['p90'] or 0:>6}")
    if opening:
        lines.append(f"  opening turn (not in p90): claude {ms(opening.get('claude_ms'))}, "
                     f"v2v {ms(opening.get('v2v_ms'))}")
    lines.append("\nGATE M3d (p90 voice->voice <= 5.0 s): " + ("PASS" if summary["pass"] else "FAIL"))
    return "\n".join(lines)


# ----------------------------------------------------------------------- one turn
def load_clips() -> list:
    paths = sorted(Path(config.REPO_ROOT, "logs", "stt").glob("corpus-*/utt_*.wav"))
    if not paths:
        raise SystemExit("no recorded utterances under logs/stt/corpus-*/ - record some with "
                         "`python -m backend.tools.stt_compare --record 6`")
    clips = []
    for p in paths:
        samples, rate = audio_mod.decode_wav(p.read_bytes())
        if rate != SAMPLE_RATE:
            raise SystemExit(f"{p.name} is {rate} Hz; the pipeline is {SAMPLE_RATE} Hz")
        clips.append(samples)
    return clips


async def one_turn(brain, tts, stt, clip, line: str) -> dict[str, Any]:
    started = time.monotonic()
    transcript = await asyncio.to_thread(stt.listen, clip)
    stt_ms = (time.monotonic() - started) * 1000.0

    chunker = SentenceChunker()
    sent = time.monotonic()
    first, claude_ms, thinking, ttft_ms, tool_ms, error = None, None, 0, None, None, ""
    reply: list[str] = []            # the whole answer, so a faster setting can be judged on quality too
    async for ev in brain.turn(line):
        if isinstance(ev, TextDelta):
            reply.append(ev.text)
            if first is None:
                closed = chunker.push(ev.text)
                if closed:
                    first, claude_ms = closed[0], (time.monotonic() - sent) * 1000.0
        elif isinstance(ev, Thinking):
            thinking += len(ev.text)
        elif isinstance(ev, BrainError):
            error = str(getattr(ev, "message", ev))[:200]
        elif isinstance(ev, TurnComplete):
            ttft_ms, tool_ms = ev.ttft_ms, ev.tool_ms
            if first is None:
                rest = chunker.close()
                if rest:
                    first, claude_ms = rest[0], (time.monotonic() - sent) * 1000.0
            break

    tts_ms = None
    if first is not None:
        speech = await asyncio.to_thread(
            (lambda: tts.say(first.text, first.emotion)) if first.emotion else (lambda: tts.say(first.text)))
        tts_ms = speech.synth_ms
    parts = (stt_ms, claude_ms, tts_ms)
    return {
        "student": line, "heard": transcript.text, "first_sentence": first.text if first else "",
        "reply": "".join(reply).strip(),
        "stt_ms": round(stt_ms), "claude_ms": round(claude_ms) if claude_ms is not None else None,
        "tts_ms": round(tts_ms) if tts_ms is not None else None,
        "v2v_ms": round(sum(parts) + PLAYBACK_SLACK_MS) if None not in parts else None,
        "ttft_ms": round(ttft_ms) if ttft_ms else None, "tool_ms": round(tool_ms) if tool_ms else None,
        "thinking_chars": thinking, "error": error,
    }


# ----------------------------------------------------------------------- the run
async def main_async(args) -> int:
    cfg = config.load()
    if args.effort or args.model:
        values = dict(cfg._values)
        if args.effort:
            values["CLAUDE_EFFORT"] = args.effort
        if args.model:
            values["CLAUDE_MODEL"] = args.model
        cfg = config.Config(values, cfg.settings_path, cfg.first_run)
    clips = load_clips()
    print(f"latency run: {args.turns} turns · {cfg.CLAUDE_MODEL} · effort {cfg.CLAUDE_EFFORT} · "
          f"{'no tools' if args.no_mcp else 'with tools'} · {len(clips)} recorded clips")

    # A ttl far beyond any real age: serve whatever snapshot exists, never fetch (ADR-024).
    student = profile_api.build(cfg.WANIKANI_TOKEN, cfg.BUNPRO_API_TOKEN, cfg.path("CACHE_DIR") / "srs",
                                10 ** 9, cfg.SRS_FETCH_BUDGET_S, registry, force=False)
    memory_text = memory_api.Memory.from_config(cfg).render() if cfg.MEMORY_ENABLED else ""
    rendered = prompt.build(profile_api.render(student), persona=cfg.TUTOR_PERSONA, memory=memory_text)
    tools = () if args.no_mcp else mcp_config.allowed_tools(cfg)
    mcp_json = mcp_config.write(cfg) if tools else None
    from backend import model_tiers        # the same newest-per-tier model the lessons use
    model = await asyncio.to_thread(model_tiers.Resolver.from_config(cfg).resolve, str(cfg.CLAUDE_MODEL))
    print(f"model: {cfg.CLAUDE_MODEL} -> {model}")
    brain = brain_api.create(cfg, registry=registry, mcp_config=mcp_json, model=model,
                             mcp_ready_markers=mcp_config.markers(cfg) if mcp_json else None,
                             system_prompt=rendered.text, allowed_tools=tools)

    tts = VoicevoxClient.from_config(cfg)
    if not tts.is_up():
        raise SystemExit(f"VOICEVOX is not answering at {cfg.VOICEVOX_URL} - start it first")
    stt = SpeechToText.from_config(cfg)
    print("loading whisper and warming the voice...")
    await asyncio.to_thread(stt.load)
    await asyncio.to_thread(tts.warm_up)
    await brain.start()

    out_dir = cfg.path("LOG_DIR") / "latency"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{dt.datetime.now():%Y%m%d-%H%M%S}.jsonl"
    rows: list[dict[str, Any]] = []
    try:
        opening = await one_turn(brain, tts, stt, clips[0], OPENING)
        print(f"  opening   claude {opening['claude_ms']} ms · ttft {opening['ttft_ms']} · "
              f"thinking {opening['thinking_chars']} · tool {opening['tool_ms']}")
        with out.open("w", encoding="utf-8") as f:
            f.write(json.dumps({"turn": 0, "opening": True, **opening}, ensure_ascii=False) + "\n")
            for i in range(args.turns):
                row = await one_turn(brain, tts, stt, clips[i % len(clips)],
                                     STUDENT_LINES[i % len(STUDENT_LINES)])
                row["turn"] = i + 1
                rows.append(row)
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()
                print(f"  turn {i + 1:>2}   v2v {row['v2v_ms']} ms = stt {row['stt_ms']} + claude "
                      f"{row['claude_ms']} + tts {row['tts_ms']} + {PLAYBACK_SLACK_MS}   "
                      f"(ttft {row['ttft_ms']}, thinking {row['thinking_chars']}, tool {row['tool_ms']})"
                      + (f"  ERROR {row['error']}" if row["error"] else ""), flush=True)
            summary = summarise(rows)
            summary.update({"model": cfg.CLAUDE_MODEL, "effort": cfg.CLAUDE_EFFORT,
                            "tools": not args.no_mcp, "opening": opening})
            f.write(json.dumps({"summary": summary}, ensure_ascii=False) + "\n")
    finally:
        await brain.aclose()
    print(format_report(summary, opening))
    print(f"\nrows: {out}")
    return 0 if summary["pass"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--turns", type=int, default=20)
    parser.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"))
    parser.add_argument("--model", help="model alias for this run only, e.g. sonnet / haiku / opus")
    parser.add_argument("--no-mcp", action="store_true", help="run without the search and Bunpro tools")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
