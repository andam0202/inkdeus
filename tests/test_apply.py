"""設定の一括適用（サーバ側・ctl・backup コマンド）: ある画像の設定を、選んだ複数の画像へ写す"""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image

import synth
from conftest import no_ids, serve
from inkdeus import applyparams, cli
from inkdeus.projects import add_folder, create_project, load_project
from inkdeus.server import create_app

W, H = 64, 48


def png_bytes(value: int = 128) -> bytes:
    buf = io.BytesIO()
    Image.new("L", (8, 4), value).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """既定のフォルダ: a（髪・肌・服のマスク）・b（髪だけ）・c・d（部分マスクなし）。別のフォルダ「二巻」: a・e（a に髪のマスク）"""
    monkeypatch.setenv("INKDEUS_CONFIG", str(tmp_path / "config"))
    img, mask = synth.scene(W, H)
    for name in ("a", "b", "c", "d"):
        synth.save(tmp_path / "images" / f"{name}.png", img)
        synth.save(tmp_path / "masks" / f"{name}.png", mask)
    parts = synth.part_masks(W, H)
    synth.save_parts(tmp_path / "parts", "a", parts)
    synth.save_parts(tmp_path / "parts", "b", {"hair": parts["hair"]})
    for name in ("a", "e"):
        synth.save(tmp_path / "more" / f"{name}.png", img)
    synth.save_parts(tmp_path / "more_parts", "a", {"hair": parts["hair"]})
    project, _ = create_project("book", tmp_path / "images", tmp_path / "masks", root=tmp_path / "projects",
                                parts=tmp_path / "parts")
    add_folder(project, "二巻", tmp_path / "more", parts=tmp_path / "more_parts")
    project = load_project(str(project.path), root=tmp_path / "projects")
    app = create_app(project.source, project.masks, project.out, project=project.info(), parts_dir=project.parts,
                     local_config=tmp_path / "none.toml", watch=False)
    return {"app": app, "client": app.test_client(), "project": project, "out": project.out, "tmp": tmp_path,
            "session": app.config["INKDEUS_SESSION"], "store": app.config["INKDEUS_BACKUPS"]}


def send(client, method, path, body=None):
    return client.open(path, method=method, data=None if body is None else json.dumps(body),
                       content_type="application/json")


def get(client, name, version="", folder=""):
    query = "&".join(f"{k}={v}" for k, v in (("version", version), ("folder", folder)) if v)
    return client.get(f"/api/params/{name}?resolve=1" + ("&" + query if query else "")).get_json()["params"]


def patch(client, name, body, version="", folder=""):
    query = "&".join(f"{k}={v}" for k, v in (("version", version), ("folder", folder)) if v)
    r = send(client, "PATCH", f"/api/params/{name}" + ("?" + query if query else ""), body)
    assert r.status_code == 200, r.data
    return r.get_json()["params"]


def apply(client, to, expect=200, **body):
    r = send(client, "POST", "/api/apply-params", {"from": {"name": "a.png"}, "to": to, **body})
    assert r.status_code == expect, r.data
    return r.get_json()


def tune_source(client) -> dict:
    """適用元（a.png）を、どの範囲も既定と違う値にする"""
    return patch(client, "a.png", {
        "unit": 2.5,
        "input": {"autoLevel": False, "smoothRadius": 5},
        "lines": {"strength": 0.4, "sigma": 1.5},
        "regions": {"person": {"levels": {"brightness": 0.3}, "solid": {"threshold": 0.33},
                               "layers": [{"type": "hatch", "angle": 10, "spacing": 3}]},
                    "background": {"mode": "white", "linked": False},
                    "hair": {"linked": False, "solid": {"threshold": 1.0}, "layers": []},
                    "skin": {"linked": False, "layers": [{"type": "hatch", "angle": 77}]}},
        "watermark": {"enabled": True, "text": "sample"},
        "output": {"binary": True}})


# ---- 範囲の読み取りと合成（サーバなし）----

