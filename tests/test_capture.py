from patchwork import cache, capture
from patchwork.controller import Controller, UserError
from tests.test_cache import make_store

import pytest

MESH_DEFAULT = b"version 4.00\n" + b"\x01" * 60
MESH_FANCY = b"version 4.00\n" + b"\x09" * 90
KTX = b"\xabKTX 20\xbb\r\n\x1a\n" + b"\x02" * 40
KTX_FANCY = b"\xabKTX 20\xbb\r\n\x1a\n" + b"\x05" * 70
MESH_H = "roblox-assettypeid: 4\r\n"


def add(store, eid, url, body, headers=MESH_H):
    import sqlite3
    raw = cache.wrap(url, headers, body)
    conn = sqlite3.connect(store.db_path)
    conn.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?,?)", (eid, raw, len(raw), 1, 0, 10, 0, 0))
    conn.commit()
    conn.close()


def body(store, eid):
    conn = store.connect()
    try:
        return store.read_body(conn, eid)[0]
    finally:
        conn.close()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from patchwork import roblox
    monkeypatch.setattr(roblox, "running_clients", lambda *a, **k: [])
    store = make_store(tmp_path, [(b"\x0f" * 16, cache.wrap("https://c0.rbxcdn.com/lobby", MESH_H, MESH_DEFAULT), 10)])
    ctl = Controller(home=tmp_path / "home", log_dir=tmp_path / "logs", cache_dir=store.directory)
    yield store, ctl
    ctl.shutdown()


def test_capture_learns_and_swaps_a_skin(setup):
    store, ctl = setup
    default_mesh, default_tex = b"\x11" * 16, b"\x12" * 16
    fancy_mesh, fancy_tex = b"\x21" * 16, b"\x22" * 16

    ctl.capture_start("original")
    add(store, default_mesh, "https://c1.rbxcdn.com/a", MESH_DEFAULT)
    add(store, default_tex, "https://c1.rbxcdn.com/b", KTX, "")
    st = ctl.capture_stop()
    assert {r["hex"] for r in st["original"]} == {default_mesh.hex(), default_tex.hex()}

    ctl.capture_start("replacement")
    add(store, fancy_mesh, "https://c2.rbxcdn.com/c", MESH_FANCY)
    add(store, fancy_tex, "https://c2.rbxcdn.com/d", KTX_FANCY, "")
    st = ctl.capture_stop()
    assert st["guess"] == {default_mesh.hex(): fancy_mesh.hex(), default_tex.hex(): fancy_tex.hex()}

    with pytest.raises(UserError, match="same kind"):
        ctl.capture_save("Bad", {default_mesh.hex(): fancy_tex.hex()})
    ctl.capture_save("Fancy scythe", st["guess"])
    ctl.runner.swapper.tick(force=True)
    assert body(store, default_mesh) == MESH_FANCY and body(store, default_tex) == KTX_FANCY

    # Survives a clear, and Roblox re-downloading the default is swapped again.
    ctl.clear_cache()
    assert body(store, default_mesh) == MESH_FANCY
    import sqlite3
    conn = sqlite3.connect(store.db_path)
    conn.execute("UPDATE files SET content = ?, atime = 5 WHERE id = ?",
                 (cache.wrap("https://c1.rbxcdn.com/a", MESH_H, MESH_DEFAULT), default_mesh))
    conn.commit()
    conn.close()
    ctl.runner.swapper.tick(force=True)
    assert body(store, default_mesh) == MESH_FANCY

    # Switching it off or deleting it puts the originals back.
    ctl.capture_toggle("Fancy scythe", False)
    ctl.runner.swapper.tick(force=True)
    assert body(store, default_mesh) == MESH_DEFAULT and body(store, default_tex) == KTX
    ctl.capture_toggle("Fancy scythe", True)
    ctl.runner.swapper.tick(force=True)
    assert body(store, default_tex) == KTX_FANCY
    ctl.capture_delete("Fancy scythe")
    ctl.runner.swapper.tick(force=True)
    assert body(store, default_tex) == KTX
    assert not list((ctl.home / "captured").glob("*.body"))


