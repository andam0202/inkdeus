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


# ---- 間隔・余白、断ち切り、自由変形、画像の追従、取り込み、外部操作 ----

def slide(page, selector: str, value: float, commit: bool = True) -> None:
    """スライダーを動かす（input）。commit で離す（change）"""
    page.evaluate("""([sel, value, commit]) => { const node = document.querySelector(sel); node.value = String(value);
        node.dispatchEvent(new Event('input', {bubbles: true}));
        if (commit) node.dispatchEvent(new Event('change', {bubbles: true})); }""", [selector, value, commit])
    page.wait_for_timeout(40)


def test_gutter_and_margin_sliders_recompute_the_whole_page(lay):
    made = grid(lay, "2,2", gutter={"v": 0.03, "h": 0.02})
    open_section(lay)
    lay.wait_for_function("() => document.querySelector('#ikl-gutter-v').value === '3'")
    assert lay.input_value("#ikl-gutter-h") == "2" and lay.input_value("#ikl-margin-top") == "5"
    page_w, page_h = made["page"]["width"], made["page"]["height"]
    short = min(page_w, page_h)
    history = depth(lay)
    slide(lay, "#ikl-gutter-v", 4, commit=False)                    # 動かしている間は軽い描き直しだけ（履歴には積まない）
    slide(lay, "#ikl-gutter-v", 6, commit=False)
    assert matches_layout(lay) is False and lay.evaluate("() => stateStack.length") == history
    live = {p["guid"]: p["pts"] for p in panels(lay)}
    slide(lay, "#ikl-gutter-v", 8)                                  # 離したときに 1 件だけ確定
    after = info(lay)
    assert after["layout"]["gutter"] == {"v": 0.08, "h": 0.02} and matches_layout(lay) and depth(lay) == history + 1
    assert live != {p["guid"]: p["pts"] for p in panels(lay)}
    b = boxes(after["cells"])
    assert (b[3][1] - b[1][3]) * page_h == pytest.approx(0.08 * short, abs=0.5)
    assert lay.inner_text("#ikl-gutter-v + .ikl-value") == "8%"
    slide(lay, "#ikl-gutter-h", 5)
    b = boxes(info(lay)["cells"])
    assert (b[1][0] - b[2][2]) * page_w == pytest.approx(0.05 * short, abs=0.5) and matches_layout(lay)
    slide(lay, "#ikl-margin-v", 10)
    slide(lay, "#ikl-margin-h", 2)
    after = info(lay)
    assert after["layout"]["margin"] == {"top": 0.1, "right": 0.02, "bottom": 0.1, "left": 0.02}
    b = boxes(after["cells"])
    assert b[1][1] * page_h == pytest.approx(0.1 * short, abs=0.5) and (1 - b[1][2]) * page_w == pytest.approx(0.02 * short, abs=0.5)
    assert matches_layout(lay) and depth(lay) == history + 4
    # 個別の余白
    lay.fill("#ikl-margin-left", "12")
    lay.press("#ikl-margin-left", "Tab")
    lay.wait_for_function("() => window.inkdeusBridge.layout.get().layout.margin.left === 0.12")
    after = info(lay)
    assert after["layout"]["margin"]["right"] == 0.02 and matches_layout(lay)
    assert boxes(after["cells"])[2][0] * page_w == pytest.approx(0.12 * short, abs=0.5)
    # 取り消し 1 回で 1 つ前の値へ
    undo(lay)
    assert info(lay)["layout"]["margin"]["left"] == 0.02 and matches_layout(lay)
    lay.wait_for_function("() => document.querySelector('#ikl-margin-left').value === '2'")


