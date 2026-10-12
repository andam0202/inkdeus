"""部分ごとの塗り分け（髪・肌・服など）: 部分マスクの置き場の登録と検出・CLI・API・設定の検証・ctl"""

from __future__ import annotations

import io
import json
import os
import tomllib

import numpy as np
import pytest
from PIL import Image

import synth
from conftest import no_ids, serve
from inkdeus import cli, schema
from inkdeus import parts as parts_mod
from inkdeus.folders import parse_add
from inkdeus.projects import (ProjectError, add_asset_source, add_folder, create_project, load_project, parts_summary,
                              set_parts)
from inkdeus.server import create_app

W, H = 64, 48


def make_dirs(root):
    """画像 a（髪・肌・服のマスクあり）・b（髪だけ）・c（マスクなし）と、別の画像フォルダ（a だけ。髪のマスクあり）"""
    img, mask = synth.scene(W, H)
    for name in ("a", "b", "c"):
        synth.save(root / "images" / f"{name}.png", img)
        synth.save(root / "masks" / f"{name}.png", mask)
    parts = synth.part_masks(W, H)
    synth.save_parts(root / "parts", "a", parts)
    synth.save_parts(root / "parts", "b", {"hair": parts["hair"]})
    synth.save(root / "more" / "a.png", img)
    synth.save_parts(root / "more_parts", "a", {"hair": parts["hair"]})
    return root / "images", root / "masks", root / "parts"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    images, masks, parts = make_dirs(tmp_path)
    monkeypatch.setenv("INKDEUS_PROJECTS", str(tmp_path / "projects"))
    monkeypatch.setenv("INKDEUS_CONFIG", str(tmp_path / "config"))
    return {"root": tmp_path, "images": images, "masks": masks, "parts": parts, "projects": tmp_path / "projects"}


def app_for(project, **kw):
    return create_app(project.source, project.masks, project.out, project=project.info(), parts_dir=project.parts,
                      local_config=project.path / "none.toml", watch=False, **kw)


def send(client, method, path, body=None):
    return client.open(path, method=method, data=None if body is None else json.dumps(body),
                       content_type="application/json")


# ---- 置き場の読み取り ----

def test_scan_names_reserved_and_limit(tmp_path):
    assert parts_mod.scan(None) == ([], [])
    names, warnings = parts_mod.scan(tmp_path / "nope")
    assert names == [] and "見つかりません" in warnings[0]
    root = tmp_path / "parts"
    for name in ("hair", "skin", "服", "person", "background", "all", "bad name", "x" * 33, ".hidden"):
        (root / name).mkdir(parents=True)
    (root / "note.txt").write_text("x", encoding="utf-8")            # ファイルは部分ではない
    names, warnings = parts_mod.scan(root)
    assert names == ["hair", "skin", "服"]
    text = "\n".join(warnings)
    for needle in ("「person」は予約", "「background」は予約", "「all」は予約", "「bad name」は部分名として使えません"):
        assert needle in text, text
    assert ".hidden" not in text and len(warnings) == 5
    # 6 部分まで。超えた分は名前順で切り捨てて知らせる
    for name in ("a1", "a2", "a3", "a4"):
        (root / name).mkdir()
    names, warnings = parts_mod.scan(root)
    assert names == ["a1", "a2", "a3", "a4", "hair", "skin"] and parts_mod.max_parts() == 6
    assert any("7 個" in w and "服" in w for w in warnings)
    assert schema.is_part_name("髪_1-a") and not schema.is_part_name("person") and not schema.is_part_name("a/b")
    assert not schema.is_part_name("") and not schema.is_part_name("a@b") and not schema.is_part_name(3)


def test_summary_counts_masks_per_part(env):
    info = parts_mod.summary(env["parts"], ["a", "b", "c"])
    assert info["images"] == 3 and info["warnings"] == []
    assert [(r["name"], r["masks"], r["missing"]) for r in info["parts"]] == [("clothes", 1, 2), ("hair", 2, 1), ("skin", 1, 2)]
    assert parts_mod.mask_path(env["parts"], "hair", "b").name == "b.png"
    assert parts_mod.mask_path(env["parts"], "skin", "b") is None and parts_mod.mask_path(None, "hair", "a") is None
    with pytest.raises(parts_mod.PartsError):
        parts_mod.check_dir(env["root"] / "nope")


