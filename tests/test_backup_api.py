"""データを失わないための仕組み（サーバ側）: 保存のたびの退避、壊れたファイルを上書きしない、筆のタイル保存、
以前の状態に戻す、ビルド識別、見つからない元画像の場所"""

from __future__ import annotations

import io
import json
import struct

import pytest
from PIL import Image

import synth
from inkdeus import backup
from inkdeus.projects import add_asset_source, add_folder, create_project
from inkdeus.server import Build, create_app


@pytest.fixture()
def env(tmp_path):
    images = tmp_path / "images"
    synth.save(images / "scene.png", synth.scene(64, 48)[0])
    synth.save(images / "plain.png", synth.flat(32, 16, 255))
    out = tmp_path / "out"
    app = create_app(images, out_dir=out, watch=False, local_config=tmp_path / "none.toml",
                     config_dir=tmp_path / "config")
    return {"tmp": tmp_path, "images": images, "out": out, "app": app, "client": app.test_client(),
            "store": app.config["INKDEUS_BACKUPS"]}


def put_params(client, name, params, version="", ids_only=False):
    r = client.put(f"/api/params/{name}" + (f"?version={version}" if version else ""), data=json.dumps(params),
                   headers={"X-Inkdeus-Client": "t", **({"X-Inkdeus-Ids-Only": "1"} if ids_only else {})})
    assert r.status_code == 200, r.data
    return r.get_json()


def png(value: int = 128, size=(8, 4), mode="L") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, value).save(buf, "PNG")
    return buf.getvalue()


def events_of(app):
    q = app.config["INKDEUS_SESSION"].hub.subscribe()
    return lambda: [q.get_nowait() for _ in range(q.qsize())]


def contents(store, key):
    return ["<absent>" if e.absent else e.path.read_bytes() for e in store.entries(key)]


# ---- 設定 ----

def test_saving_params_keeps_the_previous_version(env):
    client, store = env["client"], env["store"]
    base = client.get("/api/params/scene.png?resolve=1").get_json()["params"]
    first = put_params(client, "scene.png", base)
    assert contents(store, "scene.inkdeus.json") == ["<absent>"]              # 新しく作られた（前の版は無い）
    put_params(client, "scene.png", base)                                     # 同じ内容 → 退避しない
    assert len(store.entries("scene.inkdeus.json")) == 1
    changed = json.loads(json.dumps(base))
    changed["lines"]["strength"] = 0.5
    put_params(client, "scene.png", changed)
    old = contents(store, "scene.inkdeus.json")[0]
    assert json.loads(old)["lines"]["strength"] == 1.0 and store.root == env["out"] / ".backup"
    # 外部からの部分更新（ctl）でも退避する。層の id を補うだけの保存では退避しない
    r = client.patch("/api/params/scene.png", data=json.dumps({"lines": {"strength": 0.25}}))
    assert r.status_code == 200 and json.loads(contents(store, "scene.inkdeus.json")[0])["lines"]["strength"] == 0.5
    put_params(client, "scene.png", {**changed, "unit": 2}, ids_only=True)
    assert len(store.entries("scene.inkdeus.json")) == 3 and first["rev"]
    # バージョンと追加の画像フォルダは、置き方をそのまま写した別のキー
    client.post("/api/versions/scene.png", data=json.dumps({"version": "v2"}))
    put_params(client, "scene.png", changed, version="v2")
    assert "scene@v2.inkdeus.json" in store.keys()


