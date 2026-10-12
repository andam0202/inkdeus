"""サーバ API のテスト（ファイル入出力だけ。描画は test_render.py）"""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image

import synth
from inkdeus.cli import build_parser
from inkdeus.server import create_app, resolve_owner


@pytest.fixture()
def dirs(tmp_path):
    images = tmp_path / "images"
    masks = tmp_path / "masks"
    img, mask = synth.scene(64, 48)
    synth.save(images / "scene.png", img)
    synth.save(masks / "scene.png", mask)
    synth.save(images / "plain.png", synth.flat(32, 16, 255))
    (images / "notes.txt").write_text("not an image", encoding="utf-8")
    (tmp_path / "secret.png").write_bytes((images / "plain.png").read_bytes())
    return tmp_path, images, masks


@pytest.fixture()
def client(dirs):
    _, images, masks = dirs
    return create_app(images, mask_dir=masks).test_client()


def png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("L", (4, 4), 128).save(buf, "PNG")
    return buf.getvalue()


def test_list(client):
    data = client.get("/api/images").get_json()
    by_name = {i["name"]: i for i in data["images"]}
    assert set(by_name) == {"scene.png", "plain.png"}
    assert (by_name["scene.png"]["width"], by_name["scene.png"]["height"]) == (64, 48)
    assert by_name["scene.png"]["hasMask"] is True
    assert by_name["plain.png"]["hasMask"] is False
    assert by_name["scene.png"]["hasParams"] is False
    assert data["outDir"].endswith("inkdeus_out")


def test_image_and_mask(client, dirs):
    _, images, masks = dirs
    r = client.get("/api/image/scene.png")
    assert r.status_code == 200 and r.data == (images / "scene.png").read_bytes()
    r = client.get("/api/mask/scene.png")
    assert r.status_code == 200 and r.data == (masks / "scene.png").read_bytes()
    assert client.get("/api/mask/plain.png").status_code == 404


def test_params_roundtrip(client, dirs):
    _, images, _ = dirs
    assert client.get("/api/params/scene.png").status_code == 404
    params = {"version": 1, "layers": [{"type": "hatch", "angle": 45}], "メモ": "日本語"}
    r = client.put("/api/params/scene.png", data=json.dumps(params),
                   content_type="application/json")
    assert r.status_code == 200
    saved = images / "inkdeus_out" / "scene.inkdeus.json"
    assert saved.is_file()
    assert client.get("/api/params/scene.png").get_json() == params
    listed = {i["name"]: i for i in client.get("/api/images").get_json()["images"]}
    assert listed["scene.png"]["hasParams"] is True


def test_params_rejects_bad_json(client):
    assert client.put("/api/params/scene.png", data=b"{oops").status_code == 400
    assert client.put("/api/params/scene.png", data=b"[1, 2]").status_code == 400


def test_export(client, dirs):
    _, images, _ = dirs
    data = png_bytes()
    r = client.post("/api/export/scene.png", data=data, content_type="image/png")
    assert r.status_code == 200
    out = images / "inkdeus_out" / "scene.png"
    assert out.read_bytes() == data
    assert r.get_json()["path"] == str(out)
    assert client.post("/api/export/scene.png", data=b"not a png").status_code == 400
    # 入力画像は書き換わらない
    assert Image.open(images / "scene.png").size == (64, 48)


@pytest.mark.parametrize("name", [
    "../secret.png", "..%2Fsecret.png", "%2e%2e/secret.png", "inkdeus_out/../scene.png",
    "sub/dir/x.png", "notes.txt", "missing.png", "scene.png/..",
])
def test_unknown_names_are_404(client, name):
    assert client.get(f"/api/image/{name}").status_code == 404
    assert client.get(f"/api/mask/{name}").status_code == 404
    assert client.get(f"/api/params/{name}").status_code == 404
    assert client.put(f"/api/params/{name}", data=b"{}").status_code == 404
    assert client.post(f"/api/export/{name}", data=png_bytes()).status_code == 404


def test_traversal_writes_nothing(client, dirs):
    root, _, _ = dirs
    before = sorted(p.name for p in root.iterdir())
    client.post("/api/export/..%2Fevil.png", data=png_bytes())
    client.put("/api/params/..%2Fevil.png", data=b"{}")
    assert sorted(p.name for p in root.iterdir()) == before


def test_cross_origin_write_is_rejected(client):
    r = client.put("/api/params/scene.png", data=b"{}", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = client.put("/api/params/scene.png", data=b"{}", headers={"Origin": "http://localhost"})
    assert r.status_code == 200


def test_single_file_input(dirs):
    _, images, _ = dirs
    c = create_app(images / "plain.png").test_client()
    names = [i["name"] for i in c.get("/api/images").get_json()["images"]]
    assert names == ["plain.png"]
    assert c.get("/api/image/scene.png").status_code == 404


def test_out_dir_must_not_be_input_dir(dirs):
    _, images, _ = dirs
    with pytest.raises(ValueError):
        create_app(images, out_dir=images)


def test_static_index(client):
    r = client.get("/")
    assert r.status_code == 200 and b"inkdeus" in r.data


# ---- 所有者モード ----

def test_config_default_is_not_owner(client, tmp_path):
    assert client.get("/api/config").get_json() == {"owner": False}
    assert resolve_owner(False, {}, tmp_path / "missing.toml") is False


def test_owner_by_flag(dirs, tmp_path):
    _, images, _ = dirs
    args = build_parser().parse_args([str(images), "--owner"])
    owner = resolve_owner(args.owner, {}, tmp_path / "missing.toml")
    assert create_app(images, owner=owner).test_client().get("/api/config").get_json() == {"owner": True}


def test_owner_by_env(dirs, tmp_path):
    _, images, _ = dirs
    owner = resolve_owner(False, {"INKDEUS_OWNER": "1"}, tmp_path / "missing.toml")
    assert create_app(images, owner=owner).test_client().get("/api/config").get_json() == {"owner": True}
    assert resolve_owner(False, {"INKDEUS_OWNER": "0"}, tmp_path / "missing.toml") is False


def test_owner_by_local_toml(dirs, tmp_path):
    _, images, _ = dirs
    cfg = tmp_path / "inkdeus.local.toml"
    cfg.write_text("owner = true\n", encoding="utf-8")
    owner = resolve_owner(False, {}, cfg)
    assert create_app(images, owner=owner).test_client().get("/api/config").get_json() == {"owner": True}
    cfg.write_text("owner = false\n", encoding="utf-8")
    assert resolve_owner(False, {}, cfg) is False
    cfg.write_text("this is not toml = = =", encoding="utf-8")
    assert resolve_owner(False, {}, cfg) is False
