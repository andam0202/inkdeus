"""部分ごとの塗り分け（実際のブラウザで描いて、出力画素を確かめる）。画像と部分マスクはすべて合成"""

from __future__ import annotations

import json
import os
import time
import urllib.request

import numpy as np
import pytest
from PIL import Image

import synth
from conftest import BLANK, deep_merge, layer, no_ids, open_page, pixels, serve
from inkdeus import cli
from inkdeus.folders import FolderDef
from inkdeus.projects import create_project, set_parts
from inkdeus.server import create_app

W, H = 320, 240
SW, SH = 240, 120                 # 6 部分の画像
SIX = [f"p{i}" for i in range(1, 7)]


def figure() -> tuple[np.ndarray, np.ndarray, dict[str, np.ndarray]]:
    """人物っぽい絵と人物マスク・部分マスク（髪・肌・服）。髪の中に、明るい筋（線画が出る）を何本か引く"""
    img, mask = synth.scene(W, H)
    parts = synth.part_masks(W, H)
    for x in (150, 160, 170):
        streak = np.zeros((H, W), bool)
        streak[40:58, x:x + 2] = True
        img[streak & parts["hair"]] = (150, 140, 160)
    return img, mask, parts


def stripes() -> dict[str, np.ndarray]:
    """6 個の部分（縦の帯。幅 30、間に 10 のすき間＝どの部分にも属さない所）"""
    out = {}
    for i, name in enumerate(SIX):
        m = np.zeros((SH, SW), bool)
        m[:, i * 40:i * 40 + 30] = True
        out[name] = m
    return out


@pytest.fixture()
def live(tmp_path, browser):
    images, masks, parts = tmp_path / "images", tmp_path / "masks", tmp_path / "parts"
    img, mask, pm = figure()
    for name in ("fig", "noparts", "zero"):
        synth.save(images / f"{name}.png", img)
        synth.save(masks / f"{name}.png", mask)
    synth.save_parts(parts, "fig", pm)
    synth.save_parts(parts, "zero", {"hair": np.zeros((H, W), bool)})        # 未検出の画像: ファイルはあるが全 0
    synth.save(images / "plain.png", synth.gradient(W, 64))                   # マスクも部分も無い
    six, six_parts = tmp_path / "six", tmp_path / "six_parts"
    synth.save(six / "bands.png", synth.flat(SW, SH, 110))
    synth.save_parts(six_parts, "bands", stripes())
    out = tmp_path / "out"
    project = {"name": "部分の本", "path": str(tmp_path)}
    server, url = serve(create_app(images, mask_dir=masks, out_dir=out, project=project, parts_dir=parts,
                                   folders=[FolderDef("six", six, None, "", six_parts)]))
    pages = []

    def open_(query: str = "?image=fig.png", **kw):
        page = open_page(browser, url + query, **kw)
        pages.append(page)
        return page

    yield {"open": open_, "url": url, "port": server.server_port, "out": out, "parts": pm, "mask": mask,
           "parts_dir": parts, "root": tmp_path}
    for page in pages:
        page.close()
    server.shutdown()


def show(page, params: dict) -> np.ndarray:
    page.evaluate("(p) => { window.inkdeus.setParams(p); }", params)
    return pixels(page)


def settled(page) -> np.ndarray:
    page.evaluate("() => { window.inkdeus.render(); }")
    return pixels(page)


def regions(page) -> dict:
    return page.evaluate("() => window.inkdeus.getParams().regions")


def tabs(page) -> list[str]:
    return page.evaluate("() => [...document.querySelectorAll('.tabs button')].map((b) => b.textContent)")


def client_point(page, x, y):
    return page.evaluate("([x, y]) => window.inkdeus.paint.clientPoint(x, y)", [x, y])


def drag(page, points) -> None:
    pts = [client_point(page, *p) for p in points]
    page.mouse.move(*pts[0])
    page.mouse.down()
    for pt in pts[1:]:
        page.mouse.move(*pt, steps=6)
    page.mouse.up()


def ctl(live, capsys, *args, expect=0):
    capsys.readouterr()
    code = cli.main(["ctl", "--port", str(live["port"]), *args])
    captured = capsys.readouterr()
    assert code == expect, captured.err or captured.out
    return json.loads(captured.out if code == 0 else captured.err)


# 線画・ベタ・層 2 枚がすべて出る設定
BASE = deep_merge(BLANK, {
    "lines": {"enabled": True, "sigma": 1.0},
    "solid": {"enabled": True, "threshold": 0.15},
    "layers": [layer(threshold=0.72, angle=45, spacing=4.0, width=1.0),
               layer(threshold=0.45, angle=135, spacing=4.0, width=1.0)],
})
HAIR_PT, SKIN_PT, CLOTHES_PT, REST_PT, BG_PT = (160, 50), (160, 80), (160, 170), (160, 228), (20, 20)


def part(**kw) -> dict:
    """別の設定にした部分の項目（テスト用の省略形）"""
    base = {"linked": False, "levels": {}, "solid": {"enabled": False}, "layers": []}
    base.update(kw)
    return base


def with_parts(**parts) -> dict:
    p = json.loads(json.dumps(BASE))
    p["regions"].update(parts)
    return p


