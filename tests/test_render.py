"""描画のテスト: ヘッドレス Chromium で合成画像を描き、出力画素を数値で確かめる"""

from __future__ import annotations

import functools
import http.server
import json
import os
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageFilter

import synth
from conftest import BLANK, deep_merge, ink, layer, no_ids, open_image, open_page, pixels, render

WEB_DIR = Path(__file__).resolve().parents[1] / "web"


def lab_l(srgb: np.ndarray) -> np.ndarray:
    """sRGB のグレー値（0..255）→ Lab の L（0..1）。シェーダと同じ式"""
    c = srgb.astype(np.float64) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    f = np.where(lin > 0.008856, np.cbrt(lin), 7.787 * lin + 16.0 / 116.0)
    return np.clip(1.16 * f - 0.16, 0.0, 1.0)


# ---------------------------------------------------------------------------
# トーン層・ベタ・線画
# ---------------------------------------------------------------------------

def test_flat_white_has_no_ink(page):
    open_image(page, "white.png")
    defaults = page.evaluate("() => window.inkdeus.defaults()")
    assert defaults["watermark"]["enabled"] is False      # 透かしは既定でオフ
    img = render(page, deep_merge(defaults, {"unit": 1}))
    assert img.shape == (256, 256)
    assert (img == 255).all()
    assert page.errors == []


def test_gradient_ink_increases_toward_dark(page):
    open_image(page, "gradient.png")
    defaults = page.evaluate("() => window.inkdeus.defaults()")
    img = render(page, deep_merge(defaults, {"unit": 1}))
    strips = ink(img).reshape(128, 16, 48).mean(axis=(0, 2))    # 左（暗い）→ 右（明るい）
    assert strips[0] > 0.95, strips                # いちばん暗い帯はベタ
    assert strips[-1] < 0.02, strips               # いちばん明るい帯は白
    assert (np.diff(strips) <= 0.01).all(), strips  # 明るくなるほどインクは減る（単調）
    assert len(np.unique(np.round(strips, 2))) >= 6, strips   # 段階がある


def test_more_layers_means_more_ink(page):
    open_image(page, "dark.png")
    angles = [45, 135, 0, 90]
    means = []
    for n in range(0, 5):
        layers = [layer(threshold=0.9, angle=a, spacing=4.0, width=1.0) for a in angles[:n]]
        means.append(ink(render(page, deep_merge(BLANK, {"layers": layers}))).mean())
    # 各層のインク量は 太さ/間隔 = 0.25。重ねると 1 − 0.75^n
    expected = [1 - 0.75 ** n for n in range(0, 5)]
    assert means == pytest.approx(expected, abs=0.02), means
    assert all(b > a + 0.05 for a, b in zip(means, means[1:]))


@pytest.mark.parametrize("width,spacing", [(0.3, 3.0), (0.5, 2.0), (0.75, 2.5), (2.0, 5.0)])
def test_mean_ink_is_width_over_spacing(page, width, spacing):
    """1px 未満の線でも平均インク量が 太さ/間隔 になる"""
    open_image(page, "dark.png")
    for angle in (0, 30, 45, 90):
        layers = [layer(threshold=0.9, angle=angle, spacing=spacing, width=width)]
        mean = ink(render(page, deep_merge(BLANK, {"layers": layers}))).mean()
        assert mean == pytest.approx(width / spacing, abs=0.02), (angle, mean)


