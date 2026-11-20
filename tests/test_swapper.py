import io

from patchwork import cache, swapper
from patchwork.config import KIND_ASSET, KIND_FILE, KIND_REMOVE, KIND_URL, Swap
from patchwork.downloads import Downloads
from tests.test_cache import PNG_HEADERS, make_store, png

RED, BLUE, GREEN = png((255, 0, 0, 255)), png((0, 0, 255, 255), (8, 8)), png((0, 255, 0, 255))
MESH_A = b"version 4.00\n" + b"\x00" * 64
MESH_B = b"version 4.00\n" + b"\x07" * 80
OGG = b"OggS" + b"\x00" * 50
ID_RED, ID_MESH, ID_SOUND, ID_BLUE, ID_MESH_B = 101, 202, 303, 404, 505
ASSETS = {ID_RED: RED, ID_MESH: MESH_A, ID_BLUE: BLUE, ID_MESH_B: MESH_B}


def fake_fetch(asset_id):
    if asset_id == ID_SOUND:
        raise swapper.Unavailable("Roblox only lets signed-in players download this asset")
    return ASSETS[asset_id]


def setup(tmp_path, swaps):
    e_red, e_mesh, e_other = b"\x01" * 16, b"\x02" * 16, b"\x03" * 16
    store = make_store(tmp_path, [
        (e_red, cache.wrap("https://c1.rbxcdn.com/red", PNG_HEADERS, RED), 10),
        (e_mesh, cache.wrap("https://c2.rbxcdn.com/m", "roblox-assettypeid: 4\r\n", MESH_A), 10),
        (e_other, cache.wrap("https://c3.rbxcdn.com/o", PNG_HEADERS, GREEN), 10),
    ])
    hits = []
    sw = swapper.CacheSwapper(store, tmp_path / "app", Downloads(tmp_path / "dl"), hits.append,
                              idmap=swapper.IdMap(tmp_path / "app" / "idmap.json", fetch=fake_fetch))
    sw._replacement.__func__  # exists
    swapper.fetch_asset = fake_fetch  # replacement asset IDs use the same fake
    sw.set_swaps(swaps)
    return store, sw, hits, (e_red, e_mesh, e_other)


def body(store, eid):
    conn = store.connect()
    try:
        return store.read_body(conn, eid)[0]
    finally:
        conn.close()


def test_image_and_skin_swaps_apply_and_restore(tmp_path, monkeypatch):
    replacement = tmp_path / "blue.webp"
    from PIL import Image
    Image.new("RGBA", (8, 8), (0, 0, 255, 255)).save(replacement, format="WEBP", lossless=True)
    swaps = [Swap("Red to file", [ID_RED], KIND_FILE, str(replacement)),
             Swap("Skin", [ID_MESH], KIND_ASSET, str(ID_MESH_B)),
             Swap("Sound", [ID_SOUND], KIND_ASSET, str(ID_BLUE))]
    store, sw, hits, (e_red, e_mesh, e_other) = setup(tmp_path, swaps)
    sw.tick()
    assert cache.sniff(body(store, e_red)) == "png"  # webp converted to the slot's format
    from PIL import Image as I
    assert I.open(io.BytesIO(body(store, e_red))).getpixel((0, 0)) == (0, 0, 255, 255)
    assert body(store, e_mesh) == MESH_B  # skin swapped
    assert body(store, e_other) == GREEN  # untouched
    assert {h.asset_id for h in hits} == {ID_RED, ID_MESH}
    assert sw.state[ID_RED] == "swapped" and "signed-in" in sw.state[ID_SOUND]
    hits.clear()
    sw.tick(force=True)  # already applied: nothing new
    assert hits == []
    # turning a swap off puts the original back
    sw.set_swaps(swaps[1:])
    sw.tick()
    assert body(store, e_red) == RED and body(store, e_mesh) == MESH_B
    assert sw.restore_all() == 1
    assert body(store, e_mesh) == MESH_A


def test_remove_blanks_images_but_never_3d(tmp_path):
    swaps = [Swap("No red", [ID_RED], KIND_REMOVE, ""), Swap("No mesh", [ID_MESH], KIND_REMOVE, "")]
    store, sw, hits, (e_red, e_mesh, _) = setup(tmp_path, swaps)
    sw.tick()
    from PIL import Image
    img = Image.open(io.BytesIO(body(store, e_red)))
    assert img.size == (4, 4) and img.getpixel((0, 0))[3] == 0  # transparent, same size
    assert body(store, e_mesh) == MESH_A  # refused
    assert "can't be removed" in sw.state[ID_MESH]