def test_linked_parts_draw_exactly_as_before(live):
    page = live["open"]()
    assert page.evaluate("() => window.inkdeus.parts()") == ["clothes", "hair", "skin"]
    assert page.evaluate("() => window.inkdeus.info().hasMask") is True
    # タブは「人物 / 背景 / その画像にマスクのある部分…」。hair / skin / clothes は既定の対訳で出す
    assert tabs(page) == ["人物", "背景", "服", "髪", "肌"]
    got = show(page, BASE)
    page.evaluate("() => window.inkdeus.open('noparts.png')")           # 同じ絵・同じマスクで、部分マスクだけ無い
    assert page.evaluate("() => window.inkdeus.parts()") == [] and tabs(page) == ["人物", "背景"]
    want = show(page, BASE)
    assert (got == want).all() and (want < 128).any() and (want > 128).any()
    want_default = show(page, page.evaluate("() => window.inkdeus.defaults()"))
    page.evaluate("() => window.inkdeus.open('fig.png')")
    assert (show(page, page.evaluate("() => window.inkdeus.defaults()")) == want_default).all()
    # 部分の項目があっても、linked の間は人物と同じ（完全一致）。設定は残る
    linked = with_parts(hair=part(linked=True, solid={"enabled": True, "threshold": 1}),
                        skin=part(linked=True, layers=[layer(angle=10)]))
    assert (show(page, linked) == want).all()
    assert regions(page)["hair"]["solid"]["threshold"] == 1 and regions(page)["hair"]["linked"] is True
    # 切り離しただけ（人物と同じ中身）でも、見た目は変わらない
    page.locator('.tabs button[data-tab="hair"]').click()
    assert "髪は人物と同じ設定で描いています" in page.locator("#partLinkedNote").text_content()
    show(page, BASE)
    page.locator("#btnUnlinkPart").click()
    r = regions(page)
    assert r["hair"]["linked"] is False and no_ids(r["hair"]["layers"]) == no_ids(r["person"]["layers"])
    assert r["hair"]["solid"] == r["person"]["solid"] and "skin" not in r
    assert (settled(page) == want).all()
    assert page.locator("#btnLinkPart").count() == 1 and page.locator("#partLinkedNote").count() == 0
    assert page.errors == []


def test_solid_black_on_one_part_changes_only_that_part(live):
    page = live["open"]()
    base = show(page, BASE)
    hair = live["parts"]["hair"]
    assert (base[hair] > 0).any()                                       # 元は、髪にも黒でない画素がある
    page.locator('.tabs button[data-tab="hair"]').click()
    page.locator("#btnUnlinkPart").click()
    options = page.evaluate("() => [...document.querySelectorAll('#selPreset option')].map((o) => o.value)")
    assert "solid-black" in options and "blank" in options
    assert "white" not in options and "lines-only" not in options       # 背景向けの意味のスタイルは、部分には出さない
    page.locator("#selPreset").select_option("solid-black")
    got = settled(page)
    r = regions(page)["hair"]
    assert r["linked"] is False and r["layers"] == [] and r["solid"]["threshold"] == 1 and r["solid"]["enabled"] is True
    assert (got[hair] == 0).all()                                       # 髪は全面ベタ
    assert (got[~hair] == base[~hair]).all()                            # ほかの部分・人物の残り・背景は不変
    # 取り消せる。人物と同じに戻すと、元の絵
    page.evaluate("() => window.inkdeus.undo()")
    assert (pixels(page) == base).all()
    page.evaluate("() => window.inkdeus.redo()")
    assert (pixels(page) == got).all()
    page.locator("#btnLinkPart").click()
    assert (settled(page) == base).all() and regions(page)["hair"]["linked"] is True
    # 白抜き: 層もベタも無くなり、線画だけが残る
    skin = live["parts"]["skin"]
    page.locator('.tabs button[data-tab="skin"]').click()
    page.locator("#btnUnlinkPart").click()
    page.locator("#selPreset").select_option("blank")
    got = settled(page)
    lines_only = show(page, deep_merge(BASE, {"solid": {"enabled": False}, "layers": []}))
    show(page, with_parts(skin=part()))
    assert (pixels(page)[skin] == lines_only[skin]).all() and (got[skin] == lines_only[skin]).all()
    assert (got[~skin] == base[~skin]).all()
    assert page.errors == []


