"""白黒版のバージョン（同じ元画像から作る、設定の違う白黒版）: ファイル名の規則・API・素材の一覧"""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image

import synth
from inkdeus.server import Library, VersionError, create_app


def png_bytes(value: int = 128) -> bytes:
    buf = io.BytesIO()
    Image.new("L", (4, 4), value).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture()
def env(tmp_path):
    images = tmp_path / "images"
    synth.save(images / "scene.png", synth.flat(32, 16, 200))
    synth.save(images / "other2.png", synth.flat(32, 16, 100))
    synth.save(images / "other.png", synth.flat(32, 16, 100))
    synth.save(images / "scene@alt.png", synth.flat(32, 16, 50))      # 名前に @ を含む元画像
    app = create_app(images, out_dir=tmp_path / "out", watch=False)
    return {"app": app, "client": app.test_client(), "out": tmp_path / "out", "images": images,
            "session": app.config["INKDEUS_SESSION"], "lib": app.config["INKDEUS_LIBRARY"]}


def send(client, method, path, body=None):
    return client.open(path, method=method, data=None if body is None else json.dumps(body),
                       content_type="application/json")


def names(client, image="scene.png"):
    return [v["name"] for v in client.get(f"/api/versions/{image}").get_json()["versions"]]


def test_file_names_and_how_they_are_read_back(env):
    lib: Library = env["lib"]
    scene = lib.get("scene.png")
    assert lib.params_path(scene).name == "scene.inkdeus.json" and lib.export_path(scene).name == "scene.png"
    assert lib.params_path(scene, "白背景").name == "scene@白背景.inkdeus.json"
    assert lib.export_path(scene, "bg-1").name == "scene@bg-1.png"
    assert lib.resolve_stem("scene") == (scene, "")
    assert lib.resolve_stem("scene@bg-1") == (scene, "bg-1")
    # 元画像の一覧に実在する名前を優先する: scene@alt は「scene のバージョン alt」ではなく、元画像 scene@alt の既定
    assert lib.resolve_stem("scene@alt") == (lib.get("scene@alt.png"), "")
    assert lib.resolve_stem("scene@alt@v2") == (lib.get("scene@alt.png"), "v2")
    assert lib.resolve_stem("nope@v") is None and lib.resolve_stem("scene@") is None
    assert lib.resolve_stem("scene@a b") is None
    with pytest.raises(VersionError):
        lib.check_version(scene, "alt")                               # 元画像 scene@alt とぶつかる
    for bad in ("a@b", "a b", "a/b", "a\\b", "..", "a.b", "x" * 33, " "):
        with pytest.raises(VersionError):
            lib.check_version(scene, bad)
    assert lib.check_version(scene, "") == "" and lib.check_version(scene, "x" * 32) == "x" * 32
    assert lib.check_version(scene, "かな漢字_A-1") == "かな漢字_A-1"


def test_existing_data_is_the_default_version(env):
    client, out = env["client"], env["out"]
    out.mkdir()
    (out / "scene.inkdeus.json").write_text('{"version": 2, "unit": 2}', encoding="utf-8")
    (out / "scene.png").write_bytes(png_bytes())
    (out / "scene@alt.png").write_bytes(png_bytes())                  # 元画像 scene@alt の既定の書き出し
    rows = {i["name"]: i for i in client.get("/api/images").get_json()["images"]}
    assert [v["name"] for v in rows["scene.png"]["versions"]] == [""]
    default = rows["scene.png"]["versions"][0]
    assert default["hasParams"] and default["hasExport"] and default["exportFile"] == "scene.png"
    assert default["updatedAt"] > 0 and rows["scene.png"]["hasParams"] is True
    assert rows["scene@alt.png"]["versions"][0]["hasExport"] is True and rows["scene@alt.png"]["hasExport"] is True
    assert rows["other.png"]["versions"] == [{"name": "", "hasParams": False, "hasExport": False, "exportFresh": False,
                                             "updatedAt": None, "exportFile": "other.png", "exportVersion": None}]
    assert client.get("/api/params/scene.png").get_json()["unit"] == 2