def test_scope_parsing():
    assert applyparams.parse_scope(None).names() == ["input", "lines", "person", "background", "parts", "watermark", "output"]
    assert applyparams.parse_scope("all").names() == applyparams.parse_scope([]).names()
    assert applyparams.parse_scope("lines, unit").names() == ["lines", "unit"]
    scope = applyparams.parse_scope(["person", "parts:hair", "parts:skin"])
    assert scope.names() == ["person", "parts:hair", "parts:skin"] and scope.part_names(["skin", "clothes", "hair"]) == ["skin", "hair"]
    assert applyparams.parse_scope("parts").part_names(["hair"]) == ["hair"]
    assert applyparams.parse_scope("lines").part_names(["hair"]) == []
    for bad in ("nope", "parts:", ",", 5, ["lines", 3]):
        with pytest.raises(applyparams.ApplyError):
            applyparams.parse_scope(bad)
    assert applyparams.parse_to_version(None, "v") == ("default", "")
    assert applyparams.parse_to_version("same", "v") == ("same", "v")
    assert applyparams.parse_to_version("new:白背景", "v") == ("new", "白背景")
    for bad in ("new:", "other", 3):
        with pytest.raises(applyparams.ApplyError):
            applyparams.parse_to_version(bad, "")


def test_expand_targets():
    names = ["a.png", "b.png", "c.jpg", "p10.png"]
    ex = applyparams.expand_targets
    assert ex(["all"], names, set()) == names
    assert ex(["unconfigured"], names, {"b.png", "c.jpg"}) == ["b.png", "c.jpg"]
    assert ex(["*.png"], names, set()) == ["a.png", "b.png", "p10.png"]
    assert ex(["p1?.png", "c.jpg", "a.png", "a.png"], names, set()) == ["a.png", "c.jpg", "p10.png"]      # 一覧の順・重複なし
    assert ex("b.png", names, set()) == ["b.png"]
    for bad in (["nope.png"], ["z*"], [], [""], None, [3]):
        with pytest.raises(applyparams.ApplyError):
            ex(bad, names, set())


# ---- 範囲ごとの適用 ----

@pytest.mark.parametrize("scope, changed", [
    ("input", ["input"]), ("lines", ["lines"]), ("watermark", ["watermark"]), ("output", ["output"]), ("unit", ["unit"]),
    ("person", ["regions.person"]), ("background", ["regions.background"]),
    ("input,lines,output", ["input", "lines", "output"]),
])
def test_each_scope_changes_only_its_keys(env, scope, changed):
    client = env["client"]
    source = tune_source(client)
    before = get(client, "c.png")
    r = apply(client, ["c.png"], scope=scope)
    assert [x["name"] for x in r["applied"]] == ["c.png"] and r["skipped"] == [] and r["failed"] == []
    assert r["scope"] == scope.split(",")
    after = get(client, "c.png")

    def pick(params, dotted):
        for key in dotted.split("."):
            params = params[key]
        return params

    for dotted in ("unit", "input", "lines", "watermark", "output", "regions.person", "regions.background"):
        if dotted in changed:
            assert pick(after, dotted) == pick(source, dotted), dotted
        else:                                # 設定のまだ無い画像の層の id は、保存されるまで決まらない
            assert no_ids(pick(after, dotted)) == no_ids(pick(before, dotted)), dotted
    assert set(after["regions"]) == {"person", "background"}                 # 部分は、範囲に入れなければ写らない


