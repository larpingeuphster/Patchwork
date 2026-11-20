import math
import struct

import pytest

from patchwork import mesh


def torus(rings=24, sides=12):
    pts, faces = [], []
    for i in range(rings):
        for j in range(sides):
            u, v = 2 * math.pi * i / rings, 2 * math.pi * j / sides
            pts.append(((1 + 0.35 * math.cos(v)) * math.cos(u), 0.35 * math.sin(v), (1 + 0.35 * math.cos(v)) * math.sin(u)))
    for i in range(rings):
        for j in range(sides):
            a, b = i * sides + j, ((i + 1) % rings) * sides + j
            c, d = ((i + 1) % rings) * sides + (j + 1) % sides, i * sides + (j + 1) % sides
            faces += [(a, b, c), (a, c, d)]
    return pts, faces


@pytest.mark.parametrize("version", ["2.00", "4.00", "5.00"])
def test_binary_meshes(version):
    pts, faces = torus()
    data = mesh.make("4.00" if version == "5.00" else version, pts, faces)
    if version == "5.00":  # 5.00 = 4.00 header plus two FACS fields
        body = data[len(b"version 4.00\n"):]
        header = struct.pack("<H", 32) + body[2:24] + struct.pack("<2I", 0, 0)
        data = b"version 5.00\n" + header + body[24:]
    m = mesh.parse(data)
    assert len(m.positions) == len(pts) and len(m.faces) == len(faces)
    assert mesh.render(m, 64).startswith(b"\x89PNG")


def test_chunked_mesh_and_draco():
    pts, faces = torus(8, 6)
    verts = b"".join(struct.pack("<3f3f2f4b4B", *p, 0, 1, 0, 0, 0, 0, 0, 0, 0, 255, 255, 255, 255) for p in pts)
    core = struct.pack("<I", len(pts)) + verts + struct.pack("<I", len(faces)) + b"".join(struct.pack("<3I", *f) for f in faces)
    data = b"version 7.00\n" + b"COREMESH" + struct.pack("<2I", 1, len(core)) + core
    assert len(mesh.parse(data).faces) == len(faces)
    import DracoPy
    import numpy as np
    blob = b"\x01\x00\x00\x00" + DracoPy.encode(np.array(pts, dtype=np.float32), np.array(faces, dtype=np.uint32))
    draco = b"version 7.00\n" + b"COREMESH" + struct.pack("<2I", 2, len(blob)) + blob
    m = mesh.parse(draco)
    assert len(m.faces) == len(faces) and len(m.positions) >= 3
    assert mesh.render(m, 64).startswith(b"\x89PNG")
    junk = b"version 7.00\n" + b"COREMESH" + struct.pack("<2I", 2, 4) + b"\0" * 4
    with pytest.raises(mesh.MeshError, match="recognise"):
        mesh.parse(junk)


def test_text_mesh():
    data = b"version 1.01\n1\n[0,0,0][0,1,0][0,0,0][1,0,0][0,1,0][1,0,0][0,1,0][0,1,0][0,1,0]"
    m = mesh.parse(data)
    assert len(m.positions) == 3 and m.faces == [(0, 1, 2)]


def test_viewer_data_is_normalised_and_capped():
    pts, faces = torus(40, 30)
    v = mesh.for_viewer(mesh.parse(mesh.make("4.00", pts, faces)), max_faces=500)
    assert len(v["faces"]) == 1500 and max(abs(c) for c in v["positions"]) <= 1.0001


def draco_v7(pts, faces):
    import DracoPy
    import numpy as np
    blob = b"\x01\x00\x00\x00" + DracoPy.encode(np.array(pts, dtype=np.float32), np.array(faces, dtype=np.uint32))
    return b"version 7.00\n" + b"COREMESH" + struct.pack("<2I", 2, len(blob)) + blob


