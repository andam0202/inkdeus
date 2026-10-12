"""コマ割りの計算（web/manga-bridge/layout-core.js）: 漫画エディタなしで、空のページの上で関数を直接呼ぶ"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[1] / "web" / "manga-bridge" / "layout-core.js"
W, H = 700.0, 1000.0
S = min(W, H)


@pytest.fixture(scope="module")
def core(browser):
    page = browser.new_page()
    page.add_script_tag(path=str(CORE))
    page.wait_for_function("() => !!window.inkdeusLayoutCore")
    yield page
    page.close()


def call(page, body: str, *args):
    """`c` を計算の部品として、body（JavaScript の式・文）を実行する"""
    return page.evaluate(f"(args) => {{ const c = window.inkdeusLayoutCore; const [a, b, d, e] = args; {body} }}", list(args))


def compute(page, layout, w=W, h=H) -> dict:
    return call(page, "return c.compute(a, b, d);", layout, w, h)


def boxes(result) -> dict:
    out = {}
    for cid, cell in result["cells"].items():
        xs = [p[0] for p in cell["pts"]]
        ys = [p[1] for p in cell["pts"]]
        out[cid] = (min(xs), min(ys), max(xs), max(ys))
    return out


def flat(value) -> list:
    """入れ子の並び・辞書を、数値の 1 列にする（pytest.approx は入れ子を比べられないため）"""
    if isinstance(value, dict):
        return [x for key in sorted(value) for x in flat(value[key])]
    if isinstance(value, (list, tuple)):
        return [x for item in value for x in flat(item)]
    return [value]


def near(a, b, tol: float = 1e-6) -> bool:
    return flat(a) == pytest.approx(flat(b), abs=tol)


def overlap(a, b) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


def total_area(page, result) -> float:
    return sum(call(page, "return c.area(a);", cell["pts"]) for cell in result["cells"].values())


def ratios_ok(node) -> bool:
    if "children" not in node:
        return True
    return abs(sum(node["ratios"]) - 1) < 1e-9 and len(node["ratios"]) == len(node["children"]) \
        and all(r > 0 for r in node["ratios"]) and all(ratios_ok(ch) for ch in node["children"])


def test_grid_cells_fill_the_page_minus_gutters_and_margins(core):
    layout = call(core, "return c.grid('2,1,3', {gutter: {v: 0.04, h: 0.02}, margin: {top: 0.05, right: 0.06, bottom: 0.07, left: 0.08}});")
    result = compute(core, layout)
    assert len(result["cells"]) == 6 and len(result["bounds"]) == 5
    b = boxes(result)
    # 外周の余白（短辺に対する割合）
    assert min(x[0] for x in b.values()) == pytest.approx(0.08 * S) and min(x[1] for x in b.values()) == pytest.approx(0.05 * S)
    assert max(x[2] for x in b.values()) == pytest.approx(W - 0.06 * S) and max(x[3] for x in b.values()) == pytest.approx(H - 0.07 * S)
    # 重ならない
    for p, q in itertools.combinations(b.values(), 2):
        assert overlap(p, q) < 1e-6
    # 面積の合計 = 内側の面積 − 間隔の分（行の間 2 本、列の間は行の高さ × (コマ数 − 1) 本）
    inner_w, inner_h = W - 0.14 * S, H - 0.12 * S
    gv, gh = 0.04 * S, 0.02 * S
    row_h = (inner_h - 2 * gv) / 3
    expected = inner_w * inner_h - 2 * gv * inner_w - row_h * gh * (1 + 0 + 2)
    assert total_area(core, result) == pytest.approx(expected)
    # 同じ行のコマは同じ高さ、行の中では幅が等しい
    order = call(core, "return c.order(a);", layout)
    top_row = sorted(order[:2], key=lambda i: b[i][0])
    assert b[top_row[0]][3] - b[top_row[0]][1] == pytest.approx(row_h)
    assert b[top_row[1]][0] - b[top_row[0]][2] == pytest.approx(gh)
    assert ratios_ok(layout["root"])


def test_reading_order_is_top_to_bottom_and_right_to_left(core):
    layout = call(core, "return c.grid([2, 3]);")
    b = boxes(compute(core, layout))
    order = call(core, "return c.order(a);", layout)
    rows = [order[:2], order[2:]]
    assert b[rows[0][0]][1] < b[rows[1][0]][1]                              # 上の行が先
    for row in rows:
        xs = [b[i][0] for i in row]
        assert xs == sorted(xs, reverse=True)                                # 右から左
    ltr = call(core, "const L = c.clone(a); L.rtl = false; return c.order(L);", layout)
    assert [b[i][0] for i in ltr[:2]] == sorted(b[i][0] for i in ltr[:2])     # 左綴じなら左から右


def test_moving_a_boundary_keeps_the_gutter_and_respects_the_minimum(core):
    layout = call(core, "return c.grid('1,1,1', {gutter: {v: 0.03, h: 0.02}});")
    before = compute(core, layout)
    key = sorted(before["bounds"])[0]
    bound = before["bounds"][key]
    moved = call(core, "return c.moveBoundary(a, b, 60, d, e);", layout, key, W, H)
    after = compute(core, moved)
    assert after["bounds"][key]["pos"] == pytest.approx(bound["pos"] + 60)
    ids = call(core, "return c.order(a);", moved)
    b = boxes(after)
    assert b[ids[0]][3] - b[ids[0]][1] == pytest.approx(boxes(before)[ids[0]][3] - boxes(before)[ids[0]][1] + 60)
    assert b[ids[1]][1] - b[ids[0]][3] == pytest.approx(0.03 * S)             # 間隔は同じ
    assert near(b[ids[2]], boxes(before)[ids[2]])                  # 関係ないコマは動かない
    assert ratios_ok(moved["root"])
    # 最小の大きさを下回らない
    far = compute(core, call(core, "return c.moveBoundary(a, b, 5000, d, e);", layout, key, W, H))
    fb = boxes(far)
    assert fb[ids[1]][3] - fb[ids[1]][1] == pytest.approx(call(core, "return c.MIN_CELL;") * S)
    back = compute(core, call(core, "return c.moveBoundary(a, b, -5000, d, e);", layout, key, W, H))
    assert boxes(back)[ids[0]][3] - boxes(back)[ids[0]][1] == pytest.approx(call(core, "return c.MIN_CELL;") * S)
    # スナップ: ページの 1/3 の近くへ動かすと、ちょうど 1/3 に吸い付く
    snapped = call(core, "return c.moveBoundary(a, b, d, 700, 1000, {snap: true});", layout, key, H / 3 - bound["pos"] + 7)
    assert compute(core, snapped)["bounds"][key]["pos"] == pytest.approx(H / 3)
    free = call(core, "return c.moveBoundary(a, b, d, 700, 1000, {snap: false});", layout, key, H / 3 - bound["pos"] + 7)
    assert compute(core, free)["bounds"][key]["pos"] == pytest.approx(H / 3 + 7)
    # 元のレイアウトは書き換えない
    assert compute(core, layout)["bounds"][key]["pos"] == pytest.approx(bound["pos"])


def test_tilting_a_boundary_makes_trapezoids_with_a_constant_gutter(core):
    layout = call(core, "return c.grid('1,2', {gutter: {v: 0.04, h: 0.02}});")
    key = next(k for k, v in compute(core, layout)["bounds"].items() if v["dir"] == "v")
    tilted = call(core, "return c.setTilt(a, b, 1, 40, d, e);", layout, key, W, H)          # 右端を 40px 下げる
    result = compute(core, tilted)
    assert near(result["bounds"][key]["tilt"], [0, 40 / S])
    ids = call(core, "return c.order(a);", tilted)
    top = result["cells"][ids[0]]["pts"]                     # 左上・右上・右下・左下
    assert top[0][1] == pytest.approx(top[1][1])                                           # 上の辺は水平のまま
    assert top[2][1] - top[3][1] == pytest.approx(40)                                      # 下の辺は右下がり（台形）
    # 下の行の 2 コマ: 上の辺が同じ傾きで、縦に測った間隔はどこでも同じ
    for cid in ids[1:]:
        pts = result["cells"][cid]["pts"]
        for corner in (pts[0], pts[1]):
            f = (corner[0] - top[3][0]) / (top[2][0] - top[3][0])
            assert corner[1] - (top[3][1] + 40 * f) == pytest.approx(0.04 * S)
        assert pts[2][1] == pytest.approx(pts[3][1])                                       # 下の辺は水平
    for p, q in itertools.combinations([c["pts"] for c in result["cells"].values()], 2):
        assert not any(call(core, "return c.contains(a, b[0], b[1]);", p, pt) for pt in q)  # 重ならない
    # まっすぐに戻す
    straight = call(core, "return c.resetTilt(a, b, d, e);", tilted, key, W, H)
    assert "tilt" not in straight["root"] and near(boxes(compute(core, straight)), boxes(compute(core, layout)))
    # 傾きは、隣のコマが最小の大きさを下回らない所で止まる
    extreme = compute(core, call(core, "return c.setTilt(a, b, 0, -5000, d, e);", layout, key, W, H))
    assert min(p[1] for p in extreme["cells"][ids[0]]["pts"][2:]) - extreme["cells"][ids[0]]["pts"][0][1] \
        == pytest.approx(call(core, "return c.MIN_CELL;") * S)


def test_split_keeps_the_other_cells_and_the_gutter(core):
    layout = call(core, "return c.grid('2,1', {gutter: {v: 0.03, h: 0.02}});")
    before = boxes(compute(core, layout))
    order = call(core, "return c.order(a);", layout)
    right = order[0]
    # 同じ向きの節の中（行の中で左右に割る）: 兄弟として増える。ほかのコマの大きさは変わらない
    r = call(core, "return c.split(a, b, 'h', 0.25, d, e);", layout, right, W, H)
    after = boxes(compute(core, r["layout"]))
    assert r["first"] == right and len(after) == 4
    assert near(after[order[1]], before[order[1]]) and near(after[order[2]], before[order[2]])
    width = before[right][2] - before[right][0]
    assert after[r["first"]][0] == pytest.approx(before[right][0]) and after[r["second"]][2] == pytest.approx(before[right][2])
    assert after[r["second"]][0] - after[r["first"]][2] == pytest.approx(0.02 * S)
    assert (after[r["first"]][2] + after[r["second"]][0]) / 2 == pytest.approx(before[right][0] + width * 0.25)   # 押した位置
    assert len(r["layout"]["root"]["children"][0]["children"]) == 3 and ratios_ok(r["layout"]["root"])
    # 違う向き（行の中のコマを上下に割る）: そのコマの場所に新しい節ができる
    r2 = call(core, "return c.split(a, b, 'v', null, d, e);", layout, right, W, H)
    after2 = boxes(compute(core, r2["layout"]))
    assert after2[r2["second"]][1] - after2[r2["first"]][3] == pytest.approx(0.03 * S)
    assert after2[r2["first"]][3] - after2[r2["first"]][1] == pytest.approx(after2[r2["second"]][3] - after2[r2["second"]][1])
    assert after2[r2["first"]][1] == pytest.approx(before[right][1]) and after2[r2["second"]][3] == pytest.approx(before[right][3])
    assert call(core, "return c.order(a);", r2["layout"])[:2] == [r2["first"], r2["second"]]
    # 小さすぎるコマは割れない
    tiny = call(core, "return c.grid('12');")
    with pytest.raises(Exception, match="小さすぎて"):
        call(core, "let L = a; for (let i = 0; i < 6; i++) L = c.split(L, c.order(L)[0], 'h', 0.5, b, d).layout; return L;", tiny, W, H)


def test_merge_and_remove(core):
    layout = call(core, "return c.grid('3,1', {gutter: {v: 0.03, h: 0.02}});")
    before = boxes(compute(core, layout))
    order = call(core, "return c.order(a);", layout)               # 右・中・左・下
    r = call(core, "return c.merge(a, b, d, 700, 1000);", layout, order[0], order[1])
    after = boxes(compute(core, r["layout"]))
    assert len(after) == 3 and r["kept"] == order[1] and r["removed"] == order[0]        # 残るのは左／上の側
    assert near(after[r["kept"]], (before[order[1]][0], before[order[1]][1], before[order[0]][2], before[order[0]][3]))
    assert near(after[order[2]], before[order[2]]) and ratios_ok(r["layout"]["root"])
    # 2 つしか無い節で結合すると、節がたたまれる
    two = call(core, "return c.grid('2,1');")
    o2 = call(core, "return c.order(a);", two)
    merged = call(core, "return c.merge(a, b, d, 700, 1000).layout;", two, o2[0], o2[1])
    assert [("children" in ch) for ch in merged["root"]["children"]] == [False, False]
    assert len(boxes(compute(core, merged))) == 2
    # 隣り合っていない・別の行のコマは結合できない
    for pair in ((order[0], order[2]), (order[0], order[3]), (order[0], order[0])):
        with pytest.raises(Exception, match="隣り合う"):
            call(core, "return c.merge(a, b, d, 700, 1000);", layout, *pair)
    # 削除: 兄弟が比率に応じて埋める
    removed = call(core, "return c.remove(a, b);", layout, order[1])
    rb = boxes(compute(core, removed))
    assert len(rb) == 3
    assert rb[order[0]][2] == pytest.approx(before[order[0]][2]) and rb[order[2]][0] == pytest.approx(before[order[2]][0])
    assert rb[order[0]][0] - rb[order[2]][2] == pytest.approx(0.02 * S)
    assert rb[order[0]][2] - rb[order[0]][0] == pytest.approx(rb[order[2]][2] - rb[order[2]][0])
    assert ratios_ok(removed["root"])
    # 最後の 1 つを消すと、空になる
    one = call(core, "return c.grid('1');")
    assert call(core, "return c.remove(a, c.order(a)[0]).root;", one) is None


def test_gutter_margin_and_bleed(core):
    layout = call(core, "return c.grid('2,2');")
    wide = call(core, "return c.setMargin(c.setGutter(a, {v: 0.1, h: 0.05}), {top: 0.1, left: 0.02});", layout)
    b = boxes(compute(core, wide))
    order = call(core, "return c.order(a);", wide)
    assert b[order[2]][1] - b[order[0]][3] == pytest.approx(0.1 * S) and b[order[0]][0] - b[order[1]][2] == pytest.approx(0.05 * S)
    assert b[order[0]][1] == pytest.approx(0.1 * S) and b[order[1]][0] == pytest.approx(0.02 * S)
    assert wide["margin"]["right"] == layout["margin"]["right"]                # 書かなかった辺はそのまま
    for bad in ("c.setGutter(a, {v: 0.5})", "c.setGutter(a, {h: -0.1})", "c.setMargin(a, {top: 0.9})", "c.setGutter(a, {v: '3'})"):
        with pytest.raises(Exception, match="範囲|数値"):
            call(core, f"return {bad};", layout)
    # 断ち切り: ページの端に接している辺だけが、端まで伸びる
    top_right = order[0]
    result = compute(core, layout)
    assert result["cells"][top_right]["edges"] == {"top": True, "right": True, "bottom": False, "left": False}
    bled = call(core, "let L = c.setBleed(a, b, 'top', true); L = c.setBleed(L, b, 'right', true); return c.setBleed(L, b, 'left', true);",
                layout, top_right)
    bb = boxes(compute(core, bled))
    base = boxes(result)
    assert bb[top_right][1] == 0 and bb[top_right][2] == W
    assert bb[top_right][0] == pytest.approx(base[top_right][0]) and bb[top_right][3] == pytest.approx(base[top_right][3])
    assert all(near(bb[i], base[i]) for i in order[1:])
    off = call(core, "return c.setBleed(c.setBleed(c.setBleed(a, b, 'top', false), b, 'right', false), b, 'left', false);", bled, top_right)
    assert near(boxes(compute(core, off)), base) and "bleed" not in call(core, "return c.find(a, b).node;", off, top_right)


def test_free_cells_keep_their_shape_and_can_be_relinked(core):
    layout = call(core, "return c.grid('1,1');")
    order = call(core, "return c.order(a);", layout)
    shape = [[0.1, 0.1], [0.9, 0.2], [0.8, 0.45], [0.15, 0.4]]
    free = call(core, "return c.setFree(a, b, d);", layout, order[0], shape)
    wider = call(core, "return c.setGutter(c.setMargin(a, {top: 0.2}), {v: 0.1});", free)
    result = compute(core, wider)
    assert result["cells"][order[0]]["free"] is True
    assert near(result["cells"][order[0]]["pts"], [[x * W, y * H] for x, y in shape])     # 間隔や余白を変えても動かない
    assert not near(boxes(result)[order[1]], boxes(compute(core, layout))[order[1]])
    back = call(core, "return c.clearFree(a, b);", wider, order[0])
    assert compute(core, back)["cells"][order[0]]["free"] is False
    assert boxes(compute(core, back))[order[0]][1] == pytest.approx(0.2 * S)


def test_layout_survives_json_and_scales_with_the_page(core):
    layout = call(core, """let L = c.grid('2,1,3');
        L = c.split(L, c.order(L)[2], 'h', 0.4, 700, 1000).layout;
        L = c.setTilt(L, Object.keys(c.compute(L, 700, 1000).bounds)[0], 0, 25, 700, 1000);
        L = c.setBleed(L, c.order(L)[0], 'top', true);
        return c.assign(L, c.order(L)[1], 'guid-1');""")
    again = call(core, "return c.check(JSON.parse(JSON.stringify(a)));", layout)
    assert again == layout                                                    # JSON にして戻しても同じ
    small, large = compute(core, layout, 350, 500), compute(core, layout, 1400, 2000)
    for cid in small["cells"]:
        assert near([[x * 4, y * 4] for x, y in small["cells"][cid]["pts"]], large["cells"][cid]["pts"])
    # 横長のページでは、間隔と余白は短辺（高さ）が基準になる
    wide = boxes(compute(core, call(core, "return c.grid('1,1', {gutter: {v: 0.05}, margin: {left: 0.1}});"), 1000, 500))
    tops = sorted(wide.values(), key=lambda r: r[1])
    assert tops[1][1] - tops[0][3] == pytest.approx(25) and tops[0][0] == pytest.approx(50)


def test_check_rejects_broken_layouts_and_fills_in_ids(core):
    made = call(core, "return c.check({root: {dir: 'h', children: [{}, {dir: 'v', ratios: [2, 1], children: [{guid: 'x'}, {}]}]}});")
    assert len(call(core, "return c.leaves(a);", made)) == 3 and ratios_ok(made["root"])
    assert near(made["root"]["children"][1]["ratios"], [2 / 3, 1 / 3])
    ids = [made["root"]["id"], made["root"]["children"][0]["id"], made["root"]["children"][1]["id"]]
    assert len(set(ids)) == 3 and made["gutter"] == call(core, "return c.DEFAULTS.gutter;")
    assert call(core, "return c.check({root: {dir: 'v', children: [{guid: 'only'}]}}).root.guid;") == "only"     # 子が 1 つの節はたたむ
    for bad, word in (("[]", "JSON オブジェクト"), ("{root: {dir: 'x', children: [{}]}}", "dir"),
                      ("{root: {dir: 'v', ratios: [1], children: [{}, {}]}}", "ratios"),
                      ("{root: {dir: 'v', ratios: [1, -1], children: [{}, {}]}}", "ratios"),
                      ("{root: {dir: 'v', children: []}}", "子は"),
                      ("{root: {dir: 'v', children: [{guid: 'a'}, {guid: 'a'}]}}", "2 回"),
                      ("{gutter: {v: 3}, root: {}}", "範囲")):
        with pytest.raises(Exception, match=word):
            call(core, f"return c.check({bad});")
    for bad in ("''", "'2,x'", "'0'", "'13'", "'1,1,1,1,1,1,1,1,1,1,1,1,1'"):
        with pytest.raises(Exception, match="コマ数|行数"):
            call(core, f"return c.parseCounts({bad});")
    assert call(core, "return c.parseCounts(' 2, 1,3 ');") == [2, 1, 3]


def test_infer_rebuilds_the_tree_from_rectangles(core):
    source = call(core, "return c.grid('2,1,3', {gutter: {v: 0.04, h: 0.02}, margin: {top: 0.06, right: 0.05, bottom: 0.07, left: 0.03}});")
    result = compute(core, source)
    rects = [{"l": b[0], "t": b[1], "r": b[2], "b": b[3], "guid": cid} for cid, b in boxes(result).items()]
    rects.reverse()
    found = call(core, "return c.infer(a, b, d);", rects, W, H)
    assert near(found["gutter"], {"v": 0.04, "h": 0.02}) and near(found["margin"], source["margin"])
    again = compute(core, found)
    by_guid = {cell["guid"]: cid for cid, cell in again["cells"].items()}
    for guid, box in boxes(result).items():
        assert near(boxes(again)[by_guid[guid]], box)       # 取り込んでも位置は変わらない
    assert [again["cells"][i]["guid"] for i in call(core, "return c.order(a);", found)] == call(core, "return c.order(a);", source)
    # 行にも列にも分けられない配置（風車形）は取り込めない
    pinwheel = [{"l": 0, "t": 0, "r": 60, "b": 30, "guid": "a"}, {"l": 70, "t": 0, "r": 100, "b": 60, "guid": "b"},
                {"l": 40, "t": 70, "r": 100, "b": 100, "guid": "c"}, {"l": 0, "t": 40, "r": 30, "b": 100, "guid": "d"},
                {"l": 40, "t": 40, "r": 60, "b": 60, "guid": "e"}]
    assert call(core, "return c.infer(a, 100, 100);", pinwheel) is None
    assert call(core, "return c.infer([], 100, 100);") is None