def test_line_knockout_leaves_white_lines_inside_solid(live):
    page = live["open"]()
    hair = live["parts"]["hair"]
    lines = show(page, deep_merge(BASE, {"solid": {"enabled": False}, "layers": []}))     # 線画だけ
    strong = hair & (lines < 60)                                         # 髪の中の、線画がはっきり出る所（明るい筋の縁）
    calm = hair & (lines == 255)
    assert strong.sum() > 30 and calm.sum() > 200
    base = show(page, BASE)
    black = show(page, with_parts(hair=part(solid={"enabled": True, "threshold": 1})))
    assert (black[hair] == 0).all()
    got = show(page, with_parts(hair=part(solid={"enabled": True, "threshold": 1, "lineKnockout": 1})))
    assert (got[strong] > 190).all()                                     # ベタの中の線画の位置が白くなる
    assert (got[calm] == 0).all()                                        # 線の無い所は黒のまま
    assert (got[~hair] == base[~hair]).all()
    half = show(page, with_parts(hair=part(solid={"enabled": True, "threshold": 1, "lineKnockout": 0.5})))
    assert (half[strong] < got[strong]).all() and (half[strong] > 80).all()
    # パネルのスライダーにも出る（ベタの欄）
    page.locator('.tabs button[data-tab="hair"]').click()
    number = page.locator('[data-group="solid"] .row[data-key="lineKnockout"] input[type="number"]')
    assert number.input_value() == "0.50"
    number.fill("1")
    number.dispatch_event("change")
    assert (settled(page) == got).all()
    # 線画を消しゴムで消した所は、白抜きにもならない。線画そのものを切っても、白抜きは働く
    page.evaluate("() => window.inkdeus.paint.stroke('lines', { mode: 'erase', hardness: 1, strength: 1 }, [[140, 48, 12], [180, 48, 12]])")
    erased = pixels(page)
    band = np.zeros((H, W), bool)
    band[42:54, 144:176] = True
    assert (erased[strong & band] == 0).all() and (strong & band).sum() > 10
    page.evaluate("() => window.inkdeus.undo()")
    off = show(page, deep_merge(with_parts(hair=part(solid={"enabled": True, "threshold": 1, "lineKnockout": 1})),
                                {"lines": {"enabled": False}}))
    assert (off[strong] > 190).all() and (off[calm] == 0).all()
    # 既定（0）なら、ベタは従来どおり（人物のベタに付けても同じ）
    assert (show(page, deep_merge(BASE, {"solid": {"lineKnockout": 0}})) == base).all()
    assert page.errors == []


def test_each_part_gets_its_own_layers(live):
    page = live["open"]()
    base = show(page, BASE)
    dark = {"brightness": -1}                                            # 全面に層が掛かるようにする
    params = with_parts(
        hair=part(levels=dark, layers=[layer(threshold=0.9, angle=0, spacing=4, width=1)]),        # 水平の線
        clothes=part(levels=dark, layers=[layer(threshold=0.9, angle=90, spacing=4, width=1)]))    # 垂直の線
    params["lines"]["enabled"] = False
    got = show(page, params)
    hair = got[44:56, 150:150 + 20].astype(float)
    clothes = got[150:200, 140:180].astype(float)
    assert (hair.std(axis=1) == 0).all() and hair.std(axis=0).min() > 50          # 行ごとに一様 = 水平の線
    assert (clothes.std(axis=0) == 0).all() and clothes.std(axis=1).min() > 50    # 列ごとに一様 = 垂直の線
    # 肌（linked のまま）・人物の残り・背景は、部分の設定の影響を受けない
    nolines = show(page, deep_merge(BASE, {"lines": {"enabled": False}}))
    other = ~(live["parts"]["hair"] | live["parts"]["clothes"])
    assert (got[other] == nolines[other]).all() and (base != nolines).any()
    # 部分の層のソロ表示は、その層だけ
    show(page, params)
    page.locator('.tabs button[data-tab="clothes"]').click()
    page.locator('.layer[data-layer="0"]').get_by_title("ソロ表示（この層だけを見る）").click()
    assert page.evaluate("() => window.inkdeus.state.solo") == {"region": "clothes", "index": 0}
    solo = settled(page)
    assert (solo[~live["parts"]["clothes"]] == 255).all() and (solo[150:200, 140:180] == got[150:200, 140:180]).all()
    assert page.errors == []


def test_all_zero_mask_and_images_without_parts(live):
    page = live["open"]("?image=zero.png")
    assert page.evaluate("() => window.inkdeus.parts()") == ["hair"] and tabs(page) == ["人物", "背景", "髪"]
    base = show(page, BASE)
    got = show(page, with_parts(hair=part(solid={"enabled": True, "threshold": 1})))
    assert (got == base).all()                                           # 全 0 のマスクは、どこにも効かない
    stats = page.evaluate("() => window.inkdeus.stats()")
    assert stats["output"]["hair"]["areaRatio"] == 0 and stats["output"]["hair"]["blackRatio"] is None
    assert stats["input"]["hair"]["luminance"] is None and stats["parts"] == ["hair"]
    # 開いているタブの部分が無い画像へ移ると、人物のタブへ戻る。部分の設定は、無い画像でも残る（警告しない）
    page.locator('.tabs button[data-tab="hair"]').click()
    page.evaluate("() => window.inkdeus.save()")
    page.evaluate("() => window.inkdeus.open('plain.png')")
    assert page.evaluate("() => window.inkdeus.tab()") == "person" and tabs(page) == ["人物", "背景"]
    r = regions(page)
    assert r["hair"]["solid"]["threshold"] == 1 and r["hair"]["linked"] is False     # 直前の画像の設定を引き継いだ
    assert page.locator("#messages .msg").count() == 0
    assert page.evaluate("() => window.inkdeus.regionAt(10, 10)") == "person"        # マスクが無ければ全体が人物
    assert "hair" not in page.evaluate("() => window.inkdeus.stats().output")
    assert page.errors == []


