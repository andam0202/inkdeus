"""漫画の構成の結合テスト: 実物の漫画エディタ（上流）に連携スクリプトを差し込み、ヘッドレス Chromium で動かす。

上流は inkdeus のリポジトリに含めないので、取得済みのクローンが必要:
    環境変数 INKDEUS_MANGA_SRC（クローンのフォルダ）。未設定なら ~/ref/manga-editor-desu を探す。
どちらも無い・固定している版と違う場合は skip する。外部ネットワーク（フォント等）には一切つながない。
"""

from __future__ import annotations

import io
import json
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
    font: str | None = None        # 合成したローカルフォントの表示名（fontTools が無ければ None）


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
    font = None
    try:
        from synth_font import make_font

        # 全部の文字が塗りつぶしの四角になるフォント（画素で見分けられる）。共通設定の [fonts] で登録する
        make_font(root / "fonts" / "InkBox.ttf", "Ink Box", japanese="インク箱")
        (root / "none.toml").write_text(f"[fonts]\ndirs = [{json.dumps(str(root / 'fonts'))}]\n", encoding="utf-8")
        font = "インク箱"
    except ImportError:
        pass
    server, url = serve(app)
    yield MangaEnv(url, project.path, [], font)
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
    # コマの guid は、上流がレイヤー一覧を作り直すときに後から付く。付く前に読むと「コマの指定なし」になってしまうので待つ
    page.wait_for_function("() => { const p = window.inkdeusBridge.panels();"
                           " return p.length >= 2 && p.every((x) => !!x.guid) && !isProjectBusy(); }")
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


# ---- 白黒化の画面（別のタブ）で調整 → 漫画の画面へホットリロード ----

from conftest import BLANK, deep_merge  # noqa: E402

BLACK = deep_merge(BLANK, {"solid": {"enabled": True, "threshold": 1.0}})

PROBE = """(g) => { const o = canvas.getObjects().find(o => o.guid === g);
    if (!o) return null;
    const c = document.createElement('canvas'); c.width = 32; c.height = 32;
    const x = c.getContext('2d'); x.drawImage(o._element, 0, 0, 32, 32);
    const d = x.getImageData(0, 0, 32, 32).data; let s = 0;
    for (let i = 0; i < d.length; i += 4) s += d[i];
    return {mean: s / (d.length / 4), src: o.getSrc(), loaded: o._element.currentSrc || o._element.src,
        left: o.left, top: o.top, angle: o.angle, shownW: o.width * o.scaleX, shownH: o.height * o.scaleY,
        natural: o._element.naturalWidth, clipped: !!o.clipPath, panel: o.relatedPoly ? o.relatedPoly.guid : null,
        index: canvas.getObjects().indexOf(o), version: o.__ikbVersion || null}; }"""


def geometry(probe: dict) -> tuple:
    return (round(probe["left"], 3), round(probe["top"], 3), probe["angle"], round(probe["shownW"], 3),
            round(probe["shownH"], 3), probe["clipped"], probe["panel"], probe["index"])


def open_editor(ctx, manga_env, image: str):
    page = ctx.new_page()
    page.set_default_timeout(WAIT_MS)
    page.goto(manga_env.url + "?image=" + image)
    page.wait_for_function("() => window.inkdeus && window.inkdeus.ready")
    page.evaluate("() => window.inkdeus.ready")
    return page


def set_and_wait_hot(editor, page, params: dict) -> None:
    """白黒化の画面で設定を変え、自動書き出し → 漫画の画面での差し替えが済むまで待つ"""
    before = page.evaluate("() => window.inkdeusBridge.status().hot.updated")
    editor.evaluate("(p) => { window.inkdeus.setParams(p); }", params)
    page.wait_for_function("(n) => window.inkdeusBridge.status().hot.updated > n", arg=before)


