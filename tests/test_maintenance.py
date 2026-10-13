"""データを守るためのコマンド: backup list / restore / prune、export-project / import-project、relink"""

from __future__ import annotations

import io
import json
import shutil
import struct
import time
import zipfile

import pytest
from PIL import Image

import synth
from inkdeus import cli
from inkdeus.projects import add_folder, load_project
from inkdeus.server import create_app


def png(value: int = 128, size=(8, 4)) -> bytes:
    buf = io.BytesIO()
    Image.new("L", size, value).save(buf, "PNG")
    return buf.getvalue()


def manga_data(text: bytes) -> bytes:
    head = json.dumps([{"name": "state.json", "size": len(text)}]).encode("utf-8")
    return struct.pack("<I", len(head)) + head + text


@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("INKDEUS_PROJECTS", str(root))
    monkeypatch.setenv("INKDEUS_CONFIG", str(tmp_path / "config"))
    # リポジトリ直下の inkdeus.local.toml（利用者の物）は読まない
    monkeypatch.setattr("inkdeus.server.default_local_config", lambda: tmp_path / "none.toml")
    images, masks, parts = tmp_path / "ext" / "images", tmp_path / "ext" / "masks", tmp_path / "ext" / "parts"
    img, mask = synth.scene(64, 48)
    synth.save(images / "a.png", img)
    synth.save(images / "b.png", synth.flat(32, 16, 200))
    synth.save(masks / "a.png", mask)
    synth.save(parts / "hair" / "a.png", (mask > 0).astype("uint8") * 255)
    assert cli.main(["new", "book", "--images", str(images), "--masks", str(masks), "--parts", str(parts)]) == 0
    return {"root": root, "images": images, "masks": masks, "parts": parts, "tmp": tmp_path}


def run(capsys, *args):
    capsys.readouterr()
    code = cli.main(list(args))
    out = capsys.readouterr()
    return code, out.out, out.err


def app_of(name: str, tmp):
    project = load_project(name, check=False)
    return create_app(project.source, project.masks, project.out, project=project.info(), watch=False,
                      parts_dir=project.parts, allow_missing=True, local_config=tmp / "none.toml",
                      config_dir=tmp / "config")


def work(client, strength: float, ink: int, name="a.png", query=""):
    """設定と消しゴムを 1 回ずつ保存する"""
    params = client.get(f"/api/params/{name}?resolve=1" + query.replace("?", "&")).get_json()["params"]
    params["lines"]["strength"] = strength
    assert client.put(f"/api/params/{name}{query}", data=json.dumps(params)).status_code == 200
    assert client.put(f"/api/paint/{name}/lines{query}", data=png(ink)).status_code == 200


def strength_of(path) -> float:
    return json.loads(path.read_bytes())["lines"]["strength"]


