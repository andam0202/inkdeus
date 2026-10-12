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


# ---- 境界線のドラッグ（コマ割りモード） ----

def screen(page) -> dict:
    return page.evaluate("() => window.inkdeusBridge.layout.screen()")


def drag(page, start: dict, dx: float, dy: float, shift: bool = False, steps: int = 6) -> None:
    page.mouse.move(start["x"], start["y"])
    page.mouse.down()
    if shift:
        page.keyboard.down("Shift")
    for i in range(1, steps + 1):
        page.mouse.move(start["x"] + dx * i / steps, start["y"] + dy * i / steps)
    page.mouse.up()
    if shift:
        page.keyboard.up("Shift")
    page.wait_for_timeout(60)


def depth(page) -> int:
    return page.evaluate("() => { flushHistory(); return stateStack.length; }")


def enter(page) -> None:
    assert page.evaluate("() => window.inkdeusBridge.layout.enter()") is True


def test_dragging_a_boundary_moves_both_sides_and_keeps_the_gutter(lay):
    made = grid(lay, "1,2", gutter={"v": 0.04, "h": 0.03})
    enter(lay)
    before = info(lay)
    row = next(b for b in before["bounds"] if b["dir"] == "v")
    col = next(b for b in before["bounds"] if b["dir"] == "h")
    spots = screen(lay)
    scale = spots["scale"]
    history = depth(lay)
    drag(lay, spots["bounds"][row["key"]]["mid"], 0, 70)
    after = info(lay)
    b0, b1 = boxes(before["cells"]), boxes(after["cells"])
    page_h = before["page"]["height"]
    moved = 70 / scale / page_h
    assert b1[1][3] - b0[1][3] == pytest.approx(moved, abs=0.002)             # 上のコマが伸びる
    assert b1[2][1] - b0[2][1] == pytest.approx(moved, abs=0.002) and b1[3][1] == pytest.approx(b1[2][1])   # 下の 2 コマが縮む
    assert b1[2][1] - b1[1][3] == pytest.approx(b0[2][1] - b0[1][3], abs=1e-6)       # 間隔は同じ
    assert b1[2][3] == pytest.approx(b0[2][3]) and matches_layout(lay)
    assert depth(lay) == history + 1                                           # ドラッグ 1 回 = 履歴 1 件
    # 列の境界は左右に動く
    drag(lay, screen(lay)["bounds"][col["key"]]["mid"], -50, 0)
    b2 = boxes(info(lay)["cells"])
    assert b2[2][0] - b1[2][0] == pytest.approx(-50 / scale / before["page"]["width"], abs=0.002)
    assert b2[2][0] - b2[3][2] == pytest.approx(b1[2][0] - b1[3][2], abs=1e-6) and near(b2[1], b1[1], tol=1e-6)
    assert matches_layout(lay) and depth(lay) == history + 2
    # 取り消し 1 回で 1 つ前のドラッグの後へ、もう 1 回で最初へ。やり直しも効く
    undo(lay)
    assert near(boxes(info(lay)["cells"]), b1, tol=1e-6) and matches_layout(lay)
    undo(lay)
    assert near(boxes(info(lay)["cells"]), b0, tol=1e-6) and matches_layout(lay)
    undo(lay, "redo")
    assert near(boxes(info(lay)["cells"]), b1, tol=1e-6) and matches_layout(lay)
    # モードの間に取り消しても、モードは続いていて、また掴める
    assert lay.evaluate("() => window.inkdeusBridge.layout.isActive()")
    drag(lay, screen(lay)["bounds"][row["key"]]["mid"], 0, -30)
    assert boxes(info(lay)["cells"])[1][3] < b1[1][3] and matches_layout(lay)
    assert made["cells"][0]["guid"] == info(lay)["cells"][0]["guid"]