def test_default_scope_is_everything_but_unit(env):
    client = env["client"]
    source = tune_source(client)
    r = apply(client, ["b.png", "c.png"])
    assert r["scope"] == ["input", "lines", "person", "background", "parts", "watermark", "output"]
    for name in ("b.png", "c.png"):
        after = get(client, name)
        assert after["unit"] is None                                           # 画像倍率は、頼まれたときだけ写す
        for key in ("input", "lines", "watermark", "output"):
            assert after[key] == source[key]
        assert after["regions"]["person"] == source["regions"]["person"]      # 層の id も、適用元と同じになる
        assert after["regions"]["background"] == source["regions"]["background"]
    # 部分: その画像にマスクのある部分だけが写る。無い部分は飛ばして知らせる
    rows = {x["name"]: x for x in r["applied"]}
    assert rows["b.png"]["skippedParts"] == ["clothes", "skin"] and rows["c.png"]["skippedParts"] == ["clothes", "hair", "skin"]
    assert get(client, "b.png")["regions"]["hair"] == source["regions"]["hair"]
    assert set(get(client, "b.png")["regions"]) == {"person", "background", "hair"}
    assert set(get(client, "c.png")["regions"]) == {"person", "background"}


def test_parts_by_name_and_linked_parts(env):
    client = env["client"]
    source = tune_source(client)
    patch(client, "b.png", {"regions": {"hair": {"linked": False, "levels": {"gamma": 2}}}})
    kept = get(client, "b.png")
    # 肌だけを指す: b には肌のマスクが無いので、何も変わらない（同じ設定なら「適用」に数えない）
    r = apply(client, ["b.png"], scope="parts:skin")
    assert r["applied"] == [] and r["skipped"][0]["reason"] == "すでに同じ設定です" and r["skipped"][0]["skippedParts"] == ["skin"]
    assert get(client, "b.png") == kept
    r = apply(client, ["b.png"], scope="parts:hair")
    assert get(client, "b.png")["regions"]["hair"] == source["regions"]["hair"]
    assert get(client, "b.png")["regions"]["person"] == kept["regions"]["person"]
    # 適用元で「人物と同じ」の部分は、適用先でも人物に連動させる（持っている設定は残す）
    patch(client, "b.png", {"regions": {"hair": {"linked": False, "levels": {"gamma": 3}}}})
    patch(client, "a.png", {"regions": {"hair": {"linked": True}}})
    apply(client, ["b.png"], scope="parts")
    hair = get(client, "b.png")["regions"]["hair"]
    assert hair["linked"] is True and hair["levels"]["gamma"] == 3
    # 置き場に無い部分名は、どの画像にも当たらない（エラーにはしない）
    assert apply(client, ["b.png"], scope="parts:nope")["applied"] == []


# ---- 適用先の選び方 ----

def test_targets_all_unconfigured_glob_and_source_itself(env):
    client = env["client"]
    tune_source(client)
    patch(client, "d.png", {"lines": {"strength": 0.9}})
    r = apply(client, ["unconfigured"], scope="lines")
    assert [x["name"] for x in r["applied"]] == ["b.png", "c.png"]            # 設定のまだ無い画像だけ
    assert get(client, "d.png")["lines"]["strength"] == 0.9
    r = apply(client, ["all"], scope="lines")
    assert [x["name"] for x in r["applied"]] == ["d.png"]
    assert {x["name"]: x["reason"] for x in r["skipped"]} == {
        "a.png": "適用元の画像です", "b.png": "すでに同じ設定です", "c.png": "すでに同じ設定です"}
    patch(client, "a.png", {"lines": {"strength": 0.2}})
    r = apply(client, ["[bc].png"], scope="lines")
    assert [x["name"] for x in r["applied"]] == ["b.png", "c.png"] and get(client, "d.png")["lines"]["strength"] == 0.4
    images = {i["name"]: i for i in client.get("/api/images").get_json()["images"]}
    assert all(images[n]["hasParams"] for n in ("a.png", "b.png", "c.png", "d.png"))


