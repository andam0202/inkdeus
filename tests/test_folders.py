"""複数の画像フォルダ（画像セット）: 設定の読み書き・保存先・API・素材ソース・CLI・ctl"""

from __future__ import annotations

import io
import json
import tomllib

import pytest
from PIL import Image

import synth
from conftest import serve
from inkdeus import cli
from inkdeus import folders as folders_mod
from inkdeus.projects import (ProjectError, add_asset_source, add_folder, create_project, folder_rows, load_project,
                              remove_folder, summarize)
from inkdeus.server import create_app
from test_ctl import FakeBrowser


def png_bytes(value: int = 128) -> bytes:
    buf = io.BytesIO()
    Image.new("L", (4, 4), value).save(buf, "PNG")
    return buf.getvalue()


def make_dirs(root):
    """既定のフォルダ（a, b）と、同じ名前の画像を持つ別のフォルダ（a, c）"""
    synth.save(root / "one" / "a.png", synth.flat(32, 16, 200))
    synth.save(root / "one" / "b.png", synth.flat(32, 16, 100))
    synth.save(root / "two" / "a.png", synth.flat(48, 24, 50))
    synth.save(root / "two" / "c.png", synth.flat(48, 24, 150))
    synth.save(root / "two_masks" / "a.png", synth.flat(48, 24, 255))
    return root / "one", root / "two", root / "two_masks"


@pytest.fixture()
def env(tmp_path):
    one, two, masks = make_dirs(tmp_path)
    project, _ = create_project("book", one, root=tmp_path / "projects")
    add_folder(project, "二巻", two, masks, note="続き")
    project = load_project(str(project.path), root=tmp_path / "projects")
    app = create_app(project.source, project.masks, project.out, project=project.info(),
                     local_config=tmp_path / "none.toml", watch=False)
    return {"app": app, "client": app.test_client(), "project": project, "root": tmp_path,
            "session": app.config["INKDEUS_SESSION"], "two": two}


def send(client, method, path, body=None):
    return client.open(path, method=method, data=None if body is None else json.dumps(body),
                       content_type="application/json")


# ---- 設定ファイル ----

def test_config_roundtrip_and_backward_compatibility(tmp_path):
    one, two, masks = make_dirs(tmp_path)
    project, _ = create_project("book", one, root=tmp_path / "projects")
    assert project.folders == () and "folders" not in tomllib.loads(project.config.read_text(encoding="utf-8"))
    assert [r["name"] for r in folder_rows(project)] == ["default"]              # 既存の source / masks が既定のフォルダ
    entry = add_folder(project, "二巻", two, masks, note="続き")
    assert entry == {"name": "二巻", "images": str(two), "masks": str(masks), "note": "続き"}
    add_asset_source(project, "sfx", tmp_path / "one")                           # ほかの項目を書き直しても残る
    project = load_project(str(project.path), root=tmp_path / "projects")
    assert [(f.name, f.images, f.masks, f.note) for f in project.folders] == [("二巻", two, masks, "続き")]
    project, _ = create_project("book", one, root=tmp_path / "projects", force=True)
    assert [f.name for f in project.folders] == ["二巻"]                         # --force での更新でも残る
    # 名前の検証・重複・予約
    for bad in ("default", "a b", "a/b", "a@b", "", "x" * 33, ".."):
        with pytest.raises(ProjectError):
            add_folder(project, bad, two)
    with pytest.raises(ProjectError, match="既にあります"):
        add_folder(project, "二巻", two)
    with pytest.raises(ProjectError, match="見つかりません"):
        add_folder(project, "三巻", tmp_path / "nope")
    with pytest.raises(ProjectError):
        remove_folder(project, "nope")
    # 手で書いた設定の、使えない項目は飛ばす
    found, problems = folders_mod.parse_entries(
        [{"name": "ok", "images": "x"}, {"name": "default", "images": "x"}, {"name": "ok", "images": "y"},
         {"name": "noimg"}, "bad"], tmp_path)
    assert [f.name for f in found] == ["ok"] and found[0].images == (tmp_path / "x").resolve() and len(problems) == 4
    assert folders_mod.out_dir(tmp_path, "default") == tmp_path and folders_mod.out_dir(tmp_path, "二巻") == tmp_path / "@二巻"


