"""Sounds for the Cache tab: how long a cached sound is, and converting a replacement.

Roblox keeps a sound in the format it was uploaded in (usually OGG Vorbis, sometimes MP3).
A replacement is converted to that same format, so any common audio file can be used:
OGG, MP3, WAV, FLAC. Uses soundfile (libsndfile).
"""

from __future__ import annotations

import io


class AudioError(ValueError):
    pass


FORMATS = {"ogg": ("OGG", "VORBIS"), "mp3": ("MP3", "MPEG_LAYER_III"), "wav": ("WAV", "PCM_16")}


def _sf():
    try:
        import soundfile
        return soundfile
    except Exception as exc:  # ImportError, or libsndfile missing
        raise AudioError("sound conversion isn't available in this build") from exc


def duration(data: bytes) -> float | None:
    try:
        info = _sf().info(io.BytesIO(data))
        return info.frames / info.samplerate if info.samplerate else None
    except Exception:
        return None


def describe(data: bytes) -> str:
    try:
        info = _sf().info(io.BytesIO(data))
    except Exception:
        return ""
    secs = info.frames / info.samplerate if info.samplerate else 0
    return f"{secs:.1f} s, {info.samplerate} Hz, {'stereo' if info.channels == 2 else f'{info.channels} channel(s)'}"


def convert(data: bytes, kind: str, max_seconds: float = 600) -> bytes:
    """``data`` (any audio file libsndfile reads) as a ``kind`` (ogg/mp3/wav) file."""
    sf = _sf()
    if kind not in FORMATS:
        raise AudioError(f"Patchwork can't write .{kind} sounds")
    try:
        samples, rate = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
    except Exception as exc:
        raise AudioError("the replacement isn't a sound Patchwork can read (use OGG, MP3, WAV or FLAC)") from exc
    if not len(samples):
        raise AudioError("the replacement sound is empty")
    if len(samples) / rate > max_seconds:
        raise AudioError("the replacement sound is longer than 10 minutes")
    if samples.shape[1] > 2:
        samples = samples[:, :2]
    fmt, subtype = FORMATS[kind]
    out = io.BytesIO()
    if kind == "mp3" and rate not in (32000, 44100, 48000):
        import numpy as np
        n = int(len(samples) * 44100 / rate)
        idx = np.linspace(0, len(samples) - 1, n)
        samples = np.stack([np.interp(idx, np.arange(len(samples)), samples[:, c]) for c in range(samples.shape[1])], 1)
        samples = samples.astype("float32")
        rate = 44100
    sf.write(out, samples, rate, format=fmt, subtype=subtype)
    return out.getvalue()
