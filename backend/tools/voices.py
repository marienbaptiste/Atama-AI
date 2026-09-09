"""Browse and audition VOICEVOX voices, so `VOICEVOX_SPEAKER` is chosen by ear (spec §7).

The engine exposes no gender or age metadata — only names, style names and licence text — so the
only honest way to pick a voice is to hear it saying something the tutor would actually say.

    python -m backend.tools.voices                      # list every speaker and style
    python -m backend.tools.voices --audition 11,21,52  # render those into one labelled WAV
    python -m backend.tools.voices --audition all       # every speaker's base style (long!)
    python -m backend.tools.voices --emotions 29        # one speaker across all five emotions

The rendered file lands in .cache/ and each sample is preceded by its spoken index, so a list
printed to the terminal is enough to identify what you just heard.
"""
from __future__ import annotations

import argparse
import io
import sys
import wave
from pathlib import Path

import numpy as np

from backend import config
from backend.chunker import EMOTIONS, NEUTRAL
from backend.tts_voicevox import VoicevoxClient, VoicevoxError

SAMPLE_LINE = "こんにちは。今日は何を勉強しましょうか。"
GAP_S = 0.6


def _pcm(wav: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(wav)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2"), w.getframerate()


def _write(path: Path, chunks: list[np.ndarray], rate: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.concatenate(chunks).tobytes())
    return path


def catalogue(client: VoicevoxClient) -> list[dict]:
    return client.speakers()


def print_catalogue(speakers: list[dict]) -> None:
    for s in speakers:
        styles = ", ".join(f"{st['name']}={st['id']}" for st in s.get("styles", []))
        print(f"  {s['name']:<20} {styles}")
    print(f"\n{len(speakers)} speakers. Put an id in VOICEVOX_SPEAKER (.env or the settings page).")
    print("Audition before committing:  python -m backend.tools.voices --audition <ids>")


def audition(client: VoicevoxClient, ids: list[int], text: str, out: Path) -> tuple[Path, list[str]]:
    """Render each style id saying `text`, prefixed by a spoken index. Returns (path, labels)."""
    by_id = {int(st["id"]): (s["name"], st["name"])
             for s in client.speakers() for st in s.get("styles", [])}
    chunks: list[np.ndarray] = []
    labels: list[str] = []
    rate = 24000
    for n, style_id in enumerate(ids, 1):
        speaker, style = by_id.get(style_id, ("unknown", "?"))
        try:
            index = client.say(f"{n}番。", NEUTRAL)
            body = client.say(text, NEUTRAL)
        except VoicevoxError as exc:
            print(f"  {n:>2}. [{style_id}] {speaker} — FAILED: {exc}", file=sys.stderr)
            continue
        # The spoken index uses the SAME voice as the sample, so counting is never ambiguous.
        for wav in (index.wav, body.wav):
            pcm, rate = _pcm(wav)
            chunks.append(pcm)
        chunks.append(np.zeros(int(rate * GAP_S), dtype="<i2"))
        labels.append(f"{n:>2}. [{style_id:>3}] {speaker} · {style}")
        print(f"  {labels[-1]}")
    if not chunks:
        raise VoicevoxError("nothing rendered")
    return _write(out, chunks, rate), labels


def emotions_demo(client: VoicevoxClient, out: Path, intro: str = "みなみ先生") -> Path:
    """One speaker across every emotion — the table in backend/emotions.py, audible."""
    lines = {
        NEUTRAL: f"こんにちは。{intro}です。",
        "happy": "よくできました！すごいですね。",
        "thinking": "うーん、そうですね。もう一度言ってみてください。",
        "surprised": "えっ、本当ですか！それはすごい。",
        "serious": "ここは違います。「そういう」を使いましょう。",
    }
    chunks: list[np.ndarray] = []
    rate = 24000
    for emotion in (NEUTRAL, *EMOTIONS):
        speech = client.say(lines[emotion], emotion)
        params = client.params_for(emotion)
        print(f"  {emotion or 'neutral':<10} style={speech.style_id:<4} {params.style_name}")
        pcm, rate = _pcm(speech.wav)
        chunks.append(pcm)
        chunks.append(np.zeros(int(rate * 0.7), dtype="<i2"))
    return _write(out, chunks, rate)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Browse and audition VOICEVOX voices")
    ap.add_argument("--audition", metavar="IDS", help='comma-separated style ids, or "all"')
    ap.add_argument("--emotions", metavar="ID", type=int, help="render one speaker across all emotions")
    ap.add_argument("--text", default=SAMPLE_LINE, help="what the voices should say")
    ap.add_argument("--intro", default="みなみ先生", help="name the voice introduces itself with (--emotions)")
    args = ap.parse_args(argv)

    cfg = config.load()
    client = VoicevoxClient.from_config(cfg)
    if not client.is_up():
        print(f"VOICEVOX is not running at {cfg.VOICEVOX_URL} — `docker compose up -d voicevox`", file=sys.stderr)
        return 2
    speakers = catalogue(client)
    cache = cfg.path("CACHE_DIR")

    if args.emotions is not None:
        from types import SimpleNamespace

        from backend import emotions as emo_mod

        # Re-resolve the emotion table against the requested speaker, leaving the real config alone.
        overrides = SimpleNamespace(VOICEVOX_SPEAKER=args.emotions,
                                    **{f"EMOTION_{e.upper()}": getattr(cfg, f"EMOTION_{e.upper()}") for e in EMOTIONS})
        client.speaker = args.emotions
        client.table, warnings = emo_mod.resolve(overrides, speakers)
        for w in warnings:
            print(f"  note: {w}")
        path = emotions_demo(client, cache / f"voice-emotions-{args.emotions}.wav", intro=args.intro)
        print(f"\n{path}")
        return 0

    if args.audition:
        if args.audition.strip().lower() == "all":
            ids = [int(s["styles"][0]["id"]) for s in speakers if s.get("styles")]
        else:
            ids = [int(x) for x in args.audition.replace(" ", "").split(",") if x]
        path, _ = audition(client, ids, args.text, cache / "voice-audition.wav")
        print(f"\n{path}")
        return 0

    print_catalogue(speakers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