def test_bad_input(env):
    client = env["client"]
    tune_source(client)
    assert apply(client, ["nope.png"], expect=404)["error"].startswith("その名前の画像はありません")
    assert apply(client, ["zz*"], expect=404)["error"].startswith("当てはまる画像がありません")
    assert apply(client, [], expect=400)["error"]
    assert apply(client, ["b.png"], expect=400, scope="nope")["error"].startswith("範囲の名前が違います")
    assert apply(client, ["b.png"], expect=400, toVersion="other")["error"]
    assert apply(client, ["b.png"], expect=400, toVersion="new:a/b")["error"].startswith("バージョン名")
    assert apply(client, ["b.png"], expect=404, toFolder="無い")["error"].startswith("適用先の画像フォルダ")
    for src in ({"name": "nope.png"}, {"name": "a.png", "version": "無い"}, {"name": "a.png", "folder": "無い"}):
        r = send(client, "POST", "/api/apply-params", {"from": src, "to": ["b.png"]})
        assert r.status_code == 404, r.data
    assert send(client, "POST", "/api/apply-params", {"to": ["b.png"]}).status_code == 400
    assert not (env["out"] / "b.inkdeus.json").exists()                       # どれも、何も書かない
    assert client.get("/api/apply-params").get_json()["applies"] == []
    # 壊れた設定ファイル: 適用元なら断る。適用先なら、その画像だけ飛ばす（上書きしない）
    (env["out"] / "b.inkdeus.json").write_bytes(b"")
    r = apply(client, ["b.png", "c.png"], scope="lines")
    assert [x["name"] for x in r["applied"]] == ["c.png"] and r["skipped"][0]["reason"].startswith("設定ファイルが壊れています")
    assert (env["out"] / "b.inkdeus.json").read_bytes() == b""
    r = send(client, "POST", "/api/apply-params", {"from": {"name": "b.png"}, "to": ["c.png"]})
    assert r.status_code == 422


# ---- 適用先のバージョン ----

def test_to_version_default_same_and_new(env):
    client, out = env["client"], env["out"]
    tune_source(client)
    assert send(client, "POST", "/api/versions/a.png", {"version": "白背景"}).status_code == 200
    patch(client, "a.png", {"lines": {"strength": 0.11}}, version="白背景")
    patch(client, "b.png", {"lines": {"strength": 0.7}, "output": {"binary": False}})
    q = env["session"].hub.subscribe()
    body = {"from": {"name": "a.png", "version": "白背景"}, "to": ["b.png", "c.png"], "scope": "lines"}
    # 既定のバージョンへ
    r = send(client, "POST", "/api/apply-params", {**body, "to": ["c.png"]}).get_json()
    assert r["toVersion"] == {"kind": "default", "name": ""} and r["applied"][0]["version"] == "" and r["applied"][0]["created"] is False
    assert get(client, "c.png")["lines"]["strength"] == 0.11
    # 同じ名前のバージョンへ（無ければ、その画像の既定のバージョンを複製して作る）
    while not q.empty():
        q.get_nowait()
    r = send(client, "POST", "/api/apply-params", {**body, "toVersion": "same"}).get_json()
    assert [(x["name"], x["version"], x["created"]) for x in r["applied"]] == [("b.png", "白背景", True), ("c.png", "白背景", True)]
    events = [q.get_nowait() for _ in range(q.qsize())]
    assert [e["type"] for e in events] == ["params", "versions", "params", "versions", "apply"]
    assert events[1] == {"type": "versions", "action": "create", "folder": "default", "name": "b.png", "version": "白背景",
                         "file": "b@白背景.png", "copyFrom": ""}
    assert events[-1]["names"] == ["b.png", "c.png"] and events[-1]["action"] == "apply"
    made = get(client, "b.png", "白背景")
    assert made["lines"]["strength"] == 0.11 and made["output"]["binary"] is False       # ほかは、b の既定の設定のまま
    assert get(client, "b.png")["lines"]["strength"] == 0.7                                # 既定のバージョンは変わらない
    # もうあるバージョンには、そのまま適用する
    patch(client, "a.png", {"lines": {"strength": 0.22}}, version="白背景")
    r = send(client, "POST", "/api/apply-params", {**body, "toVersion": "same"}).get_json()
    assert [x["created"] for x in r["applied"]] == [False, False] and get(client, "b.png", "白背景")["lines"]["strength"] == 0.22
    # 新しい名前のバージョンへ（元の設定を残したいとき）
    r = send(client, "POST", "/api/apply-params", {**body, "toVersion": "new:試し"}).get_json()
    assert [(x["version"], x["created"]) for x in r["applied"]] == [("試し", True), ("試し", True)]
    assert (out / "b@試し.inkdeus.json").is_file() and get(client, "b.png")["lines"]["strength"] == 0.7
    # 適用元が既定のバージョンのときの same は、既定のバージョンへ
    r = apply(client, ["d.png"], scope="lines", toVersion="same")
    assert r["applied"][0]["version"] == "" and r["toVersion"] == {"kind": "same", "name": ""}


