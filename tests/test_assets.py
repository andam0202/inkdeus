"""漫画の構成で使う素材（ソースごとの画像の一覧・縮小画像）と、素材ソースの登録 CLI"""

from __future__ import annotations

import io
import json
import os

import numpy as np
import pytest
from PIL import Image

import synth
from inkdeus import cli
from inkdeus.projects import create_project, load_project
from inkdeus.server import create_app


def rgba_sticker(w: int = 300, h: int = 200) -> Image.Image:
    """透過つきの素材（中央だけ不透明。透明部分の色は目立つ緑にして、縮小でにじまないことを確かめられるようにする）"""
    arr = np.zeros((h, w, 4), np.uint8)
    arr[..., 1] = 255
    arr[h // 4: h * 3 // 4, w // 4: w * 3 // 4] = (255, 0, 0, 255)
    return Image.fromarray(arr, "RGBA")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("INKDEUS_PROJECTS", str(root))
    images = tmp_path / "images"
    synth.save(images / "wide.png", synth.gradient(400, 100))
    synth.save(images / "tall.png", synth.scene(120, 300)[0])
    Image.fromarray(synth.scene(200, 100)[0]).save(images / "photo.jpg", quality=90)
    (images / "notes.txt").write_text("not an image", encoding="utf-8")
    (tmp_path / "secret.png").write_bytes((images / "wide.png").read_bytes())
    sfx = tmp_path / "sfx"
    for rel in ("don.png", "impact/bang.png", "impact/crash.png", "mood/zawa.png", ".hidden/x.png"):
        (sfx / rel).parent.mkdir(parents=True, exist_ok=True)
        rgba_sticker().save(sfx / rel)
    (sfx / ".dot.png").write_bytes((sfx / "don.png").read_bytes())
    project, _ = create_project("p1", images, root=root)
    synth.save(project.out / "wide.png", synth.flat(400, 100, 255)[..., 0])          # 書き出し済みの白黒画像
    (project.out / "wide.inkdeus.json").write_text("{}", encoding="utf-8")
    return {"tmp": tmp_path, "root": root, "images": images, "sfx": sfx, "project": project,
            "local": tmp_path / "inkdeus.local.toml"}


def client_for(env):
    project = load_project("p1")
    app = create_app(project.source, project.masks, project.out, project=project.info(),
                     local_config=env["local"], cache_root=env["tmp"] / "cache", watch=False)
    return app.test_client()


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def image_of(response) -> Image.Image:
    im = Image.open(io.BytesIO(response.data))
    im.load()
    return im


# ---- 既定のソース ----

def test_builtin_sources(env):
    client = client_for(env)
    data = client.get("/api/assets").get_json()
    rows = {s["name"]: s for s in data["sources"]}
    assert list(rows) == ["source", "inked"] and data["problems"] == []
    assert rows["source"]["count"] == 3 and rows["source"]["mode"] == "panel" and rows["source"]["label"] == "元画像"
    assert rows["inked"]["count"] == 1 and rows["inked"]["label"] == "白黒" and rows["inked"]["builtin"] is True
    listing = client.get("/api/assets/source").get_json()
    assert [i["path"] for i in listing["items"]] == ["photo.jpg", "tall.png", "wide.png"]
    wide = listing["items"][2]
    assert (wide["width"], wide["height"], wide["group"], wide["name"]) == (400, 100, "", "wide.png")
    assert wide["mtime"] > 0 and wide["bytes"] == (env["images"] / "wide.png").stat().st_size
    assert listing["total"] == 3 and listing["mode"] == "panel"
    inked = client.get("/api/assets/inked").get_json()
    assert [i["path"] for i in inked["items"]] == ["wide.png"]                       # 設定JSON やキャッシュは出さない
    assert client.get("/api/assets/nope").status_code == 404
    assert client.get("/api/assets").headers["Cache-Control"] == "no-store"


def test_filter_and_paging(env):
    client = client_for(env)
    assert [i["path"] for i in client.get("/api/assets/source?q=PNG").get_json()["items"]] == ["tall.png", "wide.png"]
    empty = client.get("/api/assets/source?q=zzz").get_json()
    assert empty["items"] == [] and empty["total"] == 0
    page = client.get("/api/assets/source?offset=1&limit=1").get_json()
    assert [i["path"] for i in page["items"]] == ["tall.png"] and page["total"] == 3
    assert (page["offset"], page["limit"]) == (1, 1)
    assert client.get("/api/assets/source?offset=10").get_json()["items"] == []
    for bad in ("limit=0", "limit=abc", "offset=-1", "limit=100000"):
        assert client.get(f"/api/assets/source?{bad}").status_code == 400, bad


# ---- 画像・縮小・サムネイル ----

def test_original_image(env):
    client = client_for(env)
    r = client.get("/api/assets/source/image/wide.png")
    assert r.status_code == 200 and r.data == (env["images"] / "wide.png").read_bytes()
    assert r.headers["Cache-Control"] == "no-cache" and r.headers.get("ETag")
    again = client.get("/api/assets/source/image/wide.png", headers={"If-None-Match": r.headers["ETag"]})
    assert again.status_code == 304                                  # 変わっていなければ再送しない
    r = client.get("/api/assets/inked/image/wide.png")
    assert r.status_code == 200 and image_of(r).mode == "L"


def test_resized_image_and_cache(env):
    client = client_for(env)
    cache = env["project"].out / ".cache" / "assets"
    r = client.get("/api/assets/source/image/wide.png?max=100")
    im = image_of(r)
    assert r.status_code == 200 and r.mimetype == "image/png" and im.size == (100, 25)
    assert len(r.data) < (env["images"] / "wide.png").stat().st_size * 4
    files = sorted(cache.iterdir())
    assert len(files) == 1 and files[0].suffix == ".png"
    stamp = files[0].stat().st_mtime_ns
    assert client.get("/api/assets/source/image/wide.png?max=100").data == r.data
    assert sorted(cache.iterdir()) == files and files[0].stat().st_mtime_ns == stamp     # 作り直さない
    assert image_of(client.get("/api/assets/source/image/tall.png?max=150")).size == (60, 150)
    assert image_of(client.get("/api/assets/source/image/photo.jpg?max=50")).size == (50, 25)
    # 縮小の必要が無ければ元ファイルをそのまま返す
    r = client.get("/api/assets/source/image/wide.png?max=400")
    assert r.data == (env["images"] / "wide.png").read_bytes()
    assert client.get("/api/assets/source/image/wide.png?max=4096").data == r.data
    for bad in ("max=0", "max=abc", "max=99999", "max=-5"):
        assert client.get(f"/api/assets/source/image/wide.png?{bad}").status_code == 400, bad
    # 縮小した物は一覧（inked）に混ざらない
    assert [i["path"] for i in client.get("/api/assets/inked").get_json()["items"]] == ["wide.png"]


def test_cache_is_invalidated_when_the_file_changes(env):
    client = client_for(env)
    cache = env["project"].out / ".cache" / "assets"
    target = env["images"] / "wide.png"
    first = client.get("/api/assets/source/image/wide.png?max=100")
    assert image_of(first).getpixel((99, 12)) != image_of(first).getpixel((0, 12))        # グラデーション
    synth.save(target, synth.flat(200, 100, 40))                                           # 中身も大きさも変える
    st = target.stat()
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    second = client.get("/api/assets/source/image/wide.png?max=100")
    im = image_of(second)
    assert im.size == (100, 50) and im.convert("L").getpixel((50, 25)) == 40
    assert second.data != first.data
    assert len(list(cache.iterdir())) == 1                           # 古い版は片付ける
    listed = {i["path"]: i for i in client.get("/api/assets/source").get_json()["items"]}
    assert (listed["wide.png"]["width"], listed["wide.png"]["height"]) == (200, 100)


def test_thumbnail(env):
    client = client_for(env)
    r = client.get("/api/assets/source/thumb/wide.png?h=40")
    assert r.status_code == 200 and image_of(r).size == (160, 40) and r.mimetype == "image/jpeg"
    assert r.headers["Cache-Control"] == "no-cache"
    assert image_of(client.get("/api/assets/source/thumb/tall.png")).size == (64, 160)      # 既定は高さ 160
    assert client.get("/api/assets/source/thumb/wide.png?h=5").status_code == 400
    assert client.get("/api/assets/source/thumb/nope.png").status_code == 404


def test_transparency_is_kept(env):
    assert run_cli_add(env, "--mode", "free", "--recursive") == 0
    client = client_for(env)
    for url in ("/api/assets/sfx/image/don.png?max=150", "/api/assets/sfx/thumb/don.png?h=40"):
        r = client.get(url)
        im = image_of(r)
        assert r.status_code == 200 and r.mimetype == "image/png" and im.mode == "RGBA", url
        w, h = im.size
        assert im.getpixel((0, 0))[3] == 0 and im.getpixel((w - 1, h - 1))[3] == 0           # 角は透明のまま
        assert im.getpixel((w // 2, h // 2)) == (255, 0, 0, 255)                             # 中央は不透明の赤
        edge = np.asarray(im)[h // 2]
        visible = edge[edge[:, 3] > 0]
        assert visible[:, 1].max() <= 8                               # 透明部分の緑が縁ににじまない
    assert image_of(client.get("/api/assets/sfx/image/don.png?max=150")).size == (150, 100)


def run_cli_add(env, *extra) -> int:
    return cli.main(["assets", "add", "p1", "--name", "sfx", "--path", str(env["sfx"]), *extra])


def test_path_traversal_is_rejected(env):
    assert run_cli_add(env, "--recursive") == 0
    client = client_for(env)
    assert client.get("/api/assets/sfx/image/impact/bang.png").status_code == 200
    for kind in ("image", "thumb"):
        for rel in ("../secret.png", "impact/../../secret.png", "%2e%2e/secret.png", "..%2fsecret.png",
                    "impact/%2e%2e/%2e%2e/secret.png", "notes.txt", ".hidden/x.png", ".dot.png", "impact",
                    str(env["tmp"] / "secret.png").lstrip("/")):
            r = client.get(f"/api/assets/sfx/{kind}/{rel}")
            assert r.status_code in (301, 308, 404), (kind, rel, r.status_code)
        assert client.get(f"/api/assets/source/{kind}/notes.txt").status_code == 404
        assert client.get(f"/api/assets/source/{kind}/../secret.png").status_code in (301, 308, 404)
        assert client.get(f"/api/assets/inked/{kind}/wide.inkdeus.json").status_code == 404
        assert client.get(f"/api/assets/inked/{kind}/.cache/assets/x.png").status_code == 404


# ---- 追加ソースの登録 ----

def test_cli_add_list_remove(env, capsys):
    code, out, _ = run(capsys, "assets", "add", "p1", "--name", "sfx", "--path", str(env["sfx"]),
                       "--mode", "free", "--recursive")
    assert code == 0 and "sfx" in out and "free" in out
    raw = (env["project"].config).read_text(encoding="utf-8")
    assert "[[assets]]" in raw and 'mode = "free"' in raw and "recursive = true" in raw
    project = load_project("p1")                                     # 他の項目は変わらない
    assert project.source == env["images"] and project.out == env["project"].out
    code, out, _ = run(capsys, "assets", "list", "p1")
    assert code == 0 and "source" in out and "inked" in out and "sfx" in out and "4 枚" in out
    code, out, _ = run(capsys, "assets", "list", "p1", "--json")
    rows = {s["name"]: s for s in json.loads(out)["sources"]}
    assert rows["sfx"] | {"mode": "free", "recursive": True, "origin": "project", "count": 4} == rows["sfx"]

    client = client_for(env)
    rows = {s["name"]: s for s in client.get("/api/assets").get_json()["sources"]}
    assert rows["sfx"]["mode"] == "free" and rows["sfx"]["count"] == 4 and rows["sfx"]["missing"] is False
    listing = client.get("/api/assets/sfx").get_json()
    assert [(i["group"], i["path"]) for i in listing["items"]] == [
        ("", "don.png"), ("impact", "impact/bang.png"), ("impact", "impact/crash.png"), ("mood", "mood/zawa.png")]
    assert listing["recursive"] is True and listing["mode"] == "free"
    assert [i["path"] for i in client.get("/api/assets/sfx?q=impact").get_json()["items"]] == \
        ["impact/bang.png", "impact/crash.png"]

    # 同じ名前で登録し直すと置き換え（サブフォルダを含めない設定に変える）。稼働中のサーバにもそのまま反映される
    assert run(capsys, "assets", "add", "p1", "--name", "sfx", "--path", str(env["sfx"]))[0] == 0
    listing = client.get("/api/assets/sfx").get_json()
    assert [i["path"] for i in listing["items"]] == ["don.png"] and listing["mode"] == "panel"
    assert (env["project"].config).read_text(encoding="utf-8").count("[[assets]]") == 1

    code, out, _ = run(capsys, "assets", "remove", "p1", "--name", "sfx")
    assert code == 0 and "[[assets]]" not in (env["project"].config).read_text(encoding="utf-8")
    assert client.get("/api/assets/sfx").status_code == 404
    code, _, err = run(capsys, "assets", "remove", "p1", "--name", "sfx")
    assert code == 1 and "ありません" in err


def test_cli_add_rejects_bad_input(env, capsys):
    for name in ("source", "inked", "a/b", "..", "a b", ""):
        code, _, err = run(capsys, "assets", "add", "p1", "--name", name, "--path", str(env["sfx"]))
        assert code == 1 and "ソース名" in err, name
    code, _, err = run(capsys, "assets", "add", "p1", "--name", "x", "--path", str(env["tmp"] / "nowhere"))
    assert code == 1 and "見つかりません" in err
    code, _, err = run(capsys, "assets", "add", "no-such-project", "--name", "x", "--path", str(env["sfx"]))
    assert code == 1 and "プロジェクトが見つかりません" in err
    with pytest.raises(SystemExit):
        cli.main(["assets", "add", "p1", "--name", "x", "--path", str(env["sfx"]), "--mode", "weird"])
    assert "[[assets]]" not in (env["project"].config).read_text(encoding="utf-8")


def test_force_update_keeps_asset_sources(env, capsys):
    assert run_cli_add(env, "--mode", "free") == 0
    capsys.readouterr()
    assert run(capsys, "new", "p1", "--images", str(env["images"]), "--force", "--note", "更新")[0] == 0
    raw = (env["project"].config).read_text(encoding="utf-8")
    assert "[[assets]]" in raw and 'name = "sfx"' in raw and 'note = "更新"' in raw
    assert {s["name"] for s in client_for(env).get("/api/assets").get_json()["sources"]} == {"source", "inked", "sfx"}


def test_local_config_sources_and_problems(env):
    shared = env["tmp"] / "shared"
    shared.mkdir()
    rgba_sticker(40, 40).save(shared / "a.png")
    env["local"].write_text(
        f'[[assets]]\nname = "共通擬音"\npath = {json.dumps(str(shared))}\nmode = "free"\n\n'
        f'[[assets]]\nname = "sfx"\npath = {json.dumps(str(shared))}\n\n'
        '[[assets]]\nname = "source"\npath = "x"\n\n'
        '[[assets]]\nname = "bad"\npath = "x"\nmode = "weird"\n\n'
        '[[assets]]\nname = "relative"\npath = "shared"\n\n'
        '[[assets]]\nname = "gone"\npath = "no-such-folder"\n', encoding="utf-8")
    assert run_cli_add(env, "--recursive") == 0
    client = client_for(env)
    data = client.get("/api/assets").get_json()
    rows = {s["name"]: s for s in data["sources"]}
    assert list(rows) == ["source", "inked", "sfx", "共通擬音", "relative", "gone"]
    assert rows["sfx"]["origin"] == "project" and rows["sfx"]["count"] == 4            # プロジェクトの定義が優先
    assert rows["共通擬音"] | {"origin": "local", "mode": "free", "count": 1} == rows["共通擬音"]
    assert rows["relative"]["count"] == 1                                              # 相対パスは設定ファイルの場所から
    assert rows["gone"]["missing"] is True and rows["gone"]["count"] == 0
    assert len(data["problems"]) == 2                                                  # 予約名と、使えない mode
    assert client.get("/api/assets/共通擬音/image/a.png").status_code == 200
    assert client.get("/api/assets/gone").get_json()["items"] == []
    assert client.get("/api/assets/bad").status_code == 404


def test_run_without_project_has_builtin_sources_only(env):
    single = create_app(env["images"] / "wide.png", local_config=env["local"], cache_root=env["tmp"] / "cache",
                        watch=False).test_client()
    rows = {s["name"]: s for s in single.get("/api/assets").get_json()["sources"]}
    assert list(rows) == ["source", "inked"] and rows["source"]["count"] == 1 and rows["inked"]["count"] == 0
    assert single.get("/api/assets/source/image/tall.png").status_code == 404           # 1枚だけ開いたときは、その1枚だけ
    assert single.get("/api/assets/source/image/wide.png").status_code == 200


# ---- 元画像と白黒版の対応・更新の検知 ----

def test_source_and_inked_are_paired(env):
    client = client_for(env)
    rows = {i["path"]: i for i in client.get("/api/assets/source").get_json()["items"]}
    inked = rows["wide.png"]["inked"]
    assert rows["wide.png"]["editable"] == "wide.png" and inked["path"] == "wide.png"
    assert inked["version"] == (env["project"].out / "wide.png").stat().st_mtime_ns // 1_000_000
    assert rows["tall.png"]["inked"] is None and rows["wide.png"]["version"] > 0
    assert client.get("/api/assets/inked").get_json()["items"][0]["editable"] == "wide.png"
    pair = client.get("/api/assets/source/pair/wide.png").get_json()
    assert pair == client.get("/api/assets/inked/pair/wide.png").get_json()
    assert pair["name"] == "wide.png" and pair["source"] == {"path": "wide.png"} and pair["inked"] == inked
    assert client.get("/api/assets/source/pair/tall.png").get_json()["inked"] is None
    # 元画像の無い書き出し済み PNG
    synth.save(env["project"].out / "orphan.png", synth.flat(8, 8, 0)[..., 0])
    orphan = client.get("/api/assets/inked/pair/orphan.png").get_json()
    assert orphan["name"] is None and orphan["source"] is None and orphan["inked"]["path"] == "orphan.png"
    assert {i["path"]: i["editable"] for i in client.get("/api/assets/inked").get_json()["items"]} == \
        {"orphan.png": None, "wide.png": "wide.png"}
    for bad in ("/api/assets/source/pair/nope.png", "/api/assets/inked/pair/nope.png",
                "/api/assets/source/pair/../secret.png", "/api/assets/nope/pair/wide.png"):
        assert client.get(bad).status_code in (301, 308, 404), bad
    assert client.get("/api/assets/inked/versions").get_json()["versions"].keys() == {"orphan.png", "wide.png"}
    assert client.get("/api/assets/nope/versions").status_code == 404


def test_browser_never_keeps_a_stale_inked_image(env):
    """白黒版が書き出し直されたら、同じ URL でも新しい中身が返る（ETag が変わり、縮小キャッシュも作り直す）"""
    client = client_for(env)
    target = env["project"].out / "wide.png"
    for url in ("/api/assets/inked/image/wide.png", "/api/assets/inked/image/wide.png?max=100&v=1",
                "/api/assets/inked/thumb/wide.png?h=40"):
        first = client.get(url)
        assert first.headers["Cache-Control"] == "no-cache" and first.headers.get("ETag")
        assert client.get(url, headers={"If-None-Match": first.headers["ETag"]}).status_code == 304
        value = 10 + len(url)
        synth.save(target, synth.flat(400, 100, value)[..., 0])
        st = target.stat()
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 3_000_000_000))
        again = client.get(url, headers={"If-None-Match": first.headers["ETag"]})
        assert again.status_code == 200 and again.headers["ETag"] != first.headers["ETag"], url
        assert image_of(again).convert("L").getpixel((5, 5)) == pytest.approx(value, abs=3)
    versions = client.get("/api/assets/inked/versions").get_json()["versions"]
    assert versions["wide.png"] == target.stat().st_mtime_ns // 1_000_000
