"""Compare STT models on YOUR voice, not on a benchmark (ROADMAP V0.13, ADR-004).

Published numbers do not settle this. kotoba-whisper reports better CER/WER than large-v3 on
in-domain Japanese and only "competitive" out-of-domain — and a learner's accented Japanese is
about as out-of-domain as Japanese gets. So: record real utterances through the real mic and VAD
path, then run every candidate over the same audio and read the transcripts side by side.

    # record 6 utterances, then transcribe them with both models
    .venv/Scripts/python -m backend.tools.stt_compare --record 6

    # re-run on what you already recorded, e.g. after changing a threshold
    .venv/Scripts/python -m backend.tools.stt_compare --replay

    # quantisation is a choice too: same model, two compute types
    .venv/Scripts/python -m backend.tools.stt_compare --replay --models large-v3@int8_float16,large-v3@float16

    # try a different line-up
    .venv/Scripts/python -m backend.tools.stt_compare --replay --models large-v3,kotoba-tech/kotoba-whisper-v2.0-faster

The recordings stay in `logs/stt/`, which is gitignored — they are your voice, and they are also
the only regression corpus this project can have for STT.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

from backend import audio as audio_mod
from backend import config
from backend.stt import SAMPLE_RATE, SpeechToText
from backend.vad import EventKind, VoiceActivityDetector
from backend.voice_loop import vad_frame_samples

OUT_DIR = config.REPO_ROOT / "logs" / "stt"
DEFAULT_MODELS = ("large-v3", "kotoba-tech/kotoba-whisper-v2.0-faster")


def vram_mib() -> float:
    """What the GPU is holding right now. Returns 0.0 where nvidia-smi is not available."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10)
        return float(out.stdout.strip().splitlines()[0])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return 0.0


def record(count: int, device=None) -> list[Path]:
    """Capture `count` utterances through the real VAD, so the audio is what the loop would get."""
    cfg = config.load()
    vad = VoiceActivityDetector.from_config(cfg)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    print(f"speak {count} sentences in Japanese — pause between them. Ctrl+C to stop early.\n")
    try:
        for frame in audio_mod.capture(device, frame_samples=vad_frame_samples()):
            for event in vad.push(frame):
                if event.kind is not EventKind.SPEECH_END or event.audio is None:
                    continue
                path = OUT_DIR / f"utt_{len(saved) + 1:02d}.wav"
                pcm = np.clip(event.audio, -1.0, 1.0)
                with wave.open(str(path), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(SAMPLE_RATE)
                    w.writeframes((pcm * 32767).astype("<i2").tobytes())
                saved.append(path)
                print(f"  [{len(saved)}/{count}] {path.name}  {len(event.audio) / SAMPLE_RATE:.1f}s")
                if len(saved) >= count:
                    return saved
    except KeyboardInterrupt:
        pass
    return saved


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768


def main() -> int:
    ap = argparse.ArgumentParser(description="A/B two STT models on your own recorded speech")
    ap.add_argument("--record", type=int, default=0, metavar="N", help="record N utterances first")
    ap.add_argument("--replay", action="store_true", help="use whatever is already in logs/stt/")
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS), help="comma-separated model ids")
    args = ap.parse_args()

    if args.record:
        record(args.record, config.load().AUDIO_INPUT_DEVICE)
    clips = sorted(OUT_DIR.glob("utt_*.wav"))
    if not clips:
        print("nothing to compare — record some first:  --record 6", file=sys.stderr)
        return 2

    cfg = config.load()
    audio = {c.name: load_wav(c) for c in clips}
    results: dict[str, dict] = {}

    for spec in [m.strip() for m in args.models.split(",") if m.strip()]:
        # `large-v3@float16` — quantisation is as much a choice as the model is, and comparing
        # two of them is the same experiment, so it takes the same harness.
        name, _, compute = spec.partition("@")
        compute = compute or str(cfg.WHISPER_COMPUTE_TYPE)
        print(f"\n=== {name} @ {compute} ===")
        baseline = vram_mib()
        stt = SpeechToText(model_name=name, compute_type=compute)
        try:
            stt.load()
        except Exception as exc:                       # noqa: BLE001 — report, try the next model
            print(f"  could not load: {type(exc).__name__}: {exc}")
            continue
        held = vram_mib() - baseline
        print(f"  load {stt.load_ms:.0f} ms · warmup {stt.warmup_ms:.0f} ms · VRAM +{held:.0f} MiB")
        per_clip = {}
        for clip, samples in audio.items():
            started = time.monotonic()
            t = stt.listen(samples)
            ms = (time.monotonic() - started) * 1000.0
            mark = " " if t else "✗"
            print(f"  {mark} {clip}  {ms:6.0f} ms  {t.text or '(empty)'}"
                  + (f"   [{t.reason}]" if not t else ""))
            per_clip[clip] = {"text": t.text, "accepted": bool(t), "reason": t.reason,
                              "ms": round(ms, 1), "no_speech_prob": round(t.no_speech_prob, 3),
                              "avg_logprob": round(t.avg_logprob, 3)}
        results[spec] = {"vram_mib": round(held), "load_ms": round(stt.load_ms),
                         "median_ms": round(float(np.median([c["ms"] for c in per_clip.values()]))),
                         "clips": per_clip}
        del stt

    print("\n=== side by side ===")
    for clip in audio:
        print(f"\n{clip}")
        for name, r in results.items():
            c = r["clips"][clip]
            print(f"  {name:<44} {c['ms']:>6.0f} ms  {c['text'] or '(discarded: ' + c['reason'] + ')'}")

    print(f"\n{'model':<44} {'VRAM':>8} {'median':>8}")
    for name, r in results.items():
        print(f"  {name:<42} {r['vram_mib']:>6} MiB {r['median_ms']:>6} ms")

    report = OUT_DIR / "compare.json"
    report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwritten: {report}")
    print("The numbers are only half of it — read the transcripts. The right model is the one that\n"
          "got YOUR sentences right, not the one that was quickest to get them wrong.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