# ---- プロジェクトの設定ファイル ----

def test_project_config_roundtrip(env):
    root = env["projects"]
    project, _ = create_project("book", env["images"], env["masks"], root=root)
    assert project.parts is None and "parts" not in tomllib.loads(project.config.read_text(encoding="utf-8"))
    project, _ = create_project("book2", env["images"], env["masks"], root=root, parts=env["parts"])
    raw = tomllib.loads(project.config.read_text(encoding="utf-8"))
    assert raw["parts"] == str(env["parts"]) and project.parts == env["parts"]
    # 追加の画像フォルダにも置き場を持てる。ほかの項目を書き直しても残る
    entry = add_folder(project, "二巻", env["root"] / "more", parts=env["root"] / "more_parts")
    assert entry["parts"] == str(env["root"] / "more_parts")
    add_asset_source(project, "sfx", env["images"])
    project = load_project(str(project.path), root=root)
    assert project.parts == env["parts"] and project.folders[0].parts == env["root"] / "more_parts"
    project, _ = create_project("book2", env["images"], env["masks"], root=root, force=True)    # --force でも残る
    assert project.parts == env["parts"] and project.folders[0].parts == env["root"] / "more_parts"
    # 後付け・付け替え・外す
    assert set_parts(project, None) is None
    project = load_project(str(project.path), root=root)
    assert project.parts is None and project.folders[0].parts is not None
    assert set_parts(project, env["parts"], "二巻") == env["parts"]
    assert set_parts(project, env["root"] / "more_parts", "default") == env["root"] / "more_parts"
    project = load_project(str(project.path), root=root)
    assert project.parts == env["root"] / "more_parts" and project.folders[0].parts == env["parts"]
    set_parts(project, None, "二巻")
    assert load_project(str(project.path), root=root).folders[0].parts is None
    assert [a["name"] for a in tomllib.loads(project.config.read_text(encoding="utf-8"))["assets"]] == ["sfx"]
    with pytest.raises(ProjectError, match="見つかりません"):
        set_parts(project, env["root"] / "nope")
    with pytest.raises(ProjectError, match="ありません"):
        set_parts(project, env["parts"], "三巻")
    with pytest.raises(ProjectError):
        create_project("book3", env["images"], root=root, parts=env["root"] / "nope")
    with pytest.raises(ProjectError):
        add_folder(project, "三巻", env["root"] / "more", parts=env["root"] / "nope")
    info = parts_summary(load_project(str(project.path), root=root))
    assert info["folder"] == "default" and [r["name"] for r in info["parts"]] == ["hair"] and info["images"] == 3
    # 起動引数の --add <名前>=<画像>[,<マスク>[,<部分マスク>]]
    d = parse_add(f"x={env['images']},{env['masks']},{env['parts']}")
    assert (d.images, d.masks, d.parts) == (env["images"], env["masks"], env["parts"])
    assert parse_add(f"x={env['images']}").parts is None and parse_add(f"x={env['images']},,{env['parts']}").masks is None


