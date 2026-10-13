"""サーバ API のテスト（ファイル入出力だけ。描画は test_render.py）"""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image

import synth
from conftest import no_ids
from inkdeus.cli import build_parser
from inkdeus.server import create_app, read_local_config


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


# ---- 透かしは任意の機能（廃止した所有者モードの指定は受け付けるが何もしない） ----

def test_config_has_no_owner_mode(client):
    config = client.get("/api/config").get_json()
    assert config["project"] is None and "owner" not in config                     # 所有者まわりの項目は無い
    assert set(config) == {"project", "features", "build", "backup", "missingFolders"}


def test_owner_flag_is_accepted_but_ignored(dirs, capsys):
    _, images, _ = dirs
    plain = build_parser().parse_args(["run", str(images)])
    flagged = build_parser().parse_args(["run", str(images), "--owner"])
    assert flagged.owner is True and plain.owner is False        # 引数としては今も通る
    assert build_parser().parse_args(["open", "demo", "--owner"]).project == "demo"
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--help"])
    assert "廃止" in capsys.readouterr().out


def test_owner_settings_do_not_change_anything(dirs, tmp_path, monkeypatch):
    """INKDEUS_OWNER や inkdeus.local.toml の owner があっても、既定値も応答も変わらない"""
    _, images, _ = dirs
    def config_of(client):
        config = client.get("/api/config").get_json()
        config["build"].pop("boot")                  # 起動 ID は起動のたびに変わる
        return config

    before = create_app(images, watch=False).test_client()
    expected = (config_of(before), no_ids(before.get("/api/params/scene.png?resolve=1").get_json()))
    assert expected[1]["params"]["watermark"]["enabled"] is False
    cfg = tmp_path / "inkdeus.local.toml"
    cfg.write_text('owner = true\nprojects_root = "somewhere"\n', encoding="utf-8")
    assert read_local_config(cfg) == {"owner": True, "projects_root": "somewhere"}   # 読めはする（エラーにしない）
    monkeypatch.setenv("INKDEUS_OWNER", "1")
    monkeypatch.setattr("inkdeus.server.default_local_config", lambda: cfg)
    after = create_app(images, watch=False).test_client()
    assert (config_of(after), no_ids(after.get("/api/params/scene.png?resolve=1").get_json())) == expected
    cfg.write_text("this is not toml = = =", encoding="utf-8")
    assert read_local_config(cfg) == {}


def test_watermark_enabled_is_respected_by_the_api(client):
    """watermark.enabled は素直に効く。enabled の無い設定ファイルを読んでも壊れない"""
    r = client.patch("/api/params/scene.png", data=json.dumps({"watermark": {"enabled": True}}))
    assert r.status_code == 200 and r.get_json()["params"]["watermark"]["enabled"] is True
    r = client.post("/api/set/scene.png", data=json.dumps({"key": "watermark.enabled", "value": False}))
    assert r.status_code == 200 and r.get_json()["params"]["watermark"]["enabled"] is False
    client.put("/api/params/scene.png", data=json.dumps({"version": 2, "watermark": {"text": "x"}}))
    got = client.get("/api/params/scene.png?resolve=1").get_json()
    assert got["warnings"] == [] and got["params"]["watermark"] == {
        "enabled": False, "text": "x", "position": "br", "size": 14, "opacity": 0.35}


# ---- 外部からの操作: 設定の部分更新・状態・統計・監視 ----

def test_resolve_returns_defaults_when_no_file(client):
    r = client.get("/api/params/scene.png?resolve=1").get_json()
    assert r["exists"] is False and r["rev"] is None
    assert r["params"]["version"] == 2 and len(r["params"]["regions"]["person"]["layers"]) == 4


def test_patch_validates_and_writes(client, dirs):
    _, images, _ = dirs
    patch = {"regions": {"background": {"linked": False, "levels": {"brightness": 0.25}}}}
    r = client.patch("/api/params/scene.png", data=json.dumps(patch))
    assert r.status_code == 200
    body = r.get_json()
    assert body["params"]["regions"]["background"]["levels"]["brightness"] == 0.25
    saved = json.loads((images / "inkdeus_out" / "scene.inkdeus.json").read_text(encoding="utf-8"))
    assert saved == body["params"]
    got = client.get("/api/params/scene.png")
    assert got.headers["X-Inkdeus-Rev"] == body["rev"]
    assert client.get("/api/params/scene.png?resolve=1").get_json()["rev"] == body["rev"]
    # 検証に通らない変更は何も書かない
    bad = client.patch("/api/params/scene.png", data=json.dumps({"lines": {"sigma": 99, "nope": 1}}))
    assert bad.status_code == 400
    problems = "\n".join(bad.get_json()["problems"])
    assert "lines.sigma" in problems and "未知のキー: lines.nope" in problems
    assert client.get("/api/params/scene.png").headers["X-Inkdeus-Rev"] == body["rev"]
    assert client.patch("/api/params/missing.png", data=b"{}").status_code == 404


