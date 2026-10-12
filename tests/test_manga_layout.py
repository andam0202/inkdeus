"""コマ割りツールの結合テスト: 実物の漫画エディタ（上流）に連携スクリプトを差し込んで動かす。

上流への差分を当てた導入と、当てていない導入の両方で確かめる（コマ割りツールは差分なしでも動く）。
"""

from __future__ import annotations

import pytest

import synth
from conftest import serve
from inkdeus import manga
from inkdeus.projects import create_project
from inkdeus.server import create_app
from test_manga_bridge import SKIP_FIRST_RUN, WAIT_MS, MangaEnv, find_upstream, images_on_canvas

PANELS = """() => canvas.getObjects().filter(o => o.isPanel).map(p => ({guid: p.guid, cell: p.ikCell || null, name: p.name,
    selectable: p.selectable, evented: p.evented, stroke: p.strokeWidth,
    pts: getAbsolutePolygonPoints(p).map(q => [q.x, q.y])}))"""
EXPECTED = """() => { const L = canvas.ikLayout; if (!L) return null;
    const c = window.inkdeusBridge.layout.core.compute(L, canvas.getWidth(), canvas.getHeight());
    const out = {}; Object.values(c.cells).forEach(x => { out[x.guid] = x.pts; }); return out; }"""


@pytest.fixture(scope="module", params=["patched", "plain"])
def env(request, tmp_path_factory):
    src = find_upstream()
    if src is None:
        pytest.skip("上流の漫画エディタのクローンがありません（INKDEUS_MANGA_SRC で場所を指定）")
    root = tmp_path_factory.mktemp("layout-" + request.param)
    try:
        info = manga.install(source=src, root=root / "cache", say=lambda _m: None, patches=request.param == "patched")
    except manga.MangaError as e:
        pytest.skip(f"上流のクローンを使えません: {e}")
    images = root / "images"
    synth.save(images / "scene.png", synth.scene(900, 600)[0])
    synth.save(images / "gradient.png", synth.gradient(600, 200))
    project, _ = create_project("book", images, root=root / "projects")
    app = create_app(project.source, project.masks, project.out, project=project.info(),
                     local_config=root / "none.toml", cache_root=root / "cache", watch=False)
    server, url = serve(app)
    made = MangaEnv(url, project.path, [])
    made.patches = info["patches"]
    made.variant = request.param
    yield made
    server.shutdown()


@pytest.fixture(scope="module")
def lctx(browser, env):
    context = browser.new_context(viewport={"width": 1500, "height": 900})
    context.add_init_script(SKIP_FIRST_RUN)

    def local_only(route):
        url = route.request.url
        if url.startswith(env.url) or url.startswith(("data:", "blob:")):
            route.continue_()
        else:
            route.abort()

    context.route("**/*", local_only)
    yield context
    context.close()


def open_page(lctx, env):
    page = lctx.new_page()
    page.set_default_timeout(WAIT_MS)
    page.goto(env.url + "manga/", wait_until="domcontentloaded")
    page.wait_for_function("() => !!window.inkdeusBridge")
    status = page.evaluate("() => window.inkdeusBridge.ready")
    assert status["ok"], status
    page.wait_for_function("() => !!(window.inkdeusBridge.layout && window.inkdeusBridge.layout.supported)")
    page.evaluate("() => { window.inkdeusBridge.setAutosave({enabled: false}); window.inkdeusBridge.setOpen(true); }")
    return page


@pytest.fixture()
def lay(lctx, env):
    """白紙のページを開いた /manga/"""
    page = open_page(lctx, env)
    yield page
    page.close()


def open_section(page) -> None:
    """素材パネルの「コマ割り」の欄を開く（開閉は覚えられているので、押すのではなく開いた状態にする）"""
    page.evaluate("() => { document.getElementById('ikl').open = true; }")
    page.wait_for_selector("#ikl-grid", state="visible")


def near(a, b, tol: float = 0.6) -> bool:
    def flat(v):
        if isinstance(v, dict):
            return [x for k in sorted(v) for x in flat(v[k])]
        if isinstance(v, (list, tuple)):
            return [x for i in v for x in flat(i)]
        return [v]
    return flat(a) == pytest.approx(flat(b), abs=tol)


def panels(page) -> list[dict]:
    return page.evaluate(PANELS)


def matches_layout(page) -> bool:
    """コマの頂点が、レイアウトからの計算と一致しているか"""
    expected = page.evaluate(EXPECTED)
    found = {p["guid"]: p["pts"] for p in panels(page) if p["cell"]}
    return expected is not None and set(expected) == set(found) and all(near(found[g], expected[g]) for g in expected)


