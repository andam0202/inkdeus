"""自分用スタイル（名前を付けて保存したスタイル）: 保存・読み込み・優先順位・名前の検証・API・ctl"""

from __future__ import annotations

import json

import pytest

import synth
from conftest import no_ids, serve
from inkdeus import cli, schema
from inkdeus import styles as styles_mod
from inkdeus.server import create_app

W, H = 64, 48


def tone(threshold=0.3, angles=(10, 100)) -> dict:
    return {"solid": {"enabled": True, "threshold": threshold, "soft": 0, "lineKnockout": 0},
            "layers": [{"type": "hatch", "threshold": 0.6, "angle": a, "spacing": 3.0, "width": 0.7} for a in angles]}


@pytest.fixture()
def store(tmp_path):
    return styles_mod.Store(tmp_path / "config", tmp_path / "project")


def test_config_dir_follows_the_environment(tmp_path, monkeypatch):
    assert styles_mod.config_dir({"INKDEUS_CONFIG": str(tmp_path / "x")}) == tmp_path / "x"
    assert styles_mod.config_dir({}).name == "inkdeus" and styles_mod.config_dir({}).parent.name == ".config"
    monkeypatch.setenv("INKDEUS_CONFIG", str(tmp_path / "env"))
    assert styles_mod.Store().files["user"] == tmp_path / "env" / "styles.json"
    assert styles_mod.Store().files["project"] is None


def test_save_load_and_precedence(store, tmp_path):
    assert store.rows() == ([], [])
    entry, replaced = store.save({"name": "細かい影", "description": "メモ", "data": {**tone(), "layers": [
        {"id": "abcd1234", "type": "hatch", "angle": 10}]}}, "user")
    assert replaced is False and entry["target"] == "any" and entry["includeLevels"] is False
    assert entry["createdAt"] == entry["updatedAt"] and entry["aliases"] == []
    layers = entry["data"]["layers"]
    assert len(layers) == 1 and "id" not in layers[0] and layers[0]["angle"] == 10 and layers[0]["spacing"] == 2.0
    assert "levels" not in entry["data"] and "mode" not in entry["data"]
    saved = json.loads((tmp_path / "config" / "styles.json").read_text(encoding="utf-8"))
    assert saved["version"] == 1 and [s["name"] for s in saved["styles"]] == ["細かい影"]
    assert set(saved["styles"][0]) == {"name", "aliases", "description", "target", "includeLevels", "data", "createdAt", "updatedAt"}
    # 同じ名前は上書き（作成日時は引き継ぐ）。名前の照合は、全角/半角・大文字小文字・空白・_ の違いを無視
    store.save({"name": "Fav_(1)", "data": tone()}, "user")
    again, replaced = store.save({"name": "fav(1)", "description": "新", "data": tone(0.5)}, "user")
    assert replaced is True and again["name"] == "fav(1)" and again["data"]["solid"]["threshold"] == 0.5
    assert [r["name"] for r in store.rows()[0]] == ["細かい影", "fav(1)"]
    # プロジェクト専用が優先。利用者共通の同名は shadowed
    store.save({"name": "細かい影", "data": tone(0.9)}, "project")
    rows, problems = store.rows()
    assert problems == [] and [(r["name"], r["origin"], r["shadowed"]) for r in rows] == [
        ("細かい影", "project", False), ("細かい影", "user", True), ("fav(1)", "user", False)]
    assert rows[0]["layers"] == 2 and rows[0]["solid"] is True and rows[0]["label"] == "細かい影" and rows[0]["backgroundMode"] is None
    found, origin = store.find("細かい影")
    assert origin == "project" and found["data"]["solid"]["threshold"] == 0.9
    assert store.find("細かい影", "user")[0]["data"]["solid"]["threshold"] == 0.3
    assert store.find("FAV (1)")[1] == "user" and store.find("nope") is None
    # 明るさ補正を含める・背景の描き方・別名
    e, _ = store.save({"name": "bg白", "aliases": ["しろ背景"], "target": "background", "includeLevels": True,
                       "data": {**tone(), "levels": {"brightness": 0.3}, "mode": "white"}}, "user")
    assert e["data"]["levels"] == {"brightness": 0.3, "contrast": 1, "gamma": 1} and e["data"]["mode"] == "white"
    assert store.find("しろ背景")[0]["name"] == "bg白"
    d = styles_mod.definition(e)
    assert d["backgroundMode"] == "white" and d["levels"]["brightness"] == 0.3 and d["solid"]["threshold"] == 0.3
    assert "levels" not in styles_mod.definition(store.find("fav(1)")[0])
    # includeLevels でなければ、levels は保存しない（書かれていても落とす）
    e, _ = store.save({"name": "nolv", "data": {**tone(), "levels": {"brightness": 0.3}}}, "user")
    assert "levels" not in e["data"]


