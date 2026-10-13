"""プロジェクト（new / projects / open）。置き場は一時ディレクトリを環境変数で指定する"""

from __future__ import annotations

import json
import tomllib

import pytest

import synth
from inkdeus import cli
from inkdeus.projects import PROJECT_FILE, ProjectError, load_project, projects_root
from inkdeus.server import create_app


@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("INKDEUS_PROJECTS", str(root))
    images, masks = tmp_path / "ext" / "images", tmp_path / "ext" / "masks"
    img, mask = synth.scene(64, 48)
    synth.save(images / "a.png", img)
    synth.save(images / "b.png", synth.flat(32, 16, 200))
    synth.save(masks / "a.png", mask)
    (images / "memo.txt").write_text("x", encoding="utf-8")
    return {"root": root, "images": images, "masks": masks, "tmp": tmp_path}


def run(capsys, *args):
    capsys.readouterr()
    code = cli.main(list(args))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_projects_root_sources(tmp_path, env):
    assert projects_root() == env["root"]
    cfg = tmp_path / "inkdeus.local.toml"
    cfg.write_text(f'projects_root = {json.dumps(str(tmp_path / "elsewhere"))}\n', encoding="utf-8")
    assert projects_root({}, cfg) == tmp_path / "elsewhere"
    assert projects_root({"INKDEUS_PROJECTS": str(tmp_path / "env")}, cfg) == tmp_path / "env"
    assert projects_root({}, tmp_path / "missing.toml").name == "inkdeus-projects"


def test_new_list_open_by_reference(env, capsys):
    code, out, _ = run(capsys, "projects")
    assert code == 0 and "まだありません" in out
    code, out, _ = run(capsys, "new", "夏の本", "--images", str(env["images"]), "--masks", str(env["masks"]),
                       "--note", "テスト用")
    assert code == 0 and "1 / 2 枚" in out and "inkdeus open 夏の本" in out
    pdir = env["root"] / "夏の本"
    with open(pdir / PROJECT_FILE, "rb") as f:
        cfg = tomllib.load(f)
    assert cfg["name"] == "夏の本" and cfg["source"] == str(env["images"]) and cfg["masks"] == str(env["masks"])
    assert cfg["out"] == "out" and cfg["note"] == "テスト用" and cfg["created"]
    assert not (pdir / "source").exists()             # 参照するだけでコピーしない

    code, out, _ = run(capsys, "projects", "--json")
    data = json.loads(out)
    assert data["root"] == str(env["root"])
    row = data["projects"][0]
    assert (row["name"], row["images"], row["masks"], row["configured"], row["exported"]) == ("夏の本", 2, 1, 0, 0)

    # open と同じ組み立てでサーバを作る（起動はしない）
    project = load_project("夏の本")
    assert project.source == env["images"] and project.out == pdir / "out"
    app = create_app(project.source, project.masks, project.out, project=project.info())
    client = app.test_client()
    names = {i["name"]: i for i in client.get("/api/images").get_json()["images"]}
    assert set(names) == {"a.png", "b.png"} and names["a.png"]["hasMask"] and not names["b.png"]["hasMask"]
    state = client.get("/api/state").get_json()             # ctl state が返す内容
    assert state["project"] == {"name": "夏の本", "path": str(pdir)}
    assert state["source"] == str(env["images"]) and state["masks"] == str(env["masks"])
    assert state["out"] == str(pdir / "out")
    assert client.get("/api/config").get_json()["project"]["name"] == "夏の本"
    # 設定と書き出しはプロジェクトの out/ に入り、一覧の枚数に出る
    client.patch("/api/params/a.png", data=b"{}")
    client.post("/api/export/a.png", data=(env["images"] / "b.png").read_bytes())
    assert (pdir / "out" / "a.inkdeus.json").is_file() and (pdir / "out" / "a.png").is_file()
    row = json.loads(run(capsys, "projects", "--json")[1])["projects"][0]
    assert (row["configured"], row["exported"]) == (1, 1) and row["updated"]
    assert "設定済み 1" in run(capsys, "projects")[1]
    # パスでも開ける
    assert load_project(str(pdir)).name == "夏の本"


def test_new_with_copy(env, capsys):
    code, out, _ = run(capsys, "new", "copied", "--images", str(env["images"]), "--masks", str(env["masks"]), "--copy")
    assert code == 0 and "コピー済み" in out
    pdir = env["root"] / "copied"
    assert sorted(p.name for p in (pdir / "source").iterdir()) == ["a.png", "b.png"]
    assert [p.name for p in (pdir / "masks").iterdir()] == ["a.png"]
    project = load_project("copied")
    assert project.source == pdir / "source" and project.masks == pdir / "masks"
    # 元のフォルダが消えても開ける
    for p in env["images"].iterdir():
        p.unlink()
    assert len(create_app(project.source, project.masks, project.out).test_client()
               .get("/api/images").get_json()["images"]) == 2