def test_bleed_extends_the_edges_that_touch_the_page(lay):
    made = grid(lay, "2,1")
    enter(lay)
    open_section(lay)
    top_right = made["cells"][0]
    center = screen(lay)["cells"][top_right["id"]]["center"]
    lay.mouse.click(center["x"], center["y"])
    lay.wait_for_selector(".ikl-bleed:not([hidden])")
    shown = lay.evaluate("() => ['top', 'right', 'bottom', 'left'].map(s => !document.getElementById('ikl-bleed-' + s).parentNode.hidden)")
    assert shown == [True, True, False, False]                      # ページの端に接している辺だけ
    assert top_right["edges"] == {"top": True, "right": True, "bottom": False, "left": False}
    lay.check("#ikl-bleed-top")
    lay.wait_for_function("(id) => window.inkdeusBridge.layout.get().cells.find(c => c.id === id).rect.y === 0", arg=top_right["id"])
    lay.check("#ikl-bleed-right")
    after = info(lay)
    cell = next(c for c in after["cells"] if c["id"] == top_right["id"])
    assert cell["rect"]["y"] == 0 and cell["rect"]["x"] + cell["rect"]["w"] == pytest.approx(1)
    assert cell["rect"]["x"] == pytest.approx(top_right["rect"]["x"]) and matches_layout(lay)
    assert near(boxes(after["cells"])[2], boxes(made["cells"])[2], tol=1e-9)
    lay.uncheck("#ikl-bleed-top")
    lay.wait_for_function("(id) => window.inkdeusBridge.layout.get().cells.find(c => c.id === id).rect.y > 0", arg=top_right["id"])
    assert matches_layout(lay)
    with pytest.raises(Exception, match="top / right / bottom / left"):
        L(lay, "bleed", 1, "middle", True)


def test_free_transform_unlinks_a_panel_and_can_be_relinked(lay):
    made = grid(lay, "1,1", gutter={"v": 0.04, "h": 0.03})
    enter(lay)
    open_section(lay)
    first, second = made["cells"]
    spots = screen(lay)
    lay.mouse.click(spots["cells"][first["id"]]["center"]["x"], spots["cells"][first["id"]]["center"]["y"])
    lay.wait_for_function("() => window.inkdeusBridge.layout.get().selected.length === 1")
    history = depth(lay)
    corner = screen(lay)["cells"][first["id"]]["points"][2]         # 右下の頂点を掴んで動かす
    drag(lay, corner, -60, -40)
    after = info(lay)
    cell = next(c for c in after["cells"] if c["id"] == first["id"])
    assert cell["free"] is True and depth(lay) == history + 1
    scale = spots["scale"]
    assert cell["points"][2][0] == pytest.approx(first["points"][2][0] - 60 / scale / after["page"]["width"], abs=0.003)
    assert cell["points"][2][1] == pytest.approx(first["points"][2][1] - 40 / scale / after["page"]["height"], abs=0.003)
    assert near([cell["points"][i] for i in (0, 1, 3)], [first["points"][i] for i in (0, 1, 3)], tol=1e-9)
    assert matches_layout(lay) and "連動から外れています" in lay.inner_text(".ikl-sel-name") and lay.is_visible("#ikl-unfree")
    # 連動から外れたコマは、間隔のスライダーや境界線のドラッグでは動かない
    shape = cell["points"]
    L(lay, "gutter", 0.1, 0.03)
    L(lay, "moveBoundary", after["bounds"][0]["key"], 0.05)
    now = info(lay)
    assert near(next(c for c in now["cells"] if c["id"] == first["id"])["points"], shape, tol=1e-9)
    assert not near(boxes(now["cells"])[2], boxes(made["cells"])[2], tol=1e-4) and matches_layout(lay)
    # 辺のドラッグは 2 頂点を同時に動かす
    pts = screen(lay)["cells"][first["id"]]["points"]
    drag(lay, {"x": (pts[0]["x"] + pts[1]["x"]) / 2, "y": (pts[0]["y"] + pts[1]["y"]) / 2}, 0, 25)
    edge = next(c for c in info(lay)["cells"] if c["id"] == first["id"])["points"]
    assert edge[0][1] - shape[0][1] == pytest.approx(edge[1][1] - shape[1][1], abs=1e-9) and edge[0][1] > shape[0][1] + 0.01
    assert near(edge[2:], shape[2:], tol=1e-9) and matches_layout(lay)
    # 「連動に戻す」で元の長方形へ
    lay.click("#ikl-unfree")
    lay.wait_for_function("(id) => !window.inkdeusBridge.layout.get().cells.find(c => c.id === id).free", arg=first["id"])
    back = next(c for c in info(lay)["cells"] if c["id"] == first["id"])
    assert back["points"][0][1] == pytest.approx(back["points"][1][1]) and back["points"][2][1] == pytest.approx(back["points"][3][1])
    assert matches_layout(lay)
    # 漫画エディタ側の操作でコマを動かしたときも、その形のまま連動から外れる
    lay.keyboard.press("Escape")
    lay.evaluate("""(g) => { const p = canvas.getObjects().find(o => o.guid === g); p.left += 30; p.setCoords();
        saveInitialState(p); canvas.fire('object:modified', {target: p}); }""", second["guid"])
    moved = next(c for c in info(lay)["cells"] if c["id"] == second["id"])
    assert moved["free"] is True and matches_layout(lay)
    L(lay, "unfree", second["id"])
    assert matches_layout(lay) and not any(c["free"] for c in info(lay)["cells"])