def test_cli_new_folders_parts_and_run(env, capsys, monkeypatch):
    def run(*args, expect=0):
        capsys.readouterr()
        code = cli.main(list(args))
        out = capsys.readouterr()
        assert code == expect, out.err or out.out
        return out.out + out.err

    text = run("new", "book", "--images", str(env["images"]), "--masks", str(env["masks"]), "--parts", str(env["parts"]))
    assert "部分マスク" in text and "hair: マスクあり 2 / 3 枚" in text and "clothes: マスクあり 1 / 3 枚" in text
    assert load_project("book").parts == env["parts"]
    assert "見つかりません" in run("new", "bad", "--images", str(env["images"]), "--parts", str(env["root"] / "nope"), expect=1)
    text = run("folders", "add", "book", "--name", "二巻", "--images", str(env["root"] / "more"),
               "--parts", str(env["root"] / "more_parts"))
    assert "部分マスク" in text and load_project("book").folders[0].parts == env["root"] / "more_parts"
    listed = json.loads(run("folders", "list", "book", "--json"))["folders"]
    assert [f["parts_dir"] for f in listed] == [str(env["parts"]), str(env["root"] / "more_parts")]
    # parts list / unset / set
    info = json.loads(run("parts", "list", "book", "--json"))
    assert info["dir"] == str(env["parts"]) and info["folder"] == "default"
    assert {r["name"]: r["masks"] for r in info["parts"]} == {"clothes": 1, "hair": 2, "skin": 1}
    assert json.loads(run("parts", "list", "book", "--folder", "二巻", "--json"))["parts"] == [
        {"name": "hair", "masks": 1, "missing": 0}]
    assert "外しました" in run("parts", "unset", "book")
    assert load_project("book").parts is None and "登録されていません" in run("parts", "list", "book")
    (env["parts"] / "person").mkdir()
    text = run("parts", "set", "book", "--path", str(env["parts"]))
    assert "登録しました" in text and "「person」は予約" in text and "skin: マスクあり 1 / 3 枚" in text
    assert load_project("book").parts == env["parts"]
    run("parts", "unset", "book", "--folder", "二巻")
    assert load_project("book").folders[0].parts is None
    run("parts", "set", "book", "--folder", "二巻", "--path", str(env["root"] / "more_parts"))
    assert load_project("book").folders[0].parts == env["root"] / "more_parts"
    assert "見つかりません" in run("parts", "set", "book", "--path", str(env["root"] / "nope"), expect=1)
    assert "ありません" in run("parts", "set", "book", "--folder", "三巻", "--path", str(env["parts"]), expect=1)
    assert "ありません" in run("parts", "unset", "book", "--folder", "三巻", expect=1)
    # 直接起動: --parts-dir と --add の 3 つ目
    apps = []
    monkeypatch.setattr(cli, "serve", lambda app, host, port, project=None: apps.append((app, project)) or 0)
    run("run", str(env["images"]), "--mask-dir", str(env["masks"]), "--parts-dir", str(env["parts"]),
        "--out-dir", str(env["root"] / "o1"), "--add", f"more={env['root'] / 'more'},,{env['root'] / 'more_parts'}")
    client = apps[-1][0].test_client()
    assert client.get("/api/images").get_json()["parts"] == ["clothes", "hair", "skin"]
    assert client.get("/api/images?folder=more").get_json()["images"][0]["parts"] == ["hair"]
    assert "見つかりません" in run("run", str(env["images"]), "--parts-dir", str(env["root"] / "nope"), expect=1)
    run("open", "book")
    app, project = apps[-1]
    assert project.name == "book" and app.config["INKDEUS_LIBRARY"].parts_dir == env["parts"]


# ---- API ----

def test_api_lists_and_serves_part_masks(env):
    project, _ = create_project("book", env["images"], env["masks"], root=env["projects"], parts=env["parts"])
    add_folder(project, "二巻", env["root"] / "more", parts=env["root"] / "more_parts")
    client = app_for(load_project(str(project.path), root=env["projects"])).test_client()
    assert "parts" in client.get("/api/config").get_json()["features"]
    listing = client.get("/api/images").get_json()
    assert listing["parts"] == ["clothes", "hair", "skin"] and listing["partsDir"] == str(env["parts"])
    assert listing["partWarnings"] == []
    assert {i["name"]: i["parts"] for i in listing["images"]} == {
        "a.png": ["clothes", "hair", "skin"], "b.png": ["hair"], "c.png": []}
    r = client.get("/api/part/a.png/hair")
    assert r.status_code == 200 and r.mimetype == "image/png"
    with Image.open(io.BytesIO(r.data)) as im:
        assert im.size == (W, H) and (np.asarray(im.convert("L")) > 127).any()
    for path in ("/api/part/b.png/skin", "/api/part/c.png/hair", "/api/part/a.png/nope", "/api/part/a.png/person",
                 "/api/part/nope.png/hair", "/api/part/a.png/hair?folder=nope"):
        assert client.get(path).status_code == 404, path
    assert client.get("/api/images?folder=二巻").get_json()["images"][0]["parts"] == ["hair"]
    assert client.get("/api/part/a.png/hair?folder=二巻").status_code == 200
    assert client.get("/api/part/a.png/skin?folder=二巻").status_code == 404
    rows = {f["name"]: f for f in client.get("/api/folders").get_json()["folders"]}
    assert rows["default"]["partNames"] == ["clothes", "hair", "skin"] and rows["default"]["parts"] == str(env["parts"])
    assert rows["二巻"]["partNames"] == ["hair"]
    assert client.get("/api/state").get_json()["parts"] == str(env["parts"])
    # 予約された名前のフォルダは部分に数えず、注意として返す
    (env["parts"] / "background").mkdir()
    listing = client.get("/api/images").get_json()
    assert listing["parts"] == ["clothes", "hair", "skin"] and "background" in listing["partWarnings"][0]