def test_broken_params_are_reported_and_never_overwritten(env):
    client, store, out = env["client"], env["store"], env["out"]
    base = client.get("/api/params/scene.png?resolve=1").get_json()["params"]
    put_params(client, "scene.png", base)
    good = json.loads(json.dumps(base))
    good["lines"]["strength"] = 0.4
    put_params(client, "scene.png", good)
    put_params(client, "scene.png", {**good, "unit": 3})
    path = out / "scene.inkdeus.json"
    for junk in (b"", b"{not json", b"[1, 2]"):
        path.write_bytes(junk)
        r = client.get("/api/params/scene.png")
        body = r.get_json()
        assert r.status_code == 422 and body["broken"] is True and "壊れています" in body["error"]
        assert len(body["backups"]) == 2 and body["path"] == str(path)
        resolved = client.get("/api/params/scene.png?resolve=1").get_json()
        assert resolved["broken"] is True and any("壊れています" in w for w in resolved["warnings"])
        # 外部からの変更（ctl の set / patch / preset）は、壊れたファイルを既定値で上書きしない
        for method, url, payload in (("patch", "/api/params/scene.png", {"unit": 2}),
                                     ("post", "/api/set/scene.png", {"key": "unit", "value": 2}),
                                     ("post", "/api/style/scene.png", {"style": "ベタ", "region": "person"})):
            r = getattr(client, method)(url, data=json.dumps(payload))
            assert r.status_code == 422 and r.get_json()["broken"] is True
        assert path.read_bytes() == junk and not list(out.glob("*.broken-*"))
    # 利用者が選ぶ: 最新のバックアップから戻す（壊れたファイルは .broken-<時刻> として残る）
    seen = events_of(env["app"])
    assert client.post("/api/repair/scene.png", data=json.dumps({"action": "bogus"})).status_code == 400
    r = client.post("/api/repair/scene.png", data=json.dumps({"action": "restore"})).get_json()
    assert r["repaired"] is True and r["action"] == "restore"
    kept = list(out.glob("scene.inkdeus.json.broken-*"))
    assert len(kept) == 1 and kept[0].read_bytes() == b"[1, 2]" and r["kept"] == str(kept[0])
    assert json.loads(path.read_bytes())["lines"]["strength"] == 0.4          # 最新のバックアップ = 最後の上書きの前の版
    ev = [e for e in seen() if e["type"] == "params"]
    assert len(ev) == 1 and ev[0]["source"] is None and ev[0]["name"] == "scene.png"    # 開いている画面へ知らせる
    assert client.post("/api/repair/scene.png", data=json.dumps({"action": "restore"})).get_json()["repaired"] is False
    # 既定値で始める
    path.write_bytes(b"")
    r = client.post("/api/repair/scene.png", data=json.dumps({"action": "reset"})).get_json()
    assert r["repaired"] is True and not path.exists() and len(list(out.glob("scene.inkdeus.json.broken-*"))) == 2
    # 画面からの保存が壊れたファイルに当たったときも、上書きせずに残す
    path.write_bytes(b"\x00\x00")
    put_params(client, "scene.png", good)
    assert len(list(out.glob("scene.inkdeus.json.broken-*"))) == 3 and json.loads(path.read_bytes()) == good
    # バックアップが 1 つも無い壊れたファイルは、戻せないと伝える
    (out / "plain.inkdeus.json").write_bytes(b"")
    r = client.post("/api/repair/plain.png", data=json.dumps({"action": "restore"}))
    assert r.status_code == 409 and "戻せるバックアップがありません" in r.get_json()["error"]
    assert (out / "plain.inkdeus.json").read_bytes() == b""


# ---- 消しゴム・ペン ----

def tiles_body(width, height, channels, tiles) -> bytes:
    head = json.dumps({"width": width, "height": height, "channels": channels,
                       "tiles": [{"x": x, "y": y, "w": w, "h": h} for x, y, w, h, _d in tiles]}).encode("utf-8")
    return struct.pack("<I", len(head)) + head + b"".join(d for *_r, d in tiles)