@pytest.fixture(scope="module")
def hot(ctx, manga_env):
    """ホットリロードの検証用: 白黒版を1枚コマへ入れた漫画の画面と、白黒化の画面（別のタブ）"""
    page, status = open_manga(ctx, manga_env)
    assert status["ok"]
    page.evaluate("() => window.inkdeusBridge.setAutosave({enabled: false})")
    apply_template(page)
    panels = page.evaluate("() => window.inkdeusBridge.panels()")
    placed = page.evaluate("(g) => window.inkdeusBridge.placeAsset('inked', 'scene.png', {panel: g})", panels[0]["guid"])
    guid = placed["object"]["guid"]
    assert page.evaluate("() => window.inkdeusBridge.status().events.connected") is True
    editor = open_editor(ctx, manga_env, "scene.png")
    yield {"page": page, "editor": editor, "guid": guid, "panels": panels}
    editor.close()
    page.close()


def test_adjusting_in_the_editor_tab_updates_the_manga_canvas(hot, manga_env):
    page, editor, guid = hot["page"], hot["editor"], hot["guid"]
    assert editor.evaluate("() => window.inkdeus.info().name") == "scene.png"
    start = page.evaluate(PROBE, guid)
    set_and_wait_hot(editor, page, BLANK)                             # 何も描かない設定 → 真っ白
    white = page.evaluate(PROBE, guid)
    assert white["mean"] > 250
    history = page.evaluate("() => [stateStack.length, currentStateIndex]")
    set_and_wait_hot(editor, page, BLACK)                             # 全面ベタ → 真っ黒
    black = page.evaluate(PROBE, guid)
    assert black["mean"] < 5                                          # 画素が新しい内容に替わっている
    assert geometry(black) == geometry(white) == geometry(start)      # 位置・大きさ・切り抜き・コマ・重なり順はそのまま
    assert black["src"] == "/api/assets/inked/image/scene.png?max=2048"          # 保存される src に v は付けない
    assert "v=" in black["loaded"] and black["version"]                           # 読み込みは v つき（古い物を使い回さない）
    assert "scene.png を更新しました" in page.evaluate("() => document.querySelector('.ikb-msg').textContent")
    # 取り消しの履歴は汚さない
    page.wait_for_timeout(800)
    assert page.evaluate("() => { flushHistory(); return [stateStack.length, currentStateIndex]; }") == history
    assert "v=" not in page.evaluate("() => stateStack[currentStateIndex]")
    # 素材パネルの「白黒」のサムネイルも新しい版になる
    page.click(".ikb-tab:has-text('白黒')")
    page.wait_for_function("""(v) => { const i = document.querySelector(".ikb-item[data-path='scene.png'] img");
        return i && i.getAttribute('src').includes('v=' + v); }""", arg=black["version"])
    assert page.evaluate("() => window.inkdeusBridge.status().events.received") >= 2


def test_undo_does_not_bring_back_the_old_image(hot):
    page, guid = hot["page"], hot["guid"]
    latest = page.evaluate(PROBE, guid)["version"]
    page.evaluate("() => window.inkdeusBridge.placeAsset('sfx', 'impact/don.png')")
    page.wait_for_function("() => canvas.getObjects().filter(o => o.type === 'image').length === 2")
    page.evaluate("() => { flushHistory(); undo(); }")
    page.wait_for_function("() => !isProjectBusy() && canvas.getObjects().filter(o => o.type === 'image').length === 1")
    # 取り消しでオブジェクトは作り直されるが、白黒版は最新の物が出る
    page.wait_for_function("([g, v]) => { const o = canvas.getObjects().find(o => o.guid === g);"
                           " return o && o.__ikbVersion === v; }", arg=[guid, latest])
    probe = page.evaluate(PROBE, guid)
    assert probe["mean"] < 5 and probe["clipped"] and probe["panel"] == hot["panels"][0]["guid"]
    assert probe["src"] == "/api/assets/inked/image/scene.png?max=2048"
    page.evaluate("() => redo()")
    page.wait_for_function("() => !isProjectBusy() && canvas.getObjects().filter(o => o.type === 'image').length === 2")
    page.wait_for_function("([g, v]) => canvas.getObjects().find(o => o.guid === g).__ikbVersion === v", arg=[guid, latest])
    assert page.evaluate(PROBE, guid)["mean"] < 5