def test_replacer_finds_a_draco_cached_mesh_by_shape(tmp_path):
    from patchwork import cache, swapper
    from patchwork.config import KIND_ASSET, Swap
    from patchwork.downloads import Downloads
    from tests.test_cache import make_store

    scythe = torus(30, 12)
    other = torus(20, 10)
    scaled = ([(x * 2, y, z) for x, y, z in scythe[0]], scythe[1])  # same triangles, different size
    fancy = torus(36, 14)
    downloads = {101: mesh.make("4.00", *scythe), 202: mesh.make("4.00", *fancy)}
    cached = [draco_v7(*scythe), draco_v7(*other), draco_v7(*scaled)]
    store = make_store(tmp_path, [(bytes([i + 1]) * 16, cache.wrap(f"https://x/{i}", "roblox-assettypeid: 4\r\n", b), 10)
                                  for i, b in enumerate(cached)])
    sw = swapper.CacheSwapper(store, tmp_path / "app", Downloads(tmp_path / "dl"), lambda h: None,
                              idmap=swapper.IdMap(tmp_path / "app" / "idmap.json", fetch=downloads.__getitem__))
    swapper.fetch_asset = downloads.__getitem__
    sw.set_swaps([Swap("Skin", [101], KIND_ASSET, "202")])
    sw.tick()

    def body(i):
        conn = store.connect()
        try:
            return store.read_body(conn, bytes([i + 1]) * 16)[0]
        finally:
            conn.close()
    assert body(0) == downloads[202]  # the Draco copy of the scythe got the fancy skin
    assert body(1) == cached[1] and body(2) == cached[2]  # different shape / different size untouched
    assert sw.state[101] == "swapped"


CUBE_OBJ = b"""# a cube
v -1 -1 -1
v 1 -1 -1
v 1 1 -1
v -1 1 -1
v -1 -1 1
v 1 -1 1
v 1 1 1
v -1 1 1
vt 0 0
vt 1 0
vt 1 1
vt 0 1
f 1/1 2/2 3/3 4/4
f 5/1 8/4 7/3 6/2
f 1/1 5/2 6/3 2/4
f 2/1 6/2 7/3 3/4
f 3/1 7/2 8/3 4/4
f 5/1 1/2 4/3 8/4
"""


def test_obj_converts_to_a_roblox_mesh():
    data = mesh.from_obj(CUBE_OBJ)
    m = mesh.parse(data)
    assert m.version == "2.00" and len(m.faces) == 12
    assert mesh.shape(m)[1:] == (2.0, 2.0, 2.0)
    assert mesh.is_obj(CUBE_OBJ) and not mesh.is_obj(data)


def test_flat_or_empty_obj_is_refused():
    flat = b"v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nf 1 2 3\nf 1 3 4\nf 1 2 4\nf 2 3 4\n"
    with pytest.raises(mesh.MeshError, match="flat"):
        mesh.from_obj(flat)
    with pytest.raises(mesh.MeshError, match="empty"):
        mesh.from_obj(b"v 0 0 0\n")


def test_cache_tab_replaces_a_mesh_with_an_obj(tmp_path, monkeypatch):
    from patchwork import cache, roblox
    from patchwork.controller import Controller
    from tests.test_cache import make_store
    monkeypatch.setattr(roblox, "running_clients", lambda *a, **k: [])
    pts, faces = torus(12, 8)
    store = make_store(tmp_path, [(b"\x07" * 16, cache.wrap("https://x/bow", "roblox-assettypeid: 4\r\n",
                                                           draco_v7(pts, faces)), 10)])
    ctl = Controller(home=tmp_path / "home", log_dir=tmp_path / "logs", cache_dir=store.directory)
    try:
        obj = tmp_path / "body.obj"
        obj.write_bytes(CUBE_OBJ)
        ctl.cache_replace((b"\x07" * 16).hex(), str(obj))
        ctl.runner.swapper.tick(force=True)
        conn = store.connect()
        got = store.read_body(conn, b"\x07" * 16)[0]
        conn.close()
        assert len(mesh.parse(got).faces) == 12
    finally:
        ctl.shutdown()
