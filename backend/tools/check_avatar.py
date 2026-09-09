"""Does this GLB actually have the blendshapes TalkingHead needs?

    python -m backend.tools.check_avatar          # the configured persona's avatar
    python -m backend.tools.check_avatar --all    # every persona's
    python -m backend.tools.check_avatar <path>

Ready Player Me will happily hand you a .glb with no morph targets at all if the export
parameters are wrong, and the failure is silent: the avatar loads, renders, and never moves its
mouth. Forum reports say getting ARKit *and* Oculus visemes in one export is fiddly, so this
checks rather than trusts (V0.4, 2026-09-10).

Reads the GLB container directly — no three.js, no network, no dependencies.
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

from backend import config, constants, prompt

AVATAR_DIR = config.REPO_ROOT / "frontend" / "public"
#: A sample of ARKit's 52. If these are present the set almost certainly is.
ARKIT_SAMPLE = ("jawOpen", "eyeBlinkLeft", "eyeBlinkRight", "browInnerUp",
                "browDownLeft", "browDownRight", "eyeWideLeft", "eyeWideRight",
                "mouthSmileLeft", "mouthSmileRight")


def morph_target_names(glb: Path) -> set[str]:
    """Every morph target name in the file, from the glTF JSON chunk."""
    data = glb.read_bytes()
    magic, _version, _length = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF":
        raise SystemExit(f"{glb} is not a GLB (magic was {magic!r}) — a .gltf or a stray file?")
    chunk_len, chunk_type = struct.unpack_from("<I4s", data, 12)
    if chunk_type != b"JSON":
        raise SystemExit(f"{glb}: first chunk is {chunk_type!r}, expected JSON")
    gltf = json.loads(data[20:20 + chunk_len])
    names: set[str] = set()
    for mesh in gltf.get("meshes", []):
        # glTF puts target names in mesh.extras.targetNames; exporters vary, so accept both.
        names.update(mesh.get("extras", {}).get("targetNames", []) or [])
        for prim in mesh.get("primitives", []):
            names.update(prim.get("extras", {}).get("targetNames", []) or [])
    return names


def personas():
    """(persona, expected GLB) for every persona that declares a face."""
    out = []
    for md in sorted((config.REPO_ROOT / "prompts").glob("*.md")):
        if md.stem == "tutor":
            continue
        declared = prompt.declared_avatar(md.stem)
        if declared:
            out.append((md.stem, AVATAR_DIR / declared))
    return out


def check(glb, label = "") -> int:
    """Report one avatar. 0 usable, 1 incomplete, 2 missing."""
    tag = (label + " ") if label else ""
    if not glb.exists():
        print(tag + glb.name + ": MISSING at " + str(glb), file=sys.stderr)
        return 2
    names = morph_target_names(glb)
    visemes = {"viseme_" + v for v in constants.TALKINGHEAD_VISEMES}
    missing_v = sorted(v for v in visemes if v not in names)
    missing_a = sorted(a for a in ARKIT_SAMPLE if a not in names)
    ok = not (missing_v or missing_a)
    print(f"{tag}{glb.name:16} {glb.stat().st_size/1e6:5.1f} MB  {len(names):3} morphs  "
          f"visemes {len(visemes)-len(missing_v)}/{len(visemes)}  "
          f"arkit {len(ARKIT_SAMPLE)-len(missing_a)}/{len(ARKIT_SAMPLE)}  "
          + ("ok" if ok else "INCOMPLETE"))
    if missing_v:
        print("      missing visemes: " + str(missing_v), file=sys.stderr)
    if missing_a:
        print("      missing ARKit  : " + str(missing_a), file=sys.stderr)
    return 0 if ok else 1


def main(argv) -> int:
    args = [a for a in argv[1:] if not a.startswith("-")]
    if args:
        return check(Path(args[0]))
    rows = personas()
    if "--all" not in argv:
        who = config.load().TUTOR_PERSONA
        rows = [r for r in rows if r[0] == who] or rows
    if not rows:
        print("no persona declares an avatar (add an avatar comment)", file=sys.stderr)
        return 2
    worst = 0
    for persona, glb in rows:
        worst = max(worst, check(glb, f"{persona:8}"))
    if worst:
        print("", file=sys.stderr)
        print("Need a full-body GLB with BOTH morph target groups:", file=sys.stderr)
        print("  https://models.readyplayer.me/<ID>.glb?morphTargets=ARKit,Oculus%20Visemes",
              file=sys.stderr)
    return worst


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))