def test_counts_and_cli(env, tmp_path, capsys, monkeypatch):
    project, client = env["project"], env["client"]
    client.put("/api/params/a.png?folder=二巻", data="{}", content_type="application/json")
    client.post("/api/export/a.png", data=png_bytes())
    rows = {r["name"]: r for r in folder_rows(project)}
    assert (rows["default"]["images"], rows["default"]["exported"], rows["default"]["configured"]) == (2, 1, 0)
    assert (rows["二巻"]["images"], rows["二巻"]["masks"], rows["二巻"]["configured"], rows["二巻"]["exported"]) == (2, 1, 1, 0)
    total = summarize(project)
    assert (total["images"], total["masks"], total["configured"], total["exported"]) == (4, 1, 1, 1)
    assert [f["name"] for f in total["folders"]] == ["default", "二巻"]
    monkeypatch.setenv("INKDEUS_PROJECTS", str(tmp_path / "projects"))
    assert cli.main(["folders", "list", "book", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)["folders"]
    assert [(f["name"], f["images"]) for f in listed] == [("default", 2), ("二巻", 2)]
    assert cli.main(["projects"]) == 0
    text = capsys.readouterr().out
    assert "画像 4 /" in text and "二巻: 画像 2 /" in text and "既定: 画像 2 /" in text
    assert cli.main(["folders", "add", "book", "--name", "三巻", "--images", str(env["two"])]) == 0
    assert cli.main(["folders", "add", "book", "--name", "三巻", "--images", str(env["two"])]) == 1
    assert cli.main(["folders", "add", "book", "--name", "default", "--images", str(env["two"])]) == 1
    capsys.readouterr()
    # 登録を外すだけなら保存先は残る。--delete-out で消す
    kept = project.out / "@二巻"
    assert kept.is_dir()
    assert cli.main(["folders", "remove", "book", "--name", "二巻"]) == 0 and kept.is_dir()
    assert "残っています" in capsys.readouterr().out
    add_folder(project, "二巻", env["two"])
    assert cli.main(["folders", "remove", "book", "--name", "二巻", "--delete-out"]) == 0 and not kept.exists()
    assert (project.out / "a.png").is_file()                                     # 既定のフォルダの物は消えない
    assert [f.name for f in load_project(str(project.path), root=tmp_path / "projects").folders] == ["三巻"]


# ---- API ----

def test_same_named_images_do_not_mix(env):
    client, out, session = env["client"], env["project"].out, env["session"]
    rows = client.get("/api/folders").get_json()
    assert [(f["name"], f["label"], f["counts"]["images"]) for f in rows["folders"]] == [("default", "既定", 2), ("二巻", "二巻", 2)]
    assert rows["folders"][1]["note"] == "続き" and rows["folders"][1]["out"] == str(out / "@二巻") and rows["current"] == "default"
    assert [i["name"] for i in client.get("/api/images").get_json()["images"]] == ["a.png", "b.png"]
    listing = client.get("/api/images?folder=二巻").get_json()
    assert [i["name"] for i in listing["images"]] == ["a.png", "c.png"] and listing["folder"] == "二巻"
    assert [i["hasMask"] for i in listing["images"]] == [True, False] and listing["images"][0]["width"] == 48
    assert client.get("/api/image/a.png").data != client.get("/api/image/a.png?folder=二巻").data
    assert client.get("/api/mask/a.png?folder=二巻").status_code == 200 and client.get("/api/mask/a.png").status_code == 404
    q = session.hub.subscribe()
    client.put("/api/params/a.png", data='{"version": 2, "unit": 2}', content_type="application/json")
    r = send(client, "POST", "/api/set/a.png?folder=二巻", {"key": "unit", "value": 3}).get_json()
    events = [q.get_nowait(), q.get_nowait()]
    assert [(e["type"], e["folder"], e["name"]) for e in events] == [("params", "default", "a.png"), ("params", "二巻", "a.png")]
    assert events[1]["rev"] == r["rev"]
    # 保存先: 既定は <out>/ 直下、追加のフォルダは <out>/@<名前>/
    assert json.loads((out / "a.inkdeus.json").read_text(encoding="utf-8"))["unit"] == 2
    assert json.loads((out / "@二巻" / "a.inkdeus.json").read_text(encoding="utf-8"))["unit"] == 3
    client.post("/api/export/a.png?folder=二巻", data=png_bytes(9), headers={"X-Inkdeus-Rev": "r"})
    ev = q.get_nowait()
    assert (ev["type"], ev["folder"], ev["file"], ev["source"]) == ("exported", "二巻", "a.png", "inked@二巻")
    assert (out / "@二巻" / "a.png").read_bytes() == png_bytes(9) and not (out / "a.png").exists()
    assert client.get("/api/exported/a.png?folder=二巻").get_json()["seq"] == 1
    assert client.get("/api/exported/a.png").get_json() == {"seq": 0}
    # バージョンと統計もフォルダごと
    r = send(client, "POST", "/api/versions/a.png?folder=二巻", {"version": "v"}).get_json()
    assert r["folder"] == "二巻" and (out / "@二巻" / "a@v.inkdeus.json").is_file()
    assert q.get_nowait()["folder"] == "二巻" and q.get_nowait() == {
        "type": "versions", "action": "create", "folder": "二巻", "name": "a.png", "version": "v", "file": "a@v.png",
        "copyFrom": ""}
    assert [v["name"] for v in client.get("/api/versions/a.png").get_json()["versions"]] == [""]
    assert client.get("/api/params/a.png?version=v").status_code == 404
    rev = client.get("/api/params/a.png?folder=二巻").headers["X-Inkdeus-Rev"]
    send(client, "POST", "/api/stats/a.png?folder=二巻", {"rev": rev})
    assert client.get("/api/stats/a.png?folder=二巻").get_json()["fresh"] is True
    assert client.get("/api/stats/a.png").get_json()["stats"] is None
    # 既定のフォルダの一覧に、追加フォルダの保存先（@二巻/）は混ざらない
    assert [i["path"] for i in client.get("/api/assets/inked").get_json()["items"]] == []
    # 無いフォルダ・フォルダに無い画像
    for path in ("/api/images?folder=nope", "/api/params/a.png?folder=nope", "/api/image/c.png",
                 "/api/image/b.png?folder=二巻", "/api/images?folder=..%2Fx"):
        assert client.get(path).status_code == 404, path
    # 外部からの書き換えの監視もフォルダごと
    assert session.scan() == []
    reg = env["app"].config["INKDEUS_FOLDERS"]
    two = reg.pick("二巻")[1]
    two.scan()
    (out / "@二巻" / "a.inkdeus.json").write_text('{"version": 2, "unit": 4}', encoding="utf-8")
    assert [(e["folder"], e["name"]) for e in two.scan()] == [("二巻", "a.png")] and session.scan() == []


def test_state_and_commands_carry_the_folder(env):
    client, session = env["client"], env["session"]
    s = send(client, "POST", "/api/state", {"current": "c.png", "folder": "二巻"}).get_json()
    assert (s["current"], s["currentFolder"], s["folders"]) == ("c.png", "二巻", ["default", "二巻"])
    assert send(client, "POST", "/api/state", {"current": "c.png"}).status_code == 404        # 既定のフォルダには無い
    assert send(client, "POST", "/api/state", {"current": "a.png", "folder": "nope"}).status_code == 404
    q = session.hub.subscribe()
    r = send(client, "POST", "/api/command", {"type": "open", "name": "a.png", "folder": "二巻"}).get_json()
    assert r["folder"] == "二巻" and q.get_nowait() == {"type": "open", "folder": "二巻", "name": "a.png", "version": None}
    # フォルダを省くと、いまブラウザで開いているフォルダ
    assert send(client, "POST", "/api/command", {"type": "open", "name": "c.png"}).get_json()["folder"] == "二巻"
    assert send(client, "POST", "/api/command", {"type": "open", "name": "b.png"}).status_code == 404
    assert send(client, "POST", "/api/command", {"type": "open", "name": "b.png", "folder": "default"}).status_code == 200
    r = send(client, "POST", "/api/command", {"type": "export-all", "allFolders": True}).get_json()
    assert r["allFolders"] is True


def test_asset_sources_per_folder_and_old_urls(env):
    client, out = env["client"], env["project"].out
    for query in ("", "?folder=二巻"):
        client.put("/api/params/a.png" + query, data="{}", content_type="application/json")
        client.post("/api/export/a.png" + query, data=png_bytes(1 if query else 2))
    rows = {s["name"]: s for s in client.get("/api/assets").get_json()["sources"]}
    assert list(rows) == ["source", "inked", "source@二巻", "inked@二巻"]
    assert (rows["source"]["label"], rows["source"]["folder"], rows["source"]["kind"]) == ("元画像", "default", "source")
    assert (rows["inked@二巻"]["label"], rows["inked@二巻"]["folder"], rows["inked@二巻"]["kind"]) == ("白黒（二巻）", "二巻", "inked")
    assert rows["source@二巻"]["count"] == 2 and rows["inked@二巻"]["count"] == 1
    # これまでの URL（既定のフォルダ）はそのまま
    assert client.get("/api/assets/inked/image/a.png").data == png_bytes(2)
    assert client.get("/api/assets/source/image/b.png").status_code == 200
    assert client.get("/api/assets/inked@二巻/image/a.png").data == png_bytes(1)
    assert client.get("/api/assets/source@二巻/image/c.png").status_code == 200
    assert client.get("/api/assets/source/image/c.png").status_code == 404
    assert client.get("/api/assets/source@二巻/thumb/a.png?h=16").status_code == 200
    listing = client.get("/api/assets/source@二巻").get_json()
    assert listing["folder"] == "二巻" and listing["kind"] == "source"
    assert [(i["path"], bool(i["inked"])) for i in listing["items"]] == [("a.png", True), ("c.png", False)]
    assert client.get("/api/assets/inked@二巻").get_json()["items"][0]["base"] == "a.png"
    # 元画像と白黒版の対応は、フォルダの中で引く
    pair = client.get("/api/assets/inked@二巻/pair/a.png").get_json()
    assert (pair["name"], pair["folder"], pair["sourceName"], pair["inkedName"]) == ("a.png", "二巻", "source@二巻", "inked@二巻")
    pair = client.get("/api/assets/source/pair/a.png").get_json()
    assert (pair["folder"], pair["sourceName"], pair["inkedName"]) == ("default", "source", "inked")
    assert client.get("/api/assets/source/pair/c.png").status_code == 404
    assert client.get("/api/assets/inked@二巻/versions").get_json()["versions"].keys() == {"a.png"}
    assert client.get("/api/assets/inked@nope").status_code == 404
    # 消されたバージョンの代わりも、そのフォルダの既定
    assert client.get("/api/assets/inked@二巻/image/a@gone.png").data == png_bytes(1)
    assert client.get("/api/assets/inked/image/a@gone.png").data == png_bytes(2)


def test_folders_added_while_running_are_picked_up(env, tmp_path):
    client, project = env["client"], env["project"]
    synth.save(tmp_path / "three" / "z.png", synth.flat(16, 16, 10))
    assert client.get("/api/images?folder=三巻").status_code == 404
    add_folder(project, "三巻", tmp_path / "three")
    assert [f["name"] for f in client.get("/api/folders").get_json()["folders"]] == ["default", "二巻", "三巻"]
    assert [i["name"] for i in client.get("/api/images?folder=三巻").get_json()["images"]] == ["z.png"]
    assert "source@三巻" in [s["name"] for s in client.get("/api/assets").get_json()["sources"]]
    remove_folder(project, "二巻")
    assert client.get("/api/images?folder=二巻").status_code == 404
    # 参照先が無くなったフォルダは、一覧には「見つからない」として出る
    add_folder(project, "四巻", tmp_path / "three")
    project.config.write_text(project.config.read_text(encoding="utf-8").replace(
        f'name = "四巻"\nimages = {json.dumps(str(tmp_path / "three"))}', 'name = "四巻"\nimages = "nowhere"'), encoding="utf-8")
    rows = {f["name"]: f for f in client.get("/api/folders").get_json()["folders"]}
    assert rows["四巻"]["missing"] is True and rows["三巻"]["missing"] is False
    assert client.get("/api/images?folder=四巻").status_code == 404


def test_run_with_added_folders(tmp_path):
    one, two, masks = make_dirs(tmp_path)
    args = cli.build_parser().parse_args(["run", str(one), "--add", f"second={two},{masks}", "--add", f"third={two}"])
    defs = [folders_mod.parse_add(spec) for spec in args.add]
    assert [(d.name, d.images, d.masks) for d in defs] == [("second", two, masks), ("third", two, None)]
    client = create_app(one, folders=defs, watch=False).test_client()
    assert [f["name"] for f in client.get("/api/folders").get_json()["folders"]] == ["default", "second", "third"]
    client.put("/api/params/a.png?folder=second", data="{}", content_type="application/json")
    assert (one / "inkdeus_out" / "@second" / "a.inkdeus.json").is_file()
    for bad in ("x", "=a", "default=" + str(two), "a b=" + str(two)):
        with pytest.raises(folders_mod.FolderError):
            folders_mod.parse_add(bad)
    assert cli.main(["run", str(one), "--add", "nope=" + str(tmp_path / "missing")]) == 1
    assert cli.main(["run", str(one), "--add", f"s={two}", "--add", f"s={two}"]) == 1


# ---- ctl ----

@pytest.fixture()
def ctl_env(env, capsys):
    server, url = serve(env["app"])
    browser = FakeBrowser(url, "a.png")
    browser.start()
    assert browser.ready.wait(10)

    def run(*args, expect=0):
        capsys.readouterr()
        code = cli.main(["ctl", "--port", str(server.server_port), *args])
        captured = capsys.readouterr()
        assert code == expect, captured.err or captured.out
        return json.loads(captured.out if code == 0 else captured.err)

    yield run
    browser.close()
    server.shutdown()


def test_ctl_folder_option(env, ctl_env):
    ctl, out = ctl_env, env["project"].out
    assert [f["name"] for f in ctl("folders")["folders"]] == ["default", "二巻"]
    assert ctl("state")["currentFolder"] == "default"
    ctl("set", "unit", "2")
    assert json.loads((out / "a.inkdeus.json").read_text(encoding="utf-8"))["unit"] == 2
    # 別のフォルダを指したときは、画像名が要る
    err = ctl("get", "--folder", "二巻", expect=1)
    assert "--name" in err["error"] and set(err["images"]) == {"a.png", "c.png"}
    ctl("set", "unit", "3", "--folder", "二巻", "--name", "a.png")
    assert json.loads((out / "@二巻" / "a.inkdeus.json").read_text(encoding="utf-8"))["unit"] == 3
    assert ctl("get")["params"]["unit"] == 2
    assert ctl("get", "--folder", "nope", "--name", "a.png", expect=1)["error"]
    assert "フォルダ" in ctl("stats", "--wait", "--folder", "二巻", "--name", "a.png", expect=2)["error"]
    # ブラウザをそのフォルダへ切り替えると、以降はそのフォルダが対象
    s = ctl("open", "c.png", "--folder", "二巻")
    assert (s["current"], s["currentFolder"]) == ("c.png", "二巻")
    ctl("patch", '{"unit": 4}')
    assert json.loads((out / "@二巻" / "c.inkdeus.json").read_text(encoding="utf-8"))["unit"] == 4
    assert ctl("stats", "--wait", "--timeout", "10")["folder"] == "二巻"
    r = ctl("export", "--timeout", "10")
    assert r["folder"] == "二巻" and r["path"] == str(out / "@二巻" / "c.png")
    assert ctl("open", "a.png")["currentFolder"] == "二巻"                       # フォルダを省くと、いまのフォルダの中で
    r = ctl("version", "new", "v")
    assert r["folder"] == "二巻" and (out / "@二巻" / "a@v.inkdeus.json").is_file()
    assert [v["name"] for v in ctl("versions")["versions"]] == ["", "v"]
    assert [v["name"] for v in ctl("versions", "--folder", "default", "--name", "a.png")["versions"]] == [""]
    assert ctl("open", "b.png", expect=1)["error"]                               # いまのフォルダには無い
    assert ctl("open", "b.png", "--folder", "default")["currentFolder"] == "default"