def test_saved_manga_shows_the_latest_inked_image_when_reopened(ctx, hot, manga_env):
    page, guid = hot["page"], hot["guid"]
    page_guid = page.evaluate("() => getCanvasGUID()")
    page.evaluate("() => window.inkdeusBridge.save('hot')")
    fresh, status = open_manga(ctx, manga_env)
    try:
        assert status["ok"]
        fresh.evaluate("() => window.inkdeusBridge.setAutosave({enabled: false})")
        fresh.evaluate("() => window.inkdeusBridge.open('hot', {force: true})")
        fresh.evaluate("async (g) => { await chengeCanvasByGuid(g); await btmWaitForPageReady(); }", page_guid)
        fresh.wait_for_function("(g) => !isProjectBusy() && canvas.getObjects().some(o => o.guid === g && o._element"
                                " && o._element.naturalWidth > 0)", arg=guid)
        probe = fresh.evaluate(PROBE, guid)
        assert probe["mean"] < 5 and probe["src"] == "/api/assets/inked/image/scene.png?max=2048"
        # 別のページを開いている間に書き出し直されても、戻ったときに最新が出る
        other = fresh.evaluate("(g) => btmGetGuids().find(x => x !== g)", page_guid)
        fresh.evaluate("async (g) => { await btmSaveCurrentPage(false); await chengeCanvasByGuid(g);"
                       " await btmWaitForPageReady(); }", other)
        before = fresh.evaluate("() => window.inkdeusBridge.status().events.received")
        hot["editor"].evaluate("(p) => { window.inkdeus.setParams(p); }", BLANK)
        fresh.wait_for_function("(n) => window.inkdeusBridge.status().events.received > n", arg=before)
        fresh.evaluate("async (g) => { await btmSaveCurrentPage(false); await chengeCanvasByGuid(g);"
                       " await btmWaitForPageReady(); }", page_guid)
        fresh.wait_for_function("(g) => { const o = canvas.getObjects().find(o => o.guid === g);"
                                " return o && !!o.__ikbVersion; }", arg=guid)
        assert fresh.evaluate(PROBE, guid)["mean"] > 250
    finally:
        fresh.close()
    hot["page"].wait_for_function("(g) => canvas.getObjects().find(o => o.guid === g) !== undefined", arg=guid)


def test_reconnect_picks_up_missed_exports_and_keeps_the_shown_size(hot, manga_env):
    """接続が切れている間に白黒版が替わっても、つながり直したときに読み直す。
    画素数の違う画像に替わっても、見た目の大きさは変えない"""
    page, guid = hot["page"], hot["guid"]
    page.wait_for_function("(g) => { const o = canvas.getObjects().find(o => o.guid === g); return o && o._element; }",
                           arg=guid)
    before = page.evaluate(PROBE, guid)
    assert before["natural"] == 900
    png = manga_env.project / "out" / "scene.png"
    synth.save(png, synth.flat(450, 300, 128)[..., 0])                # 通知なしで、半分の大きさの灰色に差し替える
    page.evaluate("() => window.inkdeusBridge.reconnectEvents()")
    page.wait_for_function("(g) => canvas.getObjects().find(o => o.guid === g)._element.naturalWidth === 450", arg=guid)
    after = page.evaluate(PROBE, guid)
    assert 120 < after["mean"] < 136
    assert geometry(after) == geometry(before)                        # 拡大率を直して、見た目の大きさと位置を保つ
    assert page.evaluate("() => window.inkdeusBridge.status().events.connected") is True


