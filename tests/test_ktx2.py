import io

import pytest
from PIL import Image

from patchwork import cache, ktx2, swapper
from patchwork.config import KIND_FILE, KIND_REMOVE, Swap
from patchwork.downloads import Downloads
from tests.test_cache import make_store


def picture(seed, size=(64, 64)):
    """A distinctive test picture: quadrants in colours picked by ``seed``."""
    img = Image.new("RGBA", size)
    w, h = size
    cols = [((seed * 70 + i * 90) % 256, (seed * 30 + i * 160) % 256, (seed * 110 + i * 40) % 256, 255) for i in range(4)]
    for x in range(w):
        for y in range(h):
            img.putpixel((x, y), cols[(x * 2 // w) + 2 * (y * 2 // h)])
    return img


def png(img):
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


@pytest.mark.parametrize("fmt", ["BC1", "BC3", "RGBA8"])
@pytest.mark.parametrize("zstd", [False, True])
def test_round_trip(fmt, zstd):
    img = picture(3, (64, 32))
    tex = ktx2.parse(ktx2.make(img, fmt, levels=7, zstd=zstd))
    assert (tex.width, tex.height, len(tex.levels)) == (64, 32, 7)
    assert ktx2.distance(ktx2.signature(ktx2.decode(tex)), ktx2.signature(img)) < 6
    assert ktx2.decode(tex, 6).size == (1, 1)


def test_encode_like_keeps_shape_and_falls_back_to_bc3():
    like = ktx2.parse(ktx2.make(picture(1, (128, 64)), "BC3", levels=8, zstd=True))
    like.vk_format = 146  # BC7, which Patchwork can't write
    out = ktx2.parse(ktx2.encode_like(picture(9, (10, 10)), like))
    assert (out.width, out.height, len(out.levels), out.supercompression) == (128, 64, 8, 2)
    assert out.format_name == "BC3"
    assert ktx2.distance(ktx2.signature(ktx2.decode(out)), ktx2.signature(picture(9, (128, 64)))) < 8


ID_A, ID_B = 111, 222


def setup(tmp_path, swaps, cached):
    store = make_store(tmp_path, [(bytes([i + 1]) * 16, cache.wrap(f"https://x/{i}", "", body), 10)
                                  for i, body in enumerate(cached)])
    downloads = {ID_A: png(picture(1)), ID_B: png(picture(2))}
    sw = swapper.CacheSwapper(store, tmp_path / "app", Downloads(tmp_path / "dl"), lambda h: None,
                              idmap=swapper.IdMap(tmp_path / "app" / "idmap.json", fetch=downloads.__getitem__))
    sw.set_swaps(swaps)
    return store, sw


def body(store, i):
    conn = store.connect()
    try:
        return store.read_body(conn, bytes([i + 1]) * 16)[0]
    finally:
        conn.close()


def test_texture_pack_swap_finds_the_ktx2_copy_by_its_look(tmp_path):
    repl = tmp_path / "pack.png"
    picture(7).save(repl)
    cached = [ktx2.make(picture(1, (128, 128)), "BC1", levels=8, zstd=True),  # what Roblox cached for ID_A
              ktx2.make(picture(2, (64, 64)), "BC3", levels=7),  # ID_B, not swapped
              ktx2.make(picture(5, (64, 64)), "BC3", levels=7)]  # something else
    store, sw = setup(tmp_path, [Swap("Pack", [ID_A], KIND_FILE, str(repl))], cached)
    sw.tick()
    out = ktx2.parse(body(store, 0))
    assert (out.width, out.height, len(out.levels), out.format_name) == (128, 128, 8, "BC1")
    assert ktx2.distance(ktx2.signature(ktx2.decode(out)), ktx2.signature(picture(7))) < 8
    assert body(store, 1) == cached[1] and body(store, 2) == cached[2]
    assert sw.state[ID_A] == "swapped"
    sw.set_swaps([])
    sw.tick()
    assert body(store, 0) == cached[0]  # switched off: original back


def test_look_alikes_are_not_guessed(tmp_path):
    repl = tmp_path / "pack.png"
    picture(7).save(repl)
    twin = ktx2.make(picture(1, (64, 64)), "BC3", levels=1)
    store, sw = setup(tmp_path, [Swap("Pack", [ID_A], KIND_FILE, str(repl))], [twin, twin])
    sw.tick()
    assert body(store, 0) == twin and body(store, 1) == twin


def test_removing_a_ktx2_texture_writes_a_transparent_one(tmp_path):
    cached = [ktx2.make(picture(1, (32, 32)), "BC3", levels=6)]
    store, sw = setup(tmp_path, [Swap("Gone", [ID_A], KIND_REMOVE, "")], cached)
    sw.tick()
    out = ktx2.parse(body(store, 0))
    assert out.width == 32 and ktx2.decode(out).getpixel((5, 5))[3] == 0


def test_cache_report_lists_texture_formats(tmp_path):
    store, sw = setup(tmp_path, [], [ktx2.make(picture(1), "BC3", levels=2, zstd=True)])
    text = "\n".join(sw.report())
    assert "BC3/zstd" in text and "BC3 64x64, 2 level(s), zstd" in text