def test_boundary_drag_stops_at_the_minimum_and_snaps_with_shift(lay):
    grid(lay, "1,1,1", gutter={"v": 0.03, "h": 0.02})
    enter(lay)
    before = info(lay)
    first, second = sorted((b for b in before["bounds"]), key=lambda b: b["pos"])
    spots = screen(lay)
    drag(lay, spots["bounds"][first["key"]]["mid"], 0, 2000)                   # ずっと下まで引いても、隣のコマは最小の高さで止まる
    after = info(lay)
    cells = boxes(after["cells"])
    short = min(after["page"]["width"], after["page"]["height"])
    minimum = lay.evaluate("() => window.inkdeusBridge.layout.core.MIN_CELL")
    assert (cells[2][3] - cells[2][1]) * after["page"]["height"] == pytest.approx(minimum * short, abs=0.6)
    assert near(cells[3], boxes(before["cells"])[3], tol=1e-6) and matches_layout(lay)
    undo(lay)
    # Shift: ページの 1/2 の近くで離すと、ちょうど 1/2 に吸い付く
    spots = screen(lay)
    target = (0.5 - first["pos"]) * before["page"]["height"] * spots["scale"]
    drag(lay, spots["bounds"][first["key"]]["mid"], 0, target + 6, shift=True)
    assert next(b for b in info(lay)["bounds"] if b["key"] == first["key"])["pos"] == pytest.approx(0.5, abs=1e-6)
    undo(lay)
    drag(lay, screen(lay)["bounds"][first["key"]]["mid"], 0, target + 6)
    assert next(b for b in info(lay)["bounds"] if b["key"] == first["key"])["pos"] != pytest.approx(0.5, abs=1e-4)


def test_dragging_a_boundary_end_makes_trapezoids_and_double_click_straightens(lay):
    grid(lay, "1,1", gutter={"v": 0.04, "h": 0.03})
    enter(lay)
    key = info(lay)["bounds"][0]["key"]
    spots = screen(lay)
    history = depth(lay)
    drag(lay, spots["bounds"][key]["ends"][1], 0, 50)                           # 右端を下げる
    after = info(lay)
    bound = after["bounds"][0]
    assert bound["tilt"][0] == 0 and bound["tilt"][1] > 0.02 and depth(lay) == history + 1
    top, bottom = after["cells"][0]["points"], after["cells"][1]["points"]     # 左上・右上・右下・左下
    assert top[0][1] == pytest.approx(top[1][1]) and top[2][1] > top[3][1] + 0.02           # 上のコマは下辺が斜めの台形
    assert bottom[1][1] > bottom[0][1] + 0.02 and bottom[2][1] == pytest.approx(bottom[3][1])
    assert bottom[0][1] - top[3][1] == pytest.approx(bottom[1][1] - top[2][1], abs=1e-6)     # 間隔は両端で同じ
    assert matches_layout(lay) and len(panels(lay)[0]["pts"]) == 4
    # ダブルクリックでまっすぐに戻る
    end = screen(lay)["bounds"][key]["ends"][1]
    lay.mouse.dblclick(end["x"], end["y"])
    lay.wait_for_function("() => window.inkdeusBridge.layout.get().bounds[0].tilt.every(t => t === 0)")
    top = info(lay)["cells"][0]["points"]
    assert top[2][1] == pytest.approx(top[3][1]) and matches_layout(lay)
    undo(lay)
    assert info(lay)["bounds"][0]["tilt"][1] > 0.02 and matches_layout(lay)


def test_mode_enter_and_exit_restore_the_lock_state(lay):
    made = grid(lay, "1,1")
    image = place(lay, made["cells"][0]["guid"])
    lay.evaluate("(g) => { const p = canvas.getObjects().find(o => o.guid === g); p.selectable = true; }", made["cells"][1]["guid"])
    state = "() => canvas.getObjects().map(o => [o.guid, o.selectable, o.evented, !!o._modeLockBackup])"
    before = lay.evaluate(state)
    assert sorted(x[1] for x in before) == [False, True, True] and all(x[2] and not x[3] for x in before)

    def active() -> bool:
        return lay.evaluate("() => window.inkdeusBridge.layout.isActive()")

    def check_left() -> None:
        assert not active() and lay.evaluate(state) == before
        assert lay.evaluate("() => [ModeManager.getCurrent(), canvas.selection, canvas.defaultCursor]") == ["select", True, "default"]
        assert lay.evaluate("() => document.querySelector('.ikl-overlay').hidden")
        assert not lay.evaluate("() => document.getElementById('canvas-help-text').classList.contains('active')")

    open_section(lay)
    lay.click("#ikl-mode")
    assert active() and lay.get_attribute("#ikl-mode", "aria-pressed") == "true"
    # モードの間は掴めない（保存される selectable は変えない）
    inside = lay.evaluate(state)
    assert [x[1] for x in inside] == [x[1] for x in before] and not any(x[2] for x in inside)
    assert lay.evaluate("() => [ModeManager.getCurrent(), canvas.selection]") == ["ikLayout", False]
    assert "コマ割りモード" in lay.inner_text("#canvas-help-text")
    assert lay.evaluate("() => document.getElementById('clearMode').classList.contains('selected')")
    assert not lay.evaluate("() => document.querySelector('.ikl-overlay').hidden")
    # モードの間に保存される状態にも、ロックは入らない
    saved = lay.evaluate("() => JSON.parse(JSON.stringify(customToJSON())).objects.map(o => o.selectable)")
    assert sorted(saved) == [False, True, True]
    lay.keyboard.press("Escape")                                    # ESC
    check_left()
    enter(lay)
    lay.evaluate("() => document.activeElement && document.activeElement.blur()")
    lay.keyboard.press("Escape")                                    # キャンバス側にフォーカスがあるときの ESC（上流のショートカット）
    check_left()
    lay.click("#ikl-mode")
    assert active()
    lay.click("#clearMode")                                         # 「モード解除」のボタン
    check_left()
    enter(lay)
    lay.evaluate("() => ModeManager.knife.enable()")                # ほかのモードへ切り替える
    assert not active() and lay.evaluate("() => ModeManager.getCurrent()") == "knife"
    lay.evaluate("() => ModeManager.clearAll()")
    check_left()
    enter(lay)
    lay.click("#ikl-mode")                                          # もう一度押す
    check_left()
    assert image["guid"] in [x[0] for x in lay.evaluate(state)]