@pytest.mark.parametrize("name", ["ベタ", "黒ベタ", "hatch1", "ハッチ(1)", "HATCH_1", "重ねがけ4段", "白", "白抜き", "blank", "線だけ",
                                  "solid-black", "white", "Crosshatch-Fine"])
def test_builtin_names_and_aliases_are_rejected(store, name):
    with pytest.raises(styles_mod.StyleError, match="組み込み"):
        store.save({"name": name, "data": tone()})
    with pytest.raises(styles_mod.StyleError, match="組み込み"):
        store.save({"name": "ok", "aliases": [name], "data": tone()})
    assert store.rows() == ([], [])


@pytest.mark.parametrize("name", ["", "a b", "a/b", "a.b", "a@b", "x" * 33, "..", "a:b", 3, None, "名前\n"])
def test_bad_names_are_rejected(store, name):
    with pytest.raises(styles_mod.StyleError):
        store.save({"name": name, "data": tone()})


def test_validation_of_entries(store):
    assert store.save({"name": "記号_-(1)", "data": tone()})[0]["name"] == "記号_-(1)"
    assert store.find("記号_-（1）")[0]["name"] == "記号_-(1)"            # 全角の括弧で書いても、同じ名前として引ける
    for bad in ({"name": "x", "data": {"solid": {"threshold": 9}, "layers": []}},
                {"name": "x", "data": {"layers": [{"type": "dots"}]}},
                {"name": "x", "data": {"layers": [{"type": "hatch"}] * 9}},
                {"name": "x", "data": {"layers": [], "extra": 1}},
                {"name": "x", "data": {"layers": [], "mode": "black"}},
                {"name": "x", "data": []}, {"name": "x"},
                {"name": "x", "target": "sky", "data": tone()},
                {"name": "x", "includeLevels": "yes", "data": tone()},
                {"name": "x", "aliases": "y", "data": tone()},
                {"name": "x", "aliases": ["X"], "data": tone()},
                {"name": "x", "mystery": 1, "data": tone()},
                {"name": "x", "includeLevels": True, "data": {**tone(), "levels": {"gamma": 0}}}):
        with pytest.raises(styles_mod.StyleError):
            store.save(bad)
    # 別のスタイルの名前・別名と重なる名前・別名は付けられない
    store.save({"name": "a1", "aliases": ["えー"], "data": tone()})
    for bad in ({"name": "えー", "data": tone()}, {"name": "b1", "aliases": ["A1"], "data": tone()}):
        with pytest.raises(styles_mod.StyleError, match="重なって"):
            store.save(bad)
    with pytest.raises(styles_mod.StyleError, match="scope"):
        store.save({"name": "x", "data": tone()}, "global")
    with pytest.raises(styles_mod.StyleError, match="プロジェクトを開いている"):
        styles_mod.Store(store.files["user"].parent, None).save({"name": "x", "data": tone()}, "project")
    assert [r["name"] for r in store.rows()[0]] == ["記号_-(1)", "a1"]


def test_broken_files_do_not_crash(store, tmp_path):
    path = tmp_path / "config" / "styles.json"
    path.parent.mkdir(parents=True)
    for text in ("{broken", "[]", '{"version": 1}', '{"styles": 3}', "﻿", ""):
        path.write_text(text, encoding="utf-8")
        rows, problems = store.rows()
        assert rows == [] and len(problems) == 1 and "styles.json" in problems[0], text
    path.write_bytes(b"\xff\xfe\x00bad")
    assert store.rows()[0] == [] and store.rows()[1]
    # 使えない項目だけ飛ばして、読める物は読む
    good = {"name": "good", "data": tone()}
    path.write_text(json.dumps({"version": 1, "styles": [
        good, {"name": "ベタ", "data": tone()}, "bad", {"name": "x", "data": {"layers": 3}}, {"name": "GOOD", "data": tone()},
        {"name": "later", "data": tone(), "createdAt": "2020-01-01T00:00:00+00:00"}]}), encoding="utf-8")
    rows, problems = store.rows()
    assert [r["name"] for r in rows] == ["good", "later"] and len(problems) == 4
    assert rows[1]["createdAt"] == "2020-01-01T00:00:00+00:00"
    # 壊れたファイルがあっても保存できる（壊れた物は写しを残す）
    path.write_text("{broken", encoding="utf-8")
    store.save({"name": "new", "data": tone()})
    assert [r["name"] for r in store.rows()[0]] == ["new"] and store.rows()[1] == []
    assert path.with_name("styles.json.broken").read_text(encoding="utf-8") == "{broken"