def test_create_copy_rename_delete(env):
    client, out, session = env["client"], env["out"], env["session"]
    q = session.hub.subscribe()
    client.put("/api/params/scene.png", data='{"version": 2, "unit": 3}', content_type="application/json")
    q.get_nowait()
    r = send(client, "POST", "/api/versions/scene.png", {"version": "白背景"})
    assert r.status_code == 200 and r.get_json()["copyFrom"] == "" and r.get_json()["copiedExport"] is False
    assert (out / "scene@白背景.inkdeus.json").is_file() and names(client) == ["", "白背景"]
    assert q.get_nowait()["type"] == "params"
    assert q.get_nowait() == {"type": "versions", "action": "create", "folder": "default", "name": "scene.png", "version": "白背景",
                              "file": "scene@白背景.png", "copyFrom": ""}
    assert client.get("/api/params/scene.png?version=白背景").get_json()["unit"] == 3      # 既定の設定の複製
    # バージョンごとに設定は独立
    r = send(client, "POST", "/api/set/scene.png?version=白背景", {"key": "unit", "value": 5}).get_json()
    assert r["version"] == "白背景" and q.get_nowait() == {"type": "params", "folder": "default", "name": "scene.png", "version": "白背景",
                                                          "rev": r["rev"], "source": None}
    assert client.get("/api/params/scene.png").get_json()["unit"] == 3
    r = send(client, "PATCH", "/api/params/scene.png?version=白背景", {"lines": {"enabled": False}}).get_json()
    assert r["params"]["unit"] == 5 and r["params"]["lines"]["enabled"] is False
    assert client.get("/api/params/scene.png?resolve=1").get_json()["params"]["lines"]["enabled"] is True
    assert send(client, "POST", "/api/style/scene.png?version=白背景", {"style": "white", "region": "background"}).status_code == 200
    # 複製元を指定して作る（書き出した PNG も写せる）
    client.post("/api/export/scene.png?version=白背景", data=png_bytes(7))
    r = send(client, "POST", "/api/versions/scene.png", {"version": "v2", "copyFrom": "白背景", "copyExport": True}).get_json()
    assert r["copiedExport"] is True and r["file"] == "scene@v2.png"
    assert (out / "scene@v2.png").read_bytes() == png_bytes(7)
    assert client.get("/api/params/scene.png?version=v2").get_json()["unit"] == 5
    v2 = next(v for v in client.get("/api/versions/scene.png").get_json()["versions"] if v["name"] == "v2")
    assert v2["hasParams"] and v2["hasExport"] and v2["exportFresh"] and v2["exportFile"] == "scene@v2.png"
    # 同じ名前・無い複製元
    assert send(client, "POST", "/api/versions/scene.png", {"version": "v2"}).status_code == 409
    assert send(client, "POST", "/api/versions/scene.png", {"version": "v3", "copyFrom": "nope"}).status_code == 404
    # 名前の変更（設定も PNG も移る）
    while not q.empty():
        q.get_nowait()
    r = send(client, "PATCH", "/api/versions/scene.png", {"version": "v2", "to": "線だけ"})
    assert r.status_code == 200 and names(client) == ["", "白背景", "線だけ"]
    assert not (out / "scene@v2.png").exists() and not (out / "scene@v2.inkdeus.json").exists()
    assert (out / "scene@線だけ.png").read_bytes() == png_bytes(7)
    assert q.get_nowait() == {"type": "versions", "action": "rename", "folder": "default", "name": "scene.png", "version": "v2",
                              "file": "scene@v2.png", "to": "線だけ", "toFile": "scene@線だけ.png"}
    assert session.scan() == []                                       # サーバ自身の変更は「外部からの変更」にならない
    assert send(client, "PATCH", "/api/versions/scene.png", {"version": "線だけ", "to": "白背景"}).status_code == 409
    assert send(client, "PATCH", "/api/versions/scene.png", {"version": "nope", "to": "x"}).status_code == 404
    # 削除（設定と PNG を消す。既定は消せない）
    assert client.delete("/api/versions/scene.png?version=線だけ").get_json()["deleted"] == "線だけ"
    assert not (out / "scene@線だけ.png").exists() and not (out / "scene@線だけ.inkdeus.json").exists()
    assert q.get_nowait() == {"type": "versions", "action": "delete", "folder": "default", "name": "scene.png", "version": "線だけ",
                              "file": "scene@線だけ.png"}
    assert names(client) == ["", "白背景"] and session.scan() == []
    assert client.delete("/api/versions/scene.png").status_code == 400
    assert client.delete("/api/versions/scene.png?version=").status_code == 400
    assert client.delete("/api/versions/scene.png?version=nope").status_code == 404
    assert (out / "scene.inkdeus.json").is_file()


