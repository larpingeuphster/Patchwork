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


def test_3d_needs_an_asset_id(tmp_path):
    f = tmp_path / "x.png"
    f.write_bytes(BLUE)
    store, sw, hits, (_, e_mesh, _) = setup(tmp_path, [Swap("bad", [ID_MESH], KIND_FILE, str(f))])
    sw.tick()
    assert body(store, e_mesh) == MESH_A and "another Roblox asset ID" in sw.state[ID_MESH]


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