def test_export_goes_stale_when_a_part_mask_changes(env):
    project, _ = create_project("book", env["images"], env["masks"], root=env["projects"], parts=env["parts"])
    client = app_for(project).test_client()
    buf = io.BytesIO()
    Image.new("L", (4, 4), 200).save(buf, "PNG")
    client.put("/api/params/a.png", data="{}", content_type="application/json")
    client.post("/api/export/a.png", data=buf.getvalue())
    item = lambda: next(i for i in client.get("/api/images").get_json()["images"] if i["name"] == "a.png")  # noqa: E731
    assert item()["exportFresh"] is True
    mask = env["parts"] / "hair" / "a.png"
    later = (project.out / "a.png").stat().st_mtime_ns + 5_000_000_000
    os.utime(mask, ns=(later, later))
    assert item()["exportFresh"] is False               # 部分マスクが新しくなったら、白黒版は「古い」


def test_running_server_picks_up_parts_registered_later(env):
    project, _ = create_project("book", env["images"], env["masks"], root=env["projects"])
    add_folder(project, "二巻", env["root"] / "more")
    project = load_project(str(project.path), root=env["projects"])
    app = app_for(project)
    client = app.test_client()
    client.post("/api/stats/a.png?folder=二巻", data=json.dumps({"rev": "x"}), content_type="application/json")
    assert client.get("/api/images").get_json()["parts"] == [] and client.get("/api/part/a.png/hair").status_code == 404
    set_parts(project, env["parts"])                    # 稼働中に登録する（inkdeus parts set）
    listing = client.get("/api/images").get_json()
    assert listing["parts"] == ["clothes", "hair", "skin"] and listing["images"][0]["parts"] == ["clothes", "hair", "skin"]
    assert client.get("/api/part/a.png/hair").status_code == 200
    set_parts(project, env["root"] / "more_parts", "二巻")
    assert client.get("/api/images?folder=二巻").get_json()["images"][0]["parts"] == ["hair"]
    # 置き場だけが変わっても、そのフォルダの状態（統計など）は作り直さない
    assert client.get("/api/stats/a.png?folder=二巻").get_json()["stats"]["rev"] == "x"
    (env["parts"] / "eyes").mkdir()                     # 稼働中に部分が増えた
    synth.save(env["parts"] / "eyes" / "a.png", synth.flat(W, H, 0)[..., 0])
    assert "eyes" in client.get("/api/images").get_json()["images"][0]["parts"]
    set_parts(project, None)
    assert client.get("/api/images").get_json()["parts"] == [] and client.get("/api/part/a.png/hair").status_code == 404


# ---- 設定の検証 ----