def grid(page, counts: str, **opts) -> dict:
    return page.evaluate("([c, o]) => window.inkdeusBridge.layout.grid(c, Object.assign({force: true}, o))", [counts, opts])


def info(page) -> dict:
    return page.evaluate("() => window.inkdeusBridge.layout.get()")


def undo(page, what: str = "undo") -> None:
    page.evaluate(f"() => {what}()")
    page.wait_for_function("() => !isHistoryRestoreInProgress() && !isProjectBusy()")
    page.wait_for_timeout(50)


def boxes(cells: list[dict]) -> dict:
    return {c["number"]: (c["rect"]["x"], c["rect"]["y"], c["rect"]["x"] + c["rect"]["w"], c["rect"]["y"] + c["rect"]["h"])
            for c in cells}


def test_grid_creates_panels_that_match_the_layout(lay, env):
    assert panels(lay) == [] and info(lay)["layout"] is None
    depth = lay.evaluate("() => { flushHistory(); return stateStack.length; }")
    made = grid(lay, "2,1,3")
    assert [c["number"] for c in made["cells"]] == [1, 2, 3, 4, 5, 6] and made["unmanaged"] == 0
    found = panels(lay)
    assert len(found) == 6 and all(p["cell"] and p["guid"] and p["name"] for p in found)
    assert len({p["name"] for p in found}) == 6 and not any(p["selectable"] for p in found)   # ひな形のコマと同じくロックされて生まれる
    assert matches_layout(lay)
    # 読む順: 上の行の右から
    b = boxes(made["cells"])
    assert b[1][1] == pytest.approx(b[2][1]) and b[1][0] > b[2][0] and b[3][1] > b[1][3] and b[4][0] > b[5][0] > b[6][0]
    # 取り消し 1 回で作る前に戻り、やり直しで戻る
    assert lay.evaluate("() => stateStack.length") == depth + 1
    undo(lay)
    assert panels(lay) == [] and info(lay)["layout"] is None
    undo(lay, "redo")
    assert len(panels(lay)) == 6 and matches_layout(lay) and len(info(lay)["cells"]) == 6
    # レイヤー一覧にコマが並ぶ。目印（境界線など）はキャンバスのオブジェクトにならない
    lay.evaluate("() => window.inkdeusBridge.layout.enter()")
    assert lay.evaluate("() => canvas.getObjects().filter(o => !o.isInitMessage).length") == 6
    assert lay.evaluate("() => JSON.parse(JSON.stringify(customToJSON())).objects.length") == 6
    lay.evaluate("() => window.inkdeusBridge.layout.exit()")
    assert env.variant in ("patched", "plain")


def place(page, guid: str, rel: str = "scene.png") -> dict:
    result = page.evaluate("([g, r]) => window.inkdeusBridge.placeAsset('source', r, {panel: g, max: 1024})", [guid, rel])
    assert result["ok"]
    return result["object"]


def linked(page) -> list[dict]:
    """キャンバス上の画像: どのコマに入っているか、中心、見た目の大きさ、切り抜きの外接矩形"""
    return page.evaluate("""() => canvas.getObjects().filter(o => o.type === 'image').map(o => {
        const c = o.getCenterPoint(); const clip = o.clipPath ? o.clipPath.getBoundingRect(true, true) : null;
        return {guid: o.guid, panel: o.relatedPoly ? o.relatedPoly.guid : null, cx: c.x, cy: c.y,
                w: o.getScaledWidth(), h: o.getScaledHeight(), left: o.left, top: o.top, scaleX: o.scaleX,
                index: canvas.getObjects().indexOf(o),
                clip: clip ? [clip.left, clip.top, clip.left + clip.width, clip.top + clip.height] : null}; })""")


def panel_box(page, guid: str) -> tuple:
    pts = next(p["pts"] for p in panels(page) if p["guid"] == guid)
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def assert_covers(page, image: dict, guid: str) -> None:
    """画像がコマを覆い、コマの形で切り抜かれている"""
    l, t, r, b = panel_box(page, guid)
    assert image["panel"] == guid
    assert image["cx"] - image["w"] / 2 <= l + 1 and image["cx"] + image["w"] / 2 >= r - 1
    assert image["cy"] - image["h"] / 2 <= t + 1 and image["cy"] + image["h"] / 2 >= b - 1
    stroke = next(p["stroke"] for p in panels(page) if p["guid"] == guid)
    assert near(image["clip"], (l, t, r, b), tol=stroke + 1.5)