def test_paint_is_backed_up_and_tiles_are_patched_in_place(env):
    client, store, out = env["client"], env["store"], env["out"]
    put_params(client, "scene.png", client.get("/api/params/scene.png?resolve=1").get_json()["params"])
    url = "/api/paint/scene.png/lines"
    assert client.put(url, data=png(10)).status_code == 200
    assert client.put(url, data=png(20)).status_code == 200
    assert client.put(url, data=png(20)).status_code == 200                   # 同じ内容 → 退避しない
    key = "scene.paint/lines.png"
    assert contents(store, key) == [png(10), "<absent>"]
    # タイルの保存: 変わった所だけを書き込む（残りはそのまま）
    seen = events_of(env["app"])
    body = tiles_body(8, 4, 1, [(2, 1, 3, 2, bytes([200] * 6))])
    r = client.patch(url, data=body, headers={"X-Inkdeus-Client": "me"})
    assert r.status_code == 200 and r.get_json()["deleted"] is False
    im = Image.open(out / "scene.paint" / "lines.png")
    assert im.mode == "L" and im.size == (8, 4)
    px = list(im.getdata())
    assert [px[y * 8 + x] for y in (1, 2) for x in (2, 3, 4)] == [200] * 6 and px.count(20) == 26
    assert contents(store, key)[0] == png(20)                                 # 書き込む前の物も退避される
    assert [(e["action"], e["targets"], e["source"]) for e in seen() if e["type"] == "paint"] == [("patch", ["lines"], "me")]
    # 大きさが合わなければ受け付けない（画面は全体を保存し直す）。はみ出したタイル・足りない画素も
    assert client.patch(url, data=tiles_body(16, 4, 1, [(0, 0, 1, 1, b"\x01")])).status_code == 409
    assert client.patch(url, data=tiles_body(8, 4, 1, [(7, 3, 2, 2, bytes(4))])).status_code == 400
    assert client.patch(url, data=tiles_body(8, 4, 1, [(0, 0, 2, 2, bytes(3))])).status_code == 400
    assert client.patch(url, data=b"\x01").status_code == 400
    # 連長圧縮（[個数, 値] の繰り返し）でも送れる
    head = json.dumps({"width": 8, "height": 4, "channels": 1, "encoding": "rle",
                       "tiles": [{"x": 0, "y": 0, "w": 8, "h": 1, "n": 4}]}).encode("utf-8")
    assert client.patch(url, data=struct.pack("<I", len(head)) + head + bytes([5, 77, 3, 99])).status_code == 200
    row = [Image.open(out / "scene.paint" / "lines.png").getpixel((x, 0)) for x in range(8)]
    assert row == [77] * 5 + [99] * 3
    assert client.patch(url, data=struct.pack("<I", len(head)) + head + bytes([5, 77, 2, 99])).status_code == 400
    # まだ無い対象は、タイルから新しく作る（ペンは R = 黒・G = 白の 2 チャンネル）
    r = client.patch("/api/paint/scene.png/pen", data=tiles_body(6, 4, 2, [(1, 1, 2, 1, bytes([255, 0, 0, 128]))]))
    assert r.status_code == 200
    pen = Image.open(out / "scene.paint" / "pen.png")
    assert pen.mode == "RGB" and pen.getpixel((1, 1)) == (255, 0, 0) and pen.getpixel((2, 1)) == (0, 128, 0)
    assert pen.getpixel((0, 0)) == (0, 0, 0)
    # 全部 0 に戻ったら、ファイルごと片付ける（消す前の物は退避される）
    r = client.patch("/api/paint/scene.png/pen", data=tiles_body(6, 4, 2, [(1, 1, 2, 1, bytes(4))])).get_json()
    assert r["deleted"] is True and not (out / "scene.paint" / "pen.png").exists()
    assert len([c for c in contents(store, "scene.paint/pen.png") if c != "<absent>"]) == 1
    listed = [t["target"] for t in client.get("/api/paint/scene.png").get_json()["targets"]]
    assert listed == ["lines"] and json.loads((out / "scene.paint" / "index.json").read_text())["targets"][0]["target"] == "lines"
    # 削除でも退避する
    before = len(store.entries(key))
    assert client.delete(url).status_code == 200 and len(store.entries(key)) == before + 1
    # 壊れた絵（0 バイト）は、読むと「壊れている」と分かり、上書きしても残る
    assert client.put(url, data=png(30)).status_code == 200
    (out / "scene.paint" / "lines.png").write_bytes(b"")
    assert client.get("/api/paint/scene.png").get_json()["targets"][0]["broken"] is True
    r = client.get(url)
    assert r.status_code == 422 and r.get_json()["broken"] is True
    assert client.put(url, data=png(40)).status_code == 200
    assert len(list((out / "scene.paint").glob("lines.png.broken-*"))) == 1