@pytest.mark.parametrize("mode", ["keep", "none", "cover"])
def test_images_follow_the_panel_in_three_ways(lay, mode):
    made = grid(lay, "1,1", gutter={"v": 0.04, "h": 0.03})
    target = made["cells"][0]
    image = place(lay, target["guid"])
    # 絵を少し小さくして左上へ寄せる（コマに対する位置と大きさが分かるように）
    lay.evaluate("""(g) => { const o = canvas.getObjects().find(x => x.guid === g); o.scaleX *= 0.6; o.scaleY *= 0.6;
        o.left -= 20; o.top -= 10; o.setCoords(); saveInitialState(o); }""", image["guid"])
    assert L(lay, "setFollow", mode) == mode and lay.input_value("#ikl-follow") == mode
    before = picture(lay, image["guid"])
    old = panel_box(lay, target["guid"])
    L(lay, "moveBoundary", made["bounds"][0]["key"], 0.15)           # 上のコマを縦に伸ばす
    now = picture(lay, image["guid"])
    new = panel_box(lay, target["guid"])
    assert new[3] > old[3] + 20 and now["panel"] == target["guid"]
    assert near(now["clip"], new, tol=4)                             # 切り抜きは、どの場合も新しい形
    if mode == "none":
        assert near((now["cx"], now["cy"], now["w"], now["h"]), (before["cx"], before["cy"], before["w"], before["h"]), tol=1e-6)
    elif mode == "keep":
        k = ((new[3] - new[1]) / (old[3] - old[1])) ** 0.5           # 等倍率（コマの面積の比の平方根）
        assert now["w"] == pytest.approx(before["w"] * k, rel=1e-6) and now["h"] == pytest.approx(before["h"] * k, rel=1e-6)
        fx, fy = (before["cx"] - old[0]) / (old[2] - old[0]), (before["cy"] - old[1]) / (old[3] - old[1])
        assert (now["cx"] - new[0]) / (new[2] - new[0]) == pytest.approx(fx, abs=1e-6)       # 中心の相対位置を保つ
        assert (now["cy"] - new[1]) / (new[3] - new[1]) == pytest.approx(fy, abs=1e-6)
    else:
        assert_covers(lay, now, target["guid"])
        assert near((now["cx"], now["cy"]), ((new[0] + new[2]) / 2, (new[1] + new[3]) / 2), tol=0.01)
        assert min(now["w"] / (new[2] - new[0]), now["h"] / (new[3] - new[1])) == pytest.approx(1, abs=1e-6)
    # 画面に合わせ直しても（上流の再フィット）、絵は巻き戻らない
    lay.evaluate("() => forcedAdjustCanvasSize()")
    assert near(picture(lay, image["guid"]), now, tol=1e-6)
    # 境界を戻すと、絵も元へ戻る（行って戻っても、ずれが溜まらない）
    L(lay, "moveBoundary", made["bounds"][0]["key"], -0.15)
    back = picture(lay, image["guid"])
    if mode != "cover":
        assert near((back["cx"], back["cy"], back["w"], back["h"]), (before["cx"], before["cy"], before["w"], before["h"]), tol=1e-4)
    L(lay, "setFollow", "keep")


