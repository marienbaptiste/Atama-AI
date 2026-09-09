"""Audio devices, playback and capture (spec §8/§9, M2).

Thin wrapper over PortAudio (`sounddevice`). Two jobs:

* enumerate input/output devices so the settings interface can offer a picker (ADR-022), and
  resolve a saved choice back to a device — by name, so a saved setting survives the index
  shuffling that Windows does when devices come and go;
* play a synthesised WAV, and capture microphone frames as 16 kHz mono PCM16 for the VAD
  (spec §9 — raw frames, never encoded audio).

    python -m backend.audio            # list devices
    python -m backend.audio --test     # play a tone on the configured output
    python -m backend.audio --meter    # live input level + speech probability
"""
from __future__ import annotations

import io
import sys
import wave
from dataclasses import dataclass
from typing import Any, Iterator

import numpy as np

#: The pipeline speaks PCM16 mono at this rate end to end (spec §9).
SAMPLE_RATE = 16000
FRAME_MS = 32
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000


class AudioUnavailable(RuntimeError):
    """No usable audio backend or device. The app should degrade, not crash."""


def _sd():
    try:
        import sounddevice as sd
    except (ImportError, OSError) as exc:  # PortAudio missing on headless boxes
        raise AudioUnavailable(f"audio backend unavailable: {exc}") from None
    return sd


@dataclass(frozen=True)
class Device:
    index: int
    name: str
    kind: str                # "input" | "output"
    channels: int
    samplerate: int
    is_default: bool = False

    def as_option(self) -> dict[str, Any]:
        """One entry for the settings picker."""
        return {"value": self.name, "index": self.index, "label": self.name,
                "kind": self.kind, "default": self.is_default}


def list_devices(kind: str | None = None) -> list[Device]:
    """Every usable device. `kind` filters to "input" or "output"."""
    sd = _sd()
    try:
        defaults = tuple(sd.default.device)
    except (TypeError, ValueError):
        defaults = (-1, -1)
    out: list[Device] = []
    for index, info in enumerate(sd.query_devices()):
        for want, channels_key, default_index in (("input", "max_input_channels", defaults[0]),
                                                  ("output", "max_output_channels", defaults[1])):
            channels = int(info.get(channels_key) or 0)
            if channels <= 0 or (kind and kind != want):
                continue
            out.append(Device(index=index, name=str(info.get("name", f"device {index}")), kind=want,
                              channels=channels, samplerate=int(info.get("default_samplerate") or 0),
                              is_default=(index == default_index)))
    return out


def resolve_device(spec: str | int | None, kind: str) -> int | None:
    """A saved setting -> a device index. None means "use the system default".

    Accepts an index or a name (exact, then case-insensitive substring). Names are preferred in
    settings because indices are not stable across reboots or device plug/unplug.
    """
    if spec is None or str(spec).strip() == "":
        return None
    text = str(spec).strip()
    devices = list_devices(kind)
    if text.lstrip("-").isdigit():
        index = int(text)
        return index if any(d.index == index for d in devices) else None
    for device in devices:
        if device.name == text:
            return device.index
    lowered = text.lower()
    for device in devices:
        if lowered in device.name.lower():
            return device.index
    return None


def decode_wav(data: bytes) -> tuple[np.ndarray, int]:
    """WAV bytes -> (float32 samples in [-1, 1], samplerate). Mono or stereo in, mono out."""
    with wave.open(io.BytesIO(data), "rb") as wav:
        rate, channels, width = wav.getframerate(), wav.getnchannels(), wav.getsampwidth()
        frames = wav.readframes(wav.getnframes())
    if width != 2:
        raise AudioUnavailable(f"expected 16-bit PCM WAV, got {width * 8}-bit")
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, rate


def play(wav_bytes: bytes, device: str | int | None = None, blocking: bool = True) -> float:
    """Play a WAV. Returns its duration in seconds."""
    sd = _sd()
    samples, rate = decode_wav(wav_bytes)
    sd.play(samples, samplerate=rate, device=resolve_device(device, "output"), blocking=blocking)
    return len(samples) / float(rate or SAMPLE_RATE)


def stop() -> None:
    """Cut playback immediately — this is what barge-in calls (spec §8)."""
    try:
        _sd().stop()
    except AudioUnavailable:
        pass