def test_bad_names_are_rejected(env):
    client, out = env["client"], env["out"]
    for bad in ("a@b", "a b", "../x", "a/b", "a.b", "", "x" * 33, "alt"):       # alt は元画像 scene@alt とぶつかる
        r = send(client, "POST", "/api/versions/scene.png", {"version": bad})
        assert r.status_code == 400 and r.get_json()["error"], bad
    assert send(client, "POST", "/api/versions/scene.png", {"version": 3}).status_code == 400
    assert send(client, "POST", "/api/versions/nope.png", {"version": "a"}).status_code == 404
    assert client.get("/api/params/scene.png?version=..%2Fx").status_code == 400
    assert client.get("/api/params/scene.png?version=a%40b").status_code == 400
    assert client.get("/api/params/scene.png?version=nope").status_code == 404
    assert client.put("/api/params/scene.png?version=nope", data="{}", content_type="application/json").status_code == 404
    assert client.post("/api/export/scene.png?version=nope", data=png_bytes()).status_code == 404
    assert client.post("/api/export/scene.png?version=..%2Fx", data=png_bytes()).status_code == 400
    assert send(client, "POST", "/api/set/scene.png?version=nope", {"key": "unit", "value": 2}).status_code == 404
    assert client.get("/api/stats/scene.png?version=nope").status_code == 404
    assert not out.exists() or not any(out.iterdir())
    send(client, "POST", "/api/versions/scene.png", {"version": "a"})
    assert send(client, "PATCH", "/api/versions/scene.png", {"version": "a", "to": "b@c"}).status_code == 400
    assert send(client, "PATCH", "/api/versions/scene.png", {"version": "a", "to": "alt"}).status_code == 400
    assert send(client, "POST", "/api/command", {"type": "open", "folder": "default", "name": "scene.png", "version": "nope"}).status_code == 404
    assert send(client, "POST", "/api/command", {"type": "open", "folder": "default", "name": "scene.png", "version": "a/b"}).status_code == 400


def test_export_stats_and_state_per_version(env):
    client, out, session = env["client"], env["out"], env["session"]
    send(client, "POST", "/api/versions/scene.png", {"version": "v"})
    q = session.hub.subscribe()
    r = client.post("/api/export/scene.png?version=v", data=png_bytes(), headers={"X-Inkdeus-Rev": "r1"})
    assert r.get_json()["path"].endswith("scene@v.png") and r.get_json()["version"] == "v"
    ev = q.get_nowait()
    assert (ev["type"], ev["name"], ev["version"], ev["file"]) == ("exported", "scene.png", "v", "scene@v.png")
    assert ev["stamp"] == (out / "scene@v.png").stat().st_mtime_ns // 1_000_000
    assert not (out / "scene.png").exists()
    assert client.get("/api/exported/scene.png?version=v").get_json()["seq"] == 1
    assert client.get("/api/exported/scene.png").get_json() == {"seq": 0}
    # 統計はバージョンごと
    rev = client.get("/api/params/scene.png?version=v").headers["X-Inkdeus-Rev"]
    send(client, "POST", "/api/stats/scene.png?version=v", {"rev": rev, "output": {}})
    s = client.get("/api/stats/scene.png?version=v").get_json()
    assert s["fresh"] is True and s["version"] == "v" and s["stats"]["version"] == "v"
    assert client.get("/api/stats/scene.png").get_json()["stats"] is None
    # いま開いているバージョン
    assert send(client, "POST", "/api/state", {"current": "scene.png", "version": "v"}).get_json()["currentVersion"] == "v"
    assert client.get("/api/versions/scene.png").get_json()["current"] == "v"
    assert client.get("/api/versions/other.png").get_json()["current"] is None
    send(client, "PATCH", "/api/versions/scene.png", {"version": "v", "to": "w"})
    assert client.get("/api/state").get_json()["currentVersion"] == "w"                 # 名前の変更に追従
    client.delete("/api/versions/scene.png?version=w")
    assert client.get("/api/state").get_json()["currentVersion"] == ""                  # 削除されたら既定
    assert send(client, "POST", "/api/state", {"current": "scene.png", "version": "a b"}).status_code == 400
    assert send(client, "POST", "/api/state", {"current": None}).get_json()["currentVersion"] == ""