def test_follow_during_a_drag_is_computed_from_the_start(lay):
    """ドラッグの途中で行き過ぎて戻しても、絵の大きさは最後の位置だけで決まる"""
    made = grid(lay, "1,1", gutter={"v": 0.04, "h": 0.03})
    image = place(lay, made["cells"][0]["guid"])
    before = picture(lay, image["guid"])
    enter(lay)
    start = screen(lay)["bounds"][made["bounds"][0]["key"]]["mid"]
    lay.mouse.move(start["x"], start["y"])
    lay.mouse.down()
    for dy in (40, 120, 200, 120, 40, 0, -60, 0, 10, 1, 0):
        lay.mouse.move(start["x"], start["y"] + dy)
    lay.mouse.up()
    lay.wait_for_timeout(60)
    assert near(picture(lay, image["guid"]), before, tol=0.05) and matches_layout(lay)


def apply_upstream_template(page) -> None:
    """上流のコマのひな形を 1 つ適用する（新しいページができる）"""
    page.wait_for_function("() => typeof MangaPanelsImage_Vertical !== 'undefined' && MangaPanelsImage_Vertical.length > 2")
    page.evaluate("""async () => { if (btmShouldSaveCurrentPage()) await btmSaveProjectFile();
        setCanvasGUID(); loadSVGPlusReset(MangaPanelsImage_Vertical[2].svg); }""")
    page.wait_for_function("() => { const p = window.inkdeusBridge.panels();"
                           " return p.length >= 2 && p.every((x) => !!x.guid) && !isProjectBusy(); }")
    page.evaluate("async () => { flushHistory(); await btmSaveProjectFile(null, false); }")


def test_import_existing_panels_into_a_layout(lay):
    apply_upstream_template(lay)
    count = len(panels(lay))
    assert count >= 2 and info(lay)["layout"] is None and info(lay)["unmanaged"] == count
    open_section(lay)
    lay.wait_for_function("() => document.querySelector('.ikl-info').textContent.includes('まだレイアウトに入っていません')")
    before = {p["guid"]: p["pts"] for p in panels(lay)}
    target = panels(lay)[0]["guid"]
    image = place(lay, target)
    history = depth(lay)
    lay.click("#ikl-import")
    lay.wait_for_function("() => !!window.inkdeusBridge.layout.get().layout")
    got = info(lay)
    rectangles = lay.evaluate("""() => canvas.getObjects().filter(o => o.isPanel).filter(p => p.points && p.points.length === 4).length""")
    assert len(got["cells"]) + got["unmanaged"] == count and len(got["cells"]) >= 2 and len(got["cells"]) <= rectangles
    assert f"コマ {len(got['cells'])} 個を取り込みました" in lay.inner_text(".ikb-msg")
    assert matches_layout(lay) and depth(lay) == history + 1
    short = min(got["page"]["width"], got["page"]["height"])
    for cell in got["cells"]:                                       # 取り込んでも、コマはほとんど動かない
        xs, ys = [p[0] for p in before[cell["guid"]]], [p[1] for p in before[cell["guid"]]]
        assert near((cell["rect"]["x"] * got["page"]["width"], cell["rect"]["y"] * got["page"]["height"]), (min(xs), min(ys)), tol=short * 0.03)
    assert picture(lay, image["guid"])["panel"] == target
    # 取り込んだあとは、ほかのコマ割りと同じように操作できる
    key = got["bounds"][0]["key"]
    moved = L(lay, "moveBoundary", key, 0.03)
    assert matches_layout(lay) and not near(boxes(moved["cells"]), boxes(got["cells"]), tol=1e-4)
    after = L(lay, "split", 1, "h")
    assert len(after["cells"]) == len(got["cells"]) + 1 and matches_layout(lay)
    undo(lay)
    undo(lay)
    undo(lay)
    assert info(lay)["layout"] is None and near({p["guid"]: p["pts"] for p in panels(lay)}, before, tol=0.01)
    # 行と列に分けられない配置・長方形でないコマは取り込めない
    lay.evaluate("""() => canvas.getObjects().filter(o => o.isPanel).forEach((p, i) => {
        p.points = [{x: 0, y: 0}, {x: 60, y: 10}, {x: 30, y: 80}]; p._setPositionDimensions({}); p.setCoords(); })""")
    with pytest.raises(Exception, match="取り込めるコマ"):
        L(lay, "importPanels")