def test_swap_between_inked_and_source(hot):
    page, guid = hot["page"], hot["guid"]
    page.evaluate("(g) => { canvas.setActiveObject(canvas.getObjects().find(o => o.guid === g)); canvas.renderAll(); }", guid)
    page.wait_for_function("() => !document.querySelector('.ikb-sel').hidden")
    assert "選択中の画像: scene（白黒版）" in page.evaluate("() => document.querySelector('.ikb-sel-name').textContent")
    # 「新しいバージョンを作って調整」は、元画像との対応を問い合わせたあとに出る。出そろうのを待ってから確かめる
    page.wait_for_function("() => Array.from(document.querySelectorAll('.ikb-sel button')).filter(b => !b.hidden)"
                           ".map(b => b.textContent).join() === '白黒を調整,元画像に戻す,新しいバージョンを作って調整'")
    before = page.evaluate(PROBE, guid)
    depth = page.evaluate("() => { flushHistory(); return stateStack.length; }")
    page.click(".ikb-sel button:has-text('元画像に戻す')")
    page.wait_for_function("(g) => canvas.getObjects().find(o => o.guid === g).getSrc().includes('/source/')", arg=guid)
    source = page.evaluate(PROBE, guid)
    assert source["src"] == "/api/assets/source/image/scene.png?max=2048" and source["natural"] == 900
    assert geometry(source) == geometry(before)                       # 画素数が違っても（450 → 900）見た目は同じ
    page.wait_for_function("(n) => { flushHistory(); return stateStack.length === n + 1; }", arg=depth)   # 取り消せる
    page.wait_for_function("() => Array.from(document.querySelectorAll('.ikb-sel button')).filter(b => !b.hidden)"
                           ".map(b => b.textContent).join() === '白黒を調整,白黒版に差し替え'")
    result = page.evaluate("() => window.inkdeusBridge.swapSelected('inked')")
    assert result["changed"] and result["src"] == "/api/assets/inked/image/scene.png?max=2048"
    assert geometry(page.evaluate(PROBE, guid)) == geometry(before)
    # 白黒版がまだ無い画像は、その旨を伝える
    placed = page.evaluate("(g) => window.inkdeusBridge.placeAsset('source', 'gradient.png', {panel: g})",
                           hot["panels"][1]["guid"])
    hot["gradient"] = placed["object"]["guid"]
    with pytest.raises(Exception, match="白黒版がまだありません"):
        page.evaluate("() => window.inkdeusBridge.swapSelected('inked')")
    assert page.evaluate(PROBE, hot["gradient"])["src"].startswith("/api/assets/source/")


def test_source_is_replaced_when_its_inked_version_appears(ctx, hot, manga_env):
    page = hot["page"]
    if "gradient" not in hot:
        pytest.skip("前のテストで元画像を置けていません")
    assert page.evaluate("() => document.getElementById('ikb-autoswap').checked") is False       # 既定はオフ
    page.click(".ikb-tab:has-text('元画像')")
    page.wait_for_selector(".ikb-item[data-path='gradient.png']")
    page.evaluate("() => document.getElementById('ikb-autoswap').click()")
    assert page.evaluate("() => window.inkdeusBridge.status().autoSwap") is True
    before = page.evaluate(PROBE, hot["gradient"])
    # 「調整」で白黒化の画面を開くと、自動書き出しで白黒版ができて、置いてある元画像が切り替わる
    with ctx.expect_page() as opened:
        page.click(".ikb-item[data-path='gradient.png'] .ikb-adjust")
    editor = opened.value
    try:
        editor.wait_for_function("() => window.inkdeus && window.inkdeus.ready")
        assert editor.url.endswith("/?image=gradient.png")
        assert editor.evaluate("async () => { await window.inkdeus.ready; return window.inkdeus.info().name; }") == "gradient.png"
        page.wait_for_function("(g) => canvas.getObjects().find(o => o.guid === g).getSrc().includes('/inked/')",
                               arg=hot["gradient"])
        after = page.evaluate(PROBE, hot["gradient"])
        assert after["src"] == "/api/assets/inked/image/gradient.png?max=2048"
        assert geometry(after) == geometry(before)
        assert (manga_env.project / "out" / "gradient.png").is_file()
        # 「調整」をもう一度押しても、同じタブを使い回す（画像だけ切り替わる）
        count = len(ctx.pages)
        page.wait_for_selector(".ikb-item[data-path='scene.png'] .ikb-adjust")
        page.click(".ikb-item[data-path='scene.png'] .ikb-adjust")
        editor.wait_for_url("**/?image=scene.png")
        assert len(ctx.pages) == count
    finally:
        editor.close()
        page.evaluate("() => window.inkdeusBridge.setAutoSwap(false)")


