"""消しゴム・ペンの保存（サーバ側）と、層の id、ctl paint。描画そのものは test_paint_ui.py"""

from __future__ import annotations

import io
import json
import os
import time

import pytest
from PIL import Image

import synth
from conftest import no_ids, serve
from inkdeus import cli, schema
from inkdeus.folders import FolderDef
from inkdeus.server import create_app


def png_bytes(value: int = 128, size=(8, 4)) -> bytes:
    buf = io.BytesIO()
    Image.new("L", size, value).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture()
def env(tmp_path):
    images, more = tmp_path / "images", tmp_path / "more"
    synth.save(images / "scene.png", synth.flat(32, 16, 200))
    synth.save(images / "other.png", synth.flat(32, 16, 100))
    synth.save(more / "scene.png", synth.flat(32, 16, 50))            # 別のフォルダの、同じ名前の画像
    app = create_app(images, out_dir=tmp_path / "out", watch=False, folders=[FolderDef("別", more)])
    return {"app": app, "client": app.test_client(), "out": tmp_path / "out",
            "session": app.config["INKDEUS_SESSION"], "lib": app.config["INKDEUS_LIBRARY"]}


def send(client, method, path, body=None):
    return client.open(path, method=method, data=None if body is None else json.dumps(body),
                       content_type="application/json")


def targets(client, path="/api/paint/scene.png"):
    return [t["target"] for t in client.get(path).get_json()["targets"]]


# ---- 層の id ----

def test_layers_get_ids_and_keep_them():
    p = schema.default_params()
    ids = [l["id"] for r in schema.REGIONS for l in p["regions"][r]["layers"]]
    assert len(ids) == 8 and len(set(ids)) == 8                       # 人物・背景を通して重複しない
    assert all(schema.LAYER_ID_RE.fullmatch(i) for i in ids)
    assert schema.normalize(p, strict=True)[0] == p                   # 整え直しても id は変わらない
    assert schema.default_params() != p and no_ids(schema.default_params()) == no_ids(p)
    # 並べ替えでは変わらない。重複（複製された層）は後ろの物だけ振り直す
    layers = p["regions"]["person"]["layers"]
    swapped = schema.normalize({"regions": {"person": {"layers": [layers[1], layers[0], dict(layers[0])]}}}, strict=True)[0]
    got = [l["id"] for l in swapped["regions"]["person"]["layers"]]
    assert got[:2] == [layers[1]["id"], layers[0]["id"]] and got[2] not in got[:2]
    # 使えない id は、外部からの変更では打ち間違いとして断る
    with pytest.raises(schema.ValidationError):
        schema.normalize({"regions": {"person": {"layers": [{"id": "../x"}]}}}, strict=True)
    assert schema.lacks_layer_ids({"version": 2}) and not schema.lacks_layer_ids(p)
    # スタイルの適用では新しい id になる（前の層のマスクを引き継がない）
    styled = schema.apply_style(p, "hatch1", "person")
    assert styled["regions"]["person"]["layers"][0]["id"] not in ids
    assert len(schema.layer_ids(styled)) == 5
    # 層を丸ごと書き直す変更（id なし）は、同じ位置の層の id を引き継ぐ
    patched = schema.inherit_layer_ids(schema.merge_patch(p, {"regions": {"person": {"layers": [{"angle": 10}, {"angle": 20}]}}}), p)
    assert [l["id"] for l in patched["regions"]["person"]["layers"]] == ids[:2]