def test_deleting_a_panel_with_the_editor_leaves_a_hole_until_removed(lay):
    made = grid(lay, "1,1,1")
    middle = made["cells"][1]
    lay.evaluate("(g) => { canvas.remove(canvas.getObjects().find(o => o.guid === g)); flushHistory(); }", middle["guid"])
    after = info(lay)
    assert [c["missing"] for c in after["cells"]] == [False, True, False] and len(panels(lay)) == 2
    assert near(boxes(after["cells"]), boxes(made["cells"]), tol=1e-9)          # ほかのコマは勝手に動かない
    removed = L(lay, "remove", 2)                                    # 空いた場所を消すと、隣が埋める
    assert len(removed["cells"]) == 2 and matches_layout(lay)
    assert removed["cells"][1]["rect"]["y"] < made["cells"][2]["rect"]["y"] - 0.05


def test_ctl_manga_layout(lay, env):
    from test_manga_pdf_ui import run_ctl

    empty = run_ctl(lay, env, "manga", "layout", "get")
    assert empty["code"] == 0 and empty["layout"] is None and empty["cells"] == []
    assert "レイアウトがありません" in run_ctl(lay, env, "manga", "layout", "gutter", "3%", "2%")["error"]
    made = run_ctl(lay, env, "manga", "layout", "grid", "2,1,3")
    assert made["code"] == 0 and [c["number"] for c in made["cells"]] == [1, 2, 3, 4, 5, 6]
    assert all(c["guid"] and set(c["rect"]) == {"x", "y", "w", "h"} for c in made["cells"]) and matches_layout(lay)
    assert "コマ数" in run_ctl(lay, env, "manga", "layout", "grid", "2,x")["error"]
    spaced = run_ctl(lay, env, "manga", "layout", "gutter", "6%", "0.04")
    assert spaced["layout"]["gutter"] == {"v": 0.06, "h": 0.04} and matches_layout(lay)
    edged = run_ctl(lay, env, "manga", "layout", "margin", "8%", "2%", "8%", "0.02")
    assert edged["layout"]["margin"] == {"top": 0.08, "right": 0.02, "bottom": 0.08, "left": 0.02} and matches_layout(lay)
    assert "範囲" in run_ctl(lay, env, "manga", "layout", "gutter", "90%", "2%")["error"]
    # set: 葉がいまのコマをそのまま指していれば、形だけが変わる（コマは作り直されない）
    import json

    layout = run_ctl(lay, env, "manga", "layout", "get")["layout"]
    layout["root"]["ratios"] = [0.5, 0.2, 0.3]
    layout["gutter"]["v"] = 0.02
    guids = sorted(p["guid"] for p in panels(lay))
    reshaped = run_ctl(lay, env, "manga", "layout", "set", json.dumps(layout))
    assert reshaped["code"] == 0 and sorted(p["guid"] for p in panels(lay)) == guids and matches_layout(lay)
    heights = sorted({round(c["rect"]["h"], 4) for c in reshaped["cells"]})
    assert len(heights) == 3 and heights[2] / heights[0] == pytest.approx(2.5, rel=0.01)
    # 木を書き換えると、作り直す
    rebuilt = run_ctl(lay, env, "manga", "layout", "set",
                      json.dumps({"gutter": {"v": 0.03, "h": 0.03}, "root": {"dir": "h", "ratios": [1, 2], "children": [
                          {}, {"dir": "v", "children": [{}, {}]}]}}))
    assert rebuilt["code"] == 0 and len(rebuilt["cells"]) == 3 and len(panels(lay)) == 3 and matches_layout(lay)
    assert not set(guids) & {p["guid"] for p in panels(lay)}
    wide = [c for c in rebuilt["cells"] if c["rect"]["h"] > 0.6]
    assert len(wide) == 1 and wide[0]["number"] == 3                 # 左の縦長のコマは、右綴じでは最後に読む
    assert "dir" in run_ctl(lay, env, "manga", "layout", "set", '{"root": {"dir": "x", "children": [{}]}}')["error"]
    assert "JSON" in run_ctl(lay, env, "manga", "layout", "set", "{broken")["error"]


