import io
import sqlite3

import pytest

from patchwork import cache

PNG_HEADERS = "content-type: image/png\r\nroblox-assettypeid: 1\r\n"


def png(color=(255, 0, 0, 255), size=(4, 4)):
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGBA", size, color).save(out, format="PNG")
    return out.getvalue()


def make_store(tmp_path, entries):
    """entries: list of (id_bytes, raw_bytes, category)."""
    d = tmp_path / "Roblox"
    (d / "rbx-storage").mkdir(parents=True)
    conn = sqlite3.connect(d / "rbx-storage.db")
    conn.execute("CREATE TABLE files (id BLOB PRIMARY KEY, content BLOB, size INTEGER, hits INTEGER, "
                 "atime INTEGER, category INTEGER, score INTEGER, ttl INTEGER)")
    store = cache.Store(d)
    for eid, raw, category in entries:
        inline = len(raw) <= cache.INLINE_MAX
        conn.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?,?)",
                     (eid, raw if inline else None, len(raw), 1, 0, category, 0, 0))
        if not inline:
            p = store.blob_path(eid)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(raw)
    conn.commit()
    conn.close()
    return store


def test_checksum_key_matches_rbxcache_vectors():
    assert cache.checksum(b"roblox-assettypeid: 1\r\n") == 0xAF46D420
    assert cache.checksum(b"roblox-assettypeid: 4\r\n") == 0xE472056A
    assert cache.checksum(b"roblox-assettypeid: 84\r\n") == 0x599245D5
    assert cache.checksum(b"") == 0


def test_wrapper_round_trip():
    body = png()
    raw = cache.wrap("https://c0.rbxcdn.com/abc", PNG_HEADERS, body)
    h = cache.parse_header(raw)
    assert h.url == "https://c0.rbxcdn.com/abc" and h.status == 200 and h.asset_type == 1
    assert raw[h.body_offset:] == body and h.body_len == len(body) and h.body_sum == cache.checksum(body)
    new = cache.with_new_body(raw, h, b"\x89PNG new")
    h2 = cache.parse_header(new)
    assert new[h2.body_offset:] == b"\x89PNG new"
    assert (h2.body_len, h2.body_sum) == (8, cache.checksum(b"\x89PNG new"))
    assert h2.header_sum == h.header_sum and h2.url == h.url
    assert cache.parse_header(b"not wrapped") is None


@pytest.mark.parametrize("size", [100, 20000])  # inline and blob-file entries
def test_replace_body_inline_and_file(tmp_path, size):
    eid = bytes(range(16))
    body = b"\x89PNG" + b"x" * size
    store = make_store(tmp_path, [(eid, cache.wrap("u", PNG_HEADERS, body), 10)])
    conn = store.connect()
    store.replace_body(conn, eid, b"\x89PNG small")
    got, header = store.read_body(conn, eid)
    assert got == b"\x89PNG small" and header.body_sum == cache.checksum(got)
    assert not store.blob_path(eid).exists()  # small result goes inline
    big = b"\x89PNG" + b"y" * 30000
    store.replace_body(conn, eid, big)
    assert store.read_body(conn, eid)[0] == big and store.blob_path(eid).exists()
    assert conn.execute("SELECT content FROM files").fetchone()[0] is None
    conn.close()


def test_write_rules():
    mesh_a = b"version 4.00\n" + b"\x00" * 40
    mesh_b = b"version 5.00\n" + b"\x01" * 40
    model = b"<roblox!" + b"\x00" * 20
    # skins: like for like
    cache.check_write(mesh_a, None, 10, mesh_b)
    cache.check_write(model, 10, 10, model + b"more")
    cache.check_write(png(), 1, 10, png((0, 0, 255, 255)))
    for current, atype, cat, new in [
        (mesh_a, None, 10, b""),  # can't make a mesh disappear
        (mesh_a, None, 10, png()),  # mesh -> image
        (mesh_a, None, 10, model),  # mesh -> model
        (png(), 1, 10, mesh_a),  # image -> mesh
        (b"\x00", None, 22, b"\x01"),  # collision data
        (png(), 4, 10, png()),  # asset type says Mesh: treated as 3D, 2D replacement refused
    ]:
        with pytest.raises(cache.Refused):
            cache.check_write(current, atype, cat, new)


def test_sniff():
    assert cache.sniff(png()) == "png"
    assert cache.sniff(b"OggS....") == "ogg"
    assert cache.sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    assert cache.sniff(b"\xabKTX 20\xbb") == "ktx2"
    assert cache.sniff(b"") == "empty"