def test_ids_are_filled_into_existing_files_and_saved(env):
    client, out = env["client"], env["out"]
    out.mkdir()
    path = out / "scene.inkdeus.json"
    path.write_text('{"version": 2, "unit": 2}', encoding="utf-8")    # 層の id が入る前に保存された設定
    first = client.get("/api/params/scene.png?resolve=1").get_json()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved == first["params"] and saved["unit"] == 2
    assert all(l["id"] for l in saved["regions"]["person"]["layers"])
    again = client.get("/api/params/scene.png?resolve=1").get_json()
    assert again["params"] == first["params"] and again["rev"] == first["rev"]       # 読むたびに変わらない
    # id を補うだけの保存では、設定ファイルの更新時刻を変えない（書き出し済みの白黒版を「古い」にしない）
    old = out / "other.inkdeus.json"
    old.write_text('{"version": 2}', encoding="utf-8")
    os.utime(old, ns=(10 ** 18, 10 ** 18))
    client.get("/api/params/other.png?resolve=1")
    assert "id" in old.read_text(encoding="utf-8") and old.stat().st_mtime_ns == 10 ** 18
    filled = json.loads(old.read_text(encoding="utf-8"))
    r = client.put("/api/params/other.png", data=json.dumps(filled), content_type="application/json",
                   headers={"X-Inkdeus-Ids-Only": "1"})
    assert r.status_code == 200 and old.stat().st_mtime_ns == 10 ** 18
    client.put("/api/params/other.png", data=json.dumps(filled), content_type="application/json")
    assert old.stat().st_mtime_ns > 10 ** 18                          # ふつうの保存では更新される
    # 外部からの部分更新でも id は保たれる（1項目の変更・層の丸ごとの置換）
    ids = [l["id"] for l in saved["regions"]["person"]["layers"]]
    r = send(client, "POST", "/api/set/scene.png", {"key": "regions.person.layers.1.angle", "value": 99}).get_json()
    assert [l["id"] for l in r["params"]["regions"]["person"]["layers"]] == ids
    r = client.patch("/api/params/scene.png", data=json.dumps(
        {"regions": {"person": {"layers": [{"type": "hatch", "angle": 5}]}}})).get_json()
    assert [l["id"] for l in r["params"]["regions"]["person"]["layers"]] == ids[:1]
    assert client.get("/api/config").get_json()["features"][:2] == ["layerIds", "paint"]


# ---- 保存 API ----

def test_put_get_delete_and_index(env):
    client, out, session = env["client"], env["out"], env["session"]
    q = session.hub.subscribe()
    assert client.get("/api/paint/scene.png").get_json()["targets"] == []
    assert client.get("/api/paint/scene.png/pen").status_code == 404
    data = png_bytes(200)
    r = client.put("/api/paint/scene.png/pen", data=data, headers={"X-Inkdeus-Client": "tab-1"})
    assert r.status_code == 200 and r.get_json()["bytes"] == len(data) and r.get_json()["target"] == "pen"
    folder = out / "scene.paint"
    assert (folder / "pen.png").read_bytes() == data
    assert q.get_nowait() == {"type": "paint", "action": "put", "folder": "default", "name": "scene.png",
                              "version": "", "targets": ["pen"], "source": "tab-1"}
    client.put("/api/paint/scene.png/layer-abcd1234", data=png_bytes(1))
    client.put("/api/paint/scene.png/all", data=png_bytes(2))
    listing = client.get("/api/paint/scene.png").get_json()
    assert [t["target"] for t in listing["targets"]] == ["all", "layer-abcd1234", "pen"]
    assert listing["dir"] == str(folder) and listing["version"] == "" and listing["folder"] == "default"
    assert all(t["bytes"] > 0 and t["mtime"] > 0 and t["stamp"] > 0 for t in listing["targets"])
    index = json.loads((folder / "index.json").read_text(encoding="utf-8"))
    assert index["version"] == 1 and [t["target"] for t in index["targets"]] == ["all", "layer-abcd1234", "pen"]
    assert index["targets"][2] == {"target": "pen", "file": "pen.png", "bytes": len(data),
                                   "mtime": (folder / "pen.png").stat().st_mtime}
    assert client.get("/api/paint/scene.png/pen").data == data
    # 1つ消す。無い物を消しても失敗にはしない
    while not q.empty():
        q.get_nowait()
    r = client.delete("/api/paint/scene.png/all").get_json()
    assert r["deleted"] == ["all"] and [t["target"] for t in r["targets"]] == ["layer-abcd1234", "pen"]
    assert q.get_nowait()["action"] == "delete"
    assert client.delete("/api/paint/scene.png/all").get_json()["deleted"] == [] and q.empty()
    assert [t["target"] for t in json.loads((folder / "index.json").read_text(encoding="utf-8"))["targets"]] == [
        "layer-abcd1234", "pen"]
    # 全部消すと、フォルダごと無くなる
    r = client.delete("/api/paint/scene.png/all-targets").get_json()
    assert sorted(r["deleted"]) == ["layer-abcd1234", "pen"] and not folder.exists()
    assert q.get_nowait()["targets"] == ["layer-abcd1234", "pen"]


