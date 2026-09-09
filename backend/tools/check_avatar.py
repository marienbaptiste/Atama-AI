"""Does this GLB actually have the blendshapes TalkingHead needs?

    python -m backend.tools.check_avatar [path]

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

from backend import constants

DEFAULT = Path("frontend/public/avatar.glb")
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


def main(argv: list[str]) -> int:
    glb = Path(argv[1]) if len(argv) > 1 else DEFAULT
    if not glb.exists():
        print(f"no avatar at {glb}\n"
              f"  Download one with BOTH morph target groups:\n"
              f"  https://models.readyplayer.me/<ID>.glb?morphTargets=ARKit,Oculus%20Visemes",
              file=sys.stderr)
        return 2
    names = morph_target_names(glb)
    print(f"{glb}  ({glb.stat().st_size / 1e6:.1f} MB, {len(names)} morph targets)\n")

    visemes = {f"viseme_{v}" for v in constants.TALKINGHEAD_VISEMES}
    missing_v = sorted(v for v in visemes if v not in names)
    missing_a = sorted(a for a in ARKIT_SAMPLE if a not in names)

    print(f"  Oculus visemes : {len(visemes) - len(missing_v)}/{len(visemes)}"
          + (f"   MISSING {missing_v}" if missing_v else "   ok"))
    print(f"  ARKit (sampled): {len(ARKIT_SAMPLE) - len(missing_a)}/{len(ARKIT_SAMPLE)}"
          + (f"   MISSING {missing_a}" if missing_a else "   ok"))

    if missing_v or missing_a:
        print("\nThis avatar will load and render but the face will not animate correctly.\n"
              "Re-export with:  ?morphTargets=ARKit,Oculus%20Visemes   (and a FULL-BODY avatar —\n"
              "TalkingHead requires a Mixamo-compatible rig, which half-body exports lack).",
              file=sys.stderr)
        return 1
    print("\nUsable by TalkingHead.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