def test_set_and_style_endpoints(client):
    r = client.post("/api/set/scene.png", data=json.dumps({"key": "regions.person.layers.0.angle", "value": 60}))
    assert r.status_code == 200 and r.get_json()["params"]["regions"]["person"]["layers"][0]["angle"] == 60
    assert client.post("/api/set/scene.png", data=json.dumps({"key": "lines.sigma", "value": "thick"})).status_code == 400
    assert client.post("/api/set/scene.png", data=json.dumps({"key": "lines.sigma"})).status_code == 400
    r = client.post("/api/style/scene.png", data=json.dumps({"style": "ハッチ(1)", "region": "background"}))
    body = r.get_json()
    assert r.status_code == 200 and body["style"] == "hatch1"
    assert len(body["params"]["regions"]["background"]["layers"]) == 1
    assert body["params"]["regions"]["person"]["layers"][0]["angle"] == 60          # 先の変更は残る
    assert client.post("/api/style/scene.png", data=json.dumps({"style": "nope"})).status_code == 400
    names = [s["name"] for s in client.get("/api/styles").get_json()["styles"]]
    assert "crosshatch-ultrafine" in names


def test_state_roundtrip(client, dirs):
    _, images, masks = dirs
    s = client.get("/api/state").get_json()
    assert s["current"] is None and s["images"] == 2 and "owner" not in s and s["clients"] == 0
    assert s["source"] == str(images) and s["masks"] == str(masks) and s["out"] == str(images / "inkdeus_out")
    assert s["project"] is None
    assert client.post("/api/state", data=json.dumps({"current": "scene.png"})).get_json()["current"] == "scene.png"
    assert client.get("/api/state").get_json()["current"] == "scene.png"
    assert client.post("/api/state", data=json.dumps({"current": "missing.png"})).status_code == 404


def test_stats_roundtrip_carries_rev(client):
    empty = client.get("/api/stats/scene.png").get_json()
    assert empty["stats"] is None and empty["fresh"] is False and empty["currentRev"] is None
    rev = client.patch("/api/params/scene.png", data=b"{}").get_json()["rev"]
    stats = {"rev": rev, "renderMs": 12.5, "output": {"all": {"blackRatio": 0.3}}}
    assert client.post("/api/stats/scene.png", data=json.dumps(stats)).status_code == 200
    got = client.get("/api/stats/scene.png").get_json()
    assert got["fresh"] is True and got["currentRev"] == rev
    assert got["stats"]["output"] == stats["output"] and got["stats"]["name"] == "scene.png"
    # 設定が変わると、手元の統計は「古い」と分かる
    client.post("/api/set/scene.png", data=json.dumps({"key": "lines.sigma", "value": 1.5}))
    assert client.get("/api/stats/scene.png").get_json()["fresh"] is False
    assert client.post("/api/stats/missing.png", data=b"{}").status_code == 404


def test_events_for_own_and_external_changes(dirs):
    _, images, _ = dirs
    app = create_app(images, watch=False)
    session = app.config["INKDEUS_SESSION"]
    client = app.test_client()
    q = session.hub.subscribe()
    session.scan()                                   # 起動時点の記録（イベントは出ない）
    assert q.empty()
    # ブラウザ自身の保存: 送り主の ID が付く
    rev = client.put("/api/params/scene.png", data=b'{"version": 2}', headers={"X-Inkdeus-Client": "tab-1"}).get_json()["rev"]
    assert q.get_nowait() == {"type": "params", "folder": "default", "name": "scene.png", "version": "", "rev": rev, "source": "tab-1"}
    assert session.scan() == [] and q.empty()        # サーバ自身が書いた物は外部変更として数えない
    # 外部（エディタなど）がファイルを書き換えた
    path = images / "inkdeus_out" / "scene.inkdeus.json"
    path.write_text('{"version": 2, "unit": 3}', encoding="utf-8")
    events = session.scan()
    assert len(events) == 1 and events[0]["source"] is None and events[0]["name"] == "scene.png"
    assert events[0]["rev"] != rev and q.get_nowait() == events[0]
    assert session.scan() == []                      # 同じ内容では二度出ない
    # 外部からの PATCH も source なしで届く
    r = client.patch("/api/params/scene.png", data=b'{"unit": 2}').get_json()
    assert q.get_nowait() == {"type": "params", "folder": "default", "name": "scene.png", "version": "", "rev": r["rev"], "source": None}
    # 画像の切り替え・書き出しの指示
    assert client.post("/api/command", data=json.dumps({"type": "open", "folder": "default", "name": "plain.png"})).get_json()["clients"] == 1
    assert q.get_nowait() == {"type": "open", "folder": "default", "name": "plain.png", "version": None}
    assert client.post("/api/command", data=json.dumps({"type": "explode", "name": "plain.png"})).status_code == 400
    assert client.post("/api/command", data=json.dumps({"type": "open", "folder": "default", "name": "nope.png"})).status_code == 404
    session.hub.unsubscribe(q)
    assert session.hub.count() == 0