def test_3d_files_must_be_meshes(tmp_path):
    f = tmp_path / "x.png"
    f.write_bytes(BLUE)
    store, sw, hits, (_, e_mesh, _) = setup(tmp_path, [Swap("bad", [ID_MESH], KIND_FILE, str(f))])
    sw.tick()
    assert body(store, e_mesh) == MESH_A and ".obj" in sw.state[ID_MESH]  # a picture can't replace a mesh


def test_roblox_redownload_is_reswapped(tmp_path):
    store, sw, hits, (e_red, _, _) = setup(tmp_path, [Swap("Blue", [ID_RED], KIND_ASSET, str(ID_BLUE))])
    sw.tick()
    assert body(store, e_red) == BLUE
    conn = store.connect()  # Roblox downloads the original again
    raw = cache.wrap("https://c1.rbxcdn.com/red", PNG_HEADERS, RED)
    conn.execute("UPDATE files SET content=?, size=? WHERE id=?", (raw, len(raw), e_red))
    conn.close()
    sw.tick()
    assert body(store, e_red) == BLUE


def test_idmap_remembers_and_backs_off(tmp_path):
    calls = []

    def fetch(i):
        calls.append(i)
        if i == 2:
            raise swapper.Unavailable("nope")
        return RED
    m = swapper.IdMap(tmp_path / "idmap.json", fetch=fetch)
    assert isinstance(m.get(1), swapper.Fingerprint) and m.get(2) == "nope"
    m2 = swapper.IdMap(tmp_path / "idmap.json", fetch=fetch)
    m2.get(1), m2.get(2)
    assert calls == [1, 2]  # both remembered on disk


def test_same_size_replacement_is_still_put_back(tmp_path):
    same_size_blue = png((0, 0, 255, 255))  # same dimensions as RED, so the same byte length
    assert len(same_size_blue) == len(RED)
    ASSETS[606] = same_size_blue
    store, sw, hits, (e_red, _, _) = setup(tmp_path, [Swap("Blue", [ID_RED], KIND_ASSET, "606")])
    sw.tick()
    assert body(store, e_red) == same_size_blue
    assert sw.restore_all() == 1
    assert body(store, e_red) == RED


def test_network_failures_retry_sooner_than_refusals(tmp_path, monkeypatch):
    calls = []

    def fetch(i):
        calls.append(i)
        raise swapper.Unavailable("offline", transient=True) if i == 1 else swapper.Unavailable("sign in")
    m = swapper.IdMap(tmp_path / "idmap.json", fetch=fetch)
    m.get(1), m.get(2)
    later = swapper.time.time() + swapper.RETRY_NETWORK_AFTER + 1
    monkeypatch.setattr(swapper.time, "time", lambda: later)
    m.get(1), m.get(2)
    assert calls == [1, 2, 1]  # only the network failure was retried


def test_assets_linked_through_cached_redirects_need_no_download(tmp_path):
    """If Roblox caches its asset-delivery redirects, IDs are found without downloading anything."""
    calls = []

    def refuse(i):
        calls.append(i)
        raise swapper.Unavailable("Roblox only hands this asset out to signed-in players in the game")

    cdn = "https://fts.rbxcdn.com/abcdef0123"
    e_redirect, e_mesh, e_direct = b"\x0a" * 16, b"\x0b" * 16, b"\x0c" * 16
    store = make_store(tmp_path, [
        (e_redirect, cache.wrap("https://assetdelivery.roblox.com/v1/asset/?id=777&serverplaceid=1",
                                f"location: {cdn}\r\n", b"", status=302), 11),
        (e_mesh, cache.wrap(cdn, "roblox-assettypeid: 4\r\n", MESH_A), 10),
        (e_direct, cache.wrap("https://assetdelivery.roblox.com/v1/asset?id=888", PNG_HEADERS, RED), 10),
    ])
    ASSETS[999] = MESH_B
    logs = []
    sw = swapper.CacheSwapper(store, tmp_path / "app", Downloads(tmp_path / "dl"), logs.append,
                              idmap=swapper.IdMap(tmp_path / "app" / "idmap.json", fetch=refuse))
    swapper.fetch_asset = fake_fetch
    sw.set_swaps([Swap("Skin", [777], KIND_ASSET, "999"), Swap("Gone", [888], KIND_REMOVE, ""),
                  Swap("Unknown", [123], KIND_ASSET, "999")])
    sw.tick()
    assert body(store, e_mesh) == MESH_B  # found via the redirect
    from PIL import Image
    assert Image.open(io.BytesIO(body(store, e_direct))).getpixel((0, 0))[3] == 0  # found directly
    assert calls == [123]  # only the asset with no cache record was looked up
    assert sw.state[777] == "swapped" and sw.state[888] == "swapped"