def test_replacing_the_grid_carries_images_over_in_reading_order(lay):
    first = grid(lay, "2,1")
    a = place(lay, first["cells"][0]["guid"])                       # 1 番（右上）
    b = place(lay, first["cells"][2]["guid"], "gradient.png")       # 3 番（下）
    depth = lay.evaluate("() => { flushHistory(); return stateStack.length; }")
    second = grid(lay, "1,1,1")
    assert len(panels(lay)) == 3 and matches_layout(lay) and lay.evaluate("() => stateStack.length") == depth + 1
    images = {i["guid"]: i for i in linked(lay)}
    assert len(images) == 2
    assert_covers(lay, images[a["guid"]], second["cells"][0]["guid"])          # 1 番 → 新しい 1 番
    assert_covers(lay, images[b["guid"]], second["cells"][2]["guid"])          # 3 番 → 新しい 3 番
    for image in images.values():                                              # 中央に置き直す
        l, t, r, bb = panel_box(lay, image["panel"])
        assert near((image["cx"], image["cy"]), ((l + r) / 2, (t + bb) / 2))
        assert image["index"] > lay.evaluate("(g) => canvas.getObjects().findIndex(o => o.guid === g)", image["panel"])
    owners = lay.evaluate("() => canvas.getObjects().filter(o => o.isPanel).map(p => (p.guids || []).length)")
    assert sorted(owners) == [0, 1, 1]
    # コマが足りなければ、余った画像は最後のコマへ
    third = grid(lay, "2")
    images = {i["guid"]: i for i in linked(lay)}
    assert images[a["guid"]]["panel"] == third["cells"][0]["guid"] and images[b["guid"]]["panel"] == third["cells"][1]["guid"]
    fourth = grid(lay, "1")
    images = linked(lay)
    assert len(panels(lay)) == 1 and [i["panel"] for i in images] == [fourth["cells"][0]["guid"]] * 2
    for image in images:
        assert_covers(lay, image, fourth["cells"][0]["guid"])
    # 取り消すと、1 つ前のコマ割りと画像の入れ先に戻る
    undo(lay)
    assert len(panels(lay)) == 2 and matches_layout(lay)
    assert sorted(i["panel"] for i in linked(lay)) == sorted(c["guid"] for c in info(lay)["cells"])


def test_grid_from_the_panel_with_presets_and_confirmation(lay):
    open_section(lay)
    assert lay.is_disabled("#ikl-mode") and lay.is_disabled("#ikl-gutter-v")
    assert "コマはありません" in lay.inner_text(".ikl-info")
    assert lay.locator(".ikl-preset").count() == 8
    lay.click(".ikl-preset[data-counts='1,1,1,1']")                 # コマの無いページでは、確認なしで作る
    lay.wait_for_function("() => canvas.getObjects().filter(o => o.isPanel).length === 4")
    assert matches_layout(lay) and lay.input_value("#ikl-counts") == "1,1,1,1"
    lay.wait_for_function("() => document.querySelector('.ikl-info').textContent.includes('コマ 4 個')")
    assert lay.is_enabled("#ikl-mode") and lay.is_enabled("#ikl-gutter-v")
    # 既にコマがあれば確かめる。やめると何も変わらない
    lay.fill("#ikl-counts", "2,2,2")
    lay.click("#ikl-grid")
    lay.wait_for_selector(".confirm-dialog")
    assert "コマ 4 個" in lay.inner_text(".confirm-dialog-message")
    lay.click(".confirm-dialog-cancel")
    lay.wait_for_selector(".confirm-dialog", state="detached")
    assert len(panels(lay)) == 4
    lay.click("#ikl-grid")
    lay.click(".confirm-dialog-ok")
    lay.wait_for_function("() => canvas.getObjects().filter(o => o.isPanel).length === 6")
    assert matches_layout(lay)
    # 書けない指定は、パネルにエラーを出して何もしない
    lay.fill("#ikl-counts", "2,x")
    lay.click("#ikl-grid")
    lay.wait_for_function("() => document.querySelector('.ikb-msg').textContent.includes('コマ数')")
    assert len(panels(lay)) == 6