def test_rename_and_delete(store):
    store.save({"name": "a", "data": tone()}, "user")
    store.save({"name": "a", "data": tone(0.9)}, "project")
    store.save({"name": "b", "data": tone()}, "user")
    entry, origin = store.rename("a", "c")                       # 保存先を指さなければ、プロジェクト専用が先
    assert (entry["name"], origin) == ("c", "project")
    assert [(r["name"], r["origin"], r["shadowed"]) for r in store.rows()[0]] == [
        ("c", "project", False), ("a", "user", False), ("b", "user", False)]
    for bad in ("b", "ベタ", "a b"):
        with pytest.raises(styles_mod.StyleError):
            store.rename("a", bad, "user")
    with pytest.raises(styles_mod.StyleError) as e:
        store.rename("nope", "z")
    assert e.value.status == 404
    assert store.rename("a", "A", "user")[0]["name"] == "A"       # 表記だけ変えるのは構わない
    assert store.delete("c")[1] == "project" and store.delete("A", "user")[0]["name"] == "A"
    with pytest.raises(styles_mod.StyleError):
        store.delete("c")
    assert [r["name"] for r in store.rows()[0]] == ["b"]


def test_from_region_takes_what_is_actually_drawn():
    p = schema.normalize({"regions": {
        "person": {"solid": {"threshold": 0.3}, "levels": {"gamma": 2}, "layers": [{"type": "hatch", "angle": 10}]},
        "background": {"mode": "white", "linked": True},
        "hair": {"linked": False, "solid": {"threshold": 1}, "layers": []},
        "skin": {"linked": True, "layers": [{"type": "hatch", "angle": 99}]}}})[0]
    data, target = styles_mod.from_region(p, "person", False)
    assert target == "any" and set(data) == {"solid", "layers"} and data["solid"]["threshold"] == 0.3
    assert [l["angle"] for l in data["layers"]] == [10] and "id" not in data["layers"][0]
    data, target = styles_mod.from_region(p, "person", True)
    assert data["levels"]["gamma"] == 2
    # 人物と同じ設定で描いている背景・部分は、人物の物を保存する。背景は描き方も持つ
    data, target = styles_mod.from_region(p, "background", True)
    assert target == "background" and data["mode"] == "white" and [l["angle"] for l in data["layers"]] == [10]
    assert [l["angle"] for l in styles_mod.from_region(p, "skin", False)[0]["layers"]] == [10]
    data, target = styles_mod.from_region(p, "hair", False)
    assert target == "any" and data["solid"]["threshold"] == 1 and data["layers"] == [] and "mode" not in data
    q = schema.set_path(p, "regions.background.mode", "keep")
    assert styles_mod.from_region(q, "background", False)[1] == "any"
    with pytest.raises(styles_mod.StyleError):
        styles_mod.from_region(p, "clothes", False)
    assert styles_mod.clean_data(data, False) == data


# ---- API ----

@pytest.fixture()
def env(tmp_path):
    img, mask = synth.scene(W, H)
    for name in ("a", "b"):
        synth.save(tmp_path / "images" / f"{name}.png", img)
        synth.save(tmp_path / "masks" / f"{name}.png", mask)
    synth.save_parts(tmp_path / "parts", "a", synth.part_masks(W, H))
    (tmp_path / "proj").mkdir()
    project = {"name": "本", "path": str(tmp_path / "proj")}
    app = create_app(tmp_path / "images", mask_dir=tmp_path / "masks", out_dir=tmp_path / "out", project=project,
                     parts_dir=tmp_path / "parts", config_dir=tmp_path / "config", watch=False)
    return {"app": app, "client": app.test_client(), "root": tmp_path, "session": app.config["INKDEUS_SESSION"]}


