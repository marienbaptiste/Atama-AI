"""Microphone and speaker recovery (spec §9): missing at launch, pulled out mid-session, back again.

A fake PortAudio with PortAudio's real quirk: the device list is a SNAPSHOT taken at
initialisation, so a device plugged in later is invisible until `_terminate/_initialize`.
No real device is opened.
"""
from __future__ import annotations

import io
import threading
import time
import wave

import numpy as np
import pytest

from backend import audio as audio_mod

MAPPER_IN = "Microsoft Sound Mapper - Input"
MAPPER_OUT = "Microsoft Sound Mapper - Output"


class PortAudioError(Exception):
    pass


class FakeStream:
    def __init__(self, pa, entry):
        self.pa, self.entry = pa, entry

    def _alive(self):
        if self.entry not in self.pa.plugged:
            raise PortAudioError("Unanticipated host error")

    def start(self):
        pass

    def read(self, n):
        self._alive()
        return np.full((n, 1), 0.1, np.float32), False

    def write(self, data):
        time.sleep(0.001)                      # a real blocking write paces itself
        if self.pa.fail_writes:
            self.pa.fail_writes -= 1
            raise PortAudioError("Unanticipated host error")
        self._alive()

    def stop(self):
        pass

    def close(self):
        pass


class FakePa:
    PortAudioError = PortAudioError

    def __init__(self):
        self.default = type("D", (), {})()
        self.rescans = 0
        self.opened: list[str] = []
        self.on_rescan = None
        self.fail_writes = 0
        self.reset([])

    def reset(self, entries):
        self.plugged = list(entries)            # what is physically connected
        self.snapshot = list(entries)           # what PortAudio saw at its last initialisation
        self._defaults()

    def _defaults(self):
        ins = [i for i, (_, k) in enumerate(self.snapshot) if k == "input"]
        outs = [i for i, (_, k) in enumerate(self.snapshot) if k == "output"]
        self.default.device = [ins[-1] if ins else -1, outs[-1] if outs else -1]

    def query_devices(self, index=None, kind=None):
        rows = [{"name": n, "max_input_channels": 1 if k == "input" else 0,
                 "max_output_channels": 2 if k == "output" else 0, "default_samplerate": 48000}
                for n, k in self.snapshot]
        if isinstance(index, int):
            if not 0 <= index < len(rows):
                raise PortAudioError(f"Error querying device {index}")
            return rows[index]
        return rows

    def _terminate(self):
        pass

    def _initialize(self):
        self.rescans += 1
        if self.on_rescan is not None:
            self.on_rescan(self)
        self.snapshot = list(self.plugged)
        self._defaults()

    def _open(self, device, kind):
        index = self.default.device[0 if kind == "input" else 1] if device is None else device
        if not 0 <= index < len(self.snapshot):
            raise PortAudioError(f"Error querying device {index}")
        entry = self.snapshot[index]
        if entry not in self.plugged:
            raise PortAudioError("Unanticipated host error")
        self.opened.append(entry[0])
        return FakeStream(self, entry)

    def InputStream(self, device=None, **kw):  # noqa: N802 - mirrors sounddevice
        return self._open(device, "input")

    def OutputStream(self, device=None, **kw):  # noqa: N802
        return self._open(device, "output")


@pytest.fixture
def pa(monkeypatch):
    fake = FakePa()
    monkeypatch.setattr(audio_mod, "_sd", lambda: fake)
    monkeypatch.setattr(audio_mod, "RETRY_S", 0.01)
    monkeypatch.setattr(audio_mod, "PROBE_S", 0.0)
    monkeypatch.setattr(audio_mod, "_open_streams", 0)
    return fake


def mic(spec="", idle=lambda: True):
    statuses: list[tuple[str, str]] = []
    gen = audio_mod.capture_resilient(spec, 512, stop=threading.Event(),
                                      on_status=lambda s, d: statuses.append((s, d)), idle=idle)
    return gen, statuses


def wav(seconds: float, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate)
        w.writeframes(np.zeros(int(seconds * rate), dtype="<i2").tobytes())
    return buf.getvalue()


# ---------------------------------------------------------------- microphone
def test_no_microphone_at_launch_is_waited_for_not_fatal(pa):
    pa.on_rescan = lambda f: f.rescans >= 2 and setattr(f, "plugged", [("USB Mic", "input")])
    gen, statuses = mic()
    assert next(gen).shape == (512,)
    assert statuses == [("missing", statuses[0][1]), ("ok", "USB Mic")]   # reported once, not per retry
    gen.close()