@pytest.mark.parametrize("target", [
    "nope", "layer-", "layer-ABC12345", "layer-ab", "layer-" + "a" * 17, "pen.png", "index", "index.json",
    "solid", "solid-sky", "all-targets", "..", "%2e%2e", "pen%2f..%2f..%2fx", "layer-abcd%2fx", ".hidden", "PEN",
])
def test_target_names_are_validated(env, target):
    client, out = env["client"], env["out"]
    r = client.put(f"/api/paint/scene.png/{target}", data=png_bytes())
    assert r.status_code in (400, 404, 405), (target, r.status_code)
    assert client.get(f"/api/paint/scene.png/{target}").status_code in (400, 404)
    assert not out.exists() or not any(out.rglob("*.png"))            # どこにも書かれていない


def test_rejects_bad_requests(env):
    client, out = env["client"], env["out"]
    assert client.put("/api/paint/scene.png/pen", data=b"not a png").status_code == 400
    assert client.put("/api/paint/nope.png/pen", data=png_bytes()).status_code == 404
    assert client.put("/api/paint/..%2Fscene.png/pen", data=png_bytes()).status_code == 404
    assert client.put("/api/paint/scene.png/pen?version=nope", data=png_bytes()).status_code == 404   # 無いバージョン
    assert client.put("/api/paint/scene.png/pen?version=a%2Fb", data=png_bytes()).status_code == 400
    assert client.put("/api/paint/scene.png/pen?folder=nope", data=png_bytes()).status_code == 404
    assert client.get("/api/paint/nope.png").status_code == 404
    # 他サイトのページからは書けない
    assert client.put("/api/paint/scene.png/pen", data=png_bytes(), headers={"Origin": "http://evil.example"}).status_code == 403
    assert not out.exists() or not any(out.rglob("*.png"))