def send(client, method, path, body=None):
    return client.open(path, method=method, data=None if body is None else json.dumps(body),
                       content_type="application/json")


def test_api_lists_saves_renames_and_deletes(env):
    client, root = env["client"], env["root"]
    assert "userStyles" in client.get("/api/config").get_json()["features"]
    listing = client.get("/api/styles").get_json()
    assert all(s["origin"] == "builtin" for s in listing["styles"]) and listing["problems"] == []
    assert listing["files"] == {"user": str(root / "config" / "styles.json"), "project": str(root / "proj" / "styles.json")}
    assert listing["default"] == "crosshatch"
    q = env["session"].hub.subscribe()
    r = send(client, "PUT", "/api/styles/細かい影", {"scope": "user", "description": "メモ", "data": tone()})
    assert r.status_code == 200 and r.get_json()["replaced"] is False and r.get_json()["scope"] == "user"
    assert r.get_json()["path"] == str(root / "config" / "styles.json")
    assert q.get_nowait() == {"type": "styles", "action": "save", "name": "細かい影", "scope": "user", "source": None}
    r = send(client, "PUT", "/api/styles/細かい影", {"scope": "project", "includeLevels": True,
                                                     "data": {**tone(0.9), "levels": {"brightness": 0.2}}})
    assert r.status_code == 200 and (root / "proj" / "styles.json").is_file()
    assert send(client, "PUT", "/api/styles/細かい影?scope=user", {"data": tone(0.4)}).get_json()["replaced"] is True
    rows = [s for s in client.get("/api/styles").get_json()["styles"] if s["origin"] != "builtin"]
    assert [(s["name"], s["origin"], s["shadowed"]) for s in rows] == [("細かい影", "project", False), ("細かい影", "user", True)]
    assert rows[0]["includeLevels"] is True and rows[0]["data"]["levels"]["brightness"] == 0.2
    assert rows[1]["data"]["solid"]["threshold"] == 0.4 and rows[1]["description"] == ""
    assert client.get("/api/styles/細かい影").get_json()["scope"] == "project"
    assert client.get("/api/styles/細かい影?scope=user").get_json()["style"]["data"]["solid"]["threshold"] == 0.4
    assert client.get("/api/styles/nope").status_code == 404
    # 受け付けない物: 組み込みの名前・使えない名前・使えない中身・知らない保存先
    for name, body in (("ベタ", {"data": tone()}), ("白", {"data": tone()}), ("a b", {"data": tone()}),
                       ("x", {"data": {"solid": {"threshold": 5}}}), ("x", {"data": tone(), "scope": "global"}), ("x", {})):
        r = send(client, "PUT", f"/api/styles/{name}", body)
        assert r.status_code == 400 and r.get_json()["problems"], (name, body)
    # 名前の変更・削除
    r = send(client, "PATCH", "/api/styles/細かい影?scope=user", {"to": "粗い影"})
    assert r.status_code == 200 and r.get_json()["style"]["name"] == "粗い影" and r.get_json()["renamedFrom"] == "細かい影"
    assert send(client, "PATCH", "/api/styles/粗い影", {"to": "ベタ"}).status_code == 400
    assert send(client, "PATCH", "/api/styles/nope", {"to": "x"}).status_code == 404
    r = client.delete("/api/styles/細かい影")
    assert r.get_json() == {"deleted": "細かい影", "scope": "project"}
    assert client.delete("/api/styles/細かい影").status_code == 404
    assert [s["name"] for s in client.get("/api/styles").get_json()["styles"] if s["origin"] != "builtin"] == ["粗い影"]
    # 壊れたファイルを読んでも落ちない
    (root / "proj" / "styles.json").write_text("{broken", encoding="utf-8")
    listing = client.get("/api/styles").get_json()
    assert len(listing["problems"]) == 1 and [s["name"] for s in listing["styles"] if s["origin"] != "builtin"] == ["粗い影"]