# ---- 層の消しゴム ----

def test_layer_erasers_kept_skipped_or_forced(env):
    client, out, store = env["client"], env["out"], env["store"]
    source = tune_source(client)
    src_id = source["regions"]["person"]["layers"][0]["id"]
    ids = {}
    for name in ("b.png", "c.png"):
        saved = patch(client, name, {"lines": {"strength": 0.5}})             # 設定を保存しておく（層の id が決まる）
        ids[name] = saved["regions"]["person"]["layers"][0]["id"]
        assert client.put(f"/api/paint/{name}/layer-{ids[name]}", data=png_bytes(9)).status_code == 200
        assert client.put(f"/api/paint/{name}/pen", data=png_bytes(3)).status_code == 200
    # 層を替えない範囲なら、消しゴムはそのまま
    r = apply(client, ["b.png"], scope="lines,background")
    assert [x["name"] for x in r["applied"]] == ["b.png"] and r["applied"][0]["droppedErasers"] == []
    assert (out / "b.paint" / f"layer-{ids['b.png']}.png").is_file()
    # 層が入れ替わる範囲: 対応づけられない画像は飛ばして、一覧に出す
    r = apply(client, ["b.png", "c.png", "d.png"], scope="person")
    assert [x["name"] for x in r["applied"]] == ["d.png"]
    assert [(x["name"], x["erasers"]) for x in r["skipped"]] == [("b.png", [f"layer-{ids['b.png']}"]), ("c.png", [f"layer-{ids['c.png']}"])]
    assert "層の消しゴム" in r["skipped"][0]["reason"]
    assert get(client, "b.png")["regions"]["person"]["layers"][0]["id"] == ids["b.png"]
    # 強制: 消しゴムを捨てて適用する（捨てた分は退避される。ペンは残る）
    r = apply(client, ["b.png"], scope="person", force=True)
    assert r["applied"][0]["droppedErasers"] == [f"layer-{ids['b.png']}"] and r["force"] is True
    assert sorted(p.name for p in (out / "b.paint").glob("*.png")) == ["pen.png"]
    assert store.entries(f"b.paint/layer-{ids['b.png']}.png")[0].path.read_bytes() == png_bytes(9)
    assert get(client, "b.png")["regions"]["person"]["layers"][0]["id"] == src_id
    # 層の id が適用元と同じになったので、次からは対応づけられる（同じ id の層の消しゴムは保たれる）
    assert client.put(f"/api/paint/b.png/layer-{src_id}", data=png_bytes(5)).status_code == 200
    patch(client, "a.png", {"regions": {"person": {"layers": [{"id": src_id, "type": "hatch", "angle": 80, "spacing": 4}]}}})
    r = apply(client, ["b.png"], scope="person")
    assert r["applied"][0]["droppedErasers"] == [] and (out / "b.paint" / f"layer-{src_id}.png").read_bytes() == png_bytes(5)
    assert get(client, "b.png")["regions"]["person"]["layers"][0]["angle"] == 80
    # 取り消すと、捨てた消しゴムも戻る
    forced = client.get("/api/apply-params").get_json()["applies"][1]
    assert forced["count"] == 1 and forced["undone"] is None
    send(client, "POST", "/api/apply-params/undo", {})                        # 直前（80° の適用）を取り消す
    r = send(client, "POST", "/api/apply-params/undo", {"id": forced["id"]}).get_json()
    assert [x["name"] for x in r["undone"]] == ["b.png"]
    assert (out / "b.paint" / f"layer-{ids['b.png']}.png").read_bytes() == png_bytes(9)
    assert get(client, "b.png")["regions"]["person"]["layers"][0]["id"] == ids["b.png"]
    index = json.loads((out / "b.paint" / "index.json").read_text(encoding="utf-8"))
    assert f"layer-{ids['b.png']}" in [t["target"] for t in index["targets"]]