def test_restore_an_earlier_state_and_undo_it(env):
    client, out = env["client"], env["out"]
    base = client.get("/api/params/scene.png?resolve=1").get_json()["params"]

    def state(strength, ink):
        params = json.loads(json.dumps(base))
        params["lines"]["strength"] = strength
        put_params(client, "scene.png", params)
        assert client.put("/api/paint/scene.png/lines", data=png(ink)).status_code == 200

    import time

    for strength, ink in ((0.1, 10), (0.2, 20), (0.3, 30)):
        state(strength, ink)
        time.sleep(2.1)                                                       # 2 秒以内の変更は 1 つの時点にまとまる
    listing = client.get("/api/backups/scene.png").get_json()
    assert listing["enabled"] is True and len(listing["points"]) == 3
    assert [sorted(p["what"]) for p in listing["points"]] == [["消しゴム・ペン（lines）", "設定"]] * 3
    seen = events_of(env["app"])
    # 新しい方から 1 番 = 最後の変更の直前（0.2 / 20）
    r = client.post("/api/backups/scene.png", data=json.dumps({"at": listing["points"][0]["at"]})).get_json()
    assert sorted(r["restored"]) == ["消しゴム・ペン（lines）", "設定"]
    assert json.loads((out / "scene.inkdeus.json").read_bytes())["lines"]["strength"] == 0.2
    assert (out / "scene.paint" / "lines.png").read_bytes() == png(20)
    kinds = [(e["type"], e.get("source")) for e in seen()]
    assert ("params", None) in kinds and ("paint", None) in kinds             # 開いている画面へ知らせる
    assert len(r["points"]) == 4                                              # 戻す前の状態（0.3 / 30）も残っている
    # 取り消し: 戻した時刻を指して、もう一度戻す
    r2 = client.post("/api/backups/scene.png", data=json.dumps({"at": r["undo"]})).get_json()
    assert sorted(r2["restored"]) == ["消しゴム・ペン（lines）", "設定"]
    assert json.loads((out / "scene.inkdeus.json").read_bytes())["lines"]["strength"] == 0.3
    assert (out / "scene.paint" / "lines.png").read_bytes() == png(30)
    # いちばん古い時点 = まだ何も無かった状態
    oldest = client.get("/api/backups/scene.png").get_json()["points"][-1]
    client.post("/api/backups/scene.png", data=json.dumps({"at": oldest["at"]}))
    assert not (out / "scene.inkdeus.json").exists() and not (out / "scene.paint").exists()
    assert client.post("/api/backups/scene.png", data=json.dumps({"at": "nonsense"})).status_code == 400


def test_external_paint_changes_are_announced_to_the_open_screen(env):
    """inkdeus backup restore などで、サーバ以外が消しゴム・ペンを書き換えたら、開いている画面へ知らせる"""
    client, app, out = env["client"], env["app"], env["out"]
    session = app.config["INKDEUS_SESSION"]
    put_params(client, "scene.png", client.get("/api/params/scene.png?resolve=1").get_json()["params"])
    client.post("/api/state", data=json.dumps({"current": "scene.png"}))
    assert session.scan_paint([session]) is None                              # 最初は覚えるだけ
    assert client.put("/api/paint/scene.png/lines", data=png(10)).status_code == 200
    assert session.scan_paint([session]) is None                              # サーバ自身の書き込みは数えない
    import os
    import time

    target = out / "scene.paint" / "lines.png"
    target.write_bytes(png(99))
    os.utime(target, (time.time() + 5, time.time() + 5))
    seen = events_of(app)
    event = session.scan_paint([session])
    assert event["type"] == "paint" and event["source"] is None and event["targets"] == ["lines"]
    assert seen() == [event] and session.scan_paint([session]) is None


# ---- 漫画データ・スタイル・プロジェクトの設定 ----

def manga_data(text: bytes) -> bytes:
    head = json.dumps([{"name": "state.json", "size": len(text)}]).encode("utf-8")
    return struct.pack("<I", len(head)) + head + text


def test_manga_data_keeps_earlier_versions(env):
    client, store = env["client"], env["store"]
    for text in (b"one", b"two", b"three"):
        assert client.put("/api/manga/project/book", data=manga_data(text)).status_code == 200
    assert client.put("/api/manga/project/autosave", data=manga_data(b"auto")).status_code == 200
    assert contents(store, "_manga/book.lz4") == [manga_data(b"two"), manga_data(b"one"), "<absent>"]
    rows = {p["name"]: p for p in client.get("/api/manga/projects").get_json()["projects"]}
    assert rows["book"]["backups"] == 2 and rows["autosave"]["backups"] == 0
    listed = client.get("/api/manga/backups/book").get_json()["backups"]
    assert len(listed) == 2 and listed[0]["time"] > listed[1]["time"]
    # 自動保存にも世代バックアップが効く（上書きのたびに、前の自動保存が残る）
    for text in (b"auto2", b"auto3"):
        assert client.put("/api/manga/project/autosave", data=manga_data(text)).status_code == 200
    assert contents(store, "_manga/autosave.lz4") == [manga_data(b"auto2"), manga_data(b"auto"), "<absent>"]
    auto = client.get("/api/manga/backups/autosave").get_json()["backups"]
    assert len(auto) == 2 and client.get(f"/api/manga/backup/autosave/{auto[0]['at']}").data == manga_data(b"auto2")
    r = client.get(f"/api/manga/backup/book/{listed[1]['at']}")
    assert r.status_code == 200 and r.data == manga_data(b"one")              # 以前の版の中身（いまの保存データは変えない）
    assert client.get("/api/manga/project/book").data == manga_data(b"three")
    assert client.get("/api/manga/backup/book/20200101-000000-000").status_code == 404
    # 壊れた漫画データは、そのまま渡さない・上書きしない
    path = env["out"] / "manga" / "book.lz4"
    path.write_bytes(b"")
    r = client.get("/api/manga/project/book")
    assert r.status_code == 422 and r.get_json()["broken"] is True and r.get_json()["backups"] == 2
    assert client.put("/api/manga/project/book", data=manga_data(b"four")).status_code == 200
    kept = list(path.parent.glob("book.lz4.broken-*"))
    assert len(kept) == 1 and kept[0].read_bytes() == b"" and path.read_bytes() == manga_data(b"four")