def test_captures_are_saved(tmp_path):
    c = capture.Captures(tmp_path)
    c.add(capture.Capture("A/b<>", [capture.Pair("00", c.keep_body(b"x"), "mesh")]))
    again = capture.Captures(tmp_path)
    assert again.items[0].name == "Ab" and again.body(again.items[0].pairs[0].source) == b"x"


def test_cache_report_has_no_tokens(setup, monkeypatch):
    store, ctl = setup
    add(store, b"\x33" * 16, "https://fts.rbxcdn.com/v2/sc0/abc?__token__=secret", KTX, "")
    monkeypatch.setattr(ctl, "open_folder", lambda which: None)
    text = open(ctl.cache_report(), encoding="utf-8").read()
    assert "fts.rbxcdn.com/v2/sc0/abc" in text and "secret" not in text and "ktx2" in text


def test_cache_browser_replace_and_restore(setup, tmp_path):
    from PIL import Image

    from patchwork import ktx2
    store, ctl = setup
    tex_id, ogg_id = b"\x41" * 16, b"\x42" * 16
    original_tex = ktx2.make(Image.new("RGBA", (32, 32), (255, 0, 0, 255)), "BC3", levels=6)
    add(store, tex_id, "https://x/t", original_tex, "")
    add(store, ogg_id, "https://x/s", b"OggS" + b"\x00" * 40, "")
    rows = ctl.cache_browse()["rows"]
    assert {r["kind"] for r in rows} >= {"ktx2", "ogg", "mesh"}
    p = ctl.cache_preview(tex_id.hex())
    assert p["image"].startswith("data:image/png") and "BC3 32x32" in p["detail"]
    assert ctl.cache_preview(ogg_id.hex())["audio"].startswith("data:audio/ogg")

    pic = tmp_path / "blue.png"
    Image.new("RGBA", (8, 8), (0, 0, 255, 255)).save(pic)
    assert ctl.cache_replace(tex_id.hex(), str(pic))
    ctl.runner.swapper.tick(force=True)
    out = ktx2.parse(body(store, tex_id))
    assert out.width == 32 and ktx2.decode(out).getpixel((4, 4))[2] > 200
    assert next(r for r in ctl.cache_browse()["rows"] if r["hex"] == tex_id.hex())["replaced_by"]

    with pytest.raises(UserError, match="ogg"):
        ctl.cache_replace(ogg_id.hex(), str(pic))  # a picture can't replace a sound

    ctl.cache_restore(tex_id.hex())
    ctl.runner.swapper.tick(force=True)
    assert body(store, tex_id) == original_tex


def test_cache_watch_marks_files_loaded_since(setup):
    import sqlite3
    store, ctl = setup
    ctl.cache_watch(True)
    assert ctl.cache_browse()["fresh"] == 0
    add(store, b"\x51" * 16, "https://x/new", MESH_FANCY)  # downloaded after Watch
    conn = sqlite3.connect(store.db_path)  # and an old file Roblox read again
    conn.execute("UPDATE files SET atime = 99, hits = 2 WHERE id = ?", (b"\x0f" * 16,))
    conn.commit()
    conn.close()
    r = ctl.cache_browse()
    assert r["watching"] and r["fresh"] == 2
    assert {x["hex"] for x in r["rows"] if x["fresh"]} == {(b"\x51" * 16).hex(), (b"\x0f" * 16).hex()}
    ctl.cache_watch(False)
    assert ctl.cache_browse()["fresh"] == 0


def test_capture_does_not_clear_by_default(setup):
    store, ctl = setup
    ctl.capture_start("original")
    assert len(ctl.cache_browse()["rows"]) == 1  # the lobby mesh is still there