# ---- 退避と、一括の取り消し ----

def test_backup_and_undo_as_one_unit(env):
    client, out, store = env["client"], env["out"], env["store"]
    tune_source(client)
    patch(client, "b.png", {"lines": {"strength": 0.7}})
    b_before = (out / "b.inkdeus.json").read_bytes()
    r = apply(client, ["b.png", "c.png", "d.png"], scope="lines", toVersion="default")
    assert r["undoable"] is True and len(r["applied"]) == 3
    # 適用先ごとに、前の設定が退避されている（無かった物は「無かった」印）
    assert store.entries("b.inkdeus.json")[0].path.read_bytes() == b_before
    assert store.entries("c.inkdeus.json")[0].absent and store.entries("d.inkdeus.json")[0].absent
    rows = client.get("/api/apply-params").get_json()["applies"]
    assert len(rows) == 1 and rows[0]["id"] == r["id"] and rows[0]["count"] == 3 and rows[0]["from"]["name"] == "a.png"
    # 適用のあとで 1 枚だけ手を入れても、取り消しは「適用の直前」へ戻す
    patch(client, "d.png", {"lines": {"strength": 0.01}})
    q = env["session"].hub.subscribe()
    u = send(client, "POST", "/api/apply-params/undo", {}).get_json()
    assert u["id"] == r["id"] and [x["name"] for x in u["undone"]] == ["b.png", "c.png", "d.png"]
    assert (out / "b.inkdeus.json").read_bytes() == b_before
    assert not (out / "c.inkdeus.json").exists() and not (out / "d.inkdeus.json").exists()
    events = [q.get_nowait() for _ in range(q.qsize())]
    assert [(e["type"], e.get("name"), e.get("rev") is None) for e in events[:3]] == [
        ("params", "b.png", False), ("params", "c.png", True), ("params", "d.png", True)]
    assert events[-1]["type"] == "apply" and events[-1]["action"] == "undo"
    assert env["session"].scan() == []                                        # サーバ自身の変更は「外部からの変更」にならない
    # 取り消した適用は、もう取り消せない。戻す前の状態も残っている（画像ごとに戻せる）
    assert send(client, "POST", "/api/apply-params/undo", {}).status_code == 404
    assert send(client, "POST", "/api/apply-params/undo", {"id": r["id"]}).status_code == 409
    assert client.get("/api/apply-params").get_json()["applies"][0]["undone"] > 0
    assert len(store.entries("d.inkdeus.json")) == 3
    # バージョンを作った適用の取り消しは、作ったバージョンごと消す
    r = apply(client, ["b.png"], scope="lines", toVersion="new:試し")
    (out / "b@試し.png").write_bytes(png_bytes())
    u = send(client, "POST", "/api/apply-params/undo", {"id": r["id"]}).get_json()
    assert u["undone"] == [{"folder": "default", "name": "b.png", "version": "試し", "created": True}]
    assert not (out / "b@試し.inkdeus.json").exists() and not (out / "b@試し.png").exists()
    assert [v["name"] for v in client.get("/api/versions/b.png").get_json()["versions"]] == [""]