def test_backup_list_restore_and_prune(env, capsys):
    client = app_of("book", env["tmp"]).test_client()
    out = env["root"] / "book" / "out"
    for strength, ink in ((0.1, 10), (0.2, 20), (0.3, 30)):
        work(client, strength, ink)
        time.sleep(2.1)
    assert client.put("/api/manga/project/第1話", data=manga_data(b"one")).status_code == 200
    assert client.put("/api/manga/project/第1話", data=manga_data(b"two")).status_code == 200

    code, text, _ = run(capsys, "backup", "list", "book")
    assert code == 0 and "設定: a — 3 個" in text and "消しゴム・ペン: a / lines — 3 個" in text and "漫画データ: 第1話 — 2 個" in text
    listing = json.loads(run(capsys, "backup", "list", "book", "--json")[1])
    assert {(f["kind"], f["name"]) for f in listing["files"]} == {("params", "a"), ("paint", "a"), ("manga", "第1話")}
    assert json.loads(run(capsys, "backup", "list", "book", "--kind", "manga", "--json")[1])["files"][0]["count"] == 2

    # 画像ごとの時点の一覧（設定と消しゴム・ペンをまとめて）
    code, text, _ = run(capsys, "backup", "list", "book", "--name", "a.png")
    assert code == 0 and text.count("設定 / 消しゴム・ペン（lines）") + text.count("消しゴム・ペン（lines） / 設定") == 3
    points = json.loads(run(capsys, "backup", "list", "book", "--name", "a.png", "--json")[1])["points"]
    assert [p["number"] for p in points] == [1, 2, 3]

    # 番号で戻す（2 = 2 回目の変更の直前 = 1 回目の状態）
    code, text, _ = run(capsys, "backup", "restore", "book", "--name", "a.png", "--at", "2")
    assert code == 0 and "戻しました" in text and "取り消すには --at" in text
    assert strength_of(out / "a.inkdeus.json") == 0.1 and (out / "a.paint" / "lines.png").read_bytes() == png(10)
    assert json.loads((out / "a.paint" / "index.json").read_text())["targets"][0]["bytes"] == len(png(10))
    undo = text.split("--at ")[1].split("）")[0].strip()
    # 戻す前の状態も退避されているので、取り消せる
    code, text, _ = run(capsys, "backup", "restore", "book", "--name", "a.png", "--at", undo)
    assert code == 0 and strength_of(out / "a.inkdeus.json") == 0.3
    assert (out / "a.paint" / "lines.png").read_bytes() == png(30)
    # 種類を絞る: 設定だけ・時刻で指定
    at = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(points[0]["time"]))
    code, text, _ = run(capsys, "backup", "restore", "book", "--name", "a.png", "--kind", "params", "--at", at, "--json")
    assert code == 0 and json.loads(text)["restored"] == ["設定"]
    assert strength_of(out / "a.inkdeus.json") == 0.2 and (out / "a.paint" / "lines.png").read_bytes() == png(30)
    # 漫画データ
    code, text, _ = run(capsys, "backup", "restore", "book", "--kind", "manga", "--name", "第1話", "--at", "1")
    assert code == 0 and (env["root"] / "book" / "manga" / "第1話.lz4").read_bytes() == manga_data(b"one")
    # 使えない指定
    code, _, err = run(capsys, "backup", "restore", "book", "--name", "a.png", "--at", "99")
    assert code == 1 and "番号は 1〜" in err
    code, _, err = run(capsys, "backup", "restore", "book", "--kind", "paint", "--at", "1")
    assert code == 1 and "--name" in err
    code, _, err = run(capsys, "backup", "list", "book", "--name", "b.png", "--kind", "params")
    assert code == 0
    assert run(capsys, "backup", "restore", "book", "--name", "b.png", "--at", "1")[0] == 1
    # 間引き
    code, text, _ = run(capsys, "backup", "prune", "book")
    assert code == 0 and "間引きました" in text


def test_restored_params_reach_a_running_server(env, capsys):
    """稼働中のサーバがあれば、コマンドで戻した設定が「外部からの変更」として画面へ届く"""
    project = load_project("book")
    app = create_app(project.source, project.masks, project.out, project=project.info(), watch=False,
                     local_config=env["tmp"] / "none.toml", config_dir=env["tmp"] / "config")
    client, session = app.test_client(), app.config["INKDEUS_SESSION"]
    work(client, 0.1, 10)
    work(client, 0.2, 20)
    session.scan()
    q = session.hub.subscribe()
    assert run(capsys, "backup", "restore", "book", "--name", "a.png", "--kind", "params", "--at", "1")[0] == 0
    events = session.scan()
    assert len(events) == 1 and events[0]["type"] == "params" and events[0]["source"] is None
    assert q.get_nowait() == events[0]
    assert client.get("/api/params/a.png").get_json()["lines"]["strength"] == 0.1


