"""Device resolution, WAV decoding, and the silent-microphone check (spec §8/§9, ADR-022).

No real device is opened: `sounddevice` is stubbed, so these run on any machine including CI.
"""
from __future__ import annotations

import io
import wave

import numpy as np
import pytest

from backend import audio as audio_mod

DEVICES = [
    {"name": "Microsoft Sound Mapper - Input", "max_input_channels": 2, "max_output_channels": 0, "default_samplerate": 44100},
    {"name": "Microphone (Chat-Audeze Maxwell)", "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": 48000},
    {"name": "Speakers (Realtek(R) Audio)", "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 44100},
    {"name": "Headphones ()", "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 44100},
]


class FakeSd:
    def __init__(self, frames=None, raise_on_read=None):
        self.default = type("D", (), {"device": [1, 2]})()
        self._frames = frames
        self._raise = raise_on_read
        self.played = []

    def query_devices(self, index=None, kind=None):
        return DEVICES[index] if isinstance(index, int) else DEVICES

    def play(self, samples, samplerate, device, blocking):
        self.played.append((len(samples), samplerate, device))

    def stop(self):
        self.played.append("stopped")

    def InputStream(self, **kw):  # noqa: N802 - mirrors the sounddevice API
        outer = self

        class Stream:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n):
                if outer._raise:
                    raise outer._raise
                frame = outer._frames if outer._frames is not None else np.zeros(n, dtype=np.float32)
                return frame.reshape(-1, 1), False

        return Stream()


@pytest.fixture
def fake_sd(monkeypatch):
    sd = FakeSd()
    monkeypatch.setattr(audio_mod, "_sd", lambda: sd)
    return sd


# ------------------------------------------------------------------- devices
def test_devices_are_split_by_direction(fake_sd):
    assert [d.name for d in audio_mod.list_devices("input")] == [
        "Microsoft Sound Mapper - Input", "Microphone (Chat-Audeze Maxwell)"]
    assert [d.name for d in audio_mod.list_devices("output")] == [
        "Speakers (Realtek(R) Audio)", "Headphones ()"]


def test_the_system_default_is_flagged_for_the_picker(fake_sd):
    assert [d.name for d in audio_mod.list_devices("input") if d.is_default] == ["Microphone (Chat-Audeze Maxwell)"]
    assert audio_mod.list_devices("output")[0].as_option()["default"] is True


def test_a_saved_name_survives_index_shuffling(fake_sd):
    """Windows renumbers devices when things are plugged in, so settings store NAMES."""
    assert audio_mod.resolve_device("Microphone (Chat-Audeze Maxwell)", "input") == 1
    assert audio_mod.resolve_device("Audeze", "input") == 1        # substring
    assert audio_mod.resolve_device("audeze maxwell", "input") == 1  # case-insensitive
    assert audio_mod.resolve_device("Headphones", "output") == 3


def test_empty_or_unknown_means_system_default(fake_sd):
    for spec in ("", "   ", None, "a device that does not exist"):
        assert audio_mod.resolve_device(spec, "input") is None


def test_an_index_is_accepted_but_only_if_it_is_that_kind(fake_sd):
    assert audio_mod.resolve_device(1, "input") == 1
    assert audio_mod.resolve_device("1", "input") == 1
    assert audio_mod.resolve_device(2, "input") is None      # index 2 is an output


# ----------------------------------------------------------------------- wav
def make_wav(samples: np.ndarray, rate: int = 24000, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def test_wav_round_trips():
    tone = (0.5 * np.sin(np.linspace(0, 20, 480))).astype(np.float32)
    decoded, rate = audio_mod.decode_wav(make_wav(tone))
    assert rate == 24000 and decoded.shape == tone.shape
    assert np.allclose(decoded, tone, atol=1e-4)


def test_stereo_is_mixed_to_mono():
    stereo = np.repeat(np.array([0.5, -0.5], dtype=np.float32), 2)
    decoded, _ = audio_mod.decode_wav(make_wav(stereo, channels=2))
    assert decoded.shape == (2,)


def test_a_non_pcm16_wav_is_refused():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(1), w.setframerate(8000)
        w.writeframes(b"\x00" * 100)
    with pytest.raises(audio_mod.AudioUnavailable, match="16-bit"):
        audio_mod.decode_wav(buf.getvalue())


def test_play_resolves_the_device_and_reports_duration(fake_sd):
    seconds = audio_mod.play(make_wav(np.zeros(24000, dtype=np.float32)), "Headphones")
    assert seconds == pytest.approx(1.0)
    assert fake_sd.played[-1][2] == 3        # resolved by name


def test_stop_is_safe_without_a_backend(monkeypatch):
    monkeypatch.setattr(audio_mod, "_sd", lambda: (_ for _ in ()).throw(audio_mod.AudioUnavailable("none")))
    audio_mod.stop()                          # barge-in must never raise


# ------------------------------------------------------------- silent microphone
def test_a_muted_microphone_reads_below_the_silence_floor(monkeypatch):
    """Measured on a real muted headset, 2026-09-09: rms 0.000017."""
    monkeypatch.setattr(audio_mod, "_sd", lambda: FakeSd(frames=np.full(512, 1.7e-5, dtype=np.float32)))
    assert audio_mod.input_level(None, 0.1) < audio_mod.SILENT_RMS


def test_a_live_microphone_reads_above_it(monkeypatch):
    rng = np.random.default_rng(0)
    monkeypatch.setattr(audio_mod, "_sd", lambda: FakeSd(frames=(rng.standard_normal(512) * 0.05).astype(np.float32)))
    assert audio_mod.input_level(None, 0.1) > audio_mod.SILENT_RMS


def test_meter_exits_cleanly_on_ctrl_c(monkeypatch, capsys):
    """Ctrl+C is how the meter is meant to end — it must not print a traceback."""
    monkeypatch.setattr(audio_mod, "_sd", lambda: FakeSd(raise_on_read=KeyboardInterrupt()))
    assert audio_mod.meter(None, seconds=5) == 1        # reports silence, does not raise
    assert "muted or blocked" in capsys.readouterr().out


def test_pcm16_conversion_clips_instead_of_wrapping():
    loud = np.array([2.0, -2.0, 0.0], dtype=np.float32)
    values = np.frombuffer(audio_mod.to_pcm16(loud), dtype="<i2")
    assert values.tolist() == [32767, -32767, 0]
