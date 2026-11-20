import io
import struct

import lz4.block
import numpy as np
import pytest
import soundfile

from patchwork import audio, rbxm


def wav(seconds=0.5, rate=22050):
    out = io.BytesIO()
    soundfile.write(out, (np.sin(np.linspace(0, 400 * seconds, int(rate * seconds))) * 0.3).astype("float32"), rate,
                    format="WAV")
    return out.getvalue()


def test_convert_to_ogg_and_mp3():
    ogg = audio.convert(wav(), "ogg")
    assert ogg.startswith(b"OggS") and abs(audio.duration(ogg) - 0.5) < 0.05
    mp3 = audio.convert(ogg, "mp3")
    assert audio.duration(mp3) > 0.4
    with pytest.raises(audio.AudioError, match="isn't a sound"):
        audio.convert(b"\x89PNG not audio", "ogg")


def chunk(name, payload, compress=True):
    if compress:
        blob = lz4.block.compress(payload, store_size=False)
        return name.ljust(4, b"\0") + struct.pack("<3I", len(blob), len(payload), 0) + blob
    return name.ljust(4, b"\0") + struct.pack("<3I", 0, len(payload), 0) + payload


def binary_model(classes, name=b"Slash"):
    out = rbxm.BINARY_MAGIC + struct.pack("<HII8x", 0, len(classes), sum(n for _, n in classes))
    for cid, (cname, count) in enumerate(classes):
        inst = struct.pack("<2I", cid, len(cname)) + cname + b"\0" + struct.pack("<I", count) + b"\0" * 4 * count
        out += chunk(b"INST", inst)
    prop = struct.pack("<2I", 0, 4) + b"Name" + b"\x01" + struct.pack("<I", len(name)) + name
    out += chunk(b"PROP", prop)
    return out + chunk(b"END", b"</roblox>", compress=False)


def test_binary_animation_is_recognised():
    s = rbxm.summarise(binary_model([(b"KeyframeSequence", 1), (b"Keyframe", 12), (b"Pose", 40)]))
    assert s.is_animation and s.label == "Animation" and s.classes["Pose"] == 40 and s.names == ["Slash"]
    m = rbxm.summarise(binary_model([(b"Model", 1), (b"ParticleEmitter", 2)], b"Bow"))
    assert m.label == "Model with effects" and "“Bow”" in m.describe()


def test_xml_model():
    s = rbxm.summarise(b'<roblox version="4"><Item class="CurveAnimation"><Properties>'
                       b'<string name="Name">Reload</string></Properties></Item></roblox>')
    assert s.is_animation and s.names == ["Reload"]


def test_cache_tab_converts_a_sound_and_labels_animations(tmp_path, monkeypatch):
    from patchwork import cache, roblox
    from patchwork.controller import Controller
    from tests.test_cache import make_store
    monkeypatch.setattr(roblox, "running_clients", lambda *a, **k: [])
    ogg = audio.convert(wav(1.0), "ogg")
    anim = binary_model([(b"KeyframeSequence", 1), (b"Keyframe", 3)])
    store = make_store(tmp_path, [(b"\x61" * 16, cache.wrap("https://x/s", "", ogg), 10),
                                  (b"\x62" * 16, cache.wrap("https://x/a", "roblox-assettypeid: 24\r\n", anim), 10)])
    ctl = Controller(home=tmp_path / "home", log_dir=tmp_path / "logs", cache_dir=store.directory)
    try:
        rows = {r["hex"]: r for r in ctl.cache_browse()["rows"]}
        assert rows[(b"\x62" * 16).hex()]["group"] == "Animation"
        assert "1.0 s" in ctl.cache_preview((b"\x61" * 16).hex())["detail"]
        assert "KeyframeSequence" in ctl.cache_preview((b"\x62" * 16).hex())["detail"]
        mine = tmp_path / "mine.wav"
        mine.write_bytes(wav(0.3))
        ctl.cache_replace((b"\x61" * 16).hex(), str(mine))
        ctl.runner.swapper.tick(force=True)
        conn = store.connect()
        got = store.read_body(conn, b"\x61" * 16)[0]
        conn.close()
        assert got.startswith(b"OggS") and abs(audio.duration(got) - 0.3) < 0.05
    finally:
        ctl.shutdown()