# ---- 分割・結合・削除 ----

def L(page, call: str, *args):
    """inkdeusBridge.layout の関数を呼ぶ（例: L(page, 'split', 1, 'h')）"""
    return page.evaluate("([name, args]) => window.inkdeusBridge.layout[name](...args)", [call, list(args)])


def picture(page, guid: str) -> dict:
    return next(i for i in linked(page) if i["guid"] == guid)


def test_split_keeps_the_image_where_it_is_on_the_side_of_its_center(lay):
    made = grid(lay, "1,1", gutter={"v": 0.04, "h": 0.03})
    target = made["cells"][0]
    image = place(lay, target["guid"])
    # 絵を右へずらしておく（中心がコマの右半分にある）
    lay.evaluate("""(g) => { const o = canvas.getObjects().find(x => x.guid === g); o.left += canvas.getWidth() * 0.12;
        o.setCoords(); saveInitialState(o); canvas.fire('object:modified', {target: o}); }""", image["guid"])
    before = picture(lay, image["guid"])
    history = depth(lay)
    after = L(lay, "split", 1, "h")                                  # 縦の線で左右に割る（等分）
    assert len(after["cells"]) == 3 and len(panels(lay)) == 3 and matches_layout(lay) and depth(lay) == history + 1
    cells = {c["id"]: c for c in after["cells"]}
    left, right = cells[after["first"]], cells[after["second"]]
    assert left["guid"] == target["guid"] and right["guid"] != target["guid"]
    assert right["rect"]["x"] > left["rect"]["x"] and right["number"] == 1 and left["number"] == 2      # 右が先に読まれる
    assert left["rect"]["w"] == pytest.approx(right["rect"]["w"], abs=1e-6)
    assert right["rect"]["x"] - (left["rect"]["x"] + left["rect"]["w"]) == pytest.approx(0.03 * min(1, after["page"]["height"] / after["page"]["width"]), abs=1e-6)
    now = picture(lay, image["guid"])
    assert now["panel"] == right["guid"]                             # 中心のある側（右）に残る
    assert near((now["left"], now["top"], now["scaleX"], now["w"], now["h"]),
                (before["left"], before["top"], before["scaleX"], before["w"], before["h"]), tol=1e-6)   # 位置と大きさは不変
    l, t, r, b = panel_box(lay, right["guid"])
    assert now["clip"][0] >= l - 0.5 and now["clip"][2] <= r + 0.5   # 切り抜きは新しいコマ
    owners = lay.evaluate("() => Object.fromEntries(canvas.getObjects().filter(o => o.isPanel).map(p => [p.guid, (p.guids || []).slice()]))")
    assert owners[right["guid"]] == [image["guid"]] and owners[left["guid"]] == []
    assert lay.evaluate("([a, b]) => canvas.getObjects().findIndex(o => o.guid === a) > canvas.getObjects().findIndex(o => o.guid === b)",
                        [image["guid"], right["guid"]])              # 絵はコマより手前
    # 取り消し 1 回で割る前へ
    undo(lay)
    assert len(panels(lay)) == 2 and matches_layout(lay) and picture(lay, image["guid"])["panel"] == target["guid"]
    undo(lay, "redo")
    assert len(panels(lay)) == 3 and matches_layout(lay) and picture(lay, image["guid"])["panel"] == right["guid"]
    # 位置を指定して上下に割る
    again = L(lay, "split", right["guid"], "v", 0.25)
    cells = {c["id"]: c for c in again["cells"]}
    upper, lower = cells[again["first"]], cells[again["second"]]
    assert lower["rect"]["y"] > upper["rect"]["y"] and upper["rect"]["h"] < lower["rect"]["h"] * 0.5
    assert (upper["rect"]["y"] + upper["rect"]["h"] + lower["rect"]["y"]) / 2 == pytest.approx(
        right["rect"]["y"] + right["rect"]["h"] * 0.25, abs=1e-6)
    assert matches_layout(lay) and len(panels(lay)) == 4


