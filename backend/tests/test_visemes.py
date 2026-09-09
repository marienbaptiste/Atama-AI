"""Mora -> Oculus viseme mapping, against real VOICEVOX payloads (spec §7, ROADMAP subsystem 6).

Golden fixtures were captured live from VOICEVOX 0.25.2 on 2026-09-09.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend import visemes as v

FX = Path(__file__).parent / "fixtures" / "voicevox"


def fx(name: str) -> dict:
    return json.loads((FX / f"{name}.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ the table
@pytest.mark.parametrize("consonant,expected", [
    ("k", "kk"), ("g", "kk"),
    ("s", "SS"), ("z", "SS"), ("sh", "SS"), ("j", "SS"), ("ts", "SS"),
    ("t", "DD"), ("d", "DD"),
    ("ch", "CH"),
    ("n", "nn"),
    ("m", "PP"), ("b", "PP"), ("p", "PP"),
    ("f", "FF"), ("h", "FF"),
    ("r", "RR"),
])
def test_consonant_table_matches_the_spec(consonant, expected):
    assert v.consonant_viseme(consonant) == expected


@pytest.mark.parametrize("skipped", ["w", "y"])
def test_w_and_y_are_skipped_so_the_vowel_dominates(skipped):
    assert v.consonant_viseme(skipped) is None


@pytest.mark.parametrize("vowel,expected", [("a", "aa"), ("i", "I"), ("u", "U"), ("e", "E"), ("o", "O")])
def test_vowels_map_at_full_weight(vowel, expected):
    assert v.vowel_viseme(vowel) == (expected, v.FULL_WEIGHT)


def test_n_cl_and_pause_are_not_treated_as_vowels():
    assert v.vowel_viseme("N") == ("nn", v.FULL_WEIGHT)      # ん is a phoneme, not a devoiced vowel
    assert v.vowel_viseme("cl") == (v.SILENCE, v.FULL_WEIGHT)  # っ
    assert v.vowel_viseme("pau") == (v.SILENCE, v.FULL_WEIGHT)


@pytest.mark.parametrize("devoiced,shape", [("A", "aa"), ("I", "I"), ("U", "U"), ("E", "E"), ("O", "O")])
def test_devoiced_vowels_keep_the_shape_but_lose_weight(devoiced, shape):
    """VOICEVOX marks devoicing by upper-casing the vowel (spec §7)."""
    assert v.vowel_viseme(devoiced) == (shape, v.DEVOICED_WEIGHT)


# ------------------------------------------------------------------- goldens
def test_greeting_timeline_is_what_the_spec_describes():
    """こんにちは、みなみ先生です。 — note `wa` yields only `aa`: w is skipped."""
    tl = v.build(fx("greeting"))
    assert tl.visemes[:8] == ["kk", "O", "nn", "nn", "I", "CH", "I", "aa"]
    assert tl.vtimes[0] == pytest.approx(100.0)          # prePhonemeLength 0.1 s
    assert "sil" in tl.visemes                            # the 、 pause mora
    assert len(tl) == len(tl.vtimes) == len(tl.vdurations) == len(tl.weights)


def test_p_row_is_all_PP_closures():
    tl = v.build(fx("pa_line"))
    assert tl.visemes.count("PP") >= 10                   # ぱぴぷぺぽ ばびぶべぼ


def test_s_row_uses_SS():
    tl = v.build(fx("sa_line"))
    assert tl.visemes.count("SS") >= 5


def test_devoicing_appears_in_real_speech():
    """`tricky` contains a devoiced vowel (VOICEVOX returned 'U')."""
    tl = v.build(fx("tricky"))
    assert any(w == v.DEVOICED_WEIGHT for w in tl.weights)


@pytest.mark.parametrize("name", ["greeting", "pa_line", "sa_line", "tricky"])
def test_timeline_is_monotonic_and_never_overlaps(name):
    tl = v.build(fx(name))
    ends = [t + d for t, d in zip(tl.vtimes, tl.vdurations)]
    assert all(a <= b + 1e-6 for a, b in zip(ends, tl.vtimes[1:]))
    assert all(d > 0 for d in tl.vdurations)
    assert tl.vtimes[0] >= 0


@pytest.mark.parametrize("name,wav_ms", [
    # Measured against the real synthesised WAV, 2026-09-09, speedScale 1.0.
    ("greeting", 2325.3), ("pa_line", 2058.7), ("sa_line", 1866.7), ("tricky", 3840.0),
])
def test_computed_duration_tracks_the_real_wav(name, wav_ms):
    """Within one video frame. The engine rounds to sample boundaries, so this is not exact —
    asserting equality would be asserting VOICEVOX's rounding, not our arithmetic."""
    assert v.build(fx(name)).duration_ms == pytest.approx(wav_ms, abs=60)


@pytest.mark.parametrize("speed", [0.8, 0.9, 1.0, 1.2])
def test_speed_scale_divides_every_duration(speed):
    """The query holds lengths at speed 1; VOICEVOX applies speedScale when synthesising."""
    base = v.build(fx("greeting"))
    scaled = v.build({**fx("greeting"), "speedScale": speed})
    assert scaled.duration_ms == pytest.approx(base.duration_ms / speed, rel=1e-6)
    assert scaled.vtimes[0] == pytest.approx(base.vtimes[0] / speed, rel=1e-6)
    assert scaled.visemes == base.visemes            # same shapes, different clock


def test_missing_and_malformed_input_degrades_quietly():
    assert len(v.build({})) == 0
    assert len(v.build({"accent_phrases": [None, {"moras": [None, {}]}]})) == 0
    assert v.build({"speedScale": 0}).duration_ms == 0   # never divide by zero