def test_sse_stream_format(dirs):
    _, images, _ = dirs
    app = create_app(images, watch=False)
    client = app.test_client()
    resp = client.get("/api/events", buffered=False)
    assert resp.mimetype == "text/event-stream"
    chunks = resp.response
    assert next(chunks).startswith(b":")
    hello = json.loads(next(chunks).decode("utf-8")[6:])                 # 接続の直後に、ビルド識別と起動 ID が届く
    assert hello["type"] == "hello" and hello["build"] == client.get("/api/config").get_json()["build"]
    client.patch("/api/params/scene.png", data=b"{}")
    line = next(chunks).decode("utf-8")
    assert line.startswith("data: ") and line.endswith("\n\n")
    assert json.loads(line[6:])["type"] == "params"
    resp.close()


# ---- 書き出しの通知・一括書き出しの進み具合 ----

def test_export_status_and_exported_event(dirs):
    import os
    import time

    _, images, masks = dirs
    app = create_app(images, mask_dir=masks, watch=False)
    client = app.test_client()
    hub = app.config["INKDEUS_SESSION"].hub
    editor, listener = hub.subscribe(), hub.subscribe(listener=True)
    assert hub.count() == 1                                           # 聞くだけの相手は数えない
    row = lambda: {i["name"]: i for i in client.get("/api/images").get_json()["images"]}["scene.png"]  # noqa: E731
    assert (row()["hasExport"], row()["exportFresh"]) == (False, False)
    r = client.post("/api/export/scene.png", data=png_bytes(), headers={"X-Inkdeus-Rev": "abc"})
    out = images / "inkdeus_out"
    assert r.get_json()["mtime"] == (out / "scene.png").stat().st_mtime
    for q in (editor, listener):                                      # 通知は両方に届く
        ev = q.get_nowait()
        assert ev == {"type": "exported", "folder": "default", "source": "inked", "name": "scene.png", "file": "scene.png", "rev": "abc",
                      "mtime": (out / "scene.png").stat().st_mtime,
                      "version": "", "stamp": (out / "scene.png").stat().st_mtime_ns // 1_000_000}
    assert (row()["hasExport"], row()["exportFresh"]) == (True, False)    # 設定がまだ保存されていない
    client.put("/api/params/scene.png", data=json.dumps({"version": 2}), content_type="application/json")
    past = time.time_ns() - 60_000_000_000
    os.utime(out / "scene.inkdeus.json", ns=(past, past))
    os.utime(images / "scene.png", ns=(past, past))
    os.utime(masks / "scene.png", ns=(past, past))
    assert (row()["hasExport"], row()["exportFresh"]) == (True, True)
    os.utime(out / "scene.inkdeus.json", ns=(time.time_ns() + 60_000_000_000,) * 2)   # 設定がその後変わった
    assert row()["exportFresh"] is False
    r = client.post("/api/command", data=json.dumps({"type": "export-all", "force": True})).get_json()
    assert r["clients"] == 1 and r["force"] is True
    assert editor.get_nowait()["type"] == "params" and editor.get_nowait() == {"type": "export-all", "force": True,
                                                                              "defaultOnly": False, "allFolders": False}


def test_batch_progress_roundtrip(client):
    assert client.get("/api/batch").get_json() == {"seq": 0, "running": False}
    post = lambda body: client.post("/api/batch", data=json.dumps(body)).get_json()  # noqa: E731
    first = post({"started": True, "running": True, "total": 3, "done": 0, "junk": "x"})
    assert first["seq"] == 1 and first["running"] is True and "junk" not in first
    assert post({"running": True, "total": 3, "done": 2, "exported": 2})["seq"] == 1
    done = post({"running": False, "total": 3, "done": 3, "exported": 3, "skipped": 0, "failed": 0})
    assert done["running"] is False and client.get("/api/state").get_json()["batch"] == client.get("/api/batch").get_json()
    assert post({"started": True, "running": True})["seq"] == 2
    assert client.post("/api/batch", data=json.dumps({"running": True}),
                       headers={"Origin": "http://evil.example"}).status_code == 403