# ---- 上流への差分（inkdeus manga install が当てる）と、その効果 ----

PATCHES = ["0001-get-object-by-guid", "0002-template-to-current-page", "0003-knife-keeps-image-place"]


def test_patches_are_reported_to_the_bridge(lay, env):
    status = lay.evaluate("() => window.inkdeusBridge.status().patches")
    options = L(lay, "options")
    open_section(lay)
    assert lay.is_hidden(".ikb-patch-notice")                       # 自分で外した（--no-patches）ときも案内は出さない
    if env.variant == "patched":
        assert env.patches["applied"] == PATCHES and env.patches["failed"] is None
        assert status == {"applied": PATCHES, "pending": [], "active": PATCHES}      # サーバの記録と、実際に読み込まれた物が一致
        assert options["patches"] == {"template": True, "knife": True}
        assert lay.is_visible("#ikl-template-target") and lay.is_visible("#ikl-knife")
    else:
        assert env.patches["applied"] == [] and env.patches["disabled"] is True
        assert status == {"applied": [], "pending": PATCHES, "active": []}
        assert options["patches"] == {"template": False, "knife": False}
        assert lay.is_hidden("#ikl-template-target") and lay.is_hidden("#ikl-knife")     # 差分が無ければ、設定も出さない
    assert options["templateTarget"] == "new" and options["knifeRefit"] is False


def test_patch_get_object_by_guid_compares_with_its_argument(lay, env):
    made = grid(lay, "1,1")
    first = place(lay, made["cells"][0]["guid"])
    second = place(lay, made["cells"][1]["guid"])
    found = lay.evaluate("([a, b]) => [getObjectByGUID(a), getObjectByGUID(b)].map(o => (o ? o.guid : null))",
                         [first["guid"], second["guid"]])
    if env.variant == "patched":
        assert found == [first["guid"], second["guid"]]
    else:
        # 差分なしでは、引数ではなく最後に setGUID() が書いた値と比べるので、どちらを引いても最後の画像が返る
        assert found == [second["guid"], second["guid"]]


def test_patch_template_can_replace_the_panels_of_the_current_page(lay, env):
    made = grid(lay, "1,1,1")
    image = place(lay, made["cells"][0]["guid"])
    page_guid = lay.evaluate("() => getCanvasGUID()")
    pages = lay.evaluate("() => btmGetGuidsSize()")
    lay.wait_for_function("() => document.querySelectorAll('#svg-preview-area-vertical img.svg-preview').length > 5")
    names = lay.evaluate("() => MangaPanelsImage_Vertical.map(t => t.name)")
    click = "(i) => document.querySelectorAll('#svg-preview-area-vertical img.svg-preview')[i].click()"
    if env.variant == "plain":
        with pytest.raises(Exception, match="."):
            lay.select_option("#ikl-template-target", "current", timeout=500)        # 設定が出ていない
        L(lay, "setTemplateTarget", "current")                      # 設定だけ変えても、差分が無ければ上流の既定のまま
        lay.evaluate(click, 5)
        lay.wait_for_function("(g) => !isProjectBusy() && getCanvasGUID() !== g && window.inkdeusBridge.panels().length >= 1", arg=page_guid)
        # 新しいページになっている（元のページはページ一覧に残り、新しいページに画像は無い）
        assert page_guid in lay.evaluate("() => btmGetGuids()") and images_on_canvas(lay) == []
        L(lay, "setTemplateTarget", "new")
        return
    open_section(lay)
    lay.select_option("#ikl-template-target", "current")
    lay.evaluate(click, 5)                                          # 行と列に分けられる、コマ 2 つのひな形
    lay.wait_for_selector(".confirm-dialog")                        # いまのコマを置き換える前に確かめる
    lay.click(".confirm-dialog-ok")
    lay.wait_for_function("() => canvas.getObjects().filter(o => o.isPanel).length === 2")
    assert lay.evaluate("() => [getCanvasGUID(), btmGetGuidsSize()]") == [page_guid, pages]      # 新しいページは作られない
    got = info(lay)
    assert len(got["cells"]) == 2 and got["unmanaged"] == 0 and matches_layout(lay)             # レイアウトとして入る
    assert picture(lay, image["guid"])["panel"] == got["cells"][0]["guid"]                      # 画像は読む順に引き継ぐ
    undo(lay)
    assert len(panels(lay)) == 3 and picture(lay, image["guid"])["panel"] == made["cells"][0]["guid"]
    # 斜めのコマを含むひな形は、形のまま置く（レイアウトには入らない）
    slanted = next(i for i, n in enumerate(names) if i > 0 and lay.evaluate(
        "async (i) => { const r = await window.inkdeusBridge.layout.applyTemplate(MangaPanelsImage_Vertical[i].svg, {force: true});"
        " undo(); return !r.layout; }", i))
    lay.wait_for_function("() => !isHistoryRestoreInProgress() && canvas.getObjects().filter(o => o.isPanel).length === 3")
    result = lay.evaluate("(i) => window.inkdeusBridge.layout.applyTemplate(MangaPanelsImage_Vertical[i].svg, {force: true})", slanted)
    assert result["layout"] is None and result["unmanaged"] == len(panels(lay)) >= 2
    assert picture(lay, image["guid"])["panel"] in [p["guid"] for p in panels(lay)]
    assert lay.evaluate("() => getCanvasGUID()") == page_guid
    # 既定（新しいページを作る）に戻すと、上流の元の動き
    lay.select_option("#ikl-template-target", "new")
    lay.evaluate(click, 5)
    lay.wait_for_function("(g) => !isProjectBusy() && getCanvasGUID() !== g && window.inkdeusBridge.panels().length === 2", arg=page_guid)
    assert page_guid in lay.evaluate("() => btmGetGuids()") and info(lay)["layout"] is None and images_on_canvas(lay) == []