def test_part_regions_in_params():
    p = schema.default_params()
    assert list(p["regions"]) == ["person", "background"]                       # 部分の項目は、書かれたときだけ持つ
    assert p["regions"]["person"]["solid"]["lineKnockout"] == 0
    raw = {"regions": {"hair": {"linked": False, "solid": {"threshold": 1, "lineKnockout": 0.8}, "layers": []},
                       "skin": {"linked": True}}}
    q, problems = schema.normalize(raw, strict=True)
    assert problems == [] and list(q["regions"]) == ["person", "background", "hair", "skin"]
    hair, skin = q["regions"]["hair"], q["regions"]["skin"]
    assert hair == {"linked": False, "levels": {"brightness": 0, "contrast": 1, "gamma": 1},
                    "solid": {"enabled": True, "threshold": 1, "soft": 0, "lineKnockout": 0.8}, "layers": []}
    assert skin["linked"] is True and len(skin["layers"]) == 4
    assert schema.normalize(q, strict=True)[0] == q
    assert schema.part_keys(q) == ["hair", "skin"] and schema.region_names(q) == ["person", "background", "hair", "skin"]
    ids = [l["id"] for name in schema.region_names(q) for l in q["regions"][name]["layers"]]
    assert len(set(ids)) == len(ids) == 12 and schema.layer_ids(q) == set(ids)   # 層の id は部分も通して重複しない
    # linked は既定で true。その画像に無い部分の項目も残る（知っている部分名を渡しても、形が部分なら警告しない）
    assert schema.normalize({"regions": {"hair": {"linked": True}}}, parts=["skin"])[1] == []
    assert schema.normalize({"regions": {"hair": {}}}, parts=["hair"])[0]["regions"]["hair"]["linked"] is True
    # 形が部分でない・名前が使えない・予約の名前は、未知のキー
    for key, value in (("sky", {}), ("a b", {"linked": True}), ("all", {"linked": True}), ("hair", 3)):
        params, problems = schema.normalize({"regions": {key: value}})
        assert any(f"未知のキー: regions.{key}" in x for x in problems) and key not in params["regions"]
    # strict で部分名を渡したとき（サーバ）は、知っている名前だけを通す
    with pytest.raises(schema.ValidationError, match="regions.hiar"):
        schema.normalize({"regions": {"hiar": {"linked": False}}}, strict=True, parts=["hair"])
    for bad in ({"linked": "no"}, {"linked": False, "solid": {"lineKnockout": 2}}, {"linked": False, "mode": "white"},
                {"linked": False, "layers": [{"type": "hatch"}] * 9}):
        with pytest.raises(schema.ValidationError):
            schema.normalize({"regions": {"hair": bad}}, strict=True, parts=["hair"])
    assert schema.lacks_layer_ids({"regions": {"person": {"layers": []}, "background": {"layers": []},
                                               "hair": {"linked": False, "layers": [{"type": "hatch"}]}}}) is True


def test_set_path_patch_and_seed_for_parts():
    p = schema.normalize({"regions": {"person": {"solid": {"threshold": 0.3}, "levels": {"gamma": 2},
                                                 "layers": [{"type": "hatch", "angle": 10}]}}})[0]
    # まだ設定の無い部分へ set すると、人物の設定を写した項目ができる（linked は true のまま）
    q = schema.normalize(schema.set_path(p, "regions.hair.solid.lineKnockout", 0.8), strict=True, parts=["hair"])[0]
    hair = q["regions"]["hair"]
    assert hair["linked"] is True and hair["solid"] == {"enabled": True, "threshold": 0.3, "soft": 0, "lineKnockout": 0.8}
    assert hair["levels"]["gamma"] == 2 and [l["angle"] for l in hair["layers"]] == [10]
    assert hair["layers"][0]["id"] != p["regions"]["person"]["layers"][0]["id"]
    q2 = schema.set_path(q, "regions.hair.linked", False)
    assert schema.normalize(q2, strict=True, parts=["hair"])[0]["regions"]["hair"]["linked"] is False
    with pytest.raises(schema.ValidationError):
        schema.set_path(p, "regions.a b.solid.threshold", 1)
    with pytest.raises(schema.ValidationError):
        schema.set_path(p, "regions.person.nope.x", 1)
    # JSON Merge Patch で新しく現れた部分は、書かれていない項目を人物から補う
    merged = schema.seed_parts(schema.merge_patch(p, {"regions": {"skin": {"linked": False, "solid": {"enabled": False}}}}), p)
    skin = schema.normalize(merged, strict=True, parts=["skin"])[0]["regions"]["skin"]
    assert skin["linked"] is False and skin["solid"] == {"enabled": False, "threshold": 0.3, "soft": 0, "lineKnockout": 0}
    assert skin["levels"]["gamma"] == 2 and [l["angle"] for l in skin["layers"]] == [10]
    # 既にある部分への変更は、そのまま重なる（補い直さない）
    again = schema.seed_parts(schema.merge_patch(merged, {"regions": {"skin": {"layers": []}}}), merged)
    assert schema.normalize(again, strict=True, parts=["skin"])[0]["regions"]["skin"]["layers"] == []
    # 層を丸ごと書き直しても、同じ位置の層の id を引き継ぐ（部分でも）
    base = schema.normalize(merged, parts=["skin"])[0]
    patched = schema.inherit_layer_ids(schema.merge_patch(base, {"regions": {"skin": {"layers": [{"type": "hatch", "angle": 77}]}}}), base)
    assert patched["regions"]["skin"]["layers"][0]["id"] == base["regions"]["skin"]["layers"][0]["id"]


