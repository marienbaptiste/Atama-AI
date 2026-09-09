"""Mora -> Oculus viseme mapping, against real VOICEVOX payloads (spec §7, ROADMAP subsystem 6).

Golden fixtures were captured live from VOICEVOX 0.25.2 on 2026-09-09.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend import visemes as v

FX = Path(__file__).parent / "fixtures" / "voicevox"
#: How far `build()` alone may stray from the real WAV. It is a PREDICTION from the audio_query;
#: VOICEVOX quantises text-dependently and measured -11.4 to +46.4 ms on these fixtures
#: (2026-09-10). `fitted_to()` removes it downstream, which is what gate M2d actually rides on.
MAX_PREDICTION_DRIFT_MS = 50


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
    """`build()` is a prediction and is allowed to be a little wrong; `fitted_to()` is not.

    Asserting equality here would be asserting VOICEVOX's quantisation rather than our
    arithmetic — it is text-dependent (-11.4 ms on さしすせそ, +46.4 ms on ぱぴぷぺぽ) and no
    function of the audio_query can recover it.
    """
    assert v.build(fx(name)).duration_ms == pytest.approx(wav_ms, abs=MAX_PREDICTION_DRIFT_MS)


@pytest.mark.parametrize("name,wav_ms", [
    ("greeting", 2325.3), ("pa_line", 2058.7), ("sa_line", 1866.7), ("tricky", 3840.0),
])
def test_fitting_to_the_wav_removes_the_drift_entirely(name, wav_ms):
    """Gate M2d: with the real WAV in hand the timeline ends exactly with the audio.

    Prediction error becomes a proportional stretch spread across every viseme instead of
    cumulative lag that is worst at the end of the sentence, where it shows most.
    """
    fitted = v.build(fx(name)).fitted_to(wav_ms)
    assert fitted.duration_ms == pytest.approx(wav_ms, abs=1e-9)
    assert fitted.vtimes[-1] + fitted.vdurations[-1] <= wav_ms + 1e-6
    assert len(fitted) == len(v.build(fx(name)))          # nothing added or dropped
    assert all(b >= a for a, b in zip(fitted.vtimes, fitted.vtimes[1:]))   # still monotonic


def test_fitting_is_a_no_op_when_the_wav_length_is_unknown():
    """A malformed WAV must degrade to the predicted timeline, never to a zero-length one."""
    base = v.build(fx("greeting"))
    assert base.fitted_to(0.0) is base
    assert base.fitted_to(-1.0) is base


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