def test_export_and_import_round_trip(env, capsys):
    tmp = env["tmp"]
    project = load_project("book")
    extra = tmp / "ext" / "more"
    synth.save(extra / "c.png", synth.flat(16, 16, 90))
    add_folder(project, "second", extra)
    client = app_of("book", tmp).test_client()
    work(client, 0.1, 10)
    work(client, 0.2, 20)
    work(client, 0.7, 70, name="c.png", query="?folder=second")
    client.post("/api/versions/a.png", data=json.dumps({"version": "v2"}))
    assert client.post("/api/export/a.png", data=png(5)).status_code == 200
    assert client.put("/api/manga/project/第1話", data=manga_data(b"pages")).status_code == 200
    tone = client.get("/api/params/a.png?resolve=1").get_json()["params"]["regions"]["person"]
    data = {"solid": tone["solid"], "layers": [{k: v for k, v in layer.items() if k != "id"} for layer in tone["layers"]]}
    assert client.put("/api/styles/mine?scope=project", data=json.dumps({"data": data})).status_code == 200
    (project.out / ".cache").mkdir()
    (project.out / ".cache" / "x.jpg").write_bytes(b"cache")

    plain = tmp / "plain.zip"
    code, text, _ = run(capsys, "export-project", "book", "-o", str(plain))
    assert code == 0 and "元画像とマスクは入っていません" in text
    with zipfile.ZipFile(plain) as z:
        names = set(z.namelist())
        manifest = json.loads(z.read("manifest.json"))
        blob = b"".join(z.read(n) for n in names)
    assert names == {"manifest.json", "out/a.inkdeus.json", "out/a@v2.inkdeus.json", "out/a.paint/index.json",
                     "out/a.paint/lines.png", "out/a@v2.paint/index.json", "out/a@v2.paint/lines.png",
                     "out/@second/c.inkdeus.json", "out/@second/c.paint/index.json",
                     "out/@second/c.paint/lines.png", "manga/第1話.lz4", "project/styles.json"}
    # 絶対パス・利用者の場所は、目録にも中身にも入らない（フォルダ名と相対構成だけ）
    for secret in (str(tmp), str(env["images"]), str(env["root"])):
        assert secret.encode("utf-8") not in blob and secret not in json.dumps(manifest, ensure_ascii=False)
    assert [(f["name"], f["imagesName"], f["images"], f["sources"]) for f in manifest["folders"]] == [
        ("default", "images", 2, False), ("second", "more", 1, False)]
    assert manifest["project"]["name"] == "book" and manifest["format"] == 1 and manifest["inkdeus"]
    assert all(len(f["sha256"]) == 64 and f["bytes"] >= 0 for f in manifest["files"])

    # 元画像なしで取り込む → 「元画像の場所が未設定」のプロジェクトになる
    code, text, _ = run(capsys, "import-project", str(plain), "--name", "copy")
    assert code == 0 and "元画像の場所が未設定です（元の画像フォルダの名前: images）" in text
    assert "inkdeus folders add copy --name second" in text
    code, text, _ = run(capsys, "projects")
    assert "copy: 元画像の場所が未設定（inkdeus relink copy --images <dir>）" in text
    copy = load_project("copy", check=False)
    assert copy.unset and strength_of(copy.out / "a.inkdeus.json") == 0.2
    assert (copy.out / "a.paint" / "lines.png").read_bytes() == png(20)
    assert (copy.out / "@second" / "c.paint" / "lines.png").read_bytes() == png(70)
    assert (copy.manga / "第1話.lz4").read_bytes() == manga_data(b"pages") and (copy.path / "styles.json").is_file()
    assert not (copy.out / "a.png").exists()                                   # 書き出した PNG は入れていない
    # 元画像の場所を指定して取り込む
    code, text, _ = run(capsys, "import-project", str(plain), "--name", "linked", "--images", str(env["images"]),
                        "--masks", str(env["masks"]))
    linked = load_project("linked")
    assert code == 0 and linked.source == env["images"] and linked.masks == env["masks"] and not linked.unset
    # 同じ名前へは取り込めない。壊れた zip・書き換えられた zip も受け付けない
    code, _, err = run(capsys, "import-project", str(plain), "--name", "copy")
    assert code == 1 and "既にあります" in err
    bad = tmp / "bad.zip"
    with zipfile.ZipFile(plain) as src, zipfile.ZipFile(bad, "w") as dst:
        for n in src.namelist():
            dst.writestr(n, b"tampered" if n == "out/a.inkdeus.json" else src.read(n))
    code, _, err = run(capsys, "import-project", str(bad), "--name", "bad")
    assert code == 1 and "目録と合いません" in err and not (env["root"] / "bad").exists()
    assert not list(env["root"].glob(".bad*"))                                 # 作りかけも残さない
    evil = tmp / "evil.zip"
    with zipfile.ZipFile(evil, "w") as dst:
        dst.writestr("manifest.json", json.dumps({"format": 1, "files": [{"path": "../x", "bytes": 1, "sha256": ""}]}))
        dst.writestr("../x", b"1")
    code, _, err = run(capsys, "import-project", str(evil), "--name", "evil")
    assert code == 1 and "受け付けられない名前" in err and not (env["root"] / "x").exists()
    (tmp / "junk.zip").write_bytes(b"not a zip")
    assert run(capsys, "import-project", str(tmp / "junk.zip"), "--name", "junk")[0] == 1

    # 全部入り: 元画像・マスク・部分マスク・書き出し・バックアップ
    full = tmp / "full.zip"
    code, text, _ = run(capsys, "export-project", "book", "-o", str(full), "--with-sources", "--with-exports",
                        "--with-backups")
    assert code == 0 and "元画像とマスク・書き出した PNG・PDF・バックアップ" in text
    with zipfile.ZipFile(full) as z:
        names = set(z.namelist())
    assert {"sources/default/images/a.png", "sources/default/images/b.png", "sources/default/masks/a.png",
            "sources/default/parts/hair/a.png", "sources/second/images/c.png", "out/a.png"} <= names
    assert any(n.startswith("out/.backup/a.inkdeus.json/") for n in names)
    assert not any(".cache" in n for n in names)
    code, text, _ = run(capsys, "import-project", str(full), "--name", "whole")
    assert code == 0 and "未設定" not in text
    whole = load_project("whole")                                              # 参照先がそろっている（check が通る）
    assert whole.source == whole.path / "source" and whole.masks == whole.path / "masks"
    assert whole.parts == whole.path / "parts" and [f.name for f in whole.folders] == ["second"]
    assert whole.folders[0].images == whole.path / "folders" / "second" / "images"
    assert (whole.source / "a.png").read_bytes() == (env["images"] / "a.png").read_bytes()
    client = app_of("whole", tmp).test_client()
    listing = client.get("/api/images").get_json()
    row = {i["name"]: i for i in listing["images"]}["a.png"]
    assert row["hasMask"] and row["hasParams"] and row["hasExport"] and row["parts"] == ["hair"]
    assert [v["name"] for v in row["versions"]] == ["", "v2"]
    assert client.get("/api/paint/a.png/lines").data == png(20)
    assert client.get("/api/images?folder=second").get_json()["images"][0]["hasParams"] is True
    assert len(client.get("/api/backups/a.png").get_json()["points"]) >= 1    # バックアップも付いてくる