def test_prefer_inked_when_placing_from_the_source_tab(hot):
    page = hot["page"]
    page.click(".ikb-tab:has-text('元画像')")
    page.wait_for_selector(".ikb-item[data-path='scene.png'].ikb-has-inked")
    assert page.evaluate("() => document.querySelector('.ikb-prefer').value") == "1"             # 既定は「白黒版があれば白黒」
    assert page.evaluate("() => document.querySelector('.ikb-prefer-row').hidden") is False
    target = hot["panels"][1]
    page.evaluate("(g) => { canvas.setActiveObject(canvas.getObjects().find(o => o.guid === g)); }", target["guid"])

    def placed_srcs():
        return [i["src"] for i in images_on_canvas(page) if i["panel"] == target["guid"]]

    count = len(placed_srcs())
    page.click(".ikb-item[data-path='scene.png']")
    page.wait_for_function("(n) => canvas.getObjects().filter(o => o.type === 'image').length > n",
                           arg=len(images_on_canvas(page)))
    assert placed_srcs()[-1] == "/api/assets/inked/image/scene.png?max=2048" and len(placed_srcs()) == count + 1
    page.select_option(".ikb-prefer", "0")                            # 常に元画像
    page.evaluate("(g) => { canvas.setActiveObject(canvas.getObjects().find(o => o.guid === g)); }", target["guid"])
    total = len(images_on_canvas(page))
    page.click(".ikb-item[data-path='scene.png']")
    page.wait_for_function("(n) => canvas.getObjects().filter(o => o.type === 'image').length > n", arg=total)
    assert placed_srcs()[-1] == "/api/assets/source/image/scene.png?max=2048"
    page.select_option(".ikb-prefer", "1")
    page.click(".ikb-tab:has-text('白黒')")
    page.wait_for_function("() => document.querySelector('.ikb-prefer-row').hidden === true")


# ---- ローカルフォント ----

DARK = """() => { const c = canvas.toCanvasElement(1); const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
    let n = 0; for (let i = 0; i < d.length; i += 4) if (d[i] < 60 && d[i + 3] > 200) n += 1; return n; }"""