def test_unidentifiable_assets_are_reported_once_as_a_group(tmp_path, caplog):
    import logging

    def refuse(i):
        raise swapper.Unavailable("signed-in only")
    store = make_store(tmp_path, [])
    sw = swapper.CacheSwapper(store, tmp_path / "app", Downloads(tmp_path / "dl"), lambda h: None,
                              idmap=swapper.IdMap(tmp_path / "app" / "idmap.json", fetch=refuse))
    sw.set_swaps([Swap(f"s{i}", [1000 + i], KIND_ASSET, "5") for i in range(12)])
    with caplog.at_level(logging.WARNING, logger="patchwork"):
        sw.tick()
        sw.tick(force=True)
    grouped = [r.getMessage() for r in caplog.records if "can't be identified" in r.getMessage()]
    assert len(grouped) == 1 and grouped[0].startswith("12 asset(s)") and "and 4 more" in grouped[0]


def test_asset_id_in_url():
    assert swapper.asset_id_in("https://assetdelivery.roblox.com/v1/asset/?id=123&x=1") == 123
    assert swapper.asset_id_in("https://assetdelivery.roblox.com/v2/assetId/456") == 456
    assert swapper.asset_id_in("https://fts.rbxcdn.com/abc?id=5") is None


def test_clear_keeps_swapped_entries_so_they_load_instantly(tmp_path):
    swaps = [Swap("Skin", [ID_MESH], KIND_ASSET, str(ID_MESH_B))]
    store, sw, hits, (e_red, e_mesh, e_other) = setup(tmp_path, swaps)
    big = b"\x04" * 16  # an entry stored as its own file
    conn = store.connect()
    raw = cache.wrap("https://c4.rbxcdn.com/big", PNG_HEADERS, png((1, 2, 3, 255), (128, 128)) + b"\x00" * 9000)
    conn.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?,?)", (big, None, len(raw), 1, 0, 10, 0, 0))
    conn.close()
    store.blob_path(big).parent.mkdir(parents=True, exist_ok=True)
    store.blob_path(big).write_bytes(raw)
    other_file = store.blobs / "notes.txt"  # anything that isn't a cache file is left alone
    other_file.write_text("x")
    sw.tick()
    assert body(store, e_mesh) == MESH_B
    removed, kept, in_use = sw.clear_keeping_swaps()
    assert (removed, kept, in_use) == (3, 1, 0)
    conn = store.connect()
    ids = {bytes(r[0]) for r in conn.execute("SELECT id FROM files")}
    conn.close()
    assert ids == {e_mesh}
    assert not store.blob_path(big).exists() and other_file.exists()
    assert body(store, e_mesh) == MESH_B  # the swap survives the clear
    hits.clear()
    sw.tick(force=True)
    assert hits == [] and sw.stats["swapped"] == 1  # nothing to redo


def test_clear_drops_entries_roblox_replaced_since(tmp_path):
    swaps = [Swap("Skin", [ID_MESH], KIND_ASSET, str(ID_MESH_B))]
    store, sw, hits, (e_red, e_mesh, e_other) = setup(tmp_path, swaps)
    sw.tick()
    conn = store.connect()  # Roblox writes a fresh copy over the swapped one
    conn.execute("UPDATE files SET content = ?, atime = 9 WHERE id = ?",
                 (cache.wrap("https://c2.rbxcdn.com/m", "roblox-assettypeid: 4\r\n", MESH_A), e_mesh))
    conn.commit()
    conn.close()
    assert sw.clear_keeping_swaps() == (3, 0, 0)