def test_highlight_and_alt_click(live):
    page = live["open"]()
    show(page, BASE)
    for (x, y), region in ((HAIR_PT, "hair"), (SKIN_PT, "skin"), (CLOTHES_PT, "clothes"), (REST_PT, "person"), (BG_PT, "background")):
        assert page.evaluate("([x, y]) => window.inkdeus.regionAt(x, y)", [x, y]) == region
    assert page.evaluate("() => window.inkdeus.regionAt(-5, 10)") is None

    def tinted(pt) -> bool:
        r, g, b, _a = page.evaluate("([x, y]) => window.inkdeus.displayPixel(x, y)", list(pt))
        return r - g > 40 and b - g > 15

    assert not any(tinted(p) for p in (HAIR_PT, SKIN_PT, CLOTHES_PT, REST_PT, BG_PT))
    # 部分: その部分のマスクの所だけに色が付く
    assert page.evaluate("() => window.inkdeus.highlight('hair')") == "hair"
    assert tinted(HAIR_PT) and not any(tinted(p) for p in (SKIN_PT, CLOTHES_PT, REST_PT, BG_PT))
    # 人物: 人物の設定で描かれる所（linked の部分を含む）。背景: 背景
    page.evaluate("() => window.inkdeus.highlight('person')")
    assert all(tinted(p) for p in (HAIR_PT, SKIN_PT, CLOTHES_PT, REST_PT)) and not tinted(BG_PT)
    page.evaluate("() => window.inkdeus.highlight('background')")
    assert tinted(BG_PT) and not any(tinted(p) for p in (HAIR_PT, SKIN_PT, CLOTHES_PT, REST_PT))
    # 別の設定にした部分は、人物の強調には入らない
    show(page, with_parts(hair=part(solid={"enabled": True, "threshold": 1})))
    page.evaluate("() => window.inkdeus.highlight('person')")
    assert not tinted(HAIR_PT) and tinted(SKIN_PT) and tinted(REST_PT)
    page.evaluate("() => window.inkdeus.highlight(null)")
    assert not tinted(SKIN_PT)
    # ボタンは、押している間だけ
    page.locator('.tabs button[data-tab="skin"]').click()
    box = page.locator("#btnHighlight").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    assert page.evaluate("() => window.inkdeus.state.highlight") == "skin" and tinted(SKIN_PT) and not tinted(HAIR_PT)
    page.mouse.up()
    assert page.evaluate("() => window.inkdeus.state.highlight") is None and not tinted(SKIN_PT)
    # 出力（書き出す絵）には、強調の色は入らない
    before = pixels(page)
    page.evaluate("() => window.inkdeus.highlight('hair')")
    assert (settled(page) == before).all()
    page.evaluate("() => window.inkdeus.highlight(null)")
    # Alt+クリックで、その場所の領域のタブへ切り替わる（移動も筆も始まらない）
    view = page.evaluate("() => JSON.stringify(window.inkdeus.state.view)")
    for pt, region in ((HAIR_PT, "hair"), (BG_PT, "background"), (CLOTHES_PT, "clothes"), (REST_PT, "person"), (SKIN_PT, "skin")):
        page.keyboard.down("Alt")
        page.mouse.click(*client_point(page, *pt))
        page.keyboard.up("Alt")
        assert page.evaluate("() => window.inkdeus.tab()") == region
        assert page.locator(f'.tabs button.on[data-tab="{region}"]').count() == 1
    assert page.evaluate("() => JSON.stringify(window.inkdeus.state.view)") == view
    page.evaluate("() => window.inkdeus.paint.setTool('eraser')")
    page.keyboard.down("Alt")
    page.mouse.click(*client_point(page, *HAIR_PT))
    page.keyboard.up("Alt")
    assert page.evaluate("() => window.inkdeus.tab()") == "hair"
    assert page.evaluate("() => window.inkdeus.paint.state().targets") == []
    assert page.errors == []