@pytest.mark.parametrize("name", ["../evil", "a/b", "a\\b", "..", ".", "", " pad", ".hidden", "x:y", "q?"])
def test_bad_names_are_rejected(env, capsys, name):
    code, _, err = run(capsys, "new", name, "--images", str(env["images"]))
    assert code == 1 and "プロジェクト名として使えません" in err
    assert not env["root"].exists() or list(env["root"].iterdir()) == []
    assert not (env["tmp"] / "evil").exists()


def test_duplicate_and_force(env, capsys):
    assert run(capsys, "new", "p1", "--images", str(env["images"]))[0] == 0
    created = load_project("p1").created
    code, _, err = run(capsys, "new", "p1", "--images", str(env["images"]))
    assert code == 1 and "既にあります" in err and "--force" in err
    other = env["tmp"] / "ext2"
    synth.save(other / "c.png", synth.flat(8, 8, 0))
    (env["root"] / "p1" / "out" / "keep.txt").write_text("keep", encoding="utf-8")
    code, out, _ = run(capsys, "new", "p1", "--images", str(other), "--force", "--note", "更新")
    assert code == 0
    project = load_project("p1")
    assert project.source == other and project.masks is None and project.note == "更新"
    assert project.created == created                         # 作成日時は引き継ぐ
    assert (env["root"] / "p1" / "out" / "keep.txt").is_file()  # 出力には触らない


def test_new_requires_images(env, capsys):
    empty = env["tmp"] / "empty"
    empty.mkdir()
    code, _, err = run(capsys, "new", "p", "--images", str(empty))
    assert code == 1 and "画像が1枚もありません" in err
    code, _, err = run(capsys, "new", "p", "--images", str(env["tmp"] / "nowhere"))
    assert code == 1 and "画像フォルダが見つかりません" in err
    code, _, err = run(capsys, "new", "p", "--images", str(env["images"]), "--masks", str(env["tmp"] / "nomask"))
    assert code == 1 and "マスクフォルダが見つかりません" in err
    assert not (env["root"] / "p").exists()


def test_open_reports_missing_or_empty_source(env, capsys, monkeypatch):
    assert run(capsys, "new", "gone", "--images", str(env["images"]), "--masks", str(env["masks"]))[0] == 0
    for p in env["images"].iterdir():
        p.unlink()
    code, _, err = run(capsys, "open", "gone")
    assert code == 1 and "画像がありません" in err
    env["images"].rmdir()
    # 元画像の場所が見つからなくても起動はする（画面に、付け替え方の案内が出る。test_relink.py）
    started = {}
    monkeypatch.setattr(cli, "serve", lambda app, host, port, project=None: started.update(app=app) or 0)
    code, out, err = run(capsys, "open", "gone")
    assert code == 0 and "元画像の場所が見つかりません" in err and str(env["images"]) in err and "inkdeus relink gone" in err
    assert started["app"].config["INKDEUS_LIBRARY"].missing is True
    assert "元画像フォルダが見つかりません" in run(capsys, "projects")[1]
    code, _, err = run(capsys, "open", "no-such-project")
    assert code == 1 and "プロジェクトが見つかりません" in err and "gone" in err
    with pytest.raises(ProjectError):
        load_project(str(env["tmp"] / "not" / "a" / "project"))


def test_direct_run_shorthand(env, capsys, monkeypatch):
    """inkdeus <path> は、path が実在するときだけ run の省略形"""
    started = {}

    def fake_serve(app, host, port, project=None):
        started.update(lib=app.config["INKDEUS_LIBRARY"], port=port, project=project)
        return 0

    monkeypatch.setattr(cli, "serve", fake_serve)
    assert cli.main([str(env["images"]), "--mask-dir", str(env["masks"]), "--port", "9000"]) == 0
    assert started["lib"].folder == env["images"] and started["port"] == 9000 and started["project"] is None
    assert cli.main(["run", str(env["images"] / "a.png")]) == 0
    assert started["lib"].single == "a.png"
    assert run(capsys, "new", "viaopen", "--images", str(env["images"]))[0] == 0
    assert cli.main(["open", "viaopen", "--port", "9100"]) == 0
    assert started["project"].name == "viaopen" and started["lib"].out_dir == env["root"] / "viaopen" / "out"
    with pytest.raises(SystemExit):                            # 実在しないパスはサブコマンド扱いでエラー
        cli.main(["no-such-path-xyz"])