def test_patch_knife_keeps_the_image_where_it_is(lay, env):
    made = grid(lay, "1")
    panel_guid = made["cells"][0]["guid"]
    image = place(lay, panel_guid)
    # 絵を下へずらしておく（中心がコマの下半分にある）
    lay.evaluate("""(g) => { const o = canvas.getObjects().find(x => x.guid === g); o.top += canvas.getHeight() * 0.15;
        o.setCoords(); saveInitialState(o); flushHistory(); }""", image["guid"])
    before = picture(lay, image["guid"])
    cut = """(g) => { document.getElementById('tiltRandom').value = 0; document.getElementById('cutChangeRate').value = 0;
        const ok = blindSplitPanel(canvas.getObjects().find(o => o.guid === g), false); canvas.requestRenderAll();
        saveStateByManual(); flushHistory(); return ok; }"""
    assert lay.evaluate(cut, panel_guid) is True                    # 上流のナイフ（横に切る）
    lay.wait_for_function("() => canvas.getObjects().filter(o => o.isPanel).length === 2")
    after = picture(lay, image["guid"])
    same = near((after["left"], after["top"], after["scaleX"]), (before["left"], before["top"], before["scaleX"]), tol=1e-6)
    lower = max(panels(lay), key=lambda p: min(q[1] for q in p["pts"]))
    if env.variant == "plain":
        assert not same                                             # 上流の元の挙動: 大きいほうのコマの中央に入れ直す
        return
    assert same and after["panel"] == lower["guid"]                 # 位置と大きさを保ち、中心のある側（下）のコマに入る
    assert after["clip"][1] >= min(q[1] for q in lower["pts"]) - 0.5
    # 設定で元の挙動にも戻せる
    undo(lay)
    lay.wait_for_function("() => canvas.getObjects().filter(o => o.isPanel).length === 1")
    open_section(lay)
    lay.select_option("#ikl-knife", "refit")
    assert lay.evaluate("() => window.inkdeusKnifeRefit") is True
    assert lay.evaluate(cut, lay.evaluate("() => canvas.getObjects().find(o => o.isPanel).guid")) is True
    refit = picture(lay, image["guid"])
    assert not near((refit["left"], refit["top"], refit["scaleX"]), (before["left"], before["top"], before["scaleX"]), tol=1e-6)
    lay.select_option("#ikl-knife", "keep")
    assert lay.evaluate("() => window.inkdeusKnifeRefit") is False