def test_without_backup_apply_works_but_cannot_undo(env):
    (env["tmp"] / "none.toml").write_text("[backup]\nenabled = false\n", encoding="utf-8")
    project = env["project"]
    app = create_app(project.source, project.masks, project.out, project=project.info(), parts_dir=project.parts,
                     local_config=env["tmp"] / "none.toml", watch=False)
    client = app.test_client()
    tune_source(client)
    r = apply(client, ["b.png"], scope="lines")
    assert len(r["applied"]) == 1 and r["undoable"] is False and r["id"] is None
    assert send(client, "POST", "/api/apply-params/undo", {}).status_code == 404


# ---- フォルダをまたぐ ----

def test_across_folders(env):
    client, out = env["client"], env["out"]
    source = tune_source(client)
    r = apply(client, ["all"], toFolder="二巻")
    assert [(x["folder"], x["name"]) for x in r["applied"]] == [("二巻", "a.png"), ("二巻", "e.png")]
    assert r["skipped"] == []                                                 # 別のフォルダの a.png は、適用元とは別の画像
    other = get(client, "a.png", folder="二巻")
    assert other["lines"] == source["lines"] and other["regions"]["hair"] == source["regions"]["hair"]
    assert "skin" not in other["regions"]                                     # そのフォルダに、肌のマスクは無い
    assert (out / "@二巻" / "e.inkdeus.json").is_file()
    # 別のフォルダから既定のフォルダへ
    patch(client, "e.png", {"lines": {"strength": 0.05}}, folder="二巻")
    r = send(client, "POST", "/api/apply-params", {"from": {"name": "e.png", "folder": "二巻"}, "to": ["c.png"],
                                                   "toFolder": "default", "scope": "lines"}).get_json()
    assert r["applied"][0]["folder"] == "default" and get(client, "c.png")["lines"]["strength"] == 0.05
    # 適用先を省くと、適用元と同じフォルダ
    r = send(client, "POST", "/api/apply-params", {"from": {"name": "e.png", "folder": "二巻"}, "to": ["a.png"],
                                                   "scope": "lines"}).get_json()
    assert r["toFolder"] == "二巻" and get(client, "a.png", folder="二巻")["lines"]["strength"] == 0.05
    # まとめて取り消せる（別のフォルダの分も）
    u = send(client, "POST", "/api/apply-params/undo", {"id": client.get("/api/apply-params").get_json()["applies"][-1]["id"]})
    assert [(x["folder"], x["name"]) for x in u.get_json()["undone"]] == [("二巻", "a.png"), ("二巻", "e.png")]
    assert not (out / "@二巻" / "e.inkdeus.json").exists()


def test_applied_images_become_stale(env):
    client, out = env["client"], env["out"]
    tune_source(client)
    patch(client, "b.png", {"lines": {"strength": 0.7}})
    assert client.post("/api/export/b.png", data=png_bytes()).status_code == 200
    rows = {i["name"]: i for i in client.get("/api/images").get_json()["images"]}
    assert rows["b.png"]["exportFresh"] is True
    import os
    past = (out / "b.png").stat().st_mtime - 5
    os.utime(out / "b.png", (past, past))
    os.utime(out / "b.inkdeus.json", (past - 5, past - 5))
    apply(client, ["b.png"], scope="lines")
    rows = {i["name"]: i for i in client.get("/api/images").get_json()["images"]}
    assert rows["b.png"]["hasExport"] is True and rows["b.png"]["exportFresh"] is False


# ---- ctl と backup コマンド ----

@pytest.fixture()
def live(env):
    server, url = serve(env["app"])
    yield {**env, "port": server.server_port}
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