def test_a_microphone_pulled_out_mid_session_comes_back(pa):
    pa.reset([("USB Mic", "input")])
    gen, statuses = mic()
    next(gen)
    pa.plugged = []                                                        # pulled out
    pa.on_rescan = lambda f: f.rescans >= 2 and setattr(f, "plugged", [("USB Mic", "input")])
    next(gen)
    assert [s for s, _ in statuses] == ["ok", "lost", "missing", "ok"]
    gen.close()


def test_a_missing_chosen_device_uses_the_default_then_switches_back(pa):
    pa.reset([(MAPPER_IN, "input"), ("Laptop Mic", "input")])
    gen, statuses = mic("Headset")
    next(gen)
    assert statuses[0][0] == "fallback" and pa.opened == [MAPPER_IN]      # the mapper follows the OS
    pa.plugged.append(("Headset Microphone", "input"))
    next(gen)                                                              # the probe finds it
    assert statuses[-1] == ("ok", "Headset Microphone")
    assert pa.opened[-1] == "Headset Microphone"
    gen.close()


def test_no_probing_while_the_student_is_talking(pa):
    """A probe closes the stream for a moment; mid push-to-talk that would lose their words."""
    pa.reset([(MAPPER_IN, "input")])
    gen, _ = mic("Headset", idle=lambda: False)
    for _ in range(5):
        next(gen)
    assert pa.rescans == 0 and pa.opened == [MAPPER_IN]
    gen.close()


def test_rescan_is_refused_while_any_stream_is_open(pa):
    pa.reset([("USB Mic", "input")])
    gen, _ = mic()
    next(gen)
    assert audio_mod.rescan() is False and pa.rescans == 0
    gen.close()                                                            # closes its stream
    assert audio_mod.rescan() is True and pa.rescans == 1


def test_pick_prefers_the_chosen_device_and_says_when_it_fell_back(pa):
    pa.reset([(MAPPER_IN, "input"), ("USB Mic", "input")])
    assert audio_mod.pick("USB", "input") == (1, False)
    assert audio_mod.pick("", "input") == (0, False)
    assert audio_mod.pick("Headset", "input") == (0, True)


# ------------------------------------------------------------------ speakers
def test_playback_survives_the_output_dropping_out(pa):
    pa.reset([(MAPPER_OUT, "output"), ("Speakers", "output")])
    player = audio_mod.Player(None)
    player.open()
    try:
        pa.fail_writes = 1
        assert player.play(wav(0.1)) == pytest.approx(0.1, abs=0.01)
        assert len(pa.opened) >= 2 and pa.opened[0] == MAPPER_OUT
    finally:
        player.close()


def test_a_missing_chosen_speaker_plays_on_the_default(pa):
    pa.reset([(MAPPER_OUT, "output")])
    player = audio_mod.Player("Headphones")
    try:
        player.play(wav(0.05))
        assert pa.opened[0] == MAPPER_OUT
    finally:
        player.close()


def test_no_playback_device_at_all_is_reported_not_raised_as_portaudio(pa):
    player = audio_mod.Player(None)
    try:
        with pytest.raises(audio_mod.AudioUnavailable):
            player.play(wav(0.05))
    finally:
        player.close()


# -------------------------------------------------------------- live changes
def test_wake_reopens_on_a_new_choice_at_the_next_idle_moment(pa):
    """The settings panel picked another microphone: use it now, not at the next launch."""
    pa.reset([("USB Mic", "input"), ("Headset Mic", "input")])
    choice = {"spec": "USB"}
    wake = threading.Event()
    talking = {"on": True}
    gen = audio_mod.capture_resilient(lambda: choice["spec"], 512, stop=threading.Event(),
                                      idle=lambda: not talking["on"], wake=wake)
    next(gen)
    choice["spec"] = "Headset"
    wake.set()
    for _ in range(3):
        next(gen)
    assert pa.opened == ["USB Mic"]                  # not while push-to-talk is held
    talking["on"] = False
    next(gen)
    assert pa.opened == ["USB Mic", "Headset Mic"] and not wake.is_set()
    gen.close()


def test_with_a_watcher_there_is_no_timer_probe(pa):
    pa.reset([(MAPPER_IN, "input")])
    gen = audio_mod.capture_resilient("Headset", 512, stop=threading.Event(), wake=threading.Event())
    for _ in range(5):
        next(gen)
    assert pa.rescans == 0 and pa.opened == [MAPPER_IN]
    gen.close()


def test_the_player_switches_output_live(pa):
    pa.reset([(MAPPER_OUT, "output"), ("Speakers", "output"), ("Headphones", "output")])
    player = audio_mod.Player("Speakers")
    try:
        player.play(wav(0.02))
        player.switch("Headphones")
        player.play(wav(0.02))
        assert pa.opened[0] == "Speakers" and pa.opened[-1] == "Headphones"
    finally:
        player.close()