def test_styles_and_project_settings_are_backed_up(tmp_path, monkeypatch):
    monkeypatch.setenv("INKDEUS_PROJECTS", str(tmp_path / "projects"))
    monkeypatch.setattr("inkdeus.projects.read_local_config", lambda path=None: {})
    images = tmp_path / "images"
    synth.save(images / "a.png", synth.flat(16, 16, 200))
    project, _ = create_project("p", images, root=tmp_path / "projects")
    app = create_app(project.source, None, project.out, project=project.info(), watch=False,
                     local_config=tmp_path / "none.toml", config_dir=tmp_path / "config")
    client, store = app.test_client(), app.config["INKDEUS_BACKUPS"]
    tone = client.get("/api/params/a.png?resolve=1").get_json()["params"]["regions"]["person"]
    data = {"solid": tone["solid"], "layers": [{k: v for k, v in layer.items() if k != "id"} for layer in tone["layers"]]}
    for scope in ("project", "user"):
        for description in ("one", "two", "three"):
            r = client.put(f"/api/styles/mine?scope={scope}", data=json.dumps({"description": description, "data": data}))
            assert r.status_code == 200, r.data
    project_backups = [json.loads(c)["styles"][0]["description"] for c in contents(store, "_project/styles.json")]
    assert project_backups == ["two", "one"]                                  # プロジェクトの分は <out>/.backup/_project/
    user = backup.Store(tmp_path / "config" / ".backup")
    assert [json.loads(c)["styles"][0]["description"] for c in contents(user, "styles.json")] == ["two", "one"]
    # プロジェクトの設定（inkdeus.project.toml）: 書き換えるたびに、前の版が残る
    before = project.config.read_bytes()
    sfx = tmp_path / "sfx"
    sfx.mkdir()
    add_asset_source(project, "sfx", sfx)
    add_folder(project, "extra", images)
    kept = contents(store, "_project/inkdeus.project.toml")
    assert len(kept) == 2 and kept[1] == before and b"[[assets]]" in kept[0] and b"[[folders]]" not in kept[0]
    # 壊れた設定ファイルは上書きせずに残す
    project.config.write_text("name = = broken", encoding="utf-8")
    create_project("p", images, root=tmp_path / "projects", force=True)
    assert len(list(project.path.glob("inkdeus.project.toml.broken-*"))) == 1


def test_backup_can_be_turned_off(tmp_path):
    images = tmp_path / "images"
    synth.save(images / "a.png", synth.flat(16, 16, 200))
    cfg = tmp_path / "local.toml"
    cfg.write_text("[backup]\nenabled = false\nkeep_recent = 3\n", encoding="utf-8")
    app = create_app(images, out_dir=tmp_path / "out", watch=False, local_config=cfg, config_dir=tmp_path / "config")
    client = app.test_client()
    config = client.get("/api/config").get_json()
    assert config["backup"] == {"enabled": False, "keepRecent": 3, "maxTotalMb": 2048} and "backup" in config["features"]
    base = client.get("/api/params/a.png?resolve=1").get_json()["params"]
    put_params(client, "a.png", base)
    put_params(client, "a.png", {**base, "unit": 2})
    assert not (tmp_path / "out" / ".backup").exists()
    assert client.get("/api/backups/a.png").get_json() == {"folder": "default", "name": "a.png", "version": "",
                                                            "enabled": False, "points": []}