def test_versions_and_folders_are_independent(env):
    client, out = env["client"], env["out"]
    client.put("/api/params/scene.png", data='{"version": 2}', content_type="application/json")
    client.put("/api/paint/scene.png/pen", data=png_bytes(10))
    client.put("/api/paint/scene.png/lines", data=png_bytes(11))
    # 複製: paint も写る
    assert send(client, "POST", "/api/versions/scene.png", {"version": "白"}).status_code == 200
    assert targets(client, "/api/paint/scene.png?version=白") == ["lines", "pen"]
    assert (out / "scene@白.paint" / "pen.png").read_bytes() == png_bytes(10)
    assert json.loads((out / "scene@白.paint" / "index.json").read_text(encoding="utf-8"))["version"] == 1
    # バージョンごとに独立
    client.put("/api/paint/scene.png/pen?version=白", data=png_bytes(20))
    client.delete("/api/paint/scene.png/lines?version=白")
    assert targets(client) == ["lines", "pen"] and (out / "scene.paint" / "pen.png").read_bytes() == png_bytes(10)
    assert targets(client, "/api/paint/scene.png?version=白") == ["pen"]
    # 名前の変更に付いていく
    assert send(client, "PATCH", "/api/versions/scene.png", {"version": "白", "to": "bg"}).status_code == 200
    assert not (out / "scene@白.paint").exists()
    assert (out / "scene@bg.paint" / "pen.png").read_bytes() == png_bytes(20)
    assert targets(client, "/api/paint/scene.png?version=bg") == ["pen"]
    # 削除で消える（既定の物は残る）
    assert client.delete("/api/versions/scene.png?version=bg").status_code == 200
    assert not (out / "scene@bg.paint").exists() and targets(client) == ["lines", "pen"]
    # フォルダごとに独立（保存先は <out>/@<フォルダ>/ 以下）
    client.put("/api/paint/scene.png/all?folder=別", data=png_bytes(30))
    assert (out / "@別" / "scene.paint" / "all.png").read_bytes() == png_bytes(30)
    assert targets(client, "/api/paint/scene.png?folder=別") == ["all"] and targets(client) == ["lines", "pen"]
    assert targets(client, "/api/paint/other.png") == []              # 別の画像には無い
    # 白黒版（書き出した PNG）の一覧に、paint の置き場所は混ざらない
    listed = [i["name"] for i in client.get("/api/assets/inked").get_json()["items"]]
    assert not any("paint" in name or name in ("pen.png", "lines.png") for name in listed)


def bump(path, seconds: float) -> None:
    """更新時刻を「いま + seconds」にする（書いた順を、時計の細かさに頼らず決めるため）"""
    now = time.time_ns()
    os.utime(path, ns=(now, now + int(seconds * 1e9)))


def test_paint_makes_the_export_stale(env):
    client, out, lib = env["client"], env["out"], env["lib"]
    client.put("/api/params/scene.png", data='{"version": 2}', content_type="application/json")
    client.post("/api/export/scene.png", data=png_bytes(), content_type="image/png")
    bump(out / "scene.png", 5)
    row = lambda: client.get("/api/images").get_json()["images"]      # noqa: E731
    fresh = lambda: {i["name"]: i for i in row()}["scene.png"]["exportFresh"]   # noqa: E731
    assert fresh() is True and lib.export_status(lib.get("scene.png")) == (True, True)
    # 描くと古くなる
    client.put("/api/paint/scene.png/pen", data=png_bytes())
    bump(out / "scene.paint" / "pen.png", 10)
    assert fresh() is False and lib.export_status(lib.get("scene.png")) == (True, False)
    # 書き出し直すと新しくなる
    bump(out / "scene.png", 20)
    assert fresh() is True
    # 消しても古くなる（目録・フォルダの時刻で分かる）
    client.put("/api/paint/scene.png/all", data=png_bytes())
    bump(out / "scene.png", 30)
    assert fresh() is True
    client.delete("/api/paint/scene.png/all")
    bump(out / "scene.paint" / "index.json", 40)
    assert fresh() is False
    # 別のバージョン・別の画像の鮮度には響かない
    assert {i["name"]: i for i in row()}["other.png"]["exportFresh"] is False        # もともと書き出していない
    assert all("paint" not in v["exportFile"] for i in row() for v in i["versions"])