def test_api_applies_user_styles(env):
    client = env["client"]
    send(client, "PUT", "/api/styles/影つき", {"includeLevels": True, "data": {**tone(0.4, (20, 110)), "levels": {"gamma": 1.5}}})
    send(client, "PUT", "/api/styles/影なし", {"data": tone(0.5, (33,))})
    send(client, "PUT", "/api/styles/白背景", {"target": "background", "data": {**tone(), "layers": [], "mode": "white"}})
    send(client, "PUT", "/api/styles/人物用", {"target": "person", "data": tone()})
    r = send(client, "POST", "/api/style/a.png", {"style": "影つき", "region": "person"}).get_json()
    assert (r["style"], r["origin"], r["region"]) == ("影つき", "user", "person")
    person = r["params"]["regions"]["person"]
    assert person["levels"]["gamma"] == 1.5 and person["solid"]["threshold"] == 0.4
    assert [l["angle"] for l in person["layers"]] == [20, 110] and all(l["id"] for l in person["layers"])
    # 明るさ補正を含めないスタイルは、その領域の明るさ補正を変えない。部分にも当てられる（その部分は切り離される）
    r = send(client, "POST", "/api/style/a.png", {"style": "影なし", "region": "hair"}).get_json()
    hair = r["params"]["regions"]["hair"]
    assert hair["linked"] is False and hair["levels"]["gamma"] == 1.5 and [l["angle"] for l in hair["layers"]] == [33]
    r = send(client, "POST", "/api/style/a.png", {"style": "影なし", "region": "background"}).get_json()
    bg = r["params"]["regions"]["background"]
    assert (bg["linked"], bg["mode"], bg["solid"]["threshold"]) == (False, "keep", 0.5)
    r = send(client, "POST", "/api/style/a.png", {"style": "白背景", "region": "background"}).get_json()
    assert r["params"]["regions"]["background"]["mode"] == "white"
    ids = [l["id"] for n in ("person", "background", "hair") for l in r["params"]["regions"][n]["layers"]]
    assert len(set(ids)) == len(ids)
    # 背景向け・人物向けのスタイルは、向きの違う領域には当てられない
    for style, region in (("白背景", "person"), ("白背景", "hair"), ("白背景", "both"), ("人物用", "background"), ("nope", "person")):
        r = send(client, "POST", "/api/style/a.png", {"style": style, "region": region})
        assert r.status_code == 400, (style, region)
    assert "影つき" in send(client, "POST", "/api/style/a.png", {"style": "nope"}).get_json()["problems"][0]
    assert send(client, "POST", "/api/style/a.png", {"style": "人物用", "region": "hair"}).status_code == 200
    # 組み込みのスタイルは、これまでどおり
    assert send(client, "POST", "/api/style/a.png", {"style": "ハッチ(1)", "region": "person"}).get_json()["origin"] == "builtin"


# ---- ctl ----

@pytest.fixture()
def live(env):
    server, url = serve(env["app"])
    yield {**env, "port": server.server_port}
    server.shutdown()


@pytest.fixture()
def ctl(live, capsys):
    def run(*args, expect=0, raw=False):
        capsys.readouterr()
        code = cli.main(["ctl", "--port", str(live["port"]), *args])
        captured = capsys.readouterr()
        assert code == expect, captured.err or captured.out
        text = captured.out if code == 0 else captured.err
        return text if raw else json.loads(text)
    return run