def test_relink(env, capsys):
    tmp = env["tmp"]
    client = app_of("book", tmp).test_client()
    work(client, 0.4, 40)
    work(client, 0.5, 50, name="b.png")
    moved = tmp / "moved" / "pictures"
    moved.parent.mkdir()
    shutil.copytree(env["images"], moved)
    synth.save(moved / "new.png", synth.flat(8, 8, 10))
    (moved / "b.png").unlink()
    # 足りない画像があれば、--force 無しでは中止する
    code, text, _ = run(capsys, "relink", "book", "--images", str(moved))
    assert code == 1 and "一致 1 / 不足 1 / 余分 1" in text and "b.png" in text and "new.png" in text and "中止しました" in text
    assert load_project("book").source == env["images"]
    report = json.loads(run(capsys, "relink", "book", "--images", str(moved), "--json")[1])
    assert (report["matched"], report["missing"], report["extra"], report["applied"]) == (["a.png"], ["b.png"], ["new.png"], False)
    code, _, err = run(capsys, "relink", "book", "--images", str(tmp / "nowhere"))
    assert code == 1 and "画像フォルダが見つかりません" in err
    # そろっていれば付け替える（マスク・部分マスクの置き場は、指定しなければそのまま）
    shutil.copy(env["images"] / "b.png", moved / "b.png")
    code, text, _ = run(capsys, "relink", "book", "--images", str(moved))
    assert code == 0 and "一致 2 / 不足 0 / 余分 1" in text and "付け替えました" in text
    project = load_project("book")
    assert project.source == moved and project.masks == env["masks"] and project.parts == env["parts"]
    # 付け替えの前の設定ファイルも、バックアップに残る
    code, text, _ = run(capsys, "backup", "list", "book", "--kind", "project")
    assert code == 0 and "プロジェクトの設定" in text
    # 元の場所が消えていても、保存先に残っている設定から「あったはずの画像」を数える
    shutil.rmtree(env["images"])
    shutil.rmtree(moved)
    again = tmp / "again"
    synth.save(again / "a.png", synth.flat(8, 8, 10))
    report = json.loads(run(capsys, "relink", "book", "--images", str(again), "--json")[1])
    assert (report["matched"], report["missing"], report["applied"]) == (["a.png"], ["b"], False)
    code, text, _ = run(capsys, "relink", "book", "--images", str(again), "--masks", str(env["masks"]), "--force")
    assert code == 0 and load_project("book").source == again
    # 追加の画像フォルダ
    extra, extra2 = tmp / "x1", tmp / "x2"
    synth.save(extra / "c.png", synth.flat(8, 8, 10))
    synth.save(extra2 / "c.png", synth.flat(8, 8, 10))
    add_folder(load_project("book"), "second", extra)
    code, text, _ = run(capsys, "relink", "book", "--folder", "second", "--images", str(extra2))
    assert code == 0 and load_project("book").folders[0].images == extra2
    code, _, err = run(capsys, "relink", "book", "--folder", "nope", "--images", str(extra2))
    assert code == 1 and "その名前の画像フォルダはありません" in err


