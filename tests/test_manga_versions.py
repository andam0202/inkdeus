"""漫画の構成の画面でのバージョン（同じ元画像から作る、設定の違う白黒版）。実物の漫画エディタが要る
（無ければ skip。test_manga_bridge.py と同じ条件）"""

from __future__ import annotations

import json
import urllib.request
from urllib.parse import quote

import pytest

from conftest import BLANK
from test_manga_bridge import (  # noqa: F401 — manga_env / ctx は fixture
    BLACK, PROBE, WAIT_MS, apply_template, ctx, geometry, manga_env, open_manga,
)

pytestmark = pytest.mark.manga          # 実物の漫画エディタ（上流のクローン）が要る


def call(env, path: str, body: dict | None = None, method: str | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(env.url + path, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def open_editor(ctx, env, image: str, version: str = ""):  # noqa: F811
    page = ctx.new_page()
    page.set_default_timeout(WAIT_MS)
    page.goto(env.url + "?image=" + image + ("&version=" + quote(version) if version else ""))
    page.wait_for_function("() => window.inkdeus && window.inkdeus.ready")
    page.evaluate("() => window.inkdeus.ready")
    return page


def url_of(page, rel: str) -> str:
    return page.evaluate("(r) => window.inkdeusBridge.assetUrl('inked', r, 2048)", rel)


def select(page, guid: str) -> None:
    page.evaluate("(g) => { const o = canvas.getObjects().find(o => o.guid === g); canvas.setActiveObject(o);"
                  " canvas.requestRenderAll(); }", guid)


def wait_src(page, guid: str, src: str) -> None:
    page.wait_for_function("([g, s]) => { const o = canvas.getObjects().find(o => o.guid === g);"
                           " return !!o && o.getSrc() === s; }", arg=[guid, src])


@pytest.fixture(scope="module")
def ver(ctx, manga_env):  # noqa: F811
    """既定（合成の白黒画像）と、名前付きのバージョン「黒」（全面ベタ）を別々のコマへ入れた漫画の画面"""
    call(manga_env, "api/params/scene.png", BLANK, "PUT")
    call(manga_env, "api/versions/scene.png", {"version": "黒"})
    call(manga_env, "api/params/scene.png?version=" + quote("黒"), BLACK, "PUT")
    page, status = open_manga(ctx, manga_env)
    assert status["ok"]
    page.evaluate("() => window.inkdeusBridge.setAutosave({enabled: false})")
    apply_template(page)
    panels = page.evaluate("() => window.inkdeusBridge.panels()")
    editor = open_editor(ctx, manga_env, "scene.png", "黒")           # 開いた時点で「黒」の PNG が書き出される
    assert editor.evaluate("() => window.inkdeus.version()") == "黒"
    page.wait_for_function("() => window.inkdeusBridge.status().inkedFiles.includes('scene@黒.png')")
    a = page.evaluate("(g) => window.inkdeusBridge.placeAsset('inked', 'scene.png', {panel: g})", panels[0]["guid"])
    b = page.evaluate("(g) => window.inkdeusBridge.placeAsset('inked', 'scene@黒.png', {panel: g})", panels[1]["guid"])
    yield {"page": page, "editor": editor, "a": a["object"]["guid"], "b": b["object"]["guid"], "panels": panels}
    editor.close()
    page.close()


def test_each_panel_holds_its_own_version_and_only_that_one_reloads(ver):
    page, editor, a, b = ver["page"], ver["editor"], ver["a"], ver["b"]
    pa, pb = page.evaluate(PROBE, a), page.evaluate(PROBE, b)
    assert pa["src"] == url_of(page, "scene.png") and pb["src"] == url_of(page, "scene@黒.png")
    assert 60 < pa["mean"] < 200 and pb["mean"] < 5
    assert pa["panel"] == ver["panels"][0]["guid"] and pb["panel"] == ver["panels"][1]["guid"]
    # 「黒」を調整すると、「黒」を使っているコマの画素だけが変わる
    before = page.evaluate("() => window.inkdeusBridge.status().hot.updated")
    editor.evaluate("(p) => { window.inkdeus.setParams(p); }", BLANK)
    page.wait_for_function("(n) => window.inkdeusBridge.status().hot.updated > n", arg=before)
    qa, qb = page.evaluate(PROBE, a), page.evaluate(PROBE, b)
    assert qb["mean"] > 250 and geometry(qb) == geometry(pb)
    assert qa["mean"] == pa["mean"] and qa["loaded"] == pa["loaded"] and qa["version"] is None      # 既定の方はそのまま
    # 「白黒」のタブでは、元画像ごとにまとまって横に並び、バージョン名のラベルが付く
    page.click(".ikb-tab:has-text('白黒')")
    page.wait_for_function("() => document.querySelectorAll('.ikb-vgroup[data-base=\"scene.png\"] .ikb-item').length === 2")
    labels = page.evaluate("() => Array.from(document.querySelectorAll('.ikb-vgroup[data-base=\"scene.png\"] .ikb-item'))"
                           ".map(c => [c.dataset.path, c.dataset.version, c.querySelector('.ikb-ver').textContent])")
    assert labels == [["scene.png", "", "既定"], ["scene@黒.png", "黒", "黒"]]


def test_switching_the_selected_image_keeps_place_and_size(ver):
    page, a = ver["page"], ver["a"]
    select(page, a)
    page.wait_for_function("() => document.querySelector('.ikb-sel-ver').textContent === 'バージョン: 既定'"
                           " && !!document.querySelector('.ikb-ver-btn[data-version=\"黒\"]')")
    start = page.evaluate(PROBE, a)
    page.click(".ikb-ver-btn[data-version='黒']")
    wait_src(page, a, url_of(page, "scene@黒.png"))
    after = page.evaluate(PROBE, a)
    assert geometry(after) == geometry(start) and after["mean"] > 250           # 位置・見た目の大きさ・切り抜き・コマはそのまま
    page.wait_for_function("() => document.querySelector('.ikb-sel-ver').textContent === 'バージョン: 黒'")
    # 取り消せる
    page.evaluate("() => { flushHistory(); undo(); }")
    page.wait_for_function("() => !isProjectBusy()")
    wait_src(page, a, url_of(page, "scene.png"))
    assert geometry(page.evaluate(PROBE, a)) == geometry(start)
    # 既定へ切り替える指定・無いバージョン
    select(page, a)
    r = page.evaluate("() => window.inkdeusBridge.switchVersion('')")
    assert r["ok"] and r["changed"] is False
    assert "ありません" in page.evaluate("() => window.inkdeusBridge.switchVersion('nope').then(() => '', e => e.message)")


def test_saved_manga_keeps_the_version_of_each_panel(ctx, ver, manga_env):  # noqa: F811
    page, a, b = ver["page"], ver["a"], ver["b"]
    page_guid = page.evaluate("() => getCanvasGUID()")
    page.evaluate("() => window.inkdeusBridge.save('ver')")
    fresh, status = open_manga(ctx, manga_env)
    try:
        assert status["ok"]
        fresh.evaluate("() => window.inkdeusBridge.setAutosave({enabled: false})")
        fresh.evaluate("() => window.inkdeusBridge.open('ver', {force: true})")
        fresh.evaluate("async (g) => { await chengeCanvasByGuid(g); await btmWaitForPageReady(); }", page_guid)
        for guid in (a, b):
            fresh.wait_for_function("(g) => !isProjectBusy() && canvas.getObjects().some(o => o.guid === g && o._element"
                                    " && o._element.naturalWidth > 0)", arg=guid)
        pa, pb = fresh.evaluate(PROBE, a), fresh.evaluate(PROBE, b)
        assert pa["src"] == url_of(fresh, "scene.png") and 60 < pa["mean"] < 200
        assert pb["src"] == url_of(fresh, "scene@黒.png") and pb["mean"] > 250
    finally:
        fresh.close()


def test_new_version_rename_and_delete(ctx, ver, manga_env):  # noqa: F811
    page, b = ver["page"], ver["b"]
    start = page.evaluate(PROBE, b)
    select(page, b)
    # いま置いてあるバージョンを複製して、置いてある絵を新しいバージョンへ切り替える
    r = page.evaluate("() => window.inkdeusBridge.newVersionAndAdjust('線', {open: false})")
    assert r["ok"] and r["file"] == "scene@線.png" and r["src"] == url_of(page, "scene@線.png")
    after = page.evaluate(PROBE, b)
    assert geometry(after) == geometry(start) and after["mean"] == start["mean"]
    rows = {v["name"]: v for v in call(manga_env, "api/versions/scene.png")["versions"]}
    assert rows["線"]["hasParams"] and rows["線"]["hasExport"]
    assert call(manga_env, "api/params/scene.png?version=" + quote("線")) == call(
        manga_env, "api/params/scene.png?version=" + quote("黒"))
    assert "バージョン名" in page.evaluate(
        "() => window.inkdeusBridge.newVersionAndAdjust('a b', {open: false}).then(() => '', e => e.message)")
    # 名前の変更: 開いているページの絵は新しい名前へ付け替わる
    call(manga_env, "api/versions/scene.png", {"version": "線", "to": "線2"}, "PATCH")
    wait_src(page, b, url_of(page, "scene@線2.png"))
    assert geometry(page.evaluate(PROBE, b)) == geometry(start)
    # 削除: 使っている絵は既定のバージョンへ切り替わり、その旨が出る
    call(manga_env, "api/versions/scene.png?version=" + quote("線2"), method="DELETE")
    wait_src(page, b, url_of(page, "scene.png"))
    gone = page.evaluate(PROBE, b)
    assert geometry(gone) == geometry(start) and 60 < gone["mean"] < 200
    assert "見つからないバージョン" in page.evaluate("() => document.querySelector('.ikb-msg').textContent")
    assert page.evaluate("() => window.inkdeusBridge.status().hot.fallback") >= 1
    # 保存済みの漫画データ（「黒」を指している）を、「黒」を消したあとで開く: 絵は消えず、既定へ切り替わる
    ver["editor"].evaluate("() => window.inkdeus.open('gradient.png')")
    call(manga_env, "api/versions/scene.png?version=" + quote("黒"), method="DELETE")
    page_guid = page.evaluate("() => getCanvasGUID()")
    fresh, status = open_manga(ctx, manga_env)
    try:
        assert status["ok"]
        fresh.evaluate("() => window.inkdeusBridge.setAutosave({enabled: false})")
        fresh.evaluate("() => window.inkdeusBridge.open('ver', {force: true})")
        fresh.evaluate("async (g) => { await chengeCanvasByGuid(g); await btmWaitForPageReady(); }", page_guid)
        wait_src(fresh, b, url_of(fresh, "scene.png"))
        probe = fresh.evaluate(PROBE, b)
        assert probe["clipped"] and probe["panel"] == ver["panels"][1]["guid"] and 60 < probe["mean"] < 200
    finally:
        fresh.close()