def test_split_from_the_panel_and_with_alt_click(lay):
    grid(lay, "1,1")
    open_section(lay)
    lay.click("#ikl-mode")
    assert lay.is_hidden(".ikl-cell")
    spots = screen(lay)
    first = info(lay)["cells"][0]
    center = spots["cells"][first["id"]]["center"]
    lay.mouse.click(center["x"], center["y"])                       # コマを押して選ぶ
    lay.wait_for_selector(".ikl-cell:not([hidden])")
    assert "コマ 1" in lay.inner_text(".ikl-sel-name") and info(lay)["selected"] == [first["id"]]
    assert lay.is_disabled("#ikl-merge") and lay.is_hidden("#ikl-unfree")
    lay.click("#ikl-split-h")                                       # 縦に割る（等分）
    lay.wait_for_function("() => canvas.getObjects().filter(o => o.isPanel).length === 3")
    assert matches_layout(lay)
    lay.click("#ikl-split-v")                                       # 選択は割った元のコマに残っているので、続けて割れる
    lay.wait_for_function("() => canvas.getObjects().filter(o => o.isPanel).length === 4")
    assert matches_layout(lay)
    # Alt+クリック: 押した位置で左右に割る
    last = info(lay)["cells"][-1]
    rect = screen(lay)["cells"][last["id"]]["rect"]
    x = rect["l"] + (rect["r"] - rect["l"]) * 0.3
    lay.keyboard.down("Alt")
    lay.mouse.click(x, (rect["t"] + rect["b"]) / 2)
    lay.keyboard.up("Alt")
    lay.wait_for_function("() => canvas.getObjects().filter(o => o.isPanel).length === 5")
    after = info(lay)
    made = sorted((c for c in after["cells"] if abs(c["rect"]["y"] - last["rect"]["y"]) < 1e-6), key=lambda c: c["rect"]["x"])
    assert len(made) == 2
    gap_center = (made[0]["rect"]["x"] + made[0]["rect"]["w"] + made[1]["rect"]["x"]) / 2
    assert gap_center == pytest.approx(last["rect"]["x"] + last["rect"]["w"] * 0.3, abs=0.004) and matches_layout(lay)
    # ボタンを Alt+クリック → 次に押した位置で上下に割る
    lay.click("#ikl-split-v", modifiers=["Alt"])
    assert "クリックしてください" in lay.inner_text(".ikb-msg")
    rect = screen(lay)["cells"][made[1]["id"]]["rect"]
    lay.mouse.click((rect["l"] + rect["r"]) / 2, rect["t"] + (rect["b"] - rect["t"]) * 0.7)
    lay.wait_for_function("() => canvas.getObjects().filter(o => o.isPanel).length === 6")
    assert matches_layout(lay) and lay.evaluate("() => window.inkdeusBridge.layout.isActive()")
    # モードの間に増えたコマも、抜けたら掴める状態（保存どおり）へ戻る
    lay.keyboard.press("Escape")
    assert all(p["evented"] and not p["selectable"] for p in panels(lay))