def test_eraser_on_part_solid_and_part_layers(live):
    page = live["open"]()
    params = with_parts(
        hair=part(solid={"enabled": True, "threshold": 1}),
        clothes=part(levels={"brightness": -1}, layers=[layer(id="cccc1111", threshold=0.9, angle=90, spacing=4, width=1)]))
    params["lines"]["enabled"] = False
    base = show(page, params)
    page.evaluate("() => window.inkdeus.save()")
    hair, clothes = live["parts"]["hair"], live["parts"]["clothes"]
    eraser = """([kind]) => { const p = window.inkdeus.paint; p.setTool('eraser'); p.setEraseTarget(kind);
        p.set('eraser', { size: 16, hardness: 1, strength: 1 }); return p.target(kind); }"""
    # ベタ: 開いているタブの側。部分のタブ（別の設定にしてある部分）なら、その部分のベタ
    page.locator('.tabs button[data-tab="hair"]').click()
    found = page.evaluate(eraser, ["solid"])
    assert found["target"] == "solid-hair" and "髪のベタ" in found["label"]
    drag(page, [(120, 48), (200, 48)])
    got = pixels(page)
    band = np.zeros((H, W), bool)
    band[43:53, 135:185] = True
    assert (got[band & hair] == 255).all() and (band & hair).sum() > 200          # なぞった所の髪のベタが消えた
    assert (got[~hair] == base[~hair]).all()                                      # 髪の外は変わらない
    assert page.evaluate("() => window.inkdeus.paint.state().targets") == ["solid-hair"]
    assert page.locator('[data-group="solid"] .erased[data-erased="solid-hair"]').count() == 1
    # linked の部分のタブでは、人物のベタが対象
    page.locator('.tabs button[data-tab="skin"]').click()
    assert page.evaluate(eraser, ["solid"])["target"] == "solid-person"
    # 層: 部分のタブで層の行を選ぶと、その層が対象（表示名は部分の名前）
    page.locator('.tabs button[data-tab="clothes"]').click()
    page.locator('.layer[data-layer="0"] > summary .title').click()
    found = page.evaluate(eraser, ["layer"])
    assert found["target"] == "layer-cccc1111" and found["label"] == "服の層1" and found["region"] == "clothes"
    drag(page, [(110, 170), (210, 170)])
    got2 = pixels(page)
    band2 = np.zeros((H, W), bool)
    band2[165:175, 125:195] = True
    assert (got2[band2 & clothes] == 255).all() and (base[band2 & clothes] < 255).any()
    assert (got2[hair] == got[hair]).all()
    # 保存され、開き直しても同じ絵。書き出しにも反映される
    page.evaluate("() => window.inkdeus.paint.flush()")
    assert sorted(p.name for p in (live["out"] / "fig.paint").glob("*.png")) == ["layer-cccc1111.png", "solid-hair.png"]
    page2 = live["open"]()
    assert (pixels(page2) == got2).all()
    stats = page2.evaluate("() => window.inkdeus.stats()")
    assert stats["paint"]["targets"] == ["layer-cccc1111", "solid-hair"]
    assert 0.02 < stats["output"]["hair"]["solidRatio"] < 0.9                    # 消した分だけ、髪のベタが減っている
    r = page2.evaluate("() => window.inkdeus.exportPNG()")
    with Image.open(r["path"]) as im:
        assert (np.asarray(im) == got2).all()
    assert page.errors == [] and page2.errors == []


def test_stats_report_each_part(live, capsys):
    page = live["open"]()
    hair, skin, mask = live["parts"]["hair"], live["parts"]["skin"], live["mask"]
    show(page, BASE)
    s = page.evaluate("() => window.inkdeus.stats()")
    assert s["parts"] == ["clothes", "hair", "skin"] and "droppedParts" not in s
    out, inp = s["output"], s["input"]
    assert set(out) == {"all", "person", "background", "clothes", "hair", "skin"}
    assert out["hair"]["areaRatio"] == pytest.approx(hair.mean(), abs=0.01)
    assert out["skin"]["areaRatio"] == pytest.approx(skin.mean(), abs=0.01)
    assert out["hair"]["linked"] is True and [l["layer"] for l in out["hair"]["layers"]] == [0, 1]
    for key in ("blackRatio", "meanDensity", "solidRatio", "lineRatio"):
        assert 0 <= out["hair"][key] <= 1
    assert inp["hair"]["luminance"]["median"] < 0.25 < inp["skin"]["luminance"]["median"]      # 髪は暗く、肌は明るい
    assert inp["hair"]["adjusted"] is not None
    person_all = out["person"]["areaRatio"]
    assert person_all == pytest.approx(mask.mean(), abs=0.03)             # linked の間、人物は部分も含む
    # ctl で髪をベタにして、統計で確かめる（スキルに書いた手順）
    page.evaluate("() => window.inkdeus.save()")
    assert ctl(live, capsys, "preset", "ベタ", "--region", "hair")["style"] == "solid-black"
    r = ctl(live, capsys, "set", "regions.hair.solid.lineKnockout", "0.8")
    got = ctl(live, capsys, "stats", "--wait", "--timeout", "30")
    assert got["fresh"] is True and got["stats"]["rev"] == r["rev"]
    out = got["stats"]["output"]
    assert out["hair"]["linked"] is False and out["hair"]["solidRatio"] > 0.97 and out["hair"]["layers"] == []
    assert 0.2 < out["hair"]["blackRatio"] < 0.95                         # 線の白抜きの分だけ、黒率は 1 より下がる
    assert out["person"]["areaRatio"] == pytest.approx(person_all - hair.mean(), abs=0.02)    # 別の設定にした部分は、人物から外れる
    assert out["skin"]["linked"] is True
    assert regions(page)["hair"]["solid"] == {"enabled": True, "threshold": 1, "soft": 0, "lineKnockout": 0.8}
    assert page.locator('.tabs button[data-tab="hair"]').count() == 1
    ctl(live, capsys, "set", "regions.hair.solid.lineKnockout", "0")
    assert ctl(live, capsys, "stats", "--wait", "--timeout", "30")["stats"]["output"]["hair"]["blackRatio"] > 0.97
    assert page.errors == []