def test_external_layer_changes_prune_masks(env):
    client, out, session = env["client"], env["out"], env["session"]
    params = client.get("/api/params/scene.png?resolve=1").get_json()["params"]
    client.put("/api/params/scene.png", data=json.dumps(params), content_type="application/json")
    ids = [l["id"] for l in params["regions"]["person"]["layers"]]
    for target in (f"layer-{ids[0]}", f"layer-{ids[1]}", "pen", "solid-person"):
        client.put(f"/api/paint/scene.png/{target}", data=png_bytes())
    # 1項目だけの変更では何も消えない
    send(client, "POST", "/api/set/scene.png", {"key": "regions.person.layers.0.angle", "value": 12})
    assert set(targets(client)) == {f"layer-{ids[0]}", f"layer-{ids[1]}", "pen", "solid-person"}
    # 画面自身の保存（送り主つき）では消さない（画面が取り消しのために持っている）
    one = json.loads(json.dumps(params))
    one["regions"]["person"]["layers"] = one["regions"]["person"]["layers"][1:]
    client.put("/api/params/scene.png", data=json.dumps(one), content_type="application/json",
               headers={"X-Inkdeus-Client": "tab-1"})
    assert f"layer-{ids[0]}" in targets(client)
    # スタイルの適用（層が入れ替わる）で、無くなった層のマスクだけが片付く
    q = session.hub.subscribe()
    send(client, "POST", "/api/style/scene.png", {"style": "hatch1", "region": "person"})
    assert targets(client) == ["pen", "solid-person"]
    assert [e["type"] for e in iter(lambda: q.get_nowait() if not q.empty() else None, None)] == ["params"]
    # 層の id が抜けた設定を読んで id を補うだけのときは、片付けない
    client.put("/api/paint/scene.png/layer-abcd1234", data=png_bytes())
    (out / "scene.inkdeus.json").write_text('{"version": 2}', encoding="utf-8")
    client.get("/api/params/scene.png?resolve=1")
    assert "id" in (out / "scene.inkdeus.json").read_text(encoding="utf-8") and "layer-abcd1234" in targets(client)


# ---- ctl paint ----

@pytest.fixture()
def live(tmp_path):
    images = tmp_path / "images"
    synth.save(images / "scene.png", synth.flat(32, 16, 200))
    server, url = serve(create_app(images, out_dir=tmp_path / "out"))
    yield {"port": server.server_port, "url": url, "out": tmp_path / "out", "app": server.app}
    server.shutdown()


@pytest.fixture()
def ctl(live, capsys):
    def run(*args, expect=0):
        capsys.readouterr()
        code = cli.main(["ctl", "--port", str(live["port"]), *args])
        captured = capsys.readouterr()
        assert code == expect, captured.err or captured.out
        return json.loads(captured.out if code == 0 else captured.err)
    return run


def test_ctl_paint_list_and_clear(ctl, live):
    client = live["app"].test_client()
    client.put("/api/params/scene.png", data='{"version": 2}', content_type="application/json")
    send(client, "POST", "/api/versions/scene.png", {"version": "v1"})
    for path in ("pen", "lines", "all", "pen?version=v1"):
        client.put(f"/api/paint/scene.png/{path}", data=png_bytes())
    listed = ctl("paint", "list", "--name", "scene.png")
    assert [t["target"] for t in listed["targets"]] == ["all", "lines", "pen"] and listed["version"] == ""
    assert [t["target"] for t in ctl("paint", "list", "--name", "scene.png", "--version", "v1")["targets"]] == ["pen"]
    assert ctl("paint", "list", "--name", "scene.png", "--folder", "default")["folder"] == "default"
    # 1つ消す・全部消す
    r = ctl("paint", "clear", "lines", "--name", "scene.png")
    assert r["deleted"] == ["lines"] and [t["target"] for t in r["targets"]] == ["all", "pen"]
    r = ctl("paint", "clear", "all-targets", "--name", "scene.png")
    assert sorted(r["deleted"]) == ["all", "pen"] and r["targets"] == []
    assert not (live["out"] / "scene.paint").exists()
    assert [t["target"] for t in ctl("paint", "list", "--name", "scene.png", "--version", "v1")["targets"]] == ["pen"]
    # 使えない対象名・無い画像・無いバージョンは非0終了
    assert "対象" in ctl("paint", "clear", "nope", "--name", "scene.png", expect=1)["error"]
    ctl("paint", "list", "--name", "nope.png", expect=1)
    ctl("paint", "clear", "pen", "--name", "scene.png", "--version", "nope", expect=1)
    # 画像を開いていなければ --name が要る
    assert "--name" in ctl("paint", "list", expect=1)["error"]
