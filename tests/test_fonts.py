"""ローカルフォント: 名前の読み取り・設定からの一覧・配信・CLI（フォントはテスト内で合成する）"""

from __future__ import annotations

import json

import pytest

import synth
from inkdeus import cli, fonts
from inkdeus.projects import create_project
from inkdeus.server import create_app

pytest.importorskip("fontTools")
from synth_font import make_font  # noqa: E402


@pytest.fixture()
def env(tmp_path):
    folder = tmp_path / "fonts"
    make_font(folder / "InkSerif-Regular.ttf", "Ink Serif", "Regular", japanese="インク明朝")
    make_font(folder / "InkSerif-Heavy.ttf", "Ink Serif", "Heavy", japanese="インク明朝")
    make_font(folder / "Plain.ttf", "Plain Sans")
    (folder / "broken.otf").write_bytes(b"this is not a font")
    (folder / "notes.txt").write_text("not a font", encoding="utf-8")
    images = tmp_path / "images"
    synth.save(images / "a.png", synth.flat(16, 16, 200))
    local = tmp_path / "inkdeus.local.toml"
    return {"tmp": tmp_path, "folder": folder, "images": images, "local": local}


def write(env, text: str) -> None:
    env["local"].write_text(text.replace("DIR", json.dumps(str(env["folder"]))), encoding="utf-8")


def client_for(env, **kw):
    return create_app(env["images"], local_config=env["local"], cache_root=env["tmp"] / "cache", watch=False,
                      **kw).test_client()


def test_read_names_prefers_japanese(env):
    assert fonts.read_names(env["folder"] / "InkSerif-Heavy.ttf") == {"family": "インク明朝", "subfamily": "Heavy"}
    assert fonts.read_names(env["folder"] / "Plain.ttf") == {"family": "Plain Sans", "subfamily": "Regular"}
    assert fonts.read_names(env["folder"] / "broken.otf") == {}
    assert fonts.read_names(env["folder"] / "nowhere.ttf") == {}
    assert fonts.clean_name('  A "B":  C&D\n') == "A B C D"


def test_resolve_names_globs_and_overrides(env):
    write(env, "[fonts]\ndirs = [DIR]\n")
    found, problems = fonts.resolve([env["local"]])
    by_file = {f.path.name: f for f in found}
    assert problems == [] and set(by_file) == {"InkSerif-Regular.ttf", "InkSerif-Heavy.ttf", "Plain.ttf", "broken.otf"}
    assert by_file["InkSerif-Regular.ttf"].name == "インク明朝 Regular"          # 同名はサブファミリ名で区別する
    assert by_file["InkSerif-Heavy.ttf"].name == "インク明朝 Heavy"
    assert by_file["Plain.ttf"].name == "Plain Sans" and by_file["Plain.ttf"].format == "truetype"
    assert by_file["broken.otf"].name == "broken" and by_file["broken.otf"].format == "opentype"   # 読めない物はファイル名
    assert len({f.id for f in found}) == 4 and all(f.id.isalnum() and len(f.id) == 16 for f in found)
    # include（glob）と、表示名の上書き
    write(env, '[fonts]\ndirs = [DIR, "no-such-folder"]\ninclude = ["inkserif-h*.ttf", "Plain.ttf", "Missing*.otf"]\n'
               '[fonts.names]\n"Plain.ttf" = "ぷれーん"\n')
    found, problems = fonts.resolve([env["local"]])
    assert [(f.path.name, f.name) for f in found] == [("InkSerif-Heavy.ttf", "インク明朝"), ("Plain.ttf", "ぷれーん")]
    assert len(problems) == 2 and any("no-such-folder" in p for p in problems) and any("Missing*.otf" in p for p in problems)
    assert fonts.resolve([None, env["tmp"] / "nope.toml"]) == ([], [])             # 設定が無ければ何も無い（エラーにしない）
    css = fonts.css_for(found)
    assert css.count("@font-face") == 2 and 'font-family:"インク明朝"' in css and f"/api/fonts/file/{found[0].id}" in css