def test_six_parts_with_many_eraser_masks_and_the_layer_limit(live):
    page = live["open"]("?folder=six&image=bands.png")
    info = page.evaluate("() => window.inkdeus.partsInfo()")
    assert info["parts"] == SIX and info["ok"] is True and info["dropped"] == []
    assert info["maxTextureUnits"] >= 16 and info["maxLayers"] in (16, 32, 48, 64)
    assert tabs(page) == ["人物", "背景", *SIX]                            # 対訳の無い部分名は、名前そのまま
    angles = [0, 30, 60, 90, 120, 150]
    params = json.loads(json.dumps(BASE))
    params["lines"]["enabled"] = False
    ids = []
    for k, name in enumerate(SIX):
        layers = [layer(id=f"{name}x{i}00", threshold=0.9, angle=(angles[k] + i * 7) % 180, spacing=6, width=1, phase=i / 8)
                  for i in range(8)]
        ids += [l["id"] for l in layers]
        params["regions"][name] = part(solid={"enabled": True, "threshold": 0.2}, layers=layers)
    got = show(page, params)                                              # 人物 2 層 ＋ 6 部分 × 8 層 = 50 層
    assert page.evaluate("() => window.inkdeus.partsInfo().dropped") == []
    bands = stripes()
    means = [float(got[bands[name]].mean()) for name in SIX]
    gap = np.zeros((SH, SW), bool)
    gap[:, 30:40] = True
    person_only = show(page, BASE | {"lines": {"enabled": False}})
    assert (got[gap] == person_only[gap]).all()                          # すき間（どの部分でもない所）は人物の設定
    assert all(m < float(person_only[gap].mean()) for m in means)         # 8 層を重ねた部分は、人物（2 層）より濃い
    show(page, params)
    # 消去マスクを多数（部分の層 24 ＋ ベタ 6 ＋ 線画 ＋ すべて）＋ ペン。消去マスクの枠（32）を使い切っても描ける
    brush = "{ mode: 'erase', hardness: 1, strength: 1 }"
    for i, target in enumerate([*(f"layer-{i}" for i in ids[:24]), *(f"solid-{n}" for n in SIX), "lines", "all"]):
        ok = page.evaluate(f"([t, y]) => window.inkdeus.paint.stroke(t, {brush}, [[0, y, 2], [239, y, 2]])", [target, 3 + i * 3])
        assert ok is True, target
    assert page.evaluate("() => window.inkdeus.paint.stroke('pen', { mode: 'black', hardness: 1 }, [[0, 110, 2], [239, 110, 2]])")
    state = page.evaluate("() => window.inkdeus.paint.state()")
    assert len(state["targets"]) == 33 and "solid-p6" in state["targets"]
    painted = pixels(page)
    assert (painted[108:112, 5:235] == 0).all() and (painted != got).any()
    assert (painted[3 + 31 * 3 - 1:3 + 31 * 3 + 1, 5:235] == 255).all()                # 「すべて」でなぞった行は白
    # 枠を使い切ったあと、さらに別の対象を消そうとすると、知らせて何もしない
    assert page.evaluate(f"() => window.inkdeus.paint.stroke('layer-{ids[24]}', {brush}, [[0, 60, 2], [239, 60, 2]])") is False
    assert "多すぎます" in page.evaluate("() => document.getElementById('messages').textContent")
    assert (pixels(page) == painted).all()
    stats = page.evaluate("() => window.inkdeus.stats()")
    assert set(SIX) <= set(stats["output"]) and len(stats["output"]["p3"]["layers"]) == 8
    assert all(stats["output"][n]["areaRatio"] == pytest.approx(0.125, abs=0.01) for n in SIX)
    # 上限: 一度に扱える層の総数を超えた部分は、人物と同じ設定で描いて知らせる（エラーにはしない）
    page.evaluate("() => window.inkdeus.paint.clear('pen')")
    for target in page.evaluate("() => window.inkdeus.paint.state().targets"):
        page.evaluate("(t) => window.inkdeus.paint.clear(t)", target)
    full = show(page, params)
    assert (full == got).all()
    dropped = page.evaluate("() => window.inkdeus.limitLayers(16)")
    assert dropped == SIX[1:]                                             # 2 + 8 = 10 層までは入る。次の 8 層は入らない
    limited = pixels(page)
    assert (limited[bands["p1"]] == got[bands["p1"]]).all()
    for name in SIX[1:]:
        assert (limited[bands[name]] == person_only[bands[name]]).all()
    text = page.evaluate("() => document.getElementById('messages').textContent")
    assert "人物と同じ設定で描いています" in text and "p2" in text
    assert page.evaluate("() => window.inkdeus.stats().droppedParts") == SIX[1:]
    assert page.evaluate("() => window.inkdeus.limitLayers(64)") == [] and (pixels(page) == got).all()
    assert page.errors == []