def test_local_font_is_listed_applied_saved_and_exported(ctx, manga_env):
    if manga_env.font is None:
        pytest.skip("fontTools がありません（テスト用のフォントを合成できません）")
    name = manga_env.font
    page, status = open_manga(ctx, manga_env)
    try:
        assert status["ok"]
        page.evaluate("() => window.inkdeusBridge.setAutosave({enabled: false})")
        fonts = page.evaluate("() => window.inkdeusBridge.status().fonts")
        assert fonts["names"] == [name] and fonts["loaded"] == [] and fonts["supported"] is True
        assert "フォント: 1 個" in page.evaluate("() => document.querySelector('.ikb-font-info').textContent")
        # フォント選択に「inkdeus」の見出しでまとまって出る
        titles = page.evaluate("""() => Array.from(document.querySelectorAll('#fm-fontDropdown-fontSelector .fm-category-title'))
            .map(t => t.textContent)""")
        assert titles[0] == "inkdeus"
        option = f"#fm-fontDropdown-fontSelector .fm-font-option[data-font='{name}']"
        assert page.evaluate("(s) => document.querySelectorAll(s).length", option) == 1
        # 一覧を開いただけでは本体を読みに行かない（名前だけが並ぶ）
        page.evaluate("() => document.querySelector('#fontSelector .fm-dropdown-trigger').click()")
        assert page.evaluate("() => document.getElementById('fm-fontDropdown-fontSelector').classList.contains('fm-show')")
        page.wait_for_timeout(500)
        assert not any("/api/fonts/file/" in u for u in manga_env.requests)
        page.evaluate("() => FontSelectorManager.closeAllDropdowns()")

        page.evaluate("() => createTextbox()")                         # 横書きの文字（既定の書体）
        page.evaluate("""() => { const o = canvas.getActiveObject();
            o.set({text: 'AAAA', fontSize: 60, fill: '#000000', backgroundColor: '', strokeWidth: 0});
            canvas.renderAll(); }""")
        before = page.evaluate(DARK)
        page.evaluate("(s) => document.querySelector(s).click()", option)   # フォント選択から適用する
        page.wait_for_function("(n) => window.inkdeusBridge.status().fonts.loaded.includes(n)", arg=name)
        assert page.evaluate("() => canvas.getActiveObject().fontFamily") == name
        assert any("/api/fonts/file/" in u for u in manga_env.requests)   # 使ったときに初めて本体を取得する
        page.wait_for_function(f"(b) => ({DARK})() > b * 2", arg=before)  # 画素が変わる（四角で塗りつぶされる）
        applied = page.evaluate(DARK)
        # 縦書きにも適用できる
        page.evaluate("() => document.getElementById('verticalText').click()")
        vertical = page.evaluate("() => ({type: canvas.getActiveObject().type, font: canvas.getActiveObject().fontFamily})")
        assert vertical["font"] == name
        page.evaluate("() => { canvas.getActiveObject().set({text: 'あい', fontSize: 60, fill: '#000000', strokeWidth: 0}); canvas.renderAll(); }")
        page.wait_for_function(f"(b) => ({DARK})() > b * 1.2", arg=applied)
        both = page.evaluate(DARK)
        # 漫画データには「どこから取れるか」が相対 URL で記録される
        recorded = page.evaluate("() => buildProjectFontData()")
        assert {"name": name, "type": "web", "url": f"/api/fonts/css?family={name}"} in recorded
        page_guid = page.evaluate("() => getCanvasGUID()")
        page.evaluate("() => { canvas.discardActiveObject(); canvas.renderAll(); }")
        page.evaluate("() => window.inkdeusBridge.save('fonts')")
    finally:
        page.close()

    fresh, status = open_manga(ctx, manga_env)                         # 開き直しても、そのフォントで表示される
    try:
        fresh.evaluate("() => window.inkdeusBridge.setAutosave({enabled: false})")
        fresh.evaluate("() => window.inkdeusBridge.open('fonts', {force: true})")
        fresh.evaluate("async (g) => { if (getCanvasGUID() !== g) { await chengeCanvasByGuid(g); await btmWaitForPageReady(); } }",
                       page_guid)
        fresh.wait_for_function("(n) => window.inkdeusBridge.status().fonts.loaded.includes(n)", arg=name)
        fresh.wait_for_function(f"([lo, hi]) => {{ const n = ({DARK})(); return n > lo && n < hi; }}",
                                arg=[both * 0.85, both * 1.15])
        assert sorted(o["type"] for o in objects(fresh)) == sorted(["textbox", vertical["type"]])
        # ページの書き出しにも、そのフォントで描かれる
        fresh.evaluate("() => { document.getElementById('outputDpi').value = 72; }")
        out = fresh.evaluate("() => window.inkdeusBridge.exportPage({name: 'fonts'})")
        im = np.asarray(Image.open(out["path"]).convert("L"))
        scale = out["width"] / fresh.evaluate("() => canvas.getWidth()")
        dark = int((im < 60).sum())
        assert dark == pytest.approx(both * scale * scale, rel=0.2)
    finally:
        fresh.close()