# ---- ビルド識別・見つからない元画像の場所 ----

def test_build_identity(env, tmp_path):
    info = env["client"].get("/api/config").get_json()["build"]
    assert set(info) == {"id", "commit", "web", "webNow", "boot", "stale"}
    assert info["web"] == info["webNow"] and info["stale"] is False and len(info["boot"]) == 12
    assert info["id"].endswith(info["web"])
    again = create_app(env["images"], out_dir=tmp_path / "out2", watch=False,
                       local_config=tmp_path / "none.toml").test_client().get("/api/config").get_json()["build"]
    assert again["id"] == info["id"] and again["boot"] != info["boot"]       # 同じ中身なら同じ識別。起動 ID は毎回違う
    tagged = create_app(env["images"], out_dir=tmp_path / "out3", watch=False, local_config=tmp_path / "none.toml",
                        build_tag="next").test_client().get("/api/config").get_json()["build"]
    assert tagged["id"] == "next"
    # web/ のファイルが起動後に変わると、識別が変わり「起動後に変わった」と分かる
    web = tmp_path / "web"
    (web / "js").mkdir(parents=True)
    (web / "index.html").write_text("<html>", encoding="utf-8")
    (web / "js" / "app.js").write_text("1", encoding="utf-8")
    build = Build(web)
    first = build.info()
    assert first["stale"] is False and first["commit"] == "" and first["id"] == f"nogit-{first['web']}"
    (web / "js" / "app.js").write_text("22", encoding="utf-8")
    second = build.info()
    assert second["stale"] is True and second["webNow"] != first["web"] == second["web"] and second["id"] != first["id"]
    assert Build(web).info()["web"] == second["webNow"]


def test_git_commit_is_read_without_running_git(tmp_path):
    from inkdeus.server import _git_commit

    repo = tmp_path / "repo"
    (repo / ".git" / "refs" / "heads").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (repo / ".git" / "refs" / "heads" / "main").write_text("a" * 40 + "\n", encoding="utf-8")
    assert _git_commit(repo) == "a" * 40
    (repo / ".git" / "refs" / "heads" / "main").unlink()
    (repo / ".git" / "packed-refs").write_text("# pack-refs\n" + "b" * 40 + " refs/heads/main\n", encoding="utf-8")
    assert _git_commit(repo) == "b" * 40
    (repo / ".git" / "HEAD").write_text("c" * 40 + "\n", encoding="utf-8")
    assert _git_commit(repo) == "c" * 40 and _git_commit(tmp_path / "nowhere") == ""


def test_missing_source_folder_does_not_crash(tmp_path):
    with pytest.raises(FileNotFoundError):
        create_app(tmp_path / "gone", out_dir=tmp_path / "out", watch=False)
    app = create_app(tmp_path / "gone", out_dir=tmp_path / "out", watch=False, allow_missing=True,
                     local_config=tmp_path / "none.toml")
    client = app.test_client()
    listing = client.get("/api/images").get_json()
    assert listing["images"] == [] and listing["missing"] is True
    gone = [{"name": "default", "label": "既定", "images": str(tmp_path / "gone")}]
    assert listing["missingFolders"] == gone and client.get("/api/config").get_json()["missingFolders"] == gone
    folders = client.get("/api/folders").get_json()
    assert folders["folders"][0]["missing"] is True and folders["missingFolders"] == gone
    assert client.get("/api/state").get_json()["images"] == 0
    assert client.get("/api/image/a.png").status_code == 404
    # 稼働中に元画像の場所が消えても落ちない。追加の画像フォルダが見つからないときも一覧に出る
    images = tmp_path / "images"
    synth.save(images / "a.png", synth.flat(16, 16, 200))
    extra = tmp_path / "extra"
    synth.save(extra / "b.png", synth.flat(16, 16, 100))
    project, _ = create_project("p", images, root=tmp_path / "projects")
    add_folder(project, "second", extra)
    app = create_app(project.source, None, project.out, project=project.info(), watch=False,
                     local_config=tmp_path / "none.toml")
    client = app.test_client()
    assert client.get("/api/config").get_json()["missingFolders"] == []
    import shutil

    shutil.rmtree(extra)
    shutil.rmtree(images)
    assert client.get("/api/images").get_json()["missing"] is True
    assert [m["name"] for m in client.get("/api/config").get_json()["missingFolders"]] == ["default", "second"]
    assert client.get("/api/images?folder=second").status_code in (200, 404)