def test_unit_scales_lengths(page):
    """unit を 2 倍にすると線の周期が 2 倍になる（基準px × unit）"""
    open_image(page, "dark.png")
    layers = [layer(threshold=0.9, angle=90, spacing=4.0, width=2.0)]
    for unit, period in ((1, 4), (2, 8)):
        img = render(page, deep_merge(BLANK, {"unit": unit, "layers": layers}))
        row = img[10].astype(int)
        assert (row[:-period] == row[period:]).all()
        assert not (row[:-period // 2] == row[period // 2:]).all()


def test_layer_angle_gives_line_direction(page):
    open_image(page, "gray.png")

    def run(angle):
        layers = [layer(threshold=0.9, angle=angle, spacing=8.0, width=2.0)]
        return render(page, deep_merge(BLANK, {"layers": layers})).astype(np.float64)

    img = run(0)                                   # 水平の線: 行の中は同じ値
    assert img.std(axis=1).max() < 1 and img.std(axis=0).mean() > 30
    img = run(90)                                  # 垂直の線: 列の中は同じ値
    assert img.std(axis=0).max() < 1 and img.std(axis=1).mean() > 30
    img = run(45)                                  # 右上がり「／」: 左下へ進んでも同じ値
    assert np.abs(img[1:, :-1] - img[:-1, 1:]).mean() < 2
    assert np.abs(img[1:, 1:] - img[:-1, :-1]).mean() > 30
    img = run(135)                                 # 右下がり「＼」: 右下へ進んでも同じ値
    assert np.abs(img[1:, 1:] - img[:-1, :-1]).mean() < 2
    assert np.abs(img[1:, :-1] - img[:-1, 1:]).mean() > 30


def test_layer_threshold_limits_where_lines_appear(page):
    open_image(page, "gradient.png")
    layers = [layer(threshold=0.5, angle=45, spacing=4.0, width=2.0)]
    img = render(page, deep_merge(BLANK, {"layers": layers}))
    lum = lab_l(synth.gradient(768, 128)[0, :, 0])
    assert (img[:, lum > 0.51] == 255).all()
    assert ink(img[:, lum < 0.49]).mean() == pytest.approx(0.5, abs=0.03)


def test_width_by_darkness_and_soft_edge(page):
    open_image(page, "gradient.png")
    lum = lab_l(synth.gradient(768, 128)[0, :, 0])
    near = (lum > 0.60) & (lum < 0.68)             # しきい値 0.7 のすぐ下
    far = (lum > 0.05) & (lum < 0.15)              # ずっと暗い所

    def run(**kw):
        layers = [layer(threshold=0.7, angle=45, spacing=4.0, width=1.0, **kw)]
        return ink(render(page, deep_merge(BLANK, {"layers": layers})))

    flat = run()
    assert flat[:, near].mean() == pytest.approx(0.25, abs=0.03)
    assert flat[:, far].mean() == pytest.approx(0.25, abs=0.03)
    vary = run(widthByDarkness=1.0)
    assert vary[:, near].mean() < 0.08             # しきい値付近は細い
    assert vary[:, far].mean() > 0.35              # 暗いほど太い
    soft = run(soft=0.2)
    band = (lum > 0.62) & (lum < 0.78)
    assert 0.01 < soft[:, band].mean() < 0.2       # 境界の帯では線が細る
    assert soft[:, far].mean() == pytest.approx(0.25, abs=0.03)


def test_disabled_layer_and_opacity(page):
    open_image(page, "dark.png")
    on = layer(threshold=0.9, spacing=4.0, width=1.0)
    assert (render(page, deep_merge(BLANK, {"layers": [deep_merge(on, {"enabled": False})]})) == 255).all()
    half = ink(render(page, deep_merge(BLANK, {"layers": [deep_merge(on, {"opacity": 0.5})]}))).mean()
    assert half == pytest.approx(0.125, abs=0.015)


def test_solid_below_threshold_is_black(page):
    open_image(page, "gradient.png")
    img = render(page, deep_merge(BLANK, {"solid": {"enabled": True, "threshold": 0.15, "soft": 0}}))
    lum = lab_l(synth.gradient(768, 128)[0, :, 0])
    assert (img[:, lum < 0.14] <= 2).all()
    assert (img[:, lum > 0.16] == 255).all()
    assert (lum < 0.14).sum() > 20


def test_line_art_appears_on_binary_edge(page):
    open_image(page, "halves.png")
    img = render(page, deep_merge(BLANK, {"lines": {"enabled": True}}))
    cols = np.arange(256)
    near = np.abs(cols - 128) <= 3
    far = np.abs(cols - 128) > 12
    assert img[:, near].min() < 30                 # 境界に黒い線が出る
    assert (img[:, near].min(axis=1) < 30).all()   # どの行にも
    assert (img[:, far] == 255).all()              # 平坦な白・平坦な黒には線が出ない
    off = render(page, deep_merge(BLANK, {"lines": {"enabled": True, "strength": 0}}))
    assert (off == 255).all()


def test_only_changed_passes_are_recomputed(page):
    open_image(page, "scene.png")
    defaults = deep_merge(page.evaluate("() => window.inkdeus.defaults()"), {"unit": 1})
    set_params = "(p) => window.inkdeus.setParams(p).passes"
    first = page.evaluate(set_params, defaults)
    assert {"smooth", "blur", "composite"} <= set(first)
    # ε・φ・濃さ・層の設定は合成パスだけ
    patch = {"lines": {"epsilon": -0.02, "phi": 50, "strength": 0.8, "tau": 0.97},
             "layers": [layer(threshold=0.6)], "solid": {"threshold": 0.2}, "levels": {"brightness": 0.1}}
    cur = deep_merge(defaults, patch)
    assert page.evaluate(set_params, cur) == ["composite"]
    cur = deep_merge(cur, {"lines": {"sigma": 1.2}})             # σ はぼかしから
    assert page.evaluate(set_params, cur) == ["blur", "composite"]
    cur = deep_merge(cur, {"input": {"smoothRadius": 4}})        # 平滑化は線画に影響しない
    assert page.evaluate(set_params, cur) == ["smooth", "composite"]
    cur = deep_merge(cur, {"input": {"autoLevel": False}})       # 輝度が変われば全部
    assert page.evaluate(set_params, cur) == ["lum", "smooth", "blur", "composite"]
    cur = deep_merge(cur, {"background": {"mode": "white", "grow": 9}, "watermark": {"opacity": 0.5}})
    assert page.evaluate(set_params, cur) == ["composite"]


# ---------------------------------------------------------------------------
# 人物マスクと背景処理
# ---------------------------------------------------------------------------

def outside_mask(grow_px: float, extra: int = 2) -> np.ndarray:
    """マスクを広げた分より確実に外側の画素（True）"""
    mask = synth.figure_mask(640, 480)
    r = int(np.ceil(grow_px)) + extra
    dil = Image.fromarray(mask.astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(2 * r + 1))
    return np.asarray(dil) == 0


def test_background_white(page):
    info = open_image(page, "scene.png")
    assert info["hasMask"] is True
    base = deep_merge(page.evaluate("() => window.inkdeus.defaults()"), {"unit": 1})
    keep = render(page, deep_merge(base, {"background": {"mode": "keep"}}))
    white = render(page, deep_merge(base, {"background": {"mode": "white", "grow": 4, "silhouetteWidth": 2}}))
    out = outside_mask(4)
    assert (keep[out] < 255).mean() > 0.05          # そのままなら背景にもトーンがある
    assert (white[out] == 255).all()                # 白ならマスク外は全画素 255
    inside = synth.figure_mask(640, 480)
    assert (white[inside] < 255).mean() > 0.2       # 人物は残る


def test_background_lines_only(page):
    open_image(page, "scene.png")
    base = deep_merge(page.evaluate("() => window.inkdeus.defaults()"), {"unit": 1})
    lines = render(page, deep_merge(base, {"background": {"mode": "lines", "grow": 4, "lineStrength": 0.6}}))
    # 比較対象: トーンもベタも無く、線の濃さだけ 0.6 にした絵
    ref = render(page, deep_merge(base, {"layers": [], "solid": {"enabled": False},
                                         "lines": {"strength": 0.6}, "background": {"mode": "keep"}}))
    keep = render(page, deep_merge(base, {"background": {"mode": "keep"}}))
    out = outside_mask(4)
    assert np.abs(lines[out].astype(int) - ref[out].astype(int)).max() <= 1   # マスク外は線だけ
    assert np.abs(keep[out].astype(int) - ref[out].astype(int)).max() > 100   # （そのままならトーンがある）
    inside = synth.figure_mask(640, 480)
    assert (lines[inside] == keep[inside]).mean() > 0.97                        # 人物は変わらない


def test_mask_grow_moves_the_boundary(page):
    open_image(page, "scene.png")
    y = int(480 * 0.78)                            # 胴の中央の行

    def left_edge(grow):
        img = render(page, deep_merge(BLANK, {"background": {"mode": "white", "grow": grow, "silhouetteWidth": 2}}))
        return int(np.argmax(img[y] < 128))        # シルエット線の左端

    e0, e10, e25, shrink = left_edge(0), left_edge(10), left_edge(25), left_edge(-8)
    mask_left = int(np.argmax(synth.figure_mask(640, 480)[y]))
    assert abs(e0 - mask_left) <= 1
    assert abs((e0 - e10) - 10) <= 1
    assert abs((e0 - e25) - 25) <= 1
    assert abs((shrink - e0) - 8) <= 1


def test_silhouette_line_width(page):
    open_image(page, "scene.png")
    y = int(480 * 0.78)
    for width in (0, 2, 5):
        img = render(page, deep_merge(BLANK, {"background": {"mode": "white", "grow": 0, "silhouetteWidth": width}}))
        left = img[y, :320]
        assert abs(ink(left).sum() - width) <= 0.75, (width, ink(left).sum())


def test_background_disabled_without_mask(page):
    info = open_image(page, "gradient.png")
    assert info["hasMask"] is False
    base = deep_merge(page.evaluate("() => window.inkdeus.defaults()"), {"unit": 1})
    a = render(page, deep_merge(base, {"background": {"mode": "keep"}}))
    b = render(page, deep_merge(base, {"background": {"mode": "white"}}))
    assert (a == b).all()
    page.locator('.tabs button[data-tab="background"]').click()
    assert page.locator("#bgControls.disabled").count() == 1


# ---------------------------------------------------------------------------
# 再現性・設定の保存と読み込み・出力
# ---------------------------------------------------------------------------

def test_same_settings_give_identical_output(page):
    open_image(page, "scene.png")
    a_params = deep_merge(page.evaluate("() => window.inkdeus.defaults()"),
                          {"unit": 1, "background": {"mode": "white"}, "watermark": {"enabled": True}})
    a1 = render(page, a_params)
    other = render(page, deep_merge(a_params, {"lines": {"sigma": 1.5}, "layers": [layer()]}))
    a2 = render(page, a_params)
    a3 = render(page, a_params)
    assert (a1 != other).any()
    assert (a1 == a2).all() and (a2 == a3).all()


def test_params_save_and_reload_give_same_output(browser, main_site):
    page = open_page(browser, main_site.url)
    open_image(page, "halves.png")
    params = deep_merge(page.evaluate("() => window.inkdeus.defaults()"), {
        "unit": 1.5,
        "lines": {"sigma": 1.1, "epsilon": -0.02},
        "layers": [layer(threshold=0.8, angle=30, spacing=3.0, width=0.75, widthByDarkness=0.4, phase=0.3),
                   layer(threshold=0.4, angle=120, spacing=2.5, width=0.5)],
        "watermark": {"enabled": True, "text": "sample", "position": "tl", "size": 20},
    })
    before = render(page, params)
    page.evaluate("() => window.inkdeus.save()")
    saved = json.loads((main_site.out / "halves.inkdeus.json").read_text(encoding="utf-8"))
    assert saved == page.evaluate("() => window.inkdeus.getParams()")
    assert saved["regions"]["person"]["layers"][0]["angle"] == 30 and saved["version"] == 2
    page.close()

    page = open_page(browser, main_site.url)       # 開き直して保存済みの設定を読む
    open_image(page, "halves.png")
    assert page.evaluate("() => window.inkdeus.getParams()") == saved
    assert (pixels(page) == before).all()
    page.close()


def test_binary_output_is_pure_black_and_white(page):
    open_image(page, "scene.png")
    base = deep_merge(page.evaluate("() => window.inkdeus.defaults()"), {"unit": 1})
    for wm in ({"enabled": False}, {"enabled": True, "position": "tile", "opacity": 0.35},
               {"enabled": True, "position": "br", "opacity": 0.35}):
        gray = render(page, deep_merge(base, {"watermark": wm, "layers": [layer(threshold=0.7, opacity=0.5)]}))
        assert len(np.unique(gray)) > 2
        img = render(page, deep_merge(base, {"watermark": wm, "output": {"binary": True},
                                             "layers": [layer(threshold=0.7, opacity=0.5)]}))
        assert set(np.unique(img)) <= {0, 255}
        assert 0 in img and 255 in img


def test_unknown_keys_are_reported_and_missing_keys_filled(page):
    open_image(page, "white.png")
    result = page.evaluate("""() => window.inkdeus.setParams({
        version: 2, mystery: 1, lines: { sigma: 1.0, bogus: true, phi: 'x' },
        regions: {
            person: { layers: [{ type: 'kakeami', threshold: 0.4, extra: 0 }], solid: { threshold: 9 }, hue: 1 },
            sky: {},
        },
    })""")
    text = "\n".join(result["warnings"])
    for needle in ("未知のキー: mystery", "未知のキー: lines.bogus", "lines.phi", "regions.person.layers[0].type",
                   "未知のキー: regions.person.layers[0].extra", "regions.person.solid.threshold",
                   "未知のキー: regions.person.hue", "未知のキー: regions.sky"):
        assert needle in text, text
    params = page.evaluate("() => window.inkdeus.getParams()")
    defaults = page.evaluate("() => window.inkdeus.defaults()")
    assert params["lines"]["sigma"] == 1.0 and params["lines"]["phi"] == defaults["lines"]["phi"]
    assert no_ids(params["regions"]["background"]) == no_ids(defaults["regions"]["background"])
    person = params["regions"]["person"]
    assert person["layers"][0]["type"] == "hatch" and person["layers"][0]["threshold"] == 0.4
    assert person["solid"]["threshold"] == 1
    assert person["levels"] == {"brightness": 0, "contrast": 1, "gamma": 1}
    assert page.locator("#messages .msg").count() >= 1      # 画面にも警告が出る


def test_old_format_is_migrated(page):
    """旧形式（トップレベルに solid / layers / background）は人物・背景の領域へ移して読む"""
    open_image(page, "scene.png")
    old = {"version": 1, "unit": 1, "solid": {"enabled": True, "threshold": 0.3},
           "layers": [layer(threshold=0.66, angle=20)],
           "background": {"mode": "white", "grow": 7, "silhouetteWidth": 2, "lineStrength": 0.4}}
    result = page.evaluate("(p) => window.inkdeus.setParams(p)", old)
    assert result["warnings"] == []
    params = page.evaluate("() => window.inkdeus.getParams()")
    assert params["version"] == 2 and "layers" not in params and "solid" not in params
    assert params["regions"]["person"]["solid"]["threshold"] == 0.3
    assert [l["angle"] for l in params["regions"]["person"]["layers"]] == [20]
    bg = params["regions"]["background"]
    assert (bg["mode"], bg["grow"], bg["lineStrength"], bg["linked"]) == ("white", 7, 0.4, True)


def test_default_preset_values(page):
    d = page.evaluate("() => window.inkdeus.defaults()")
    person = d["regions"]["person"]
    got = [(l["threshold"], l["angle"], l["spacing"], l["width"]) for l in person["layers"]]
    assert got == [(0.72, 45, 2.0, 0.5), (0.52, 135, 2.0, 0.5), (0.38, 0, 2.0, 0.5), (0.27, 90, 2.0, 0.5)]
    assert person["layers"][0]["widthByDarkness"] == 0.6
    assert person["solid"]["threshold"] == 0.15 and person["solid"]["enabled"] is True
    assert all(l["type"] == "hatch" for l in person["layers"])
    assert d["regions"]["background"]["linked"] is True and d["regions"]["background"]["mode"] == "keep"
    styles = page.evaluate("() => window.inkdeus.styles()")
    assert {"hatch1", "hatch1-fine", "crosshatch", "crosshatch-fine", "crosshatch-ultrafine",
            "lines-only", "white"} <= set(styles)
    assert d["unit"] is None
    open_image(page, "scene.png")
    page.evaluate("() => window.inkdeus.setParams({})")
    assert page.evaluate("() => window.inkdeus.info().unit") == pytest.approx(640 / 1280)


def test_export_saves_grayscale_png_identical_to_preview(page, main_site):
    open_image(page, "scene.png")
    base = deep_merge(page.evaluate("() => window.inkdeus.defaults()"), {"unit": 1})
    preview = render(page, base)
    result = page.evaluate("() => window.inkdeus.exportPNG()")
    path = main_site.out / "scene.png"
    assert result["path"] == str(path)
    with Image.open(path) as im:
        assert im.mode == "L" and im.size == (640, 480)
        assert (np.asarray(im) == preview).all()
    assert str(path) in page.locator("#exportInfo").inner_text()
    # 入力画像は書き換わっていない
    with Image.open(main_site.images / "scene.png") as im:
        assert im.mode == "RGB"


# ---------------------------------------------------------------------------
# 透かし
# ---------------------------------------------------------------------------

def rect_mask(shape, rect) -> np.ndarray:
    m = np.zeros(shape, bool)
    m[rect["y"]:rect["y"] + rect["h"], rect["x"]:rect["x"] + rect["w"]] = True
    return m


def test_watermark_changes_only_its_area(page):
    open_image(page, "scene.png")
    base = deep_merge(page.evaluate("() => window.inkdeus.defaults()"), {"unit": 1})
    off = render(page, deep_merge(base, {"watermark": {"enabled": False}}))
    assert page.evaluate("() => window.inkdeus.watermark().rect") is None
    for position in ("br", "tl", "center"):
        on = render(page, deep_merge(base, {"watermark": {"enabled": True, "position": position, "size": 24}}))
        rect = page.evaluate("() => window.inkdeus.watermark().rect")
        area = rect_mask(on.shape, rect)
        assert (on[~area] == off[~area]).all(), position
        assert (on[area] != off[area]).mean() > 0.05, position
    r = page.evaluate("() => window.inkdeus.watermark().rect")
    assert abs((r["x"] + r["w"] / 2) - 320) <= 1 and abs((r["y"] + r["h"] / 2) - 240) <= 1
    tile = render(page, deep_merge(base, {"watermark": {"enabled": True, "position": "tile", "size": 24}}))
    quads = [(tile[:240, :320], off[:240, :320]), (tile[240:, 320:], off[240:, 320:])]
    assert all((a != b).any() for a, b in quads)   # タイル状なら画面全体に繰り返す


def test_watermark_is_off_by_default_and_switch_adds_it(page):
    assert "owner" not in page.evaluate("() => window.inkdeus.info()")
    assert page.evaluate("() => window.inkdeus.defaults().watermark.enabled") is False
    open_image(page, "white.png")
    page.evaluate("() => window.inkdeus.setParams({ unit: 1 })")     # 既定値（watermark.enabled は補われる）
    assert page.evaluate("() => window.inkdeus.getParams().watermark.enabled") is False
    assert (pixels(page) == 255).all()
    page.locator("summary", has_text="透かし").click()
    switch = page.get_by_label("透かしを入れる")
    assert switch.count() == 1 and not switch.is_checked()
    switch.check()
    page.evaluate("() => window.inkdeus.render()")
    assert page.evaluate("() => window.inkdeus.getParams().watermark.enabled") is True
    img = pixels(page)
    rect = page.evaluate("() => window.inkdeus.watermark().rect")
    area = rect_mask(img.shape, rect)
    assert (img[area] < 255).any() and (img[~area] == 255).all()
    switch.uncheck()                                                 # スイッチで外せる
    page.evaluate("() => window.inkdeus.render()")
    assert page.evaluate("() => window.inkdeus.getParams().watermark.enabled") is False
    assert (pixels(page) == 255).all()


def test_watermark_enabled_setting_is_respected(plain_page, site):
    """設定の watermark.enabled がそのまま効く（強制しない）。プレビューにも書き出しにも"""
    page = plain_page
    open_image(page, "white.png")
    img = render(page, deep_merge(BLANK, {"watermark": {"enabled": False}}))
    wm = page.evaluate("() => window.inkdeus.watermark()")
    assert wm["enabled"] is False and wm["rect"] is None
    assert (img == 255).all()
    page.locator("summary", has_text="透かし").click()
    assert page.get_by_label("透かしを入れる").count() == 1       # スイッチは常に出る
    assert page.locator("#wmForcedNote").count() == 0
    page.evaluate("() => window.inkdeus.exportPNG()")
    with Image.open(site.out / "white.png") as im:
        assert (np.asarray(im) == 255).all()                      # 書き出しにも入らない

    img = render(page, deep_merge(BLANK, {"watermark": {"enabled": True}}))
    wm = page.evaluate("() => window.inkdeus.watermark()")
    assert wm["enabled"] is True
    area = rect_mask(img.shape, wm["rect"])
    assert (img[area] < 255).sum() > 50 and (img[~area] == 255).all()
    page.evaluate("() => window.inkdeus.exportPNG()")
    with Image.open(site.out / "white.png") as im:
        assert (np.asarray(im) == img).all()                      # 書き出しにも入る

    # enabled が無い設定（古い設定ファイルなど）は既定のオフとして読める
    result = page.evaluate("(p) => window.inkdeus.setParams(p)", deep_merge(BLANK, {"watermark": {"text": "x"}}))
    assert page.evaluate("() => window.inkdeus.getParams().watermark.enabled") is False
    assert result["warnings"] == [] and (pixels(page) == 255).all()


def test_watermark_minimums_keep_rendering_intact(plain_page, site):
    """極端な値（空の文字列・小さすぎる大きさ・薄すぎる濃さ）でも描画が壊れない"""
    page = plain_page
    open_image(page, "white.png")
    result = page.evaluate("(p) => window.inkdeus.setParams(p)",
                           deep_merge(BLANK, {"watermark": {"enabled": True, "text": "   ", "size": 0, "opacity": 0}}))
    assert any("watermark.size" in w for w in result["warnings"])
    assert any("watermark.opacity" in w for w in result["warnings"])
    fields = page.evaluate("() => window.inkdeus.schema().sections.watermark")
    wm = page.evaluate("() => window.inkdeus.watermark()")
    assert 0 < fields["size"]["min"] <= 8 and 0 < fields["opacity"]["min"] <= 0.15
    assert wm["size"] == fields["size"]["min"] and wm["opacity"] == fields["opacity"]["min"]
    assert wm["text"] == "inkdeus"                  # 空の文字列なら既定の文字列
    img = pixels(page)
    area = rect_mask(img.shape, wm["rect"])
    assert wm["rect"]["w"] > 0 and wm["rect"]["h"] >= fields["size"]["min"]
    assert (img[area] < 255).any() and (img[~area] == 255).all()
    assert page.errors == []
    # UI の数値欄も最小値より下げられない
    page.locator("summary", has_text="透かし").click()
    number = page.locator('#panel .row[data-key="opacity"] input[type="number"]').last
    number.fill("0")
    number.dispatch_event("change")
    assert page.evaluate("() => window.inkdeus.getParams().watermark.opacity") == fields["opacity"]["min"]


# ---------------------------------------------------------------------------
# UI の最小限の確認
# ---------------------------------------------------------------------------

def test_undo_redo_and_slider_reset(page):
    open_image(page, "gray.png")
    render(page, deep_merge(BLANK, {"lines": {"enabled": True, "sigma": 0.8}}))
    row = page.locator('#panel .row[data-key="sigma"]')
    number = row.locator('input[type="number"]')
    number.fill("2")
    number.dispatch_event("change")
    get_sigma = "() => window.inkdeus.getParams().lines.sigma"
    assert page.evaluate(get_sigma) == 2
    page.locator("#topbar .brand").click()          # 入力欄からフォーカスを外す
    page.keyboard.press("Control+z")
    assert page.evaluate(get_sigma) == 0.8
    page.keyboard.press("Control+y")
    assert page.evaluate(get_sigma) == 2
    row.locator("label").dblclick()                 # ダブルクリックで既定値
    assert page.evaluate(get_sigma) == page.evaluate("() => window.inkdeus.schema().sections.lines.sigma.def")


def test_layer_list_editing(page):
    open_image(page, "gray.png")
    page.evaluate("() => window.inkdeus.setParams({ unit: 1 })")
    count = "() => window.inkdeus.getParams().regions.person.layers.length"
    assert page.evaluate(count) == 4
    page.locator("#btnAddLayer").click()
    assert page.evaluate(count) == 5
    first = page.locator('.layer[data-layer="0"]')
    first.get_by_title("複製").click()
    layers = page.evaluate("() => window.inkdeus.getParams().regions.person.layers")
    assert len(layers) == 6 and no_ids(layers[0]) == no_ids(layers[1]) and layers[0]["id"] != layers[1]["id"]
    page.locator('.layer[data-layer="0"]').get_by_title("下へ").click()
    page.locator('.layer[data-layer="5"]').get_by_title("削除").click()
    assert page.evaluate(count) == 5
    # ソロ表示: その層だけになる（出力が変わり、解除で戻る）
    page.evaluate("() => window.inkdeus.render()")
    normal = pixels(page)
    page.locator('.layer[data-layer="3"]').get_by_title("ソロ表示（この層だけを見る）").click()
    page.evaluate("() => window.inkdeus.render()")
    solo = pixels(page)
    assert (solo != normal).any() and ink(solo).mean() < ink(normal).mean()
    page.locator('.layer[data-layer="3"]').get_by_title("ソロ表示（この層だけを見る）").click()
    page.evaluate("() => window.inkdeus.render()")
    assert (pixels(page) == normal).all()
    # プリセット
    page.locator("#selPreset").select_option("lines-only")
    assert page.evaluate(count) == 0
    assert page.evaluate("() => window.inkdeus.getParams().regions.person.solid.enabled") is False
    for _ in range(9):                              # 層は最大 8
        if page.locator("#btnAddLayer").is_enabled():
            page.locator("#btnAddLayer").click()
    assert page.evaluate(count) == 8


def test_view_zoom_and_original_toggle(page):
    open_image(page, "scene.png")
    zoom = "() => window.inkdeus.state.view.zoom"
    page.locator("#btnActual").click()
    assert page.evaluate(zoom) == 1
    box = page.locator("#gl").bounding_box()
    page.mouse.move(box["x"] + 200, box["y"] + 200)
    page.mouse.wheel(0, -400)
    assert page.evaluate(zoom) > 1
    page.locator("#btnFit").click()
    assert 0 < page.evaluate(zoom) < 2.5
    page.keyboard.down("Space")
    assert page.evaluate("() => window.inkdeus.state.original") is True
    page.keyboard.up("Space")
    assert page.evaluate("() => window.inkdeus.state.original") is False
    page.locator("#btnCompare").click()
    page.locator("#splitHandle").wait_for(state="visible", timeout=5000)
    assert page.errors == []


# ---------------------------------------------------------------------------
# サーバなし（静的配信だけ）
# ---------------------------------------------------------------------------

class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def test_standalone_mode_without_api(browser, site, tmp_path):
    handler = functools.partial(QuietHandler, directory=str(WEB_DIR))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        page = open_page(browser, f"http://127.0.0.1:{httpd.server_port}/")
        info = page.evaluate("() => window.inkdeus.info()")
        assert info["server"] is False and "owner" not in info
        page.set_input_files("#fileImage", str(site.images / "scene.png"))
        page.wait_for_function("() => window.inkdeus.info().width === 640")
        page.set_input_files("#fileMask", str(site.masks / "scene.png"))
        page.wait_for_function("() => window.inkdeus.info().hasMask === true")
        params_file = tmp_path / "p.inkdeus.json"
        params_file.write_text(json.dumps(deep_merge(BLANK, {
            "background": {"mode": "white", "grow": 4}, "watermark": {"enabled": False},
            "layers": [layer(threshold=0.6)]})), encoding="utf-8")
        page.set_input_files("#fileParams", str(params_file))
        page.wait_for_function("() => window.inkdeus.getParams().regions.background.mode === 'white'")
        page.evaluate("() => window.inkdeus.render()")
        img = pixels(page)
        wm = page.evaluate("() => window.inkdeus.watermark()")
        assert wm["enabled"] is False and wm["rect"] is None    # サーバなしでも設定どおり（透かし無し）
        out = outside_mask(4)
        assert (img[out] == 255).all()
        with page.expect_download() as dl:           # 書き出しはダウンロードになる
            page.locator("summary", has_text="出力").scroll_into_view_if_needed()
            page.locator("#btnExport").click()
        saved = tmp_path / "export.png"
        dl.value.save_as(saved)
        assert dl.value.suggested_filename == "scene_inkdeus.png"
        with Image.open(saved) as im:
            assert im.mode == "L" and (np.asarray(im) == img).all()
        with page.expect_download() as dl:           # 設定の保存もダウンロード
            page.locator("#btnSaveJSON").click()
        assert dl.value.suggested_filename == "scene.inkdeus.json"
        page.close()
    finally:
        httpd.shutdown()


# ---------------------------------------------------------------------------
# 大きな画像（5120x3840）
# ---------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.skipif(os.environ.get("INKDEUS_SKIP_BIG") == "1", reason="INKDEUS_SKIP_BIG=1")
def test_large_image_end_to_end(browser, main_site, capsys):
    w, h = 5120, 3840
    img, mask = synth.scene(w, h)
    synth.save(main_site.images / "big.png", img, fast=True)
    synth.save(main_site.masks / "big.png", mask, fast=True)
    del img
    page = open_page(browser, main_site.url)
    try:
        max_size = page.evaluate("() => window.inkdeus.info().maxTextureSize")
        assert max_size >= max(w, h), f"この環境の最大テクスチャサイズは {max_size}"
        t0 = time.time()
        info = open_image(page, "big.png")           # 読み込み＋マスクの距離場＋最初の描画
        t_load = time.time() - t0
        assert (info["width"], info["height"], info["hasMask"]) == (w, h, True)
        assert info["unit"] == pytest.approx(4.0)
        params = deep_merge(page.evaluate("() => window.inkdeus.defaults()"),
                            {"background": {"mode": "white"}, "watermark": {"enabled": True}})
        t0 = time.time()
        first = page.evaluate("(p) => window.inkdeus.setParams(p)", params)
        t_render = time.time() - t0
        t0 = time.time()
        tweak = page.evaluate("(p) => window.inkdeus.setParams(p)",
                              deep_merge(params, {"layers": [layer(threshold=0.6, widthByDarkness=0.5)]}))
        t_tweak = time.time() - t0
        assert tweak["passes"] == ["composite"]
        t0 = time.time()
        heavy = page.evaluate("(p) => window.inkdeus.setParams(p)",
                              deep_merge(params, {"lines": {"sigma": 1.0}, "input": {"smoothRadius": 2.5}}))
        t_heavy = time.time() - t0
        assert set(heavy["passes"]) == {"smooth", "blur", "composite"}
        page.evaluate("(p) => window.inkdeus.setParams(p)", params)
        stats = page.evaluate("""() => {
            const px = window.inkdeus.readPixels(0, 1800, 5120, 64);
            let sum = 0, dark = 0;
            for (const v of px) { sum += v; if (v < 128) dark += 1; }
            return { mean: sum / px.length, dark };
        }""")
        assert 20 < stats["mean"] < 250 and stats["dark"] > 1000
        t0 = time.time()
        result = page.evaluate("() => window.inkdeus.exportPNG()")
        t_export = time.time() - t0
        path = main_site.out / "big.png"
        assert result["path"] == str(path)
        Image.MAX_IMAGE_PIXELS = None
        with Image.open(path) as im:
            assert im.mode == "L" and im.size == (w, h)
            arr = np.asarray(im)
        outside = ~mask
        outside[:, w // 2 - 1200:w // 2 + 1200] = False          # 人物の近くは除く
        outside[int(h * 0.8):, int(w * 0.8):] = False            # 透かし（右下）は除く
        assert (arr[outside] == 255).all()
        assert (arr[mask] < 128).mean() > 0.1
        assert page.errors == []
        with capsys.disabled():
            print(f"\n[5120x3840] renderer={info['renderer']}"
                  f"\n  読み込み〜最初の描画（距離場の作成を含む）: {t_load:.1f}s"
                  f"\n  設定変更後の再描画: {t_render:.1f}s（内部計測 {first['ms']:.0f}ms, passes={first['passes']}）"
                  f"\n  合成だけの再描画: {t_tweak:.1f}s（内部計測 {tweak['ms']:.0f}ms）"
                  f"\n  平滑化＋ぼかし＋合成の再描画: {t_heavy:.1f}s（内部計測 {heavy['ms']:.0f}ms）"
                  f"\n  書き出し（PNG 化＋保存）: {t_export:.1f}s / {path.stat().st_size / 1e6:.1f} MB")
    finally:
        page.close()
        (main_site.images / "big.png").unlink(missing_ok=True)
        (main_site.masks / "big.png").unlink(missing_ok=True)