def test_api_lists_and_serves_fonts(env):
    client = client_for(env)
    assert client.get("/api/fonts").get_json() == []                               # 設定なし
    assert client.get("/api/fonts/css").get_data(as_text=True) == ""
    write(env, '[fonts]\ndirs = [DIR]\ninclude = ["InkSerif-*.ttf"]\n')
    rows = client.get("/api/fonts").get_json()                                     # 稼働中に書き足しても反映される
    assert [r["name"] for r in rows] == ["インク明朝 Heavy", "インク明朝 Regular"]
    heavy = rows[0]
    assert heavy["file"] == "InkSerif-Heavy.ttf" and heavy["format"] == "truetype"
    assert heavy["size"] == (env["folder"] / "InkSerif-Heavy.ttf").stat().st_size
    assert heavy["url"] == f"/api/fonts/file/{heavy['id']}" and heavy["css"] == "/api/fonts/css?family=インク明朝 Heavy"
    r = client.get(heavy["url"])
    assert r.status_code == 200 and r.mimetype == "font/ttf"
    assert r.data == (env["folder"] / "InkSerif-Heavy.ttf").read_bytes()
    assert r.headers["Cache-Control"] == "public, max-age=86400" and r.headers.get("ETag")
    assert client.get(heavy["url"], headers={"If-None-Match": r.headers["ETag"]}).status_code == 304
    # 一覧に無い物は受け付けない
    plain_id = fonts.resolve([env["local"]])[0][0].id[::-1]
    for bad in (f"/api/fonts/file/{plain_id}", "/api/fonts/file/Plain.ttf", "/api/fonts/file/../fonts/Plain.ttf",
                "/api/fonts/file/%2e%2e%2fPlain.ttf", "/api/fonts/file/"):
        assert client.get(bad).status_code in (301, 308, 404), bad
    css = client.get("/api/fonts/css", query_string={"family": "インク明朝 Heavy"})
    assert css.status_code == 200 and css.mimetype == "text/css" and css.get_data(as_text=True).count("@font-face") == 1
    assert heavy["url"] in css.get_data(as_text=True) and css.headers["Cache-Control"] == "no-cache"
    assert client.get("/api/fonts/css", query_string={"family": "Plain Sans"}).status_code == 404
    assert client.get("/api/fonts/css").get_data(as_text=True).count("@font-face") == 2


def test_project_fonts_are_merged_and_survive_config_rewrites(env, capsys, monkeypatch):
    monkeypatch.setenv("INKDEUS_PROJECTS", str(env["tmp"] / "projects"))
    write(env, '[fonts]\ndirs = [DIR]\ninclude = ["Plain.ttf"]\n')
    project, _ = create_project("p1", env["images"], root=env["tmp"] / "projects")
    with open(project.config, "a", encoding="utf-8") as f:
        f.write(f'\n[fonts]\ndirs = [{json.dumps(str(env["folder"]))}]\ninclude = ["InkSerif-Heavy.ttf", "Plain.ttf"]\n'
                '[fonts.names]\n"InkSerif-Heavy.ttf" = "極太"\n')
    client = client_for(env, project=project.info())
    assert [r["name"] for r in client.get("/api/fonts").get_json()] == ["Plain Sans", "極太"]   # 共通 + プロジェクト（重複は1つ）
    # 素材ソースの登録や --force で設定ファイルを書き直しても、[fonts] は残る
    assert cli.main(["assets", "add", "p1", "--name", "x", "--path", str(env["folder"])]) == 0
    assert cli.main(["new", "p1", "--images", str(env["images"]), "--force"]) == 0
    capsys.readouterr()
    assert [r["name"] for r in client.get("/api/fonts").get_json()] == ["Plain Sans", "極太"]
    assert "[[assets]]" in project.config.read_text(encoding="utf-8")


def test_cli_fonts_list(env, capsys, monkeypatch):
    monkeypatch.setattr(cli, "default_local_config", lambda: env["local"])
    assert cli.main(["fonts", "list"]) == 0
    assert "登録されていません" in capsys.readouterr().out
    write(env, '[fonts]\ndirs = [DIR, "no-such-folder"]\ninclude = ["Plain.ttf", "Nope*.ttf"]\n')
    assert cli.main(["fonts", "list"]) == 0
    out = capsys.readouterr().out
    assert "Plain Sans" in out and "Plain.ttf" in out and out.count("注意:") == 2
    assert cli.main(["fonts", "list", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [f["name"] for f in data["fonts"]] == ["Plain Sans"] and len(data["problems"]) == 2
