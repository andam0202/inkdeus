"""消しゴム・ペン（実際のブラウザで描いて、出力画素を確かめる）。画像はすべて合成"""

from __future__ import annotations

import json
import os
import time

import numpy as np
import pytest
from PIL import Image

import synth
from conftest import BLANK, deep_merge, layer, open_page, pixels, serve
from inkdeus import cli
from inkdeus.folders import FolderDef
from inkdeus.server import create_app

W, H = 256, 192
ID_A, ID_B, ID_BG = "aaaa1111", "bbbb2222", "cccc3333"


def mix_image() -> np.ndarray:
    """左半分が暗い灰色（ベタになる）、右半分が明るい灰色（ハッチが見える）。
    右半分の中の明るさの段差（x = 192）に線画が出る"""
    img = np.full((H, W, 3), 150, np.uint8)
    img[:, : W // 2] = 40
    img[:, 192:] = 215
    return img


# 層 A（水平の線）と層 B（垂直の線）・ベタ（左半分）・線画（境目）がすべて出る設定
MIX = deep_merge(BLANK, {
    "lines": {"enabled": True, "sigma": 1.0},
    "solid": {"enabled": True, "threshold": 0.3},
    "layers": [layer(id=ID_A, threshold=0.9, angle=0, spacing=4, width=1),
               layer(id=ID_B, threshold=0.9, angle=90, spacing=4, width=1)],
})


def variant(**off) -> dict:
    """MIX から、指定した物だけを切った設定（その対象を「全部消した」ときの見本）"""
    p = json.loads(json.dumps(MIX))
    layers = p["regions"]["person"]["layers"]
    if off.get("a"):
        layers[0]["enabled"] = False
    if off.get("b"):
        layers[1]["enabled"] = False
    if off.get("solid"):
        p["regions"]["person"]["solid"]["enabled"] = False
    if off.get("lines"):
        p["lines"]["enabled"] = False
    return p


@pytest.fixture()
def live(tmp_path, browser):
    images, masks, more = tmp_path / "images", tmp_path / "masks", tmp_path / "more"
    synth.save(images / "mix.png", mix_image())
    synth.save(images / "other.png", mix_image())
    img, mask = synth.scene(320, 240)
    synth.save(images / "scene.png", img)
    synth.save(masks / "scene.png", mask)
    synth.save(more / "mix.png", mix_image())                  # 別のフォルダの、同じ名前の画像
    out = tmp_path / "out"
    server, url = serve(create_app(images, mask_dir=masks, out_dir=out, folders=[FolderDef("別", more)]))
    pages = []

    def open_(query: str = "?image=mix.png", **kw):
        page = open_page(browser, url + query, **kw)
        pages.append(page)
        return page

    yield {"open": open_, "url": url, "port": server.server_port, "out": out, "app": server.app, "masks": masks}
    for page in pages:
        page.close()
    server.shutdown()


def show(page, params: dict) -> np.ndarray:
    page.evaluate("(p) => { window.inkdeus.setParams(p); }", params)
    return pixels(page)


def settled(page) -> np.ndarray:
    """画面の操作（キー・ボタン）のあと、予約された再描画を済ませてから読む"""
    page.evaluate("() => { window.inkdeus.render(); }")
    return pixels(page)


def paint(page) -> dict:
    return page.evaluate("() => window.inkdeus.paint.state()")


def drag(page, points, **kw) -> None:
    """画像の座標の並びを、マウスでなぞる"""
    pts = [page.evaluate("([x, y]) => window.inkdeus.paint.clientPoint(x, y)", p) for p in points]
    page.mouse.move(*pts[0])
    page.mouse.down(**kw)
    for pt in pts[1:]:
        page.mouse.move(*pt, steps=6)
    page.mouse.up(**kw)


def eraser(page, kind: str, mode: str = "erase", **values) -> None:
    page.evaluate("""([kind, mode, values]) => { const p = window.inkdeus.paint; p.setTool('eraser'); p.setEraseTarget(kind);
        if (p.state().eraseMode !== mode) p.toggle(); p.set('eraser', values); }""",
                  [kind, mode, {"size": 40, "hardness": 1, "strength": 1, **values}])


def pen(page, color: str = "black", **values) -> None:
    page.evaluate("""([color, values]) => { const p = window.inkdeus.paint; p.setTool('pen');
        if (p.state().penColor !== color) p.toggle(); p.set('pen', values); }""",
                  [color, {"size": 12, "hardness": 1, **values}])


ACROSS = [(10, 96), (246, 96)]                 # 画像を横切る1筆（太さ 40 なら、行 77〜115 あたりが帯になる）
BAND = (slice(80, 112), slice(20, 236))        # 帯の内側（確実に全部消えている所）
ABOVE, BELOW = slice(0, 70), slice(122, H)     # 帯の外


def same_outside(a: np.ndarray, b: np.ndarray) -> bool:
    return bool((a[ABOVE] == b[ABOVE]).all() and (a[BELOW] == b[BELOW]).all())


# ---- 消しゴム ----

def test_erasing_a_selected_layer_removes_only_that_layer(live):
    page = live["open"]()
    no_a, no_b = show(page, variant(a=True)), show(page, variant(b=True))
    base = show(page, MIX)
    assert (no_a != base).any() and (no_b != base).any()
    assert page.locator("#toolbar #toolHand.on").count() == 1 and paint(page)["tool"] == "hand"
    # 層を選ばずに消そうとすると、選び方を知らせるだけで何も消えない
    eraser(page, "layer")
    drag(page, ACROSS)
    assert (pixels(page) == base).all() and paint(page)["targets"] == []
    assert "層の行をクリック" in page.evaluate("() => document.getElementById('messages').textContent")
    # 層の行をクリックして選ぶ（行の強調で示す。ソロ表示にはならない）
    page.locator('.layer[data-layer="0"] > summary .title').click()
    assert paint(page)["selected"] == {"region": "person", "id": ID_A}
    assert page.locator('.layer.selected[data-layer="0"]').count() == 1 and page.locator(".layer.selected").count() == 1
    assert page.evaluate("() => window.inkdeus.state.solo") is None
    assert "人物の層1" in page.evaluate("() => document.getElementById('eraseTargetLabel').textContent")
    drag(page, ACROSS)
    got = pixels(page)
    assert (got[BAND] == no_a[BAND]).all()                     # 帯の中は、層 A だけが消えている（層 B・ベタ・線画はそのまま）
    assert same_outside(got, base) and (got != base).any()
    assert paint(page)["targets"] == [f"layer-{ID_A}"]
    # 層の行とツールバーに「消しあり」の印
    assert page.locator(f'.layer[data-layer="0"] .erased[data-erased="layer-{ID_A}"]').count() == 1
    assert page.locator('.layer[data-layer="1"] .erased').count() == 0
    assert page.evaluate("() => document.getElementById('eraseMark').hidden") is False
    # 別の層を選んで消すと、その層だけが消える
    page.locator('.layer[data-layer="1"] > summary .title').click()
    drag(page, [(10, 40), (246, 40)])
    got2 = pixels(page)
    band2 = (slice(24, 56), slice(20, 236))
    assert (got2[band2] == no_b[band2]).all() and (got2[BAND] == no_a[BAND]).all()
    # 「戻す」でなぞると戻る
    eraser(page, "layer", "restore", size=60)
    drag(page, [(10, 40), (246, 40)])
    assert (pixels(page) == got).all()
    page.locator('.layer[data-layer="0"] > summary .title').click()
    drag(page, ACROSS)
    assert (pixels(page) == base).all()
    assert page.errors == []


def test_solid_lines_and_all_targets(live):
    page = live["open"]()
    no_solid, no_lines = show(page, variant(solid=True)), show(page, variant(lines=True))
    base = show(page, MIX)
    assert (no_solid[BAND] != base[BAND]).any() and (no_lines[BAND] != base[BAND]).any()
    # ベタ
    eraser(page, "solid")
    drag(page, ACROSS)
    got = pixels(page)
    assert (got[BAND] == no_solid[BAND]).all() and same_outside(got, base)
    assert paint(page)["targets"] == ["solid-person"]
    assert page.locator('[data-group="solid"] .erased[data-erased="solid-person"]').count() == 1
    eraser(page, "solid", "restore", size=60)
    drag(page, ACROSS)
    assert (pixels(page) == base).all()
    # 線画
    eraser(page, "lines")
    drag(page, ACROSS)
    got = pixels(page)
    assert (got[BAND] == no_lines[BAND]).all() and same_outside(got, base)
    assert page.locator('.erased[data-erased="lines"]').count() == 1
    eraser(page, "lines", "restore", size=60)
    drag(page, ACROSS)
    assert (pixels(page) == base).all()
    # すべて: その場所が白になる
    eraser(page, "all")
    drag(page, ACROSS)
    got = pixels(page)
    assert (got[BAND] == 255).all() and same_outside(got, base)
    # 強さ 0.5: 半分まで薄くなる（左半分のベタ = 黒 が、中間の灰色になる）
    eraser(page, "all", strength=0.5)
    drag(page, [(10, 40), (246, 40)])
    half = pixels(page)[24:56, 20:100]
    assert (np.abs(half.astype(int) - 128) <= 2).all(), half
    # 同じ所を強さ 0.5 でもう一度なぞっても、それ以上は薄くならない（強さ = 消す量の上限）
    drag(page, [(10, 40), (246, 40)])
    assert (pixels(page)[24:56, 20:100] == half).all()
    # 「この対象の消しゴムを全部取り消す」
    page.locator("#eraseClear").click()
    assert (pixels(page) == base).all() and "all" not in paint(page)["targets"]
    assert page.errors == []


SCENE = deep_merge(BLANK, {
    "layers": [layer(id=ID_A, threshold=1.0, angle=0, spacing=4, width=1)],
    "background": {"linked": False, "mode": "keep", "solid": {"enabled": False},
                   "layers": [layer(id=ID_BG, threshold=1.0, angle=90, spacing=4, width=1)]},
})


def test_person_and_background_layers_are_separate(live):
    page = live["open"]("?image=scene.png")
    off = json.loads(json.dumps(SCENE))
    off["regions"]["person"]["layers"][0]["enabled"] = False
    no_person = show(page, off)
    off = json.loads(json.dumps(SCENE))
    off["regions"]["background"]["layers"][0]["enabled"] = False
    no_bg = show(page, off)
    base = show(page, SCENE)
    mask = synth.figure_mask(320, 240)
    band = (slice(135, 165), slice(20, 300))                     # y=150 を横切る帯（胴体と、その左右の背景）
    assert mask[band].any() and (~mask[band]).any()
    # 人物の層を消す: 人物の中だけが変わり、背景の層はそのまま
    eraser(page, "layer")
    page.locator('.layer[data-layer="0"] > summary .title').click()
    assert paint(page)["selected"] == {"region": "person", "id": ID_A}
    drag(page, [(10, 150), (310, 150)])
    got = pixels(page)
    assert (got[band] == no_person[band]).all() and (got[band] != base[band]).any()
    far = np.zeros_like(mask)
    far[:, :60] = True                                            # 人物から離れた背景
    assert (got[far] == base[far]).all()
    assert (got[:120] == base[:120]).all()
    # 背景のタブの層を選ぶと、背景の層だけが消える（同じ番号の層でも、どちらのタブの層かで決まる）
    page.locator('.tabs button[data-tab="background"]').click()
    page.locator('.layer[data-layer="0"] > summary .title').click()
    assert paint(page)["selected"] == {"region": "background", "id": ID_BG}
    assert "背景の層1" in page.evaluate("() => document.getElementById('eraseTargetLabel').textContent")
    drag(page, [(10, 60), (310, 60)])
    got2 = pixels(page)
    band2 = (slice(45, 75), slice(20, 300))                       # 頭と、その左右の背景
    assert (got2[band2] == no_bg[band2]).all() and (got2[band2] != got[band2]).any()
    head = np.zeros_like(mask)
    head[60:75, 150:170] = True
    assert mask[head].all() and (got2[head] == base[head]).all()  # 人物の中の層は残っている
    assert (got2[band] == got[band]).all()
    assert paint(page)["targets"] == [f"layer-{ID_A}", f"layer-{ID_BG}"]
    # ベタの消しゴムは、開いているタブの側（人物 / 背景）に当たる
    assert page.evaluate("() => window.inkdeus.paint.target('solid').target") == "solid-background"
    page.locator('.tabs button[data-tab="person"]').click()
    assert page.evaluate("() => window.inkdeus.paint.target('solid').target") == "solid-person"
    assert page.errors == []


# ---- ペン ----

def column(img: np.ndarray, x: int, value: int) -> int:
    return int((img[:, x] == value).sum())


def test_pen_black_white_and_stacking_order(live):
    page = live["open"]()
    base = show(page, MIX)
    pen(page, "black", size=12)
    drag(page, ACROSS)
    got = pixels(page)
    assert (got[92:100, 20:236] == 0).all()                      # 太さ 12 の黒い線
    assert (got[:86] == base[:86]).all() and (got[106:] == base[106:]).all()
    assert paint(page)["targets"] == ["pen"]
    assert page.evaluate("() => document.getElementById('penMark').hidden") is False
    # 白（修正液）は、ベタの上にも黒いペンの上にも乗る（後から描いた方が勝つ）
    pen(page, "white", size=12)
    drag(page, [(60, 20), (60, 170)])
    got = pixels(page)
    assert (got[30:160, 57:63] == 255).all()
    assert (got[92:100, 70:120] == 0).all()                      # 黒い線のほかの所は残っている
    # その上から黒で描くと、また黒が勝つ
    pen(page, "black", size=12)
    drag(page, [(20, 140), (120, 140)])
    got = pixels(page)
    assert (got[136:144, 30:110] == 0).all() and (got[30:130, 57:63] == 255).all()
    # 「ペンで描いた物を全部消す」
    page.locator("#penClear").click()
    assert (pixels(page) == base).all() and paint(page)["targets"] == []
    assert page.evaluate("() => document.getElementById('penMark').hidden") is True
    assert page.errors == []


def test_brush_size_and_hardness_show_in_the_pixels(live):
    page = live["open"]()
    show(page, MIX)
    x = 60                                                       # 左半分（ベタ = 黒）の上に、白で描いて幅を測る
    widths = {}
    for size in (12, 30):
        pen(page, "white", size=size)
        drag(page, ACROSS)
        widths[size] = column(pixels(page), x, 255)
        page.evaluate("() => window.inkdeus.paint.clear('pen')")
    assert abs(widths[12] - 12) <= 2 and abs(widths[30] - 30) <= 2, widths      # 太いほど広い
    # 硬さ 1 は縁がくっきり、下げると縁がなだらか（中間の灰色が増える）
    soft = {}
    for hardness in (1, 0.2):
        pen(page, "white", size=30, hardness=hardness)
        drag(page, ACROSS)
        col = pixels(page)[:, x].astype(int)
        soft[hardness] = int(((col > 0) & (col < 255)).sum())
        if hardness < 1:
            upper = col[70:97]                                    # 縁 → 中心へ、だんだん白くなる
            assert (np.diff(upper) >= 0).all() and upper[0] == 0 and upper[-1] == 255, upper
        page.evaluate("() => window.inkdeus.paint.clear('pen')")
    assert soft[1] <= 2 and soft[0.2] >= 12, soft
    # 消しゴムも同じ（太さ・硬さ）
    eraser(page, "all", size=20)
    drag(page, ACROSS)
    narrow = column(pixels(page), x, 255)
    page.evaluate("() => window.inkdeus.paint.clear('all')")
    eraser(page, "all", size=60)
    drag(page, ACROSS)
    wide = column(pixels(page), x, 255)
    assert abs(narrow - 20) <= 2 and abs(wide - 60) <= 2, (narrow, wide)
    page.evaluate("() => window.inkdeus.paint.clear('all')")
    eraser(page, "all", size=60, hardness=0)
    drag(page, ACROSS)
    col = pixels(page)[:, x].astype(int)
    assert int(((col > 0) & (col < 255)).sum()) >= 30
    assert page.errors == []


STROKE_WITH_PEN = """([points, type, pressure]) => {
    const c = document.getElementById('gl');
    const fire = (kind, [x, y], buttons) => c.dispatchEvent(new PointerEvent(kind, {
        bubbles: true, clientX: x, clientY: y, pointerId: 7, pointerType: type, pressure: buttons ? pressure : 0,
        button: kind === 'pointermove' ? -1 : 0, buttons, isPrimary: true }));
    const pts = points.map(([x, y]) => window.inkdeus.paint.clientPoint(x, y));
    fire('pointerdown', pts[0], 1);
    for (let i = 1; i <= 20; i++) {
        const t = i / 20;
        fire('pointermove', [pts[0][0] + (pts[1][0] - pts[0][0]) * t, pts[0][1] + (pts[1][1] - pts[0][1]) * t], 1);
    }
    fire('pointerup', pts[1], 0);
}"""


def test_pen_pressure_changes_the_width(live):
    page = live["open"]()
    show(page, MIX)

    def width(pointer: str, pressure: float) -> int:
        page.evaluate(STROKE_WITH_PEN, [ACROSS, pointer, pressure])
        w = column(pixels(page), 60, 255)
        page.evaluate("() => window.inkdeus.paint.clear('pen')")
        return w

    pen(page, "white", size=40)
    assert page.evaluate("() => document.getElementById('penPressure').checked") is True      # 既定でオン
    light, full = width("pen", 0.25), width("pen", 1.0)
    assert abs(light - 10) <= 2 and abs(full - 40) <= 2, (light, full)
    assert abs(width("mouse", 0.5) - 40) <= 2                    # 筆圧の無いマウスは一定
    # チェックを外すと、ペンタブレットでも一定
    page.locator("#penPressure").uncheck()
    assert abs(width("pen", 0.25) - 40) <= 2
    assert page.errors == []


# ---- 取り消し・やり直し ----

def test_undo_redo_mixes_strokes_and_settings_in_order(live):
    page = live["open"]()
    base = show(page, MIX)
    start = page.evaluate("() => window.inkdeus.historyInfo()")
    eraser(page, "all")
    drag(page, ACROSS)
    s1 = pixels(page)
    s2 = show(page, variant(solid=True))                         # 設定の変更（消しゴムはそのまま）
    assert (s2[BAND] == 255).all()
    pen(page, "black", size=12)
    drag(page, [(10, 40), (246, 40)])
    s3 = pixels(page)
    assert (s3[36:44, 20:236] == 0).all()
    info = page.evaluate("() => window.inkdeus.historyInfo()")
    assert info == {"length": start["length"] + 3, "pos": start["pos"] + 3} and paint(page)["steps"] == 2
    assert paint(page)["historyBytes"] > 0
    # 行った順に戻る: ペン → 設定 → 消しゴム
    page.keyboard.press("Control+z")
    assert (settled(page) == s2).all()
    page.locator("#btnUndo").click()
    assert (settled(page) == s1).all()
    assert page.evaluate("() => window.inkdeus.getParams().regions.person.solid.enabled") is True
    page.evaluate("() => window.inkdeus.undo()")
    assert (pixels(page) == base).all()
    # やり直し
    page.keyboard.press("Control+y")
    assert (settled(page) == s1).all()
    page.evaluate("() => window.inkdeus.redo()")
    assert (pixels(page) == s2).all()
    page.locator("#btnRedo").click()
    assert (settled(page) == s3).all() and page.evaluate("() => document.getElementById('btnRedo').disabled") is True
    # 戻してから新しく描くと、やり直しの分は捨てられる
    page.evaluate("() => { window.inkdeus.undo(); window.inkdeus.undo(); }")
    drag(page, [(10, 150), (246, 150)])
    assert page.evaluate("() => window.inkdeus.historyInfo()") == {"length": start["length"] + 2, "pos": start["pos"] + 2}
    assert paint(page)["steps"] == 2
    # 「全部消す」も1件として戻せる
    after = pixels(page)
    page.evaluate("() => window.inkdeus.paint.clear('pen')")
    assert (pixels(page) != after).any()
    page.evaluate("() => window.inkdeus.undo()")
    assert (pixels(page) == after).all()
    assert page.errors == []


def test_layer_delete_duplicate_and_style_with_masks(live):
    page = live["open"]()
    no_a = show(page, variant(a=True))
    base = show(page, MIX)
    eraser(page, "layer")
    page.locator('.layer[data-layer="0"] > summary .title').click()
    drag(page, ACROSS)
    erased = pixels(page)
    page.evaluate("() => window.inkdeus.paint.flush()")
    folder = live["out"] / "mix.paint"
    assert (folder / f"layer-{ID_A}.png").is_file()
    # 複製した層には、マスクは付いてこない（同じ線を引く層が増えるので、帯の中にも線が戻る）
    page.locator('.layer[data-layer="0"]').get_by_title("複製").click()
    ids = page.evaluate("() => window.inkdeus.getParams().regions.person.layers.map((l) => l.id)")
    assert ids[0] == ID_A and ids[2] == ID_B and ids[1] not in (ID_A, ID_B)
    assert (settled(page)[BAND] == base[BAND]).all()
    page.evaluate("() => window.inkdeus.undo()")
    assert (pixels(page) == erased).all()
    # 層を削除すると、その層のマスクも消える（保存先からも）。取り消すとマスクごと戻る
    page.locator('.layer[data-layer="0"]').get_by_title("削除").click()
    assert (settled(page) == no_a).all()
    page.evaluate("async () => { await window.inkdeus.save(); await window.inkdeus.paint.flush(); }")
    assert not (folder / f"layer-{ID_A}.png").exists() and not folder.exists()
    page.evaluate("() => window.inkdeus.undo()")
    assert (pixels(page) == erased).all()
    page.evaluate("async () => { await window.inkdeus.save(); await window.inkdeus.paint.flush(); }")
    assert (folder / f"layer-{ID_A}.png").is_file()
    # 名前付きスタイルを当てると層が入れ替わる（新しい id）ので、古い層のマスクは効かなくなる。取り消すと戻る
    page.evaluate("() => { document.getElementById('selPreset').value = 'hatch1';"
                  " document.getElementById('selPreset').dispatchEvent(new Event('change')); }")
    styled = settled(page)
    new_ids = page.evaluate("() => window.inkdeus.getParams().regions.person.layers.map((l) => l.id)")
    assert len(new_ids) == 1 and new_ids[0] not in (ID_A, ID_B)
    params = page.evaluate("() => window.inkdeus.getParams()")
    page.evaluate("() => window.inkdeus.paint.clear('layer-" + ID_A + "')")      # マスクを消しても、絵は変わらない（＝効いていなかった）
    assert (pixels(page) == styled).all() and page.evaluate("() => window.inkdeus.getParams()") == params
    page.evaluate("() => { window.inkdeus.undo(); window.inkdeus.undo(); }")
    assert (pixels(page) == erased).all()
    assert page.errors == []


# ---- 保存・読み込み・バージョン・フォルダ ----

def wait_for(predicate, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("時間内に条件を満たしませんでした")


def api(live, path: str) -> dict:
    return live["app"].test_client().get(path).get_json()


def targets_on_disk(live, path: str = "/api/paint/mix.png") -> list[str]:
    return [t["target"] for t in api(live, path)["targets"]]


def draw_some(page) -> np.ndarray:
    """層 A の消しゴム・「すべて」の消しゴム・黒と白のペンを1筆ずつ"""
    eraser(page, "layer")
    page.locator('.layer[data-layer="0"] > summary .title').click()
    drag(page, ACROSS)
    eraser(page, "all", size=24, hardness=0.5)
    drag(page, [(30, 30), (220, 60)])
    pen(page, "black", size=8)
    drag(page, [(140, 20), (240, 170)])
    pen(page, "white", size=10, hardness=0.6)
    drag(page, [(20, 150), (120, 130)])
    return pixels(page)


def test_saved_after_a_short_wait_and_reloaded_identically(live):
    page = live["open"]()
    base = show(page, MIX)
    drawn = draw_some(page)
    assert (drawn != base).any()
    folder = live["out"] / "mix.paint"
    # 筆を置いてから少し待つと、描いた対象だけが保存される（手で保存を頼まなくてよい）
    wait_for(lambda: sorted(p.name for p in folder.glob("*")) == ["all.png", "index.json", f"layer-{ID_A}.png", "pen.png"])
    page.wait_for_function("() => !window.inkdeus.paint.state().saving")
    state = paint(page)
    assert state["persistent"] is True and state["onServer"] == ["all", f"layer-{ID_A}", "pen"]
    assert page.evaluate("() => document.getElementById('paintNote').hidden") is True
    with Image.open(folder / "all.png") as im:
        assert im.mode == "L" and im.size == (W, H)             # 消去マスクは 8bit グレースケール（長辺 2048 まで）
    with Image.open(folder / "pen.png") as im:
        assert im.mode == "RGB" and im.size == (W, H)           # ペンは原寸（R = 黒、G = 白）
        rgb = np.asarray(im)
    assert rgb[..., 0].max() == 255 and rgb[..., 1].max() == 255 and (rgb[..., 2] == 0).all()
    index = json.loads((folder / "index.json").read_text(encoding="utf-8"))
    assert [t["target"] for t in index["targets"]] == ["all", f"layer-{ID_A}", "pen"]
    stamps = {p.name: p.stat().st_mtime_ns for p in folder.glob("*.png")}
    # 変わった対象だけが保存し直される
    pen(page, "black", size=8)
    drag(page, [(200, 100), (240, 110)])
    drawn = pixels(page)
    page.evaluate("() => window.inkdeus.paint.flush()")
    after = {p.name: p.stat().st_mtime_ns for p in folder.glob("*.png")}
    assert after["pen.png"] > stamps["pen.png"] and after["all.png"] == stamps["all.png"]
    assert after[f"layer-{ID_A}.png"] == stamps[f"layer-{ID_A}.png"]
    page.evaluate("() => window.inkdeus.save()")
    # 別の画像を開いても引き継がれない（設定は引き継ぐが、消しゴム・ペンは引き継がない）
    page.evaluate("() => window.inkdeus.open('other.png')")
    assert paint(page)["targets"] == [] and (pixels(page) == base).all()
    assert page.evaluate("() => window.inkdeus.getParams().regions.person.layers[0].id") == ID_A      # 設定は引き継いだ
    assert not (live["out"] / "other.paint").exists()
    assert page.locator(".erased").count() == 0
    # 戻ると、描いた物がそのまま出る
    page.evaluate("() => window.inkdeus.open('mix.png')")
    assert (pixels(page) == drawn).all()
    assert paint(page)["targets"] == ["all", f"layer-{ID_A}", "pen"]
    # 読み込み直しても同じ出力
    again = live["open"]()
    assert (pixels(again) == drawn).all()
    assert again.locator(f'.erased[data-erased="layer-{ID_A}"]').count() == 1
    # 「戻す」で全部戻した対象は、保存先からも消える
    eraser(again, "all", "restore", size=80)
    drag(again, [(30, 30), (220, 60)])
    again.evaluate("() => window.inkdeus.paint.flush()")
    assert not (folder / "all.png").exists() and "all" not in paint(again)["targets"]
    assert targets_on_disk(live) == [f"layer-{ID_A}", "pen"]
    assert page.errors == [] and again.errors == []


def test_versions_and_folders_keep_their_own_paint(live):
    page = live["open"]()
    base = show(page, MIX)
    eraser(page, "all")
    drag(page, ACROSS)
    default = pixels(page)
    out = live["out"]
    # バージョンの複製で、描いた物も写る（保存の待ち時間の途中でも）
    page.evaluate("() => window.inkdeus.createVersion('v1')")
    assert (pixels(page) == default).all() and paint(page)["targets"] == ["all"]
    assert (out / "mix@v1.paint" / "all.png").read_bytes() == (out / "mix.paint" / "all.png").read_bytes()
    # v1 にだけ描き足す
    pen(page, "black", size=10)
    drag(page, [(10, 150), (246, 150)])
    v1 = pixels(page)
    page.evaluate("() => window.inkdeus.paint.flush()")
    assert targets_on_disk(live, "/api/paint/mix.png?version=v1") == ["all", "pen"] and targets_on_disk(live) == ["all"]
    # 既定へ戻ると、既定の絵のまま。v1 へ戻ると v1 の絵で、筆の取り消しも続きからできる
    page.evaluate("() => window.inkdeus.openVersion('')")
    assert (pixels(page) == default).all() and paint(page)["targets"] == ["all"]
    page.evaluate("() => window.inkdeus.undo()")
    assert (pixels(page) == base).all()                          # 既定の履歴（消しゴムの1筆）が戻る
    page.evaluate("() => window.inkdeus.redo()")
    page.evaluate("() => window.inkdeus.openVersion('v1')")
    assert (pixels(page) == v1).all()
    page.evaluate("() => window.inkdeus.undo()")
    assert (pixels(page) == default).all()
    page.evaluate("() => window.inkdeus.redo()")
    assert (pixels(page) == v1).all()
    # 名前の変更に付いていき、削除で消える
    page.evaluate("() => window.inkdeus.renameVersion('v1', 'v2')")
    assert not (out / "mix@v1.paint").exists() and (out / "mix@v2.paint" / "pen.png").is_file()
    assert (pixels(page) == v1).all()
    page.evaluate("() => window.inkdeus.deleteVersion('v2')")
    assert not (out / "mix@v2.paint").exists() and page.evaluate("() => window.inkdeus.version()") == ""
    assert (pixels(page) == default).all() and (out / "mix.paint" / "all.png").is_file()
    # 別のフォルダの同じ名前の画像には、何も無い（保存先は <out>/@<フォルダ>/ 以下）
    page.evaluate("() => window.inkdeus.setFolder('別')")
    assert page.evaluate("() => window.inkdeus.info().name") == "mix.png" and paint(page)["targets"] == []
    show(page, MIX)
    assert (pixels(page) == base).all()
    pen(page, "black", size=10)
    drag(page, [(10, 30), (246, 30)])
    other = pixels(page)
    page.evaluate("async () => { await window.inkdeus.save(); await window.inkdeus.paint.flush(); }")
    assert (out / "@別" / "mix.paint" / "pen.png").is_file() and not (out / "mix.paint" / "pen.png").exists()
    page.evaluate("() => window.inkdeus.setFolder('default')")
    assert (pixels(page) == default).all()
    page.evaluate("() => window.inkdeus.setFolder('別')")
    assert (pixels(page) == other).all()
    assert page.errors == []


def read_png(path) -> np.ndarray:
    with Image.open(path) as im:
        assert im.mode == "L"
        return np.asarray(im).copy()


def test_export_and_export_all_include_the_paint(live):
    page = live["open"]()
    show(page, MIX)
    page.evaluate("() => window.inkdeus.save()")
    drawn = draw_some(page)
    out = live["out"]
    r = page.evaluate("() => window.inkdeus.exportPNG()")
    assert (read_png(r["path"]) == drawn).all()                  # 書き出しはプレビューと同じ画素
    # 書き出しの前に保存を済ませてあるので、書き出した白黒版は「最新」
    assert (out / "mix.paint" / "pen.png").is_file()
    row = {i["name"]: i for i in api(live, "/api/images")["images"]}["mix.png"]
    assert row["hasExport"] and row["exportFresh"]
    # 描き足すと「古い」になり、一覧の印も変わる
    pen(page, "black", size=8)
    drag(page, [(10, 180), (246, 180)])
    drawn = pixels(page)
    page.evaluate("() => window.inkdeus.paint.flush()")
    row = {i["name"]: i for i in api(live, "/api/images")["images"]}["mix.png"]
    assert row["hasExport"] and not row["exportFresh"]
    assert page.evaluate("() => window.inkdeus.autoExport().dirty") is True
    # バージョンを作って、そちらにだけ描き足す
    page.evaluate("() => window.inkdeus.createVersion('v1')")
    eraser(page, "all", size=30)
    drag(page, [(128, 10), (128, 180)])
    v1 = pixels(page)
    # 一括書き出し: 各画像・各バージョンが、それぞれの消しゴム・ペンを反映して書き出される
    result = page.evaluate("() => window.inkdeus.exportAll({ force: true })")
    assert result["failed"] == 0 and result["exported"] == 4, result      # mix / mix@v1 / other / scene
    assert (read_png(out / "mix.png") == drawn).all()
    assert (read_png(out / "mix@v1.png") == v1).all() and (v1 != drawn).any()
    page.evaluate("() => window.inkdeus.open('other.png')")
    assert (read_png(out / "other.png") == pixels(page)).all() and paint(page)["targets"] == []
    rows = {i["name"]: i for i in api(live, "/api/images")["images"]}
    assert all(v["exportFresh"] for v in rows["mix.png"]["versions"])
    # 何も変えていなければ、次の一括書き出しでは飛ばす
    result = page.evaluate("() => window.inkdeus.exportAll()")
    assert (result["exported"], result["skipped"]) == (0, 4)
    assert page.errors == []


def test_auto_export_follows_the_strokes(live):
    page = live["open"](auto_export=True)
    out = live["out"]
    show(page, MIX)
    wait_for((out / "mix.png").is_file)
    page.wait_for_function("() => window.inkdeus.autoExport().label === '書き出し済み'")
    before = read_png(out / "mix.png")
    eraser(page, "all")
    drag(page, ACROSS)
    assert page.evaluate("() => window.inkdeus.autoExport().label") == "未反映の変更あり"
    page.wait_for_function("() => window.inkdeus.autoExport().label === '書き出し済み'")
    after = read_png(out / "mix.png")
    assert (after[BAND] == 255).all() and (after == pixels(page)).all() and (before[BAND] != 255).any()
    # 筆の保存が先に済んでいるので、白黒版は「最新」
    assert (out / "mix.paint" / "all.png").stat().st_mtime_ns <= (out / "mix.png").stat().st_mtime_ns
    row = {i["name"]: i for i in api(live, "/api/images")["images"]}["mix.png"]
    assert row["exportFresh"]
    assert page.errors == []


def test_stats_reflect_the_paint(live):
    page = live["open"]()
    show(page, MIX)
    plain = page.evaluate("() => window.inkdeus.stats()")
    assert plain["paint"] == {"targets": [], "penUsed": False}
    eraser(page, "solid", size=120)
    drag(page, ACROSS)
    stats = page.evaluate("() => window.inkdeus.stats()")
    assert stats["paint"] == {"targets": ["solid-person"], "penUsed": False}
    assert stats["output"]["all"]["solidRatio"] < plain["output"]["all"]["solidRatio"] * 0.6      # 最終出力に対する数値
    assert stats["output"]["all"]["blackRatio"] < plain["output"]["all"]["blackRatio"]
    pen(page, "black", size=60)
    drag(page, [(140, 60), (240, 60)])
    stats2 = page.evaluate("() => window.inkdeus.stats()")
    assert stats2["paint"] == {"targets": ["pen", "solid-person"], "penUsed": True}
    assert stats2["output"]["all"]["blackRatio"] > stats["output"]["all"]["blackRatio"]
    assert set(stats2) == set(plain)                             # 既存の項目はそのまま（paint が増えただけ）
    assert page.errors == []


def test_layer_ids_are_filled_on_open_and_saved_without_making_exports_stale(live):
    out = live["out"]
    out.mkdir()
    old = deep_merge(BLANK, {"layers": [layer(threshold=0.9), layer(threshold=0.5, angle=135)]})     # 層の id が入る前の設定
    (out / "mix.inkdeus.json").write_text(json.dumps(old), encoding="utf-8")
    Image.new("L", (W, H), 255).save(out / "mix.png")
    row = {i["name"]: i for i in api(live, "/api/images")["images"]}["mix.png"]
    assert row["hasExport"] and row["exportFresh"]
    stamp = (out / "mix.inkdeus.json").stat().st_mtime_ns
    page = live["open"]()
    saved = json.loads((out / "mix.inkdeus.json").read_text(encoding="utf-8"))
    ids = [l["id"] for l in saved["regions"]["person"]["layers"]]
    assert len(ids) == 2 and len(set(ids)) == 2 and all(len(i) == 8 for i in ids)      # 開いた時点で補って保存してある
    assert page.evaluate("() => window.inkdeus.getParams().regions.person.layers.map((l) => l.id)") == ids
    assert len({l["id"] for r in ("person", "background") for l in saved["regions"][r]["layers"]}) == 2 + 4
    # 見た目は変わらないので、設定ファイルの更新時刻はそのまま＝書き出し済みの白黒版は「最新」のまま
    assert (out / "mix.inkdeus.json").stat().st_mtime_ns == stamp
    row = {i["name"]: i for i in api(live, "/api/images")["images"]}["mix.png"]
    assert row["exportFresh"]
    assert page.evaluate("() => window.inkdeus.state.images.find((i) => i.name === 'mix.png').exportFresh") is True
    # 開き直しても同じ id（消しゴムのマスクと結び付くので、変わってはいけない）
    again = live["open"]()
    assert again.evaluate("() => window.inkdeus.getParams().regions.person.layers.map((l) => l.id)") == ids
    # 並べ替えても id は層に付いていく。追加した層には新しい id
    again.locator('.layer[data-layer="0"]').get_by_title("下へ").click()
    assert again.evaluate("() => window.inkdeus.getParams().regions.person.layers.map((l) => l.id)") == ids[::-1]
    again.locator("#btnAddLayer").click()
    now = again.evaluate("() => window.inkdeus.getParams().regions.person.layers.map((l) => l.id)")
    assert now[:2] == ids[::-1] and now[2] not in ids
    assert page.errors == [] and again.errors == []


# ---- キー操作 ----

def test_keys_switch_tools_and_brackets_change_size_only_while_painting(live):
    page = live["open"]()
    show(page, MIX)
    page.locator("#gl").hover()
    # 手のひらのとき: [ ] は前後の画像、スペースは元画像
    page.keyboard.press("]")
    page.wait_for_function("() => window.inkdeus.info().name === 'other.png'")
    page.keyboard.press("[")
    page.wait_for_function("() => window.inkdeus.info().name === 'mix.png'")
    page.keyboard.down("Space")
    assert page.evaluate("() => window.inkdeus.state.original") is True
    page.keyboard.up("Space")
    assert page.evaluate("() => window.inkdeus.state.original") is False
    # E / B / H でツール、X で 消す⇔戻す・黒⇔白
    page.keyboard.press("e")
    assert paint(page)["tool"] == "eraser" and page.locator("#toolEraser.on").count() == 1
    assert page.evaluate("() => document.getElementById('eraserOpts').hidden") is False
    assert paint(page)["eraseMode"] == "erase"
    page.keyboard.press("x")
    assert paint(page)["eraseMode"] == "restore" and page.locator("#eraseModeRestore.on").count() == 1
    page.keyboard.press("x")
    assert paint(page)["eraseMode"] == "erase"
    # 消しゴムの最中: [ ] は太さ（画像は切り替わらない）
    size = paint(page)["eraser"]["size"]
    page.keyboard.press("]")
    bigger = paint(page)["eraser"]["size"]
    assert bigger > size and page.evaluate("() => window.inkdeus.info().name") == "mix.png"
    assert page.evaluate("() => Number(document.querySelector('#eraserSize input[type=number]').value)") == bigger
    page.keyboard.press("[")
    page.keyboard.press("[")
    assert paint(page)["eraser"]["size"] < size and page.evaluate("() => window.inkdeus.info().name") == "mix.png"
    # 筆の大きさを示す円が出る（太さ × 表示倍率）
    box = page.evaluate("() => { const c = document.getElementById('brushCursor'); return [c.hidden, c.getBoundingClientRect().width]; }")
    zoom = page.evaluate("() => window.inkdeus.state.view.zoom")
    assert box[0] is False and box[1] == pytest.approx(paint(page)["eraser"]["size"] * zoom, rel=0.05)
    page.keyboard.press("b")
    assert paint(page)["tool"] == "pen" and page.evaluate("() => document.getElementById('penOpts').hidden") is False
    assert paint(page)["penColor"] == "black"
    page.keyboard.press("x")
    assert paint(page)["penColor"] == "white" and page.locator("#penWhite.on").count() == 1
    size = paint(page)["pen"]["size"]
    page.keyboard.press("]")
    assert paint(page)["pen"]["size"] > size
    # ツール使用中のスペースは「一時的に手のひら」: ドラッグは移動になり、何も描かれない。元画像は O キー
    base = pixels(page)
    view = page.evaluate("() => ({ ...window.inkdeus.state.view })")
    page.keyboard.down("Space")
    assert page.evaluate("() => window.inkdeus.state.original") is False and paint(page)["space"] is True
    drag(page, [(60, 60), (120, 100)])
    page.keyboard.up("Space")
    moved = page.evaluate("() => ({ ...window.inkdeus.state.view })")
    assert moved["cx"] != view["cx"] and (pixels(page) == base).all() and paint(page)["targets"] == []
    page.keyboard.down("o")
    assert page.evaluate("() => window.inkdeus.state.original") is True
    page.keyboard.up("o")
    assert page.evaluate("() => window.inkdeus.state.original") is False
    # 中ボタンのドラッグも移動
    drag(page, [(60, 60), (100, 60)], button="middle")
    assert page.evaluate("() => window.inkdeus.state.view.cx") != moved["cx"] and paint(page)["targets"] == []
    # スペースを離すと、また描ける
    drag(page, [(60, 60), (120, 60)])
    assert paint(page)["targets"] == ["pen"]
    # 入力欄にフォーカスがあるときは効かない
    page.locator(".gal-search").focus()
    page.keyboard.type("hbe[x")
    assert paint(page)["tool"] == "pen" and paint(page)["penColor"] == "white"
    page.locator(".gal-search").fill("")
    page.locator("#gl").hover()
    page.evaluate("() => document.activeElement.blur()")
    page.keyboard.press("h")
    assert paint(page)["tool"] == "hand" and page.evaluate("() => document.getElementById('brushCursor').hidden") is True
    page.keyboard.press("x")                                     # 手のひらでは何も起きない
    assert paint(page)["penColor"] == "white"
    assert page.errors == []


# ---- ctl paint（開いている画面にも反映される） ----

def test_ctl_paint_reaches_the_open_page(live, capsys):
    page = live["open"]()
    base = show(page, MIX)
    page.evaluate("() => window.inkdeus.save()")
    draw_some(page)
    page.evaluate("() => window.inkdeus.paint.flush()")

    def ctl(*args):
        capsys.readouterr()
        assert cli.main(["ctl", "--port", str(live["port"]), *args]) == 0
        return json.loads(capsys.readouterr().out)

    listed = ctl("paint", "list")                                # 省略時は、いまブラウザで開いている画像・バージョン
    assert [t["target"] for t in listed["targets"]] == ["all", f"layer-{ID_A}", "pen"] and listed["name"] == "mix.png"
    no_pen = None
    r = ctl("paint", "clear", "pen")
    assert r["deleted"] == ["pen"]
    page.wait_for_function("() => { const s = window.inkdeus.paint.state(); return !s.loading && !s.targets.includes('pen'); }")
    no_pen = pixels(page)
    assert (no_pen != base).any() and paint(page)["targets"] == ["all", f"layer-{ID_A}"]
    assert "外部から変更" in page.evaluate("() => document.getElementById('messages').textContent")
    assert paint(page)["steps"] == 0                             # 筆の履歴は、読み直した絵には当てはまらないので消える
    r = ctl("paint", "clear", "all-targets")
    assert sorted(r["deleted"]) == ["all", f"layer-{ID_A}"]
    page.wait_for_function("() => { const s = window.inkdeus.paint.state(); return !s.loading && s.targets.length === 0; }")
    assert (settled(page) == base).all() and not (live["out"] / "mix.paint").exists()
    assert page.locator(".erased").count() == 0
    # 外部から消したあとも、続けて描いて保存できる
    pen(page, "black", size=8)
    drag(page, ACROSS)
    page.evaluate("() => window.inkdeus.paint.flush()")
    assert [t["target"] for t in ctl("paint", "list")["targets"]] == ["pen"]
    assert page.errors == []


# ---- 消しゴム・ペンを保存できない相手（古いサーバ・サーバなし）でも、メモリ上で働く ----

def test_works_in_memory_when_the_server_has_no_paint_api(live, browser):
    requests: list[str] = []

    def old_server(page):
        # paint の API も層の id も知らない、ひとつ前のサーバのふりをする
        page.route("**/api/config", lambda route: route.fulfill(json={"project": None}))
        page.route("**/api/paint/**", lambda route: route.fulfill(status=404, body="Not Found"))
        page.on("request", lambda req: requests.append(f"{req.method} {req.url}"))

    page = live["open"](prepare=old_server)
    base = show(page, MIX)
    drawn = draw_some(page)
    assert (drawn != base).any()
    state = paint(page)
    assert state["persistent"] is False and state["targets"] == ["all", f"layer-{ID_A}", "pen"]
    note = page.evaluate("() => { const n = document.getElementById('paintNote'); return [n.hidden, n.textContent]; }")
    assert note[0] is False and "保存されません" in note[1]
    page.wait_for_timeout(1500)                                  # 保存の待ち時間が過ぎても、何も送らない
    assert not any("/api/paint" in r for r in requests) and not (live["out"] / "mix.paint").exists()
    # 取り消し・やり直しは働く
    page.evaluate("() => window.inkdeus.undo()")
    assert (pixels(page) != drawn).any()
    page.evaluate("() => window.inkdeus.redo()")
    assert (pixels(page) == drawn).all()
    # 書き出しには反映される
    r = page.evaluate("() => window.inkdeus.exportPNG()")
    assert (read_png(r["path"]) == drawn).all()
    # 設定は、層の id を除いて保存する（id を知らないサーバの外部操作が、設定を受け付けなくならないように）
    page.evaluate("() => window.inkdeus.save()")
    saved = json.loads((live["out"] / "mix.inkdeus.json").read_text(encoding="utf-8"))
    assert all("id" not in l for l in saved["regions"]["person"]["layers"])
    assert page.evaluate("() => window.inkdeus.getParams().regions.person.layers[0].id") == ID_A      # 画面の中では持っている
    # 画像を離れると消える
    page.evaluate("() => window.inkdeus.open('other.png')")
    page.evaluate("() => window.inkdeus.open('mix.png')")
    assert paint(page)["targets"] == []
    assert page.errors == []


def test_works_in_memory_without_any_server(browser, tmp_path):
    import functools
    import http.server
    import threading
    from pathlib import Path

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    web = Path(__file__).resolve().parents[1] / "web"
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(web)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    page = browser.new_page(viewport={"width": 1200, "height": 800})
    try:
        page.goto(f"http://127.0.0.1:{httpd.server_port}/")
        page.wait_for_function("window.inkdeus && window.inkdeus.ready")
        page.evaluate("() => window.inkdeus.ready")
        path = synth.save(tmp_path / "mix.png", mix_image())
        page.set_input_files("#fileImage", str(path))
        page.wait_for_function("() => window.inkdeus.info().width === 256")
        base = show(page, MIX)
        assert page.locator("#toolbar #toolEraser").count() == 1
        drawn = draw_some(page)
        assert (drawn != base).any() and paint(page)["persistent"] is False
        assert page.evaluate("() => document.getElementById('paintNote').hidden") is False
        with page.expect_download() as got:                      # 書き出し（ダウンロード）には反映される
            page.evaluate("() => { window.inkdeus.exportPNG(); }")
        saved = tmp_path / "saved.png"
        got.value.save_as(saved)
        assert (read_png(saved) == drawn).all()
        page.evaluate("() => window.inkdeus.undo()")
        assert (pixels(page) != drawn).any()
    finally:
        page.close()
        httpd.shutdown()


# ---- 4 倍の画像 ----

@pytest.mark.skipif(os.environ.get("INKDEUS_SKIP_BIG") == "1", reason="INKDEUS_SKIP_BIG=1")
def test_large_image_strokes_save_and_export(browser, tmp_path, capsys):
    w, h = 5120, 3840
    images = tmp_path / "images"
    img = np.full((h, w, 3), 150, np.uint8)
    img[:, : w // 2] = 40
    synth.save(images / "big.png", img, fast=True)
    del img
    out = tmp_path / "out"
    server, url = serve(create_app(images, out_dir=out))
    page = open_page(browser, url)
    try:
        params = deep_merge(MIX, {"unit": None, "lines": {"enabled": False}})
        t0 = time.time()
        page.evaluate("(p) => { window.inkdeus.setParams(p); }", params)
        page.evaluate("() => window.inkdeus.save()")
        t_render = time.time() - t0
        state = paint(page)
        assert state["maskSize"] == [2048, 1536]                 # 消去マスクは長辺 2048 まで
        times = {}
        t0 = time.time()
        eraser(page, "all", size=40)                             # 基準px 40 × unit 4 = 160px
        drag(page, [(400, 1900), (4700, 1900)])
        times["消しゴム（すべて）"] = time.time() - t0
        t0 = time.time()
        eraser(page, "layer", size=60)
        page.locator('.layer[data-layer="0"] > summary .title').click()
        drag(page, [(2800, 600), (4800, 1200)])
        times["消しゴム（層）"] = time.time() - t0
        t0 = time.time()
        pen(page, "black", size=6)                               # 24px
        drag(page, [(300, 3000), (4800, 3300)])
        times["ペン（黒）"] = time.time() - t0
        t0 = time.time()
        pen(page, "white", size=8)
        drag(page, [(600, 300), (1800, 3500)])
        times["ペン（白）"] = time.time() - t0
        assert paint(page)["targets"] == ["all", f"layer-{ID_A}", "pen"] and paint(page)["steps"] == 4
        history_mb = paint(page)["historyBytes"] / 1e6
        assert history_mb < 64                                   # 触ったタイルの分だけを持つ（全画面ぶんは持たない）
        t0 = time.time()
        page.evaluate("() => window.inkdeus.paint.flush()")
        t_save = time.time() - t0
        folder = out / "big.paint"
        Image.MAX_IMAGE_PIXELS = None
        with Image.open(folder / "all.png") as im:
            assert im.mode == "L" and im.size == (2048, 1536)
        with Image.open(folder / "pen.png") as im:
            assert im.mode == "RGB" and im.size == (w, h)
        t0 = time.time()
        page.evaluate("() => window.inkdeus.undo()")
        page.evaluate("() => window.inkdeus.redo()")
        t_undo = time.time() - t0
        t0 = time.time()
        result = page.evaluate("() => window.inkdeus.exportPNG()")
        t_export = time.time() - t0
        with Image.open(result["path"]) as im:
            assert im.mode == "L" and im.size == (w, h)
            arr = np.asarray(im)
        assert (arr[1840:1960, 500:4600] == 255).all()           # 「すべて」の帯は白
        assert (arr[3207:3219, 3490:3510] == 0).all()            # 黒いペン（右半分の x = 3500 では y ≈ 3213 を通る。太さ 24px）
        assert (arr[3150:3170, 3490:3510] != 0).any()            # その外は元のまま（ハッチの白が残っている）
        assert (arr[1000:1010, 850:870] == 255).all()            # 白いペン（ベタの上）
        assert (arr[100:200, 100:400] == 0).all()                # 触っていない所のベタはそのまま
        assert page.errors == []
        with capsys.disabled():
            info = page.evaluate("() => window.inkdeus.info()")
            print(f"\n[5120x3840 消しゴム・ペン] renderer={info['renderer']}"
                  f"\n  設定の適用と最初の描画: {t_render:.1f}s"
                  + "".join(f"\n  {k}の1筆（ポインタ操作〜正規の合成）: {v:.1f}s" for k, v in times.items())
                  + f"\n  取り消し用に持っている画素: {history_mb:.1f} MB（4 筆）"
                  f"\n  保存（3 対象の PNG 化＋送信）: {t_save:.1f}s"
                  f" / all.png {(folder / 'all.png').stat().st_size / 1e3:.0f} KB・pen.png {(folder / 'pen.png').stat().st_size / 1e3:.0f} KB"
                  f"\n  取り消し＋やり直し: {t_undo:.1f}s"
                  f"\n  書き出し（PNG 化＋保存）: {t_export:.1f}s")
    finally:
        page.close()
        server.shutdown()