def test_missing_source_is_shown_and_relink_brings_everything_back(env, capsys, monkeypatch):
    """元画像の場所が消えても起動でき、付け替えると設定・漫画データがそのままつながる"""
    tmp = env["tmp"]
    client = app_of("book", tmp).test_client()
    work(client, 0.6, 60)
    assert client.put("/api/manga/project/第1話", data=manga_data(b"/api/assets/source/image/a.png?max=2048")).status_code == 200
    moved = tmp / "elsewhere"
    shutil.move(str(env["images"]), str(moved))
    started = {}
    monkeypatch.setattr(cli, "serve", lambda app, host, port, project=None: started.update(app=app) or 0)
    code, _, err = run(capsys, "open", "book")
    assert code == 0 and "元画像の場所が見つかりません" in err and "inkdeus relink book --images <dir>" in err
    client = started["app"].test_client()
    config = client.get("/api/config").get_json()
    assert [m["name"] for m in config["missingFolders"]] == ["default"]
    assert client.get("/api/images").get_json() == {**client.get("/api/images").get_json(), "images": [], "missing": True}
    assert client.get("/api/assets/source/image/a.png").status_code == 404
    assert client.get("/api/manga/project/第1話").status_code == 200           # 漫画データは開ける
    assert run(capsys, "relink", "book", "--images", str(moved))[0] == 0
    client = app_of("book", tmp).test_client()
    row = {i["name"]: i for i in client.get("/api/images").get_json()["images"]}["a.png"]
    assert row["hasParams"] and client.get("/api/config").get_json()["missingFolders"] == []
    # 漫画データの中の画像は「画像名」で引かれる（場所には縛られない）ので、付け替え後もそのまま表示される
    saved = client.get("/api/manga/project/第1話").data
    url = saved[saved.index(b"/api/assets"):].decode("utf-8")
    assert client.get(url).status_code == 200
    assert client.get("/api/paint/a.png/lines").data == png(60)
