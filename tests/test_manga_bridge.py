"""漫画の構成の結合テスト: 実物の漫画エディタ（上流）に連携スクリプトを差し込み、ヘッドレス Chromium で動かす。

上流は inkdeus のリポジトリに含めないので、取得済みのクローンが必要:
    環境変数 INKDEUS_MANGA_SRC（クローンのフォルダ）。未設定なら ~/ref/manga-editor-desu を探す。
どちらも無い・固定している版と違う場合は skip する。外部ネットワーク（フォント等）には一切つながない。
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import synth
from conftest import serve
from inkdeus import manga
from inkdeus.projects import add_asset_source, create_project
from inkdeus.server import create_app

WAIT_MS = 180_000
# 初回起動の案内（言語選択・チュートリアル）を出さないための、上流側の保存値
SKIP_FIRST_RUN = """
try {
  localStorage.setItem('tutorial_lang_selected', 'true');
  localStorage.setItem('manga_tutorial_state',
    JSON.stringify({quickStartCompleted: true, hintsShown: {}, comfyUIGuideShown: true}));
} catch (e) {}
"""


def find_upstream() -> Path | None:
    for cand in (os.environ.get("INKDEUS_MANGA_SRC"), "~/ref/manga-editor-desu"):
        if cand and (Path(cand).expanduser() / "index.html").is_file():
            return Path(cand).expanduser()
    return None


@dataclass
class MangaEnv:
    url: str
    project: Path
    requests: list


@pytest.fixture(scope="module")
def manga_env(tmp_path_factory):
    src = find_upstream()
    if src is None:
        pytest.skip("上流の漫画エディタのクローンがありません（INKDEUS_MANGA_SRC で場所を指定）")
    root = tmp_path_factory.mktemp("manga")
    try:
        manga.install(source=src, root=root / "cache", say=lambda _m: None)
    except manga.MangaError as e:
        pytest.skip(f"上流のクローンを使えません: {e}")
    images = root / "images"
    synth.save(images / "scene.png", synth.scene(900, 600)[0])
    synth.save(images / "gradient.png", synth.gradient(600, 200))
    sfx = root / "sfx" / "impact"
    sfx.mkdir(parents=True)
    sticker = np.zeros((120, 200, 4), np.uint8)
    sticker[30:90, 40:160] = (255, 60, 0, 255)
    Image.fromarray(sticker, "RGBA").save(sfx / "don.png")
    project, _ = create_project("book", images, root=root / "projects")
    synth.save(project.out / "scene.png", synth.halves(900, 600)[..., 0])          # 書き出し済みの白黒画像
    add_asset_source(project, "sfx", root / "sfx", mode="free", recursive=True)
    app = create_app(project.source, project.masks, project.out, project=project.info(),
                     local_config=root / "none.toml", cache_root=root / "cache", watch=False)
    server, url = serve(app)
    yield MangaEnv(url, project.path, [])
    server.shutdown()


@pytest.fixture(scope="module")
def ctx(browser, manga_env):
    context = browser.new_context(viewport={"width": 1500, "height": 900})
    context.add_init_script(SKIP_FIRST_RUN)

    def local_only(route):
        url = route.request.url
        manga_env.requests.append(url)
        if url.startswith(manga_env.url) or url.startswith(("data:", "blob:")):
            route.continue_()
        else:
            route.abort()                      # 外部（フォント・CDN）は即座に失敗させる。待たない

    context.route("**/*", local_only)
    yield context
    context.close()


def open_manga(ctx, manga_env):
    page = ctx.new_page()
    page.set_default_timeout(WAIT_MS)
    page.goto(manga_env.url + "manga/", wait_until="domcontentloaded")
    page.wait_for_function("() => !!window.inkdeusBridge")
    status = page.evaluate("() => window.inkdeusBridge.ready")       # 上流の初期化完了まで待つ
    return page, status


@pytest.fixture(scope="module")
def mpage(ctx, manga_env):
    page, status = open_manga(ctx, manga_env)
    page.bridge_status = status
    page.evaluate("() => window.inkdeusBridge.setAutosave({enabled: false})")     # 自動保存は専用のテストで確かめる
    yield page
    page.close()


def objects(page) -> list[dict]:
    return page.evaluate("() => window.inkdeusBridge.objects()")


def images_on_canvas(page) -> list[dict]:
    """キャンバス上の画像: 保存される src、読み込めたか、コマとの紐づけ"""
    return page.evaluate("""() => canvas.getObjects().filter(o => o.type === 'image').map(o => ({
        name: o.name, src: o.getSrc(), loaded: !!(o._element && o._element.naturalWidth > 0),
        clipped: !!o.clipPath, panel: o.relatedPoly ? o.relatedPoly.guid : null, guid: o.guid || null}))""")


def apply_template(page) -> None:
    """コマ割りのひな形を1つ適用する（上流の「コマのひな形をクリックしたとき」と同じ呼び出し）"""
    # ひな形のデータは、ひな形の一覧が開かれたときに後から読み込まれる（起動時は縦のひな形が開く）
    page.wait_for_function("() => typeof MangaPanelsImage_Vertical !== 'undefined' && MangaPanelsImage_Vertical.length > 2")
    page.evaluate("""async () => {
        if (btmShouldSaveCurrentPage()) await btmSaveProjectFile();
        setCanvasGUID();
        loadSVGPlusReset(MangaPanelsImage_Vertical[2].svg);
    }""")
    page.wait_for_function("() => window.inkdeusBridge.panels().length >= 2 && !isProjectBusy()")
    page.evaluate("async () => { flushHistory(); await btmSaveProjectFile(null, false); }")


def test_bridge_loads_on_the_rewritten_page(mpage, manga_env):
    status = mpage.bridge_status
    assert status["ok"] is True and status["complete"] is True, status
    assert all(status["features"].values()) and not any(status["missing"].values())
    html = mpage.content()
    assert "/manga-bridge/bridge.js" in html and "/manga-bridge/bridge.css" in html
    assert mpage.evaluate("() => document.querySelector('#inkdeus-bridge .ikb-link').getAttribute('href')") == "/"
    # アクセス解析・service worker・manifest は読み込まれない
    asked = "\n".join(manga_env.requests)
    for gone in ("GoogleAnalytics", "googletagmanager", "google-analytics", "worker-register", "service-worker.js",
                 "manifest.json"):
        assert gone not in asked, gone
    assert mpage.evaluate("async () => (await navigator.serviceWorker.getRegistrations()).length") == 0
    assert mpage.evaluate("() => typeof gtag === 'function' && window.dataLayer.length") == 0
    # 素材パネルにソースのタブと画像が出る
    mpage.wait_for_selector(".ikb-item[data-path='scene.png']")
    tabs = mpage.evaluate("() => Array.from(document.querySelectorAll('.ikb-tab')).map(t => t.textContent)")
    assert [t.rstrip("0123456789") for t in tabs] == ["元画像", "白黒", "sfx"]
    sources = {s["name"]: s for s in mpage.evaluate("() => window.inkdeusBridge.listSources()")["sources"]}
    assert sources["source"]["count"] == 2 and sources["inked"]["count"] == 1 and sources["sfx"]["mode"] == "free"


def test_place_image_into_panel(mpage):
    apply_template(mpage)
    panels = mpage.evaluate("() => window.inkdeusBridge.panels()")
    assert len(panels) >= 2 and all(p["isPanel"] for p in panels)
    # コマが選ばれていなければ、その旨を伝えて何もしない
    with pytest.raises(Exception, match="コマが選ばれていません"):
        mpage.evaluate("() => window.inkdeusBridge.placeAsset('source', 'scene.png')")
    assert images_on_canvas(mpage) == []
    target = panels[0]
    result = mpage.evaluate("(g) => window.inkdeusBridge.placeAsset('source', 'scene.png', {panel: g, max: 1024})",
                            target["guid"])
    assert result["ok"] and result["mode"] == "panel" and result["panel"]["guid"] == target["guid"]
    imgs = images_on_canvas(mpage)
    assert len(imgs) == 1
    img = imgs[0]
    assert img["loaded"] and img["clipped"] and img["panel"] == target["guid"]        # コマに紐づき、コマの形で切り抜かれる
    assert img["src"] == "/api/assets/source/image/scene.png?max=1024"                # data URL ではなく URL（相対）のまま
    owner = mpage.evaluate("(g) => canvas.getObjects().find(o => o.guid === g).guids", target["guid"])
    assert img["guid"] in owner
    # 履歴（保存される状態）にも URL のまま入り、画像の実体は埋め込まれない
    state = mpage.evaluate("""() => { flushHistory(); const s = JSON.parse(stateStack[currentStateIndex]);
        return {srcs: s.objects.filter(o => o.type === 'image').map(o => o.src), embedded: imageMap.size}; }""")
    assert state == {"srcs": ["/api/assets/source/image/scene.png?max=1024"], "embedded": 0}


def test_undo_redo_and_page_switch_restore_the_image(mpage):
    mpage.evaluate("() => undo()")
    mpage.wait_for_function("() => !isProjectBusy() && canvas.getObjects().filter(o => o.type === 'image').length === 0")
    mpage.evaluate("() => redo()")
    mpage.wait_for_function("() => !isProjectBusy() && canvas.getObjects().filter(o => o.type === 'image').length === 1")
    img = images_on_canvas(mpage)[0]
    assert img["loaded"] and img["clipped"] and img["panel"] and img["src"].startswith("/api/assets/")
    # 別のページへ移って戻る
    guids = mpage.evaluate("() => btmGetGuids()")
    assert len(guids) == 2
    here = mpage.evaluate("() => getCanvasGUID()")
    other = [g for g in guids if g != here][0]
    for guid in (other, here):
        mpage.evaluate("""async (g) => { await btmSaveCurrentPage(false); await chengeCanvasByGuid(g);
            await btmWaitForPageReady(); }""", guid)
        mpage.wait_for_function("(g) => !isProjectBusy() && getCanvasGUID() === g", arg=guid)
    mpage.wait_for_function("() => canvas.getObjects().filter(o => o.type === 'image').length === 1")
    img = images_on_canvas(mpage)[0]
    assert img["loaded"] and img["clipped"] and img["panel"]


def test_place_free_asset(mpage):
    before = len(images_on_canvas(mpage))
    result = mpage.evaluate("() => window.inkdeusBridge.placeAsset('sfx', 'impact/don.png')")
    assert result["ok"] and result["mode"] == "free" and result["panel"] is None
    imgs = images_on_canvas(mpage)
    assert len(imgs) == before + 1
    free = imgs[-1]
    assert free["name"] == "don" and free["loaded"] and free["panel"] is None and not free["clipped"]   # コマにはめない
    assert free["src"] == "/api/assets/sfx/image/impact/don.png?max=2048"
    width = mpage.evaluate("() => canvas.getWidth()")
    assert result["object"]["width"] == pytest.approx(width * 0.25, rel=0.01)           # 既定の幅はページ幅の 25%
    assert mpage.evaluate("() => canvas.getActiveObject() && canvas.getActiveObject().name") == "don"


def test_click_thumbnail_puts_image_into_selected_panel(mpage):
    """画面の操作: キャンバス上でコマを押してから、素材パネルのサムネイルを押す"""
    mpage.evaluate("() => canvas.discardActiveObject()")
    second = mpage.evaluate("() => window.inkdeusBridge.panels()[1]")
    spot = mpage.evaluate("""(p) => { const r = canvas.getElement().getBoundingClientRect();
        return {x: r.left + p.left + p.width / 2, y: r.top + p.top + p.height / 2}; }""", second)
    mpage.mouse.click(spot["x"], spot["y"])
    mpage.wait_for_function("(g) => (window.inkdeusBridge.getTargetPanel() || {}).guid === g", arg=second["guid"])
    mpage.click(".ikb-tab:has-text('白黒')")
    mpage.wait_for_selector(".ikb-item[data-path='scene.png']")
    mpage.wait_for_function("() => window.inkdeusBridge.status().source === 'inked'")
    mpage.click(".ikb-item[data-path='scene.png']")
    mpage.wait_for_function(
        "(g) => canvas.getObjects().some(o => o.type === 'image' && o.relatedPoly && o.relatedPoly.guid === g)",
        arg=second["guid"])
    placed = [i for i in images_on_canvas(mpage) if i["panel"] == second["guid"]]
    assert len(placed) == 1 and placed[0]["src"] == "/api/assets/inked/image/scene.png?max=2048"
    assert "コマに入れました" in mpage.evaluate("() => document.querySelector('.ikb-msg').textContent")


def test_save_reload_and_reopen(ctx, mpage, manga_env):
    before = sorted((i["name"], i["src"], i["panel"], i["clipped"]) for i in images_on_canvas(mpage))
    assert len(before) == 3
    page_guid = mpage.evaluate("() => getCanvasGUID()")
    info = mpage.evaluate("() => window.inkdeusBridge.save('第1話')")
    saved = manga_env.project / "manga" / "第1話.lz4"
    assert info["pages"] == 2 and info["path"] == str(saved) and saved.stat().st_size == info["bytes"] > 0
    with pytest.raises(Exception, match="自動保存用"):
        mpage.evaluate("() => window.inkdeusBridge.save('autosave')")
    listed = mpage.evaluate("() => window.inkdeusBridge.listSaved()")
    assert [p["name"] for p in listed] == ["第1話"]

    # ページを読み込み直すと白紙に戻り、「前回の続きを開きますか」と聞かれる
    page, status = open_manga(ctx, manga_env)
    try:
        assert status["ok"]
        page.evaluate("() => window.inkdeusBridge.setAutosave({enabled: false})")
        assert images_on_canvas(page) == []
        resume = page.evaluate("""() => { const r = document.querySelector('.ikb-resume');
            return {hidden: r.hidden, text: r.textContent, name: r.dataset.name}; }""")
        assert resume["hidden"] is False and resume["name"] == "第1話" and "前回の続き" in resume["text"]
        page.click(".ikb-resume button.ikb-primary")
        page.wait_for_function("() => btmGetGuids().length === 2 && !isProjectBusy() && !window.inkdeusBridge.status().busy")
        assert page.evaluate("() => document.querySelector('.ikb-resume').hidden") is True
        page.evaluate("async (g) => { await chengeCanvasByGuid(g); await btmWaitForPageReady(); }", page_guid)
        page.wait_for_function("() => !isProjectBusy() && canvas.getObjects().filter(o => o.type === 'image').length === 3")
        after = images_on_canvas(page)
        assert all(i["loaded"] for i in after)
        assert sorted((i["name"], i["src"], i["panel"], i["clipped"]) for i in after) == before   # 画像とコマが元どおり
        assert len(page.evaluate("() => window.inkdeusBridge.panels()")) >= 2
        assert page.evaluate("() => window.inkdeusBridge.status().name") == "第1話"

        # 書き出し（原稿サイズと DPI から決まる大きさ。テストでは小さくする）
        page.evaluate("() => { document.getElementById('outputDpi').value = 72; }")
        out = page.evaluate("() => window.inkdeusBridge.exportPage()")
        png = manga_env.project / "manga" / "pages" / "第1話" / "002.png"
        assert out["path"] == str(png) and out["number"] == 2
        im = Image.open(io.BytesIO(png.read_bytes()))
        arr = np.asarray(im.convert("RGB")).astype(np.float64)
        assert im.size == (out["width"], out["height"]) and min(im.size) > 300
        assert arr.std() > 20 and len(np.unique(arr.reshape(-1, 3), axis=0)) > 50          # 空ではない（絵が写っている）
        # 全ページ
        results = page.evaluate("() => window.inkdeusBridge.exportAllPages()")
        assert [r["number"] for r in results] == [1, 2]
        assert sorted(p.name for p in png.parent.iterdir()) == ["001.png", "002.png"]
        assert page.evaluate("() => getCanvasGUID()") == page_guid                          # 元のページへ戻る
        listing = page.evaluate("async () => (await fetch('/api/manga/pages/第1話')).json()")
        assert [p["number"] for p in listing["pages"]] == [1, 2]

        # 自動保存: 変更しても、時間が来るまでは保存しない
        auto = manga_env.project / "manga" / "autosave.lz4"
        assert not auto.exists()
        page.evaluate("() => window.inkdeusBridge.setAutosave({enabled: true, seconds: 3600})")
        page.evaluate("() => window.inkdeusBridge.placeAsset('sfx', 'impact/don.png')")
        page.wait_for_function("() => window.inkdeusBridge.status().autosave.dirty === true")
        page.wait_for_timeout(2500)
        assert not auto.exists()
        # ページを切り替えると、その時点で保存する
        other = page.evaluate("(g) => btmGetGuids().find(x => x !== g)", page_guid)
        page.evaluate("""async (g) => { await btmSaveCurrentPage(false); await chengeCanvasByGuid(g);
            await btmWaitForPageReady(); }""", other)
        page.wait_for_function("() => window.inkdeusBridge.status().autosave.lastAt > 0")
        assert auto.stat().st_size > 0
        page.wait_for_function("() => window.inkdeusBridge.status().autosave.dirty === false")
        first_save = auto.stat().st_mtime_ns
        # 変更から一定時間後にも保存する
        page.evaluate("() => window.inkdeusBridge.setAutosave({seconds: 1})")
        page.evaluate("() => window.inkdeusBridge.placeAsset('sfx', 'impact/don.png')")
        page.wait_for_function("(t) => window.inkdeusBridge.status().autosave.lastAt > t",
                               arg=page.evaluate("() => window.inkdeusBridge.status().autosave.lastAt"))
        assert auto.stat().st_mtime_ns > first_save
        rows = {p["name"]: p for p in page.evaluate("() => window.inkdeusBridge.listSaved()")}
        assert rows["autosave"]["autosave"] is True and "第1話" in rows
        # 保存データは小さい（画像の実体を埋め込まず URL で持つため）
        assert auto.stat().st_size < 2_000_000
    finally:
        page.close()


def test_unknown_names_are_reported_not_thrown_into_the_editor(mpage):
    with pytest.raises(Exception, match="読み込めません|404"):
        mpage.evaluate("() => window.inkdeusBridge.placeFree('sfx', 'nope.png')")
    with pytest.raises(Exception, match="選んでください"):
        mpage.evaluate("() => window.inkdeusBridge.open('a.b')")
    assert mpage.evaluate("() => window.inkdeusBridge.status().busy") is False
    assert mpage.evaluate("() => typeof canvas.getObjects().length") == "number"             # 漫画エディタは動き続ける