def test_layout_survives_page_switch_save_and_reopen(lctx, lay, env):
    made = grid(lay, "2,1", gutter={"v": 0.05, "h": 0.04})
    moved = lay.evaluate("(k) => window.inkdeusBridge.layout.moveBoundary(k, 0.08)", made["bounds"][0]["key"])
    first_page = lay.evaluate("() => getCanvasGUID()")
    layout = info(lay)["layout"]
    assert layout["page"] == first_page and layout["gutter"] == {"v": 0.05, "h": 0.04}
    # 新しいページを作ると、レイアウトは付いてこない（前のページの物が残らない）
    lay.evaluate("async () => { await loadBookSize(210, 297, false, true); }")
    lay.wait_for_function("(g) => !isProjectBusy() && getCanvasGUID() !== g", arg=first_page)
    assert info(lay)["layout"] is None and panels(lay) == []
    other = grid(lay, "1,1,1")
    second_page = lay.evaluate("() => getCanvasGUID()")
    assert len(other["cells"]) == 3 and info(lay)["layout"]["page"] == second_page
    # 元のページへ戻ると、そのページのレイアウトが戻っていて、続けて操作できる
    for guid, count in ((first_page, 3), (second_page, 3), (first_page, 3)):
        lay.evaluate("""async (g) => { await btmSaveCurrentPage(false); await chengeCanvasByGuid(g);
            await btmWaitForPageReady(); }""", guid)
        lay.wait_for_function("(g) => !isProjectBusy() && getCanvasGUID() === g", arg=guid)
        lay.wait_for_function("(n) => canvas.getObjects().filter(o => o.isPanel).length === n", arg=count)
    assert near(info(lay)["layout"]["root"], layout["root"], tol=1e-9) and matches_layout(lay)
    assert near(boxes(info(lay)["cells"]), boxes(moved["cells"]), tol=1e-6)
    lay.evaluate("(k) => window.inkdeusBridge.layout.moveBoundary(k, -0.03)", made["bounds"][0]["key"])
    assert matches_layout(lay)
    expected = info(lay)
    # 保存して、別のタブで開き直す
    saved = lay.evaluate("() => window.inkdeusBridge.save('layout')")
    assert saved["pages"] == 2
    page = open_page(lctx, env)
    try:
        page.evaluate("() => window.inkdeusBridge.open('layout', {force: true})")
        page.wait_for_function("() => !isProjectBusy() && btmGetGuidsSize() === 2")
        page.evaluate("""async (g) => { await btmSaveCurrentPage(false); await chengeCanvasByGuid(g);
            await btmWaitForPageReady(); }""", first_page)
        page.wait_for_function("(g) => !isProjectBusy() && getCanvasGUID() === g", arg=first_page)
        page.wait_for_function("() => canvas.getObjects().filter(o => o.isPanel).length === 3")
        again = info(page)
        assert again["layout"] == expected["layout"] and near(boxes(again["cells"]), boxes(expected["cells"]), tol=1e-6)
        assert [c["guid"] for c in again["cells"]] == [c["guid"] for c in expected["cells"]] and matches_layout(page)
        # 開き直したあとも、続けて操作できる
        after = page.evaluate("() => window.inkdeusBridge.layout.split(1, 'v')")
        assert len(after["cells"]) == 4 and matches_layout(page) and len(panels(page)) == 4
    finally:
        page.close()


def test_positions_follow_the_window_size_and_do_not_roll_back(lay):
    made = grid(lay, "2,2")
    lay.evaluate("(k) => window.inkdeusBridge.layout.moveBoundary(k, 0.1)", made["bounds"][0]["key"])
    image = place(lay, info(lay)["cells"][0]["guid"])
    before = info(lay)
    size = lay.evaluate("() => [canvas.getWidth(), canvas.getHeight()]")
    lay.set_viewport_size({"width": 1100, "height": 560})
    lay.wait_for_function("(w) => Math.abs(canvas.getWidth() - w) > 5", arg=size[0])
    lay.wait_for_timeout(300)
    assert matches_layout(lay)                                      # 新しい大きさでも、レイアウトの計算どおり
    assert near(boxes(info(lay)["cells"]), boxes(before["cells"]), tol=1e-6)     # ページに対する割合は変わらない
    # 小さい画面のまま操作して、元の大きさへ戻す。操作の結果が巻き戻らない
    lay.evaluate("() => window.inkdeusBridge.layout.gutter(0.06, 0.05)")
    changed = info(lay)
    lay.set_viewport_size({"width": 1500, "height": 900})
    lay.wait_for_function("(w) => Math.abs(canvas.getWidth() - w) < 2", arg=size[0])
    lay.wait_for_timeout(300)
    assert matches_layout(lay) and near(boxes(info(lay)["cells"]), boxes(changed["cells"]), tol=1e-6)
    pic = next(i for i in linked(lay) if i["guid"] == image["guid"])
    assert_covers(lay, pic, changed["cells"][0]["guid"])

