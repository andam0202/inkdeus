"""漫画の構成の画面での複数の画像フォルダ。実物の漫画エディタが要る（無ければ skip）"""

from __future__ import annotations

import json
import urllib.request
from urllib.parse import quote

import pytest

import synth
from conftest import BLANK, serve
from inkdeus import manga
from inkdeus.projects import add_folder, create_project, load_project
from inkdeus.server import create_app
from test_manga_bridge import (  # noqa: F401 — ctx は fixture（この下の manga_env を使う）
    PROBE, WAIT_MS, MangaEnv, apply_template, ctx, find_upstream, geometry, open_manga,
)


@pytest.fixture(scope="module")
def manga_env(tmp_path_factory):
    """既定のフォルダと「二巻」に、同じ名前の画像（scene.png）と書き出し済みの白黒版がある"""
    src = find_upstream()
    if src is None:
        pytest.skip("上流の漫画エディタのクローンがありません（INKDEUS_MANGA_SRC で場所を指定）")
    root = tmp_path_factory.mktemp("manga_folders")
    try:
        manga.install(source=src, root=root / "cache", say=lambda _m: None)
    except manga.MangaError as e:
        pytest.skip(f"上流のクローンを使えません: {e}")
    synth.save(root / "one" / "scene.png", synth.scene(900, 600)[0])
    synth.save(root / "two" / "scene.png", synth.scene(600, 400)[0])
    synth.save(root / "two" / "extra.png", synth.gradient(600, 200))
    project, _ = create_project("book", root / "one", root=root / "projects")
    add_folder(project, "二巻", root / "two")
    project = load_project(str(project.path), root=root / "projects")
    synth.save(project.out / "scene.png", synth.halves(900, 600)[..., 0])
    synth.save(project.out / "@二巻" / "scene.png", synth.halves(600, 400)[..., 0])
    app = create_app(project.source, project.masks, project.out, project=project.info(),
                     local_config=root / "none.toml", cache_root=root / "cache", watch=False)
    server, url = serve(app)
    yield MangaEnv(url, project.path, [])
    server.shutdown()


def call(env, path: str, body: dict | None = None, method: str | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(env.url + path, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def test_images_of_two_folders_side_by_side(ctx, manga_env):  # noqa: F811
    page, status = open_manga(ctx, manga_env)
    editor = None
    try:
        assert status["ok"]
        page.evaluate("() => window.inkdeusBridge.setAutosave({enabled: false})")
        st = page.evaluate("() => window.inkdeusBridge.status()")
        assert st["folders"] == ["default", "二巻"] and st["folder"] == "default"
        # タブはフォルダの数だけ増えない（元画像・白黒は1つずつ）。フォルダは小さな選択欄で切り替える
        assert page.evaluate("() => Array.from(document.querySelectorAll('.ikb-tab')).map(t => t.dataset.source)") == [
            "source", "inked"]
        assert page.evaluate("() => document.querySelector('.ikb-folder-row').hidden") is False
        page.select_option(".ikb-folder", "二巻")
        page.wait_for_function("() => window.inkdeusBridge.status().source === 'source@二巻'"
                               " && document.querySelectorAll('.ikb-item').length === 2")
        assert page.evaluate("() => Array.from(document.querySelectorAll('.ikb-tab')).map(t => t.dataset.source)") == [
            "source@二巻", "inked@二巻"]
        assert sorted(page.evaluate("() => Array.from(document.querySelectorAll('.ikb-item')).map(c => c.dataset.path)")) == [
            "extra.png", "scene.png"]
        apply_template(page)
        panels = page.evaluate("() => window.inkdeusBridge.panels()")
        a = page.evaluate("(g) => window.inkdeusBridge.placeAsset('inked', 'scene.png', {panel: g})", panels[0]["guid"])
        # 「二巻」の元画像のタブから入れる: 白黒版があるので、そのフォルダの白黒版が入る
        page.evaluate("(g) => { const p = canvas.getObjects().find(o => o.guid === g); canvas.setActiveObject(p); }",
                      panels[1]["guid"])
        b = page.evaluate("(g) => window.inkdeusBridge.placeAsset('source@二巻', 'scene.png', {panel: g, inked: 'prefer'})",
                          panels[1]["guid"])
        ga, gb = a["object"]["guid"], b["object"]["guid"]
        pa, pb = page.evaluate(PROBE, ga), page.evaluate(PROBE, gb)
        assert pa["src"] == "/api/assets/inked/image/scene.png?max=2048"                  # 既定のフォルダはこれまでの URL
        assert pb["src"] == f"/api/assets/{quote('inked@二巻', safe='')}/image/scene.png?max=2048"
        assert pa["natural"] == 900 and pb["natural"] == 600 and 60 < pb["mean"] < 200
        # 「二巻」の方を調整すると、そのコマだけが変わる
        call(manga_env, "api/params/scene.png?folder=" + quote("二巻"), BLANK, "PUT")
        editor = ctx.new_page()
        editor.set_default_timeout(WAIT_MS)
        editor.goto(manga_env.url + "?folder=" + quote("二巻") + "&image=scene.png")
        editor.wait_for_function("() => window.inkdeus && window.inkdeus.ready")
        editor.evaluate("() => window.inkdeus.ready")
        assert editor.evaluate("() => [window.inkdeus.folder(), window.inkdeus.info().width]") == ["二巻", 600]
        page.wait_for_function("() => window.inkdeusBridge.status().hot.updated > 0")
        qa, qb = page.evaluate(PROBE, ga), page.evaluate(PROBE, gb)
        assert qb["mean"] > 250 and geometry(qb) == geometry(pb)
        assert qa["mean"] == pa["mean"] and qa["loaded"] == pa["loaded"]
        # 選択中の絵の「元画像に戻す」「白黒を調整」も、そのフォルダの物を指す
        page.evaluate("(g) => { canvas.setActiveObject(canvas.getObjects().find(o => o.guid === g)); canvas.renderAll(); }", gb)
        page.wait_for_function("() => document.querySelector('.ikb-sel-name').textContent.includes('二巻')")
        r = page.evaluate("() => window.inkdeusBridge.swapSelected('source')")
        assert r["src"] == f"/api/assets/{quote('source@二巻', safe='')}/image/scene.png?max=2048"
        assert geometry(page.evaluate(PROBE, gb)) == geometry(pb)
        r = page.evaluate("() => window.inkdeusBridge.swapSelected('inked')")
        assert r["src"] == pb["src"]
        # 保存して開き直しても、それぞれのフォルダの画像を指す
        page_guid = page.evaluate("() => getCanvasGUID()")
        page.evaluate("() => window.inkdeusBridge.save('folders')")
        fresh, status = open_manga(ctx, manga_env)
        try:
            fresh.evaluate("() => window.inkdeusBridge.setAutosave({enabled: false})")
            fresh.evaluate("() => window.inkdeusBridge.open('folders', {force: true})")
            fresh.evaluate("async (g) => { await chengeCanvasByGuid(g); await btmWaitForPageReady(); }", page_guid)
            for guid in (ga, gb):
                fresh.wait_for_function("(g) => !isProjectBusy() && canvas.getObjects().some(o => o.guid === g && o._element"
                                        " && o._element.naturalWidth > 0)", arg=guid)
            assert fresh.evaluate(PROBE, ga)["src"] == pa["src"] and fresh.evaluate(PROBE, gb)["src"] == pb["src"]
            assert fresh.evaluate(PROBE, gb)["mean"] > 250 and fresh.evaluate(PROBE, ga)["natural"] == 900
        finally:
            fresh.close()
    finally:
        if editor is not None:
            editor.close()
        page.close()
