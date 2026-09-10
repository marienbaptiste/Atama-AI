"""Generate the preview page's audio: one real `speak` payload per persona.

    python -m backend.tools.make_preview

Writes `frontend/public/<persona>.speak.json` — exactly the shape of the `speak` WS message
(`backend/models.py`), so the preview exercises the real contract rather than a mock: our
VOICEVOX audio, our viseme timeline, in milliseconds.

The output is generated, not source: it is git-ignored (several MB of base64 audio) and anyone
who clones the repo runs this once. Needs VOICEVOX up.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

from backend import config, prompt
from backend.tts_voicevox import VoicevoxClient, VoicevoxError

OUT = config.REPO_ROOT / "frontend" / "public"
LINE = "こんにちは。日本語を一緒に勉強しましょう。"
EMOTIONS = ("", "happy", "thinking", "surprised", "serious")


def main(argv: list[str]) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    made, cast = 0, []
    for md in sorted((config.REPO_ROOT / "prompts").glob("*.md")):
        if md.stem == "tutor":
            continue
        style = prompt.declared_voice(md.stem)
        glb, own_face = prompt.resolved_avatar(md.stem)
        if not glb or style is None:
            continue
        cfg = config.load(env={"VOICEVOX_SPEAKER": str(style), "TUTOR_PERSONA": md.stem})
        tts = VoicevoxClient.from_config(cfg)
        if not tts.is_up():
            print(f"VOICEVOX is not answering at {cfg.VOICEVOX_URL} — "
                  f"`docker compose up -d voicevox`", file=sys.stderr)
            return 2
        tts.warm_up()
        payloads = {}
        for emotion in EMOTIONS:
            try:
                speech = tts.say(LINE, emotion)
            except VoicevoxError as exc:
                print(f"{md.stem}/{emotion or 'neutral'}: {exc}", file=sys.stderr)
                return 1
            timeline = speech.timeline.as_message()
            payloads[emotion or "neutral"] = {
                "type": "speak",
                "audio_b64": base64.b64encode(speech.wav).decode(),
                "visemes": timeline["visemes"],
                "vtimes": timeline["vtimes"],
                "vdurations": timeline["vdurations"],
                "text": LINE,
                "emotion": emotion,
                "turn": 0,
            }
        path = OUT / f"{md.stem}.speak.json"
        path.write_text(json.dumps(payloads), encoding="utf-8")
        made += 1
        # `body` describes the MODEL, not the character: a male persona wearing the stand-in
        # should get her idle animations, or the poses fight the mesh.
        owner = next((m.stem for m in sorted((config.REPO_ROOT / "prompts").glob("*.md"))
                      if prompt.declared_avatar(m.stem) == glb), md.stem)
        cast.append({"id": md.stem, "glb": glb, "own_face": own_face,
                     "body": "M" if owner in ("tanaka", "hayashi") else "F",
                     "voice": style})
        print(f"  {md.stem:8} style {style:3} -> {path.name} "
              f"({len(payloads)} emotions, {path.stat().st_size / 1e6:.1f} MB)"
              f"{'' if own_face else '  [stand-in face: ' + glb + ']'}")
    if not made:
        print("no persona declares both a voice and an avatar", file=sys.stderr)
        return 2
    (OUT / "cast.json").write_text(json.dumps(cast, ensure_ascii=False), encoding="utf-8")
    print(f"\nopen frontend/public/preview.html over HTTP, e.g.\n"
          f"  python -m http.server 8778 --bind 127.0.0.1 --directory frontend/public")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