def test_merge_keeps_both_images_and_remove_handles_images(lay):
    made = grid(lay, "3,1", gutter={"v": 0.04, "h": 0.03})
    a = place(lay, made["cells"][0]["guid"])
    b = place(lay, made["cells"][1]["guid"], "gradient.png")
    c = place(lay, made["cells"][2]["guid"])
    pa, pb = picture(lay, a["guid"]), picture(lay, b["guid"])
    with pytest.raises(Exception, match="隣り合う"):
        L(lay, "merge", 1, 3)
    with pytest.raises(Exception, match="隣り合う"):
        L(lay, "merge", 1, 4)
    history = depth(lay)
    merged = L(lay, "merge", 1, 2)
    assert len(merged["cells"]) == 3 and len(panels(lay)) == 3 and matches_layout(lay) and depth(lay) == history + 1
    kept = next(x for x in merged["cells"] if x["id"] == merged["kept"])
    assert kept["guid"] == made["cells"][1]["guid"]                  # 残るのは左側のコマ
    assert kept["rect"]["x"] == pytest.approx(made["cells"][1]["rect"]["x"])
    assert kept["rect"]["x"] + kept["rect"]["w"] == pytest.approx(made["cells"][0]["rect"]["x"] + made["cells"][0]["rect"]["w"])
    na, nb = picture(lay, a["guid"]), picture(lay, b["guid"])
    assert na["panel"] == nb["panel"] == kept["guid"]                # 両方の絵が残り、新しいコマで切り抜かれる
    assert near((na["left"], na["top"], na["scaleX"]), (pa["left"], pa["top"], pa["scaleX"]), tol=1e-6)
    assert near((nb["left"], nb["top"], nb["scaleX"]), (pb["left"], pb["top"], pb["scaleX"]), tol=1e-6)
    box = panel_box(lay, kept["guid"])
    assert na["clip"][2] <= box[2] + 0.5 and nb["clip"][0] >= box[0] - 0.5
    assert sorted(lay.evaluate("(g) => canvas.getObjects().find(o => o.guid === g).guids", kept["guid"])) == sorted([a["guid"], b["guid"]])
    undo(lay)
    assert len(panels(lay)) == 4 and matches_layout(lay) and picture(lay, a["guid"])["panel"] == made["cells"][0]["guid"]
    undo(lay, "redo")
    # 画面から: 2 つ選んで（Shift+クリック）結合
    enter(lay)
    open_section(lay)
    cells = info(lay)["cells"]
    spots = screen(lay)
    first, second = spots["cells"][cells[0]["id"]]["center"], spots["cells"][cells[1]["id"]]["center"]
    lay.mouse.click(first["x"], first["y"])
    lay.keyboard.down("Shift")
    lay.mouse.click(second["x"], second["y"])
    lay.keyboard.up("Shift")
    lay.wait_for_function("() => window.inkdeusBridge.layout.get().selected.length === 2")
    assert lay.is_enabled("#ikl-merge") and lay.is_disabled("#ikl-split-h") and "コマ 1 と 2" in lay.inner_text(".ikl-sel-name")
    lay.click("#ikl-merge")
    lay.wait_for_function("() => canvas.getObjects().filter(o => o.isPanel).length === 2")
    assert matches_layout(lay) and len(linked(lay)) == 3
    assert len({i["panel"] for i in linked(lay)}) == 1               # 行が 1 コマになり、3 枚とも同じコマ
    lay.keyboard.press("Escape")
    undo(lay)
    # 削除（既定は画像を残す）: 兄弟が埋め、絵はコマなしの自由な画像になる
    before = info(lay)
    gone = before["cells"][1]                                        # 左の（c の入った）コマ
    removed = L(lay, "remove", 2)
    assert len(removed["cells"]) == 2 and len(panels(lay)) == 2 and matches_layout(lay)
    free = picture(lay, c["guid"])
    assert free["panel"] is None and free["clip"] is None
    wide = removed["cells"][0]
    assert wide["rect"]["x"] == pytest.approx(gone["rect"]["x"]) and wide["rect"]["w"] > before["cells"][0]["rect"]["w"]
    undo(lay)
    assert len(panels(lay)) == 3 and picture(lay, c["guid"])["panel"] == gone["guid"] and matches_layout(lay)
    # 「画像も消す」
    removed = L(lay, "remove", 2, {"images": "delete"})
    assert len(panels(lay)) == 2 and c["guid"] not in [i["guid"] for i in linked(lay)] and len(linked(lay)) == 2
    # 最後の 1 つまで消すと、レイアウトは無くなる
    L(lay, "remove", 1)
    L(lay, "remove", 1)
    assert panels(lay) == [] and info(lay)["layout"] is None and len(linked(lay)) == 2