@pytest.mark.parametrize("name,region,key", [
    ("ベタ", "person", "solid-black"), ("黒ベタ", "hair", "solid-black"), ("塗りつぶし", "background", "solid-black"),
    ("solid-black", None, "solid-black"), ("Solid-Black", None, "solid-black"),
    ("白抜き", "person", "blank"), ("白", "hair", "blank"), ("真っ白", "肌", "blank"), ("blank", "background", "blank"),
    ("白", "background", "white"), ("白抜き", "background", "white"), ("白", None, "white"), ("白", "both", "white"),
    ("線だけ", "hair", "lines-only"), ("トーンなし", None, "blank"),
])
def test_new_styles_and_region_dependent_aliases(name, region, key):
    assert schema.resolve_style(name, region) == key


def test_styles_apply_to_parts():
    p = schema.default_params()
    q = schema.apply_style(p, "ベタ", "hair", parts=["hair"])
    hair = q["regions"]["hair"]
    assert hair["linked"] is False and hair["layers"] == []
    assert hair["solid"] == {"enabled": True, "threshold": 1.0, "soft": 0, "lineKnockout": 0}
    assert q["regions"]["person"] == p["regions"]["person"] and q["regions"]["background"] == p["regions"]["background"]
    assert schema.normalize(q, strict=True, parts=["hair"])[0] == q
    # 連動中の部分に当てると、人物の明るさ補正を写してから切り離す。切り離し済みなら、明るさ補正はそのまま
    bright = schema.set_path(p, "regions.person.levels.brightness", 0.25)
    q = schema.apply_style(bright, "hatch1", "skin", parts=["skin"])
    assert q["regions"]["skin"]["levels"]["brightness"] == 0.25 and len(q["regions"]["skin"]["layers"]) == 1
    q = schema.set_path(q, "regions.skin.levels.brightness", -0.5)
    q = schema.apply_style(q, "白抜き", "skin")
    skin = q["regions"]["skin"]
    assert skin["levels"]["brightness"] == -0.5 and skin["layers"] == [] and skin["solid"]["enabled"] is False
    # 人物に「ベタ」「白抜き」。背景に「白抜き」は、白（mode=white）ではなく「層もベタも無し」
    assert schema.apply_style(p, "ベタ", "person")["regions"]["person"]["solid"]["threshold"] == 1.0
    bg = schema.apply_style(p, "blank", "background")["regions"]["background"]
    assert (bg["mode"], bg["linked"], bg["layers"], bg["solid"]["enabled"]) == ("keep", False, [], False)
    assert schema.apply_style(p, "白", "background")["regions"]["background"]["mode"] == "white"
    both = schema.apply_style(p, "ベタ", "both")
    assert list(both["regions"]) == ["person", "background"] and both["regions"]["background"]["solid"]["threshold"] == 1.0
    # 適用先の検証（部分名を渡したときは、その中にある物だけ）
    with pytest.raises(schema.ValidationError, match="hair"):
        schema.apply_style(p, "ベタ", "hiar", parts=["hair"])
    with pytest.raises(schema.ValidationError):
        schema.apply_style(p, "ベタ", "hair")                 # 部分名を渡さなければ、設定に入っている部分だけ
    assert schema.apply_style(q, "ベタ", "skin")["regions"]["skin"]["solid"]["threshold"] == 1.0
    names = {s["name"]: s for s in schema.list_styles()}
    assert names["solid-black"]["solid"] is True and names["solid-black"]["layers"] == 0 and "ベタ" in names["solid-black"]["aliases"]
    assert names["blank"]["aliasesNonBackground"] == ["白", "白抜き", "真っ白"] and names["blank"]["origin"] == "builtin"
    # 人物・部分向けの別名どうしもぶつからない
    seen = {}
    for key, style in schema.load()["styles"].items():
        for n in style.get("aliasesNonBackground", []):
            assert seen.setdefault(schema._style_key(n), key) == key


# ---- 稼働中のサーバへの外部操作（ctl） ----

@pytest.fixture()
def live(env):
    project, _ = create_project("book", env["images"], env["masks"], root=env["projects"], parts=env["parts"])
    server, url = serve(app_for(project))
    yield {"port": server.server_port, "out": project.out, "project": project}
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