def capture(device: str | int | None = None, frame_samples: int = FRAME_SAMPLES) -> Iterator[np.ndarray]:
    """Yield mono float32 frames at SAMPLE_RATE until the caller stops iterating.

    PortAudio resamples to 16 kHz for us, so the VAD and Whisper both get exactly what spec §9
    asks for without a resampling step of our own.
    """
    sd = _sd()
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        blocksize=frame_samples, device=resolve_device(device, "input")) as stream:
        while True:
            frame, overflowed = stream.read(frame_samples)
            yield frame[:, 0].copy()


def to_pcm16(frame: np.ndarray) -> bytes:
    """float32 [-1, 1] -> PCM16 bytes, the wire format of spec §8's `audio_chunk`."""
    return (np.clip(frame, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


#: Below this RMS over a second of audio, the microphone is delivering digital silence — muted
#: at the OS or hardware level, or blocked by privacy settings. Verified on a muted headset
#: 2026-09-09: rms 0.00001, i.e. three orders of magnitude below a quiet room.
SILENT_RMS = 0.0005


def input_level(device: str | int | None = None, seconds: float = 1.0) -> float:
    """RMS of a short capture. 0.0 means nothing is arriving at all."""
    sd = _sd()
    frames = []
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        blocksize=FRAME_SAMPLES, device=resolve_device(device, "input")) as stream:
        for _ in range(max(1, int(seconds * SAMPLE_RATE / FRAME_SAMPLES))):
            frames.append(stream.read(FRAME_SAMPLES)[0][:, 0])
    audio = np.concatenate(frames) if frames else np.zeros(1, dtype=np.float32)
    return float(np.sqrt(np.mean(np.square(audio))))


def meter(device: str | int | None = None, seconds: float = 30.0) -> int:
    """Live input level and speech probability, so a dead microphone is visible, not mysterious."""
    from backend import config
    from backend.vad import VoiceActivityDetector

    sd = _sd()
    resolved = resolve_device(device, "input")
    name = sd.query_devices(resolved if resolved is not None else sd.default.device[0])["name"]
    print(f"listening on: {name}")
    print("speak — the bar should move. Ctrl+C to stop.\n")
    try:
        vad = VoiceActivityDetector.from_config(config.load())
    except Exception:
        vad = None
    peak_seen = 0.0
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        blocksize=FRAME_SAMPLES, device=resolved) as stream:
        for _ in range(int(seconds * SAMPLE_RATE / FRAME_SAMPLES)):
            frame = stream.read(FRAME_SAMPLES)[0][:, 0]
            level = float(np.sqrt(np.mean(np.square(frame))))
            peak_seen = max(peak_seen, level)
            prob = vad.probability(frame) if vad is not None else 0.0
            bars = int(min(1.0, level * 20) * 40)
            flag = " SPEECH" if prob >= 0.5 else ""
            print(f"\r|{'#' * bars:<40}| rms {level:.4f}  speech {prob:.2f}{flag}   ", end="", flush=True)
    print()
    if peak_seen < SILENT_RMS:
        print(f"\nNothing arrived (peak rms {peak_seen:.5f}). The stream opened, so the device exists —")
        print("it is muted or blocked, not missing. Check, in order:")
        print("  1. the physical mute on the headset/mic")
        print("  2. Windows Settings > Privacy & security > Microphone > 'Let desktop apps access'")
        print("  3. Windows Sound settings > Input > the device level is not 0")
        return 1
    return 0


def _main(argv: list[str]) -> int:
    try:
        devices = list_devices()
    except AudioUnavailable as exc:
        print(exc, file=sys.stderr)
        return 2
    for kind in ("input", "output"):
        print(f"\n{kind.upper()}")
        for d in (x for x in devices if x.kind == kind):
            print(f"  [{d.index:>2}] {'*' if d.is_default else ' '} {d.name[:58]:<58} {d.samplerate or '?'} Hz")
    print("\n* = system default. Put a NAME (or a substring of one) in AUDIO_INPUT_DEVICE /"
          "\nAUDIO_OUTPUT_DEVICE — names survive reboots, indices do not.")
    if "--meter" in argv:
        from backend import config
        return meter(config.load().AUDIO_INPUT_DEVICE)
    if "--test" in argv:
        from backend import config
        cfg = config.load()
        tone = (0.2 * np.sin(2 * np.pi * 440 * np.arange(SAMPLE_RATE) / SAMPLE_RATE)).astype(np.float32)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(SAMPLE_RATE)
            w.writeframes((tone * 32767).astype("<i2").tobytes())
        print(f"\nplaying a 1 s tone on {cfg.AUDIO_OUTPUT_DEVICE or 'the system default'}…")
        play(buf.getvalue(), cfg.AUDIO_OUTPUT_DEVICE)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