def test_ctl_style_save_apply_export_import(ctl, live, tmp_path):
    root = live["root"]
    ctl("set", "regions.person.layers.0.angle", "77", "--name", "a.png")
    ctl("set", "regions.person.levels.brightness", "0.25", "--name", "a.png")
    r = ctl("style", "save", "お気に入り", "--region", "person", "--name", "a.png", "--description", "斜め")
    assert (r["style"]["name"], r["scope"], r["replaced"]) == ("お気に入り", "user", False)
    assert r["style"]["data"]["layers"][0]["angle"] == 77 and "levels" not in r["style"]["data"]
    r = ctl("style", "save", "明るめ", "--region", "person", "--name", "a.png", "--with-levels", "--scope", "project")
    assert r["scope"] == "project" and r["style"]["data"]["levels"]["brightness"] == 0.25
    assert (root / "proj" / "styles.json").is_file() and (root / "config" / "styles.json").is_file()
    # 背景から保存すると、背景の描き方も入る（人物と同じ設定の背景は、人物の層）
    ctl("set", "regions.background.mode", '"lines"', "--name", "a.png")
    r = ctl("style", "save", "線の背景", "--region", "background", "--name", "a.png")
    assert r["style"]["target"] == "background" and r["style"]["data"]["mode"] == "lines"
    assert r["style"]["data"]["layers"][0]["angle"] == 77
    # 一覧に、出どころつきで並ぶ
    rows = {s["name"]: s for s in ctl("presets")["styles"]}
    assert rows["hatch1"]["origin"] == "builtin" and rows["お気に入り"]["origin"] == "user" and rows["明るめ"]["origin"] == "project"
    assert rows["お気に入り"]["description"] == "斜め" and ctl("style", "list")["styles"] == ctl("presets")["styles"]
    # 別の画像・別の領域へ適用する
    r = ctl("preset", "お気に入り", "--region", "person", "--name", "b.png")
    assert (r["style"], r["origin"]) == ("お気に入り", "user")
    b = ctl("get", "--name", "b.png")["params"]["regions"]
    a = ctl("get", "--name", "a.png")["params"]["regions"]
    assert no_ids(b["person"]["layers"]) == no_ids(a["person"]["layers"]) and b["person"]["solid"] == a["person"]["solid"]
    assert b["person"]["levels"]["brightness"] == 0                       # 明るさ補正は含めていない
    assert ctl("preset", "明るめ", "--region", "hair", "--name", "a.png")["origin"] == "project"
    hair = ctl("get", "--name", "a.png")["params"]["regions"]["hair"]
    assert hair["linked"] is False and hair["levels"]["brightness"] == 0.25 and hair["layers"][0]["angle"] == 77
    assert ctl("preset", "線の背景", "--region", "background", "--name", "b.png")["region"] == "background"
    assert ctl("get", "--name", "b.png")["params"]["regions"]["background"]["mode"] == "lines"
    assert ctl("preset", "線の背景", "--region", "person", "--name", "b.png", expect=1)["problems"]
    # 受け付けない名前（組み込みと重なる・使えない文字）。保存する領域が無い
    for name in ("ベタ", "白抜き", "hatch1", "a b"):
        assert ctl("style", "save", name, "--name", "a.png", expect=1)["problems"], name
    assert ctl("style", "save", "x", "--region", "clothes", "--name", "a.png", expect=1)["problems"]
    # 書き出し → 別の保存先へ取り込み
    exported = ctl("style", "export", "お気に入り")
    assert exported["version"] == 1 and [s["name"] for s in exported["styles"]] == ["お気に入り"]
    path = tmp_path / "export.json"
    path.write_text(ctl("style", "export", "お気に入り", raw=True), encoding="utf-8")
    assert ctl("style", "import", str(path), expect=1)["skipped"] == ["お気に入り"]          # 同じ名前がある
    assert ctl("style", "import", str(path), "--overwrite")["saved"] == ["お気に入り"]
    r = ctl("style", "import", str(path), "--scope", "project")
    assert r == {"scope": "project", "saved": ["お気に入り"], "skipped": []}
    both = [(s["name"], s["origin"], s["shadowed"]) for s in ctl("presets")["styles"] if s["name"] == "お気に入り"]
    assert both == [("お気に入り", "project", False), ("お気に入り", "user", True)]
    # 1 件だけの JSON・styles.json そのものも取り込める。壊れたファイルは非0終了
    single = tmp_path / "single.json"
    single.write_text(json.dumps({**exported["styles"][0], "name": "写し"}), encoding="utf-8")
    assert ctl("style", "import", str(single))["saved"] == ["写し"]
    assert ctl("style", "import", str(root / "proj" / "styles.json"), "--overwrite")["saved"] == ["明るめ", "お気に入り"]
    bad = tmp_path / "bad.json"
    bad.write_text("{broken", encoding="utf-8")
    for target in (bad, tmp_path / "nope.json"):
        assert "error" in ctl("style", "import", str(target), expect=1)
    bad.write_text(json.dumps({"styles": [{"name": "ベタ", "data": tone()}]}), encoding="utf-8")
    assert ctl("style", "import", str(bad), expect=1)["problems"]
    # 名前の変更・削除
    assert ctl("style", "rename", "写し", "写し2")["style"]["name"] == "写し2"
    assert ctl("style", "delete", "お気に入り") == {"deleted": "お気に入り", "scope": "project"}
    assert ctl("style", "delete", "お気に入り", "--scope", "user")["scope"] == "user"
    assert "error" in ctl("style", "delete", "お気に入り", expect=1)
    assert ctl("preset", "お気に入り", "--name", "b.png", expect=1)["problems"]
    assert ctl("style", "export", "nope", expect=1)["error"]