def test_versions_folders_and_export_all_keep_parts(live):
    page = live["open"]()
    hair = live["parts"]["hair"]
    base = show(page, BASE)
    page.evaluate("() => window.inkdeus.save()")
    # バージョン: 部分マスクは元画像の物を共有し、部分の設定はバージョンごと
    page.evaluate("() => window.inkdeus.createVersion('髪ベタ')")
    assert page.evaluate("() => window.inkdeus.parts()") == ["clothes", "hair", "skin"]
    page.locator('.tabs button[data-tab="hair"]').click()
    page.locator("#btnUnlinkPart").click()
    page.locator("#selPreset").select_option("solid-black")
    black = settled(page)
    assert (black[hair] == 0).all()
    page.evaluate("() => window.inkdeus.save()")
    page.evaluate("() => window.inkdeus.openVersion('')")
    assert (pixels(page) == base).all() and "hair" not in regions(page)
    assert page.evaluate("() => window.inkdeus.tab()") == "hair"            # 同じ画像なら、タブはそのまま
    page.evaluate("() => window.inkdeus.openVersion('髪ベタ')")
    assert (pixels(page) == black).all()
    # 画像フォルダを切り替えると、そのフォルダの部分になる（前のフォルダの部分のタブは、人物へ戻る）
    page.evaluate("() => window.inkdeus.setFolder('six')")
    assert page.evaluate("() => window.inkdeus.parts()") == SIX and page.evaluate("() => window.inkdeus.tab()") == "person"
    assert page.evaluate("() => window.inkdeus.partsInfo().names") == SIX
    page.evaluate("() => window.inkdeus.setFolder('default')")
    assert page.evaluate("() => window.inkdeus.partsInfo().names") == ["clothes", "hair", "skin"]
    # 一括書き出し: 各画像・各バージョンが、それぞれの部分の設定で書き出される
    r = page.evaluate("() => window.inkdeus.exportAll({ force: true })")
    assert r["failed"] == 0 and r["exported"] >= 5, r
    with Image.open(live["out"] / "fig@髪ベタ.png") as im:
        assert (np.asarray(im) == black).all()
    with Image.open(live["out"] / "fig.png") as im:
        assert (np.asarray(im) == base).all()
    listing = json.loads(urllib.request.urlopen(live["url"] + "api/images").read())
    fig = next(i for i in listing["images"] if i["name"] == "fig.png")
    assert fig["parts"] == ["clothes", "hair", "skin"] and all(v["exportFresh"] for v in fig["versions"])
    # 一覧のバッジ: 部分マスクのある画像に「部分」
    badge = lambda name: page.locator(f'.gal-item[data-name="{name}"] .gal-badge[data-kind="parts"]')  # noqa: E731
    assert badge("fig.png").count() == 1 and badge("fig.png").text_content() == "部分"
    assert "hair" in badge("fig.png").get_attribute("title")
    assert badge("zero.png").count() == 1 and badge("plain.png").count() == 0 and badge("noparts.png").count() == 0
    assert page.errors == []


def test_parts_registered_while_running_show_up_after_reload(tmp_path, browser):
    img, mask, pm = figure()
    synth.save(tmp_path / "images" / "fig.png", img)
    synth.save(tmp_path / "masks" / "fig.png", mask)
    synth.save_parts(tmp_path / "parts", "fig", pm)
    project, _ = create_project("book", tmp_path / "images", tmp_path / "masks", root=tmp_path / "projects")
    server, url = serve(create_app(project.source, project.masks, project.out, project=project.info(),
                                   local_config=tmp_path / "none.toml"))
    try:
        page = open_page(browser, url)
        assert page.evaluate("() => window.inkdeus.parts()") == [] and tabs(page) == ["人物", "背景"]
        set_parts(project, tmp_path / "parts")                          # 稼働中に登録（inkdeus parts set）
        page.reload()
        page.wait_for_function("window.inkdeus && window.inkdeus.ready")
        page.evaluate("() => window.inkdeus.ready")
        assert page.evaluate("() => window.inkdeus.parts()") == ["clothes", "hair", "skin"]
        assert tabs(page) == ["人物", "背景", "服", "髪", "肌"]
        page.close()
    finally:
        server.shutdown()


def test_old_server_without_parts_feature_keeps_working(live):
    requests: list[str] = []

    def old_server(page):
        # 部分を知らない、ひとつ前のサーバのふりをする（features に parts が無く、一覧に parts が無く、/api/part も無い）
        page.route("**/api/config", lambda route: route.fulfill(json={"project": None, "features": ["layerIds", "paint"]}))

        def images(route):
            data = route.fetch().json()
            for key in ("parts", "partsDir", "partWarnings"):
                data.pop(key, None)
            for item in data["images"]:
                item.pop("parts", None)
            route.fulfill(json=data)

        page.route("**/api/images*", images)
        page.route("**/api/part/**", lambda route: route.fulfill(status=404, body="Not Found"))
        page.on("request", lambda req: requests.append(f"{req.method} {req.url}"))

    page = live["open"](prepare=old_server)
    info = page.evaluate("() => window.inkdeus.partsInfo()")
    assert info["ok"] is False and info["parts"] == [] and info["names"] == []
    assert tabs(page) == ["人物", "背景"]                                 # 部分のタブは出ない
    assert page.locator('.row[data-key="lineKnockout"]').count() == 0     # 「ベタの中の線画を白抜き」も出ない
    assert page.locator('.gal-badge[data-kind="parts"]').count() == 0
    assert not any("/api/part/" in r for r in requests)
    page.evaluate("() => window.inkdeus.open('noparts.png')")
    want = show(page, BASE)
    page.evaluate("() => window.inkdeus.open('fig.png')")
    got = show(page, BASE)
    assert (got == want).all()                                           # 従来どおりの絵
    # 画面に部分の項目や lineKnockout が入っても（設定JSON を開いたなど）、サーバへは除いて保存する
    # （知らないキーがあると、そのサーバの外部操作（ctl）が設定を受け付けなくなるため）
    show(page, with_parts(hair=part(solid={"enabled": True, "threshold": 1, "lineKnockout": 1})))
    page.evaluate("() => window.inkdeus.save()")
    saved = json.loads((live["out"] / "fig.inkdeus.json").read_text(encoding="utf-8"))
    assert list(saved["regions"]) == ["person", "background"]
    assert all("lineKnockout" not in saved["regions"][r]["solid"] for r in ("person", "background"))
    assert (pixels(page) == want).all()                                  # 部分マスクが無いので、絵にも効かない
    # スタイル・層の操作・書き出しは従来どおり
    page.locator("#selPreset").select_option("hatch1")
    assert len(regions(page)["person"]["layers"]) == 1
    r = page.evaluate("() => window.inkdeus.exportPNG()")
    with Image.open(r["path"]) as im:
        assert (np.asarray(im) == settled(page)).all()
    assert page.errors == []