def test_external_edits_of_a_version_file_are_noticed(env):
    client, out, session = env["client"], env["out"], env["session"]
    send(client, "POST", "/api/versions/scene.png", {"version": "v"})
    assert session.scan() == []
    path = out / "scene@v.inkdeus.json"
    path.write_text('{"version": 2, "unit": 4}', encoding="utf-8")
    events = session.scan()
    assert [(e["type"], e["name"], e["version"]) for e in events] == [("params", "scene.png", "v")]
    path.unlink()
    assert [(e["name"], e["version"], e["rev"]) for e in session.scan()] == [("scene.png", "v", None)]
    # 名前に @ を含む元画像の設定ファイルは、その画像の既定のバージョンの物
    (out / "scene@alt.inkdeus.json").write_text('{"version": 2}', encoding="utf-8")
    assert [(e["name"], e["version"]) for e in session.scan()] == [("scene@alt.png", "")]


def test_assets_list_versions_with_base(env):
    client, out = env["client"], env["out"]
    for v in ("b", "a"):
        send(client, "POST", "/api/versions/scene.png", {"version": v})
        client.post(f"/api/export/scene.png?version={v}", data=png_bytes())
    client.put("/api/params/scene.png", data="{}", content_type="application/json")
    client.post("/api/export/scene.png", data=png_bytes())
    client.post("/api/export/scene@alt.png", data=png_bytes())
    client.put("/api/params/other2.png", data="{}", content_type="application/json")
    client.post("/api/export/other2.png", data=png_bytes())
    (out / "orphan.png").write_bytes(png_bytes())                     # 元画像の無い PNG
    rows = client.get("/api/assets/inked").get_json()["items"]
    # 元画像ごとにまとまり、既定が先頭（other2 が scene とそのバージョンの間に割り込まない）
    assert [(r["path"], r["base"], r["version"]) for r in rows] == [
        ("orphan.png", None, ""), ("other2.png", "other2.png", ""),
        ("scene.png", "scene.png", ""), ("scene@a.png", "scene.png", "a"), ("scene@b.png", "scene.png", "b"),
        ("scene@alt.png", "scene@alt.png", "")]
    assert all(r["stamp"] > 0 for r in rows) and rows[3]["editable"] == "scene.png"
    src = {r["path"]: r for r in client.get("/api/assets/source").get_json()["items"]}
    assert src["scene.png"]["inked"]["path"] == "scene.png"
    assert [(v["name"], v["path"]) for v in src["scene.png"]["versions"]] == [
        ("", "scene.png"), ("a", "scene@a.png"), ("b", "scene@b.png")]
    assert src["other.png"]["versions"] == [] and src["other.png"]["inked"] is None
    # 対応: どの名前からでも引ける
    pair = client.get("/api/assets/inked/pair/scene@a.png").get_json()
    assert pair["name"] == "scene.png" and pair["version"] == "a" and pair["inked"]["path"] == "scene@a.png"
    assert pair["default"]["path"] == "scene.png" and pair["missing"] is False and len(pair["versions"]) == 3
    pair = client.get("/api/assets/source/pair/scene.png").get_json()
    assert pair["version"] == "" and pair["inked"]["path"] == "scene.png" and pair["inked"]["name"] == ""
    pair = client.get("/api/assets/inked/pair/scene@alt.png").get_json()
    assert pair["name"] == "scene@alt.png" and pair["version"] == ""
    pair = client.get("/api/assets/inked/pair/orphan.png").get_json()
    assert pair["name"] is None and pair["inked"]["path"] == "orphan.png"
    # 消された・名前の変わったバージョン: 対応は「見つからない」と答え、画像は既定（無ければ元画像）で代える
    pair = client.get("/api/assets/inked/pair/scene@gone.png").get_json()
    assert pair["missing"] is True and pair["inked"] is None and pair["default"]["path"] == "scene.png"
    r = client.get("/api/assets/inked/image/scene@gone.png")
    assert r.status_code == 200 and r.headers["X-Inkdeus-Fallback"] == "1" and r.data == (out / "scene.png").read_bytes()
    r = client.get("/api/assets/inked/image/other@gone.png")
    assert r.status_code == 200 and r.data == (env["images"] / "other.png").read_bytes()      # 既定も無ければ元画像
    assert "X-Inkdeus-Fallback" not in client.get("/api/assets/inked/image/scene@a.png").headers
    assert client.get("/api/assets/inked/image/nope@gone.png").status_code == 404
    assert client.get("/api/assets/inked/image/gone.png").status_code == 404
    assert client.get("/api/assets/inked/pair/nope@gone.png").status_code == 404
    assert client.get("/api/assets/inked/image/..%2Fimages%2Fscene.png").status_code == 404