def test_ctl_apply_and_undo(ctl, live):
    client = live["client"]
    tune_source(client)
    r = ctl("apply", "--from", "a.png", "--to", "b.png", "c.png", "--scope", "lines,parts:hair")
    assert [x["name"] for x in r["applied"]] == ["b.png", "c.png"] and r["scope"] == ["lines", "parts:hair"]
    assert get(client, "b.png")["regions"]["hair"]["solid"]["threshold"] == 1.0
    r = ctl("apply", "--from", "a.png", "--to", "unconfigured", "--to-version", "new:試し", "--scope", "output")
    assert [(x["name"], x["version"]) for x in r["applied"]] == [("d.png", "試し")]
    r = ctl("apply", "--from", "a.png", "--to", "all", "--to-folder", "二巻", "--scope", "lines")
    assert [x["folder"] for x in r["applied"]] == ["二巻", "二巻"]
    patch(client, "e.png", {"input": {"smoothRadius": 7}}, folder="二巻")
    r = ctl("apply", "--from", "e.png", "--from-folder", "二巻", "--to", "*.png", "--scope", "input")
    assert r["toFolder"] == "二巻" and [x["name"] for x in r["skipped"]] == ["e.png"]
    rows = ctl("apply", "--list")["applies"]
    assert [x["count"] for x in rows] == [1, 2, 1, 2]
    # 取り消し: 直前の物から順に。番号でも選べる
    assert [x["name"] for x in ctl("apply", "--undo")["undone"]] == ["a.png"]
    assert [x["folder"] for x in ctl("apply", "--undo")["undone"]] == ["二巻", "二巻"]
    assert [x["name"] for x in ctl("apply", "--undo", "4")["undone"]] == ["b.png", "c.png"]
    assert not (live["out"] / "c.inkdeus.json").exists() and (live["out"] / "d@試し.inkdeus.json").is_file()
    # 不正な入力は非 0 で終わり、何も変えない
    assert "error" in ctl("apply", "--from", "a.png", expect=1)
    assert "error" in ctl("apply", "--from", "a.png", "--to", "nope.png", expect=1)
    assert "error" in ctl("apply", "--from", "a.png", "--to", "b.png", "--scope", "bad", expect=1)
    assert "error" in ctl("apply", "--undo", "99", expect=1)
    # --export はブラウザが要る（適用そのものは済んでいる）
    r = ctl("apply", "--from", "a.png", "--to", "c.png", "--scope", "lines", "--export", expect=2)
    assert r["error"].startswith("適用は済みましたが") and [x["name"] for x in r["applied"]] == ["c.png"]
    assert get(client, "c.png")["lines"]["strength"] == 0.4


def test_backup_command_lists_and_undoes_applies(env, capsys, monkeypatch):
    monkeypatch.setenv("INKDEUS_PROJECTS", str(env["tmp"] / "projects"))
    monkeypatch.setattr("inkdeus.server.default_local_config", lambda: env["tmp"] / "none.toml")
    client, out = env["client"], env["out"]
    tune_source(client)
    patch(client, "b.png", {"lines": {"strength": 0.7}})
    apply(client, ["b.png", "c.png"], scope="lines")
    apply(client, ["d.png"], scope="output", toVersion="new:試し")

    def run(*args):
        capsys.readouterr()
        code = cli.main(["backup", *args])
        return code, capsys.readouterr()

    code, text = run("list", "book", "--kind", "apply")
    assert code == 0 and "a.png → 1 枚（output）" in text.out and "a.png → 2 枚（lines）" in text.out
    code, text = run("list", "book", "--kind", "apply", "--json")
    rows = json.loads(text.out)["applies"]
    assert [r["count"] for r in rows] == [1, 2] and rows[0]["toVersion"] == {"kind": "new", "name": "試し"}
    code, text = run("restore", "book", "--kind", "apply", "--at", "2", "--json")
    assert code == 0 and [r["name"] for r in json.loads(text.out)["undone"]] == ["b.png", "c.png"]
    assert get(client, "b.png")["lines"]["strength"] == 0.7 and not (out / "c.inkdeus.json").exists()
    code, text = run("restore", "book", "--kind", "apply", "--at", "2")
    assert code == 1 and "もう取り消してあります" in text.err
    code, text = run("restore", "book", "--kind", "apply", "--at", "1")
    assert code == 0 and "1 枚" in text.out and not (out / "d@試し.inkdeus.json").exists()
    code, text = run("list", "book", "--kind", "apply")
    assert text.out.count("［取り消し済み］") == 2