def test_ctl_preset_and_set_on_parts(ctl, live):
    r = ctl("preset", "ベタ", "--region", "hair", "--name", "a.png")
    assert (r["style"], r["region"]) == ("solid-black", "hair")
    r = ctl("set", "regions.hair.solid.lineKnockout", "0.8", "--name", "a.png")
    hair = ctl("get", "--name", "a.png")["params"]["regions"]["hair"]
    assert hair == {"linked": False, "levels": {"brightness": 0, "contrast": 1, "gamma": 1},
                    "solid": {"enabled": True, "threshold": 1.0, "soft": 0, "lineKnockout": 0.8}, "layers": []}
    saved = json.loads((live["out"] / "a.inkdeus.json").read_text(encoding="utf-8"))
    assert saved["regions"]["hair"] == hair
    # 「白」は、部分に当てると白抜き（線画だけ残す）、背景に当てると白
    assert ctl("preset", "白", "--region", "skin", "--name", "a.png")["style"] == "blank"
    assert ctl("preset", "白", "--region", "background", "--name", "a.png")["style"] == "white"
    params = ctl("get", "--name", "a.png")["params"]
    assert params["regions"]["skin"]["linked"] is False and params["regions"]["background"]["mode"] == "white"
    # まだ設定の無い部分へ set / patch すると、人物の設定を写した項目ができる
    ctl("set", "regions.clothes.levels.brightness", "0.3", "--name", "a.png")
    clothes = ctl("get", "--name", "a.png")["params"]["regions"]["clothes"]
    assert clothes["linked"] is True and clothes["levels"]["brightness"] == 0.3
    assert no_ids(clothes["layers"]) == no_ids(params["regions"]["person"]["layers"])
    ctl("patch", json.dumps({"regions": {"clothes": {"linked": False, "layers": [{"type": "hatch", "angle": 20}]}}}),
        "--name", "a.png")
    clothes = ctl("get", "--name", "a.png")["params"]["regions"]["clothes"]
    assert clothes["linked"] is False and [l["angle"] for l in clothes["layers"]] == [20]
    # 部分マスクの置き場に無い部分名（打ち間違い）は通さない。何も変わらない
    before = (live["out"] / "a.inkdeus.json").read_text(encoding="utf-8")
    for args in (("preset", "ベタ", "--region", "hiar"), ("set", "regions.hiar.solid.threshold", "1"),
                 ("patch", json.dumps({"regions": {"hiar": {"linked": False}}})),
                 ("set", "regions.hair.solid.lineKnockout", "3"), ("set", "regions.hair.mode", '"white"')):
        err = ctl(*args, "--name", "a.png", expect=1)
        assert err["problems"], args
    assert "hair" in ctl("preset", "ベタ", "--region", "hiar", "--name", "a.png", expect=1)["problems"][0]
    assert (live["out"] / "a.inkdeus.json").read_text(encoding="utf-8") == before
    # その画像にマスクが無い部分でも、置き場にある部分名なら設定できる（別の画像へ引き継いだときのため）
    assert ctl("preset", "ベタ", "--region", "skin", "--name", "c.png")["region"] == "skin"
    styles = {s["name"] for s in ctl("presets")["styles"]}
    assert {"solid-black", "blank", "white", "hatch1"} <= styles
    assert ctl("folders")["folders"][0]["partNames"] == ["clothes", "hair", "skin"]


def test_paint_targets_accept_part_solids(env):
    project, _ = create_project("book", env["images"], env["masks"], root=env["projects"], parts=env["parts"])
    client = app_for(project).test_client()
    buf = io.BytesIO()
    Image.new("L", (4, 4), 200).save(buf, "PNG")
    client.put("/api/params/a.png", data="{}", content_type="application/json")
    for target in ("solid-hair", "solid-skin", "solid-person"):
        assert client.put(f"/api/paint/a.png/{target}", data=buf.getvalue()).status_code == 200, target
    for target in ("solid-a b", "solid-eyes", "solid-all"):           # 部分マスクの置き場に無い部分のベタは受け付けない
        assert client.put(f"/api/paint/a.png/{target}", data=buf.getvalue()).status_code == 400, target
    assert [r["target"] for r in client.get("/api/paint/a.png").get_json()["targets"]] == [
        "solid-hair", "solid-person", "solid-skin"]
    # 外部からの変更で層が入れ替わっても、部分のベタの消しゴムは残る
    send(client, "POST", "/api/style/a.png", {"style": "ベタ", "region": "hair"})
    assert "solid-hair" in [r["target"] for r in client.get("/api/paint/a.png").get_json()["targets"]]