# ---- 大きな画像（5120x3840）＋ 3 部分: 合成の重さが、領域の数に比例して増えないこと ----

@pytest.mark.skipif(os.environ.get("INKDEUS_SKIP_BIG") == "1", reason="INKDEUS_SKIP_BIG=1")
def test_large_image_with_three_parts(tmp_path, browser, capsys):
    w, h = 5120, 3840
    img, mask = synth.scene(w, h)
    synth.save(tmp_path / "images" / "big.png", img, fast=True)
    synth.save(tmp_path / "masks" / "big.png", mask, fast=True)
    pm = synth.part_masks(w, h)
    for name, arr in pm.items():
        synth.save(tmp_path / "parts" / name / "big.png", arr, fast=True)
    del img
    server, url = serve(create_app(tmp_path / "images", mask_dir=tmp_path / "masks", out_dir=tmp_path / "out",
                                   parts_dir=tmp_path / "parts"))
    try:
        t0 = time.time()
        page = open_page(browser, url)
        t_load = time.time() - t0
        info = page.evaluate("() => ({ ...window.inkdeus.info(), ...window.inkdeus.partsInfo() })")
        assert (info["width"], info["height"], info["hasMask"]) == (w, h, True) and info["parts"] == ["clothes", "hair", "skin"]
        four = [layer(threshold=t, angle=a, spacing=2.0, width=0.5) for t, a in ((0.72, 45), (0.52, 135), (0.38, 0), (0.27, 90))]
        two = deep_merge(BLANK, {"unit": None, "lines": {"enabled": True}, "solid": {"enabled": True, "threshold": 0.15},
                                 "layers": four,
                                 "background": {"linked": False, "solid": {"enabled": True}, "layers": [dict(l, angle=(l["angle"] + 20) % 180) for l in four]}})
        five = json.loads(json.dumps(two))
        for k, name in enumerate(("hair", "skin", "clothes")):
            five["regions"][name] = part(solid={"enabled": True, "threshold": 0.15},
                                         layers=[dict(l, angle=(l["angle"] + 10 * (k + 1)) % 180) for l in four])

        def composite_ms(params) -> float:
            page.evaluate("(p) => { window.inkdeus.setParams(p); }", params)
            times = []
            for i in range(3):                          # 層の太さだけを変えて、合成だけをやり直す
                tweak = json.loads(json.dumps(params))
                tweak["regions"]["person"]["layers"][0]["width"] = 0.5 + 0.01 * (i + 1)
                r = page.evaluate("(p) => window.inkdeus.setParams(p)", tweak)
                assert r["passes"] == ["composite"]
                times.append(r["ms"])
            return min(times)

        ms_two = composite_ms(two)
        ms_five = composite_ms(five)
        ms_two_again = composite_ms(two)
        got = page.evaluate("""() => {
            const px = window.inkdeus.readPixels(0, 700, 5120, 64);
            let sum = 0; for (const v of px) sum += v; return sum / px.length; }""")
        assert 5 < got < 250
        stats = page.evaluate("() => window.inkdeus.stats()")
        assert stats["parts"] == ["clothes", "hair", "skin"] and "droppedParts" not in stats
        ratio = ms_five / min(ms_two, ms_two_again)
        with capsys.disabled():
            print(f"\n[5120x3840 ＋ 部分 3] renderer={info['renderer']}"
                  f"\n  読み込み〜最初の描画（距離場・部分マップの作成を含む）: {t_load:.1f}s"
                  f"\n  合成だけの再描画: 人物＋背景（各 4 層）{min(ms_two, ms_two_again):.0f}ms → "
                  f"人物＋背景＋3 部分（各 4 層）{ms_five:.0f}ms（{ratio:.2f} 倍）"
                  f"\n  uniform の上限 {info['maxUniformVectors']} 本 / テクスチャ {info['maxTextureUnits']} 枚 / "
                  f"一度に扱える層 {info['maxLayers']}")
        assert ratio < 1.6, f"領域が増えたときの合成が重すぎます（{ratio:.2f} 倍）"
        assert page.errors == []
        page.close()
    finally:
        server.shutdown()
