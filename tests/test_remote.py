"""外部からの操作とホットリロード、領域別の設定、統計（実際のブラウザで確かめる）"""

from __future__ import annotations

import json
import time
import urllib.request

import numpy as np
import pytest

import synth
from conftest import BLANK, deep_merge, ink, layer, make_images, open_image, open_page, pixels, render, serve
from inkdeus import cli
from inkdeus.server import create_app


@pytest.fixture()
def live(tmp_path, browser):
    """このテスト専用のサーバ（所有者モード・プロジェクトあり）と、そこへつないだページ"""
    images, masks = make_images(tmp_path)
    out = tmp_path / "out"
    project = {"name": "テスト本", "path": str(tmp_path)}
    server, url = serve(create_app(images, mask_dir=masks, out_dir=out, owner=True, project=project))
    page = open_page(browser, url)
    yield {"page": page, "url": url, "port": server.server_port, "out": out}
    page.close()
    server.shutdown()


def api(live, path):
    with urllib.request.urlopen(live["url"] + path, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def ctl(live, capsys, *args, expect=0):
    capsys.readouterr()
    code = cli.main(["ctl", "--port", str(live["port"]), *args])
    captured = capsys.readouterr()
    assert code == expect, captured.err or captured.out
    return json.loads(captured.out if code == 0 else captured.err)


def settle(page):
    """手元の変更を保存して、rev を返す"""
    page.evaluate("() => window.inkdeus.save()")
    return page.evaluate("() => window.inkdeus.rev()")


def wait_rev(page, rev):
    page.wait_for_function("(rev) => window.inkdeus.rev() === rev", arg=rev, timeout=15000)


BASE = deep_merge(BLANK, {
    "lines": {"enabled": True},
    "solid": {"enabled": True, "threshold": 0.15},
    "layers": [layer(threshold=0.72, angle=45, spacing=4.0, width=1.0),
               layer(threshold=0.45, angle=135, spacing=4.0, width=1.0)],
})


def test_browser_reports_current_image_and_project(live, capsys):
    page = live["page"]
    assert page.locator("#projectName").inner_text() == "テスト本"
    open_image(page, "scene.png")
    state = ctl(live, capsys, "state")
    assert state["current"] == "scene.png" and state["clients"] == 1 and state["owner"] is True
    assert state["project"]["name"] == "テスト本" and state["images"] == 6
    assert ctl(live, capsys, "open", "gradient.png", "--timeout", "30")["current"] == "gradient.png"
    assert page.evaluate("() => window.inkdeus.info().name") == "gradient.png"


def test_external_file_edit_is_hot_reloaded(live):
    page = live["page"]
    open_image(page, "scene.png")
    before = render(page, BASE)
    rev0 = settle(page)
    path = live["out"] / "scene.inkdeus.json"
    assert rev0 and path.is_file()

    # エディタなどで設定ファイルを直接書き換える
    edited = json.loads(path.read_text(encoding="utf-8"))
    edited["regions"]["person"]["layers"] = [layer(threshold=0.72, angle=0, spacing=6.0, width=3.0)]
    edited["regions"]["person"]["solid"]["enabled"] = False
    path.write_text(json.dumps(edited), encoding="utf-8")

    page.wait_for_function("(rev) => window.inkdeus.rev() !== rev", arg=rev0, timeout=15000)
    rev1 = page.evaluate("() => window.inkdeus.rev()")
    after = pixels(page)
    assert (after != before).mean() > 0.05                      # 描画が変わった
    params = page.evaluate("() => window.inkdeus.getParams()")
    assert [l["angle"] for l in params["regions"]["person"]["layers"]] == [0]
    assert page.locator("#saveState").inner_text() == "外部から更新"
    # 新しい rev に対する統計が届く
    deadline = time.time() + 15
    while not (stats := api(live, "api/stats/scene.png"))["fresh"]:
        assert time.time() < deadline, stats
        time.sleep(0.2)
    assert stats["stats"]["rev"] == rev1 == stats["currentRev"]
    assert stats["stats"]["output"]["person"]["layers"] == [{"layer": 0, "coverage": pytest.approx(0.6, abs=0.4)}]
    # 取り消し履歴に1件として積まれていて、Ctrl+Z で戻せる
    page.locator("#topbar .brand").click()
    page.keyboard.press("Control+z")
    page.evaluate("() => window.inkdeus.render()")
    assert (pixels(page) == before).all()
    # 壊れた JSON を書かれても落ちない（メッセージを出して今の設定のまま）
    path.write_text("{ broken", encoding="utf-8")
    page.locator("#messages .msg.error").first.wait_for(timeout=15000)
    assert (pixels(page) == before).all()


def test_ctl_patch_updates_browser_and_stats_wait(live, capsys):
    page = live["page"]
    open_image(page, "scene.png")
    before = render(page, BASE)
    settle(page)
    r = ctl(live, capsys, "patch", '{"regions": {"person": {"solid": {"threshold": 0.5}}}}')
    wait_rev(page, r["rev"])
    after = pixels(page)
    assert ink(after).mean() > ink(before).mean() + 0.02          # ベタが増えた
    got = ctl(live, capsys, "stats", "--wait", "--timeout", "30")
    assert got["fresh"] is True and got["stats"]["rev"] == r["rev"] and got["stats"]["name"] == "scene.png"
    assert got["stats"]["renderMs"] >= 0 and got["stats"]["width"] == 640
    solid_before = got["stats"]["output"]["person"]["solidRatio"]
    # 1項目だけ変える → 統計の rev も進む
    r2 = ctl(live, capsys, "set", "regions.person.solid.threshold", "0.1")
    got2 = ctl(live, capsys, "stats", "--wait", "--timeout", "30")
    assert got2["stats"]["rev"] == r2["rev"] != r["rev"]
    assert got2["stats"]["output"]["person"]["solidRatio"] < solid_before
    assert page.evaluate("() => window.inkdeus.getParams().regions.person.solid.threshold") == 0.1
    # 書き出しも外から指示できる（保存先が返る）
    exported = ctl(live, capsys, "export", "--timeout", "60")
    assert exported["path"] == str(live["out"] / "scene.png") and (live["out"] / "scene.png").is_file()
    assert exported["rev"] == r2["rev"]


def test_patch_without_browser_is_applied_on_next_open(live, capsys):
    page = live["page"]
    open_image(page, "scene.png")
    # 開いていない画像の設定を変える → ファイルだけ更新され、開いたときに反映される
    r = ctl(live, capsys, "preset", "white", "--region", "background", "--name", "halves.png")
    assert (live["out"] / "halves.inkdeus.json").is_file()
    assert page.evaluate("() => window.inkdeus.info().name") == "scene.png"
    open_image(page, "halves.png")
    assert page.evaluate("() => window.inkdeus.rev()") == r["rev"]
    assert page.evaluate("() => window.inkdeus.getParams().regions.background.mode") == "white"


def test_own_save_does_not_cause_reload_loop(live):
    page = live["page"]
    open_image(page, "scene.png")
    render(page, BASE)
    settle(page)
    requests: list[tuple[str, str]] = []
    page.on("request", lambda r: requests.append((r.method, r.url.split("/api/")[-1])) if "/api/" in r.url else None)
    render(page, deep_merge(BASE, {"lines": {"sigma": 1.3}}))
    rev = settle(page)
    page.wait_for_timeout(2500)                                  # 監視（0.5秒ごと）が何周かする間待つ
    puts = [r for r in requests if r[0] == "PUT"]
    gets = [r for r in requests if r[0] == "GET" and r[1].startswith("params/")]
    assert len(puts) == 1, requests                              # 保存は1回だけ
    assert gets == [], requests                                  # 自分の保存では読み直さない
    assert page.evaluate("() => window.inkdeus.rev()") == rev
    assert page.locator("#saveState").inner_text() == "保存済み"
    assert len([r for r in requests if r[0] == "POST" and r[1].startswith("stats/")]) == 1
    assert api(live, "api/stats/scene.png")["stats"]["rev"] == rev


def test_background_levels_change_only_the_background(live):
    page = live["page"]
    open_image(page, "scene.png")
    base = deep_merge(BASE, {"background": {"linked": False, "mode": "keep", "grow": 0},
                             "regions": {"background": {"solid": {"enabled": True, "threshold": 0.45},
                                                        "layers": BASE["regions"]["person"]["layers"]}}})
    a = render(page, base)
    sa = page.evaluate("() => window.inkdeus.stats()")
    b = render(page, deep_merge(base, {"regions": {"background": {"levels": {"brightness": 0.35}}}}))
    sb = page.evaluate("() => window.inkdeus.stats()")
    mask = synth.figure_mask(640, 480)
    assert (a[mask] == b[mask]).all()                            # 人物領域の画素は不変
    assert (a[~mask] != b[~mask]).mean() > 0.02
    assert sb["output"]["background"]["blackRatio"] < sa["output"]["background"]["blackRatio"] - 0.05
    assert sb["output"]["background"]["solidRatio"] < sa["output"]["background"]["solidRatio"]
    assert sb["output"]["person"] == sa["output"]["person"]      # 人物の統計も不変
    assert sb["input"]["background"]["adjusted"]["median"] > sa["input"]["background"]["adjusted"]["median"] + 0.2
    assert sb["input"]["background"]["luminance"] == sa["input"]["background"]["luminance"]
    # ガンマ・コントラストも背景だけに効く
    c = render(page, deep_merge(base, {"regions": {"background": {"levels": {"gamma": 2.5, "contrast": 0.6}}}}))
    assert (a[mask] == c[mask]).all() and ink(c[~mask]).mean() < ink(a[~mask]).mean()
    # 人物側の補正は人物だけに効く
    d = render(page, deep_merge(base, {"levels": {"brightness": -0.3}}))
    assert ink(d[mask]).mean() > ink(a[mask]).mean() + 0.02
    outside = ~np.asarray(synth.figure_mask(640, 480))
    assert (a[outside] == d[outside]).all()


def test_linked_background_follows_person(live):
    page = live["page"]
    open_image(page, "scene.png")
    linked = render(page, deep_merge(BASE, {"background": {"linked": True}, "levels": {"brightness": 0.2}}))
    # 連動中は、背景側に何が書いてあっても人物の設定で描く
    ignored = render(page, deep_merge(BASE, {"levels": {"brightness": 0.2}, "regions": {"background": {
        "linked": True, "levels": {"brightness": -1}, "layers": [], "solid": {"enabled": False}}}}))
    assert (linked == ignored).all()
    same = render(page, deep_merge(BASE, {"levels": {"brightness": 0.2}, "regions": {"background": {
        "linked": False, "levels": {"brightness": 0.2}, "solid": BASE["regions"]["person"]["solid"],
        "layers": BASE["regions"]["person"]["layers"]}}}))
    assert (linked == same).all()                                # 同じ値で切り離しても見た目は同じ
    # マスクの無い画像は全体が人物扱い（背景の独自設定は効かない）
    open_image(page, "gradient.png")
    p = render(page, BASE)
    q = render(page, deep_merge(BASE, {"regions": {"background": {"linked": False, "mode": "white", "layers": []}}}))
    assert (p == q).all()
    assert page.evaluate("() => window.inkdeus.stats().output.person.areaRatio") == 1


def test_different_presets_per_region(live, capsys):
    page = live["page"]
    open_image(page, "scene.png")
    render(page, deep_merge(BASE, {"unit": 2, "background": {"grow": 0}}))
    settle(page)
    ctl(live, capsys, "preset", "ハッチ(1)", "--region", "background")
    r = ctl(live, capsys, "preset", "クロスハッチ極細", "--region", "person")
    wait_rev(page, r["rev"])
    regions = page.evaluate("() => window.inkdeus.getParams().regions")
    assert [l["angle"] for l in regions["person"]["layers"]] == [45, 135, 0, 90]
    assert [l["spacing"] for l in regions["person"]["layers"]] == [1.0] * 4
    assert [l["angle"] for l in regions["background"]["layers"]] == [45]
    assert regions["background"]["linked"] is False and regions["background"]["mode"] == "keep"
    stats = ctl(live, capsys, "stats", "--wait", "--timeout", "30")["stats"]
    assert [x["layer"] for x in stats["output"]["person"]["layers"]] == [0, 1, 2, 3]
    assert [x["layer"] for x in stats["output"]["background"]["layers"]] == [0]
    cover = [x["coverage"] for x in stats["output"]["person"]["layers"]]
    assert cover == sorted(cover, reverse=True) and cover[0] > cover[-1]     # 暗い層ほど狭い
    assert stats["backgroundMode"] == "keep" and stats["hasMask"] is True
    assert stats["output"]["person"]["areaRatio"] + stats["output"]["background"]["areaRatio"] == pytest.approx(1)

    # 画素でも確かめる: 背景は 45° の線だけ、人物は 4 方向（線画とベタを消して層だけを見る）
    params = page.evaluate("() => window.inkdeus.getParams()")
    img = render(page, deep_merge(params, {"lines": {"enabled": False}, "solid": {"enabled": False},
                                           "regions": {"background": {"solid": {"enabled": False}}}})).astype(float)
    bg = img[300:420, 20:140]                                    # 左下の暗い箱（背景）
    assert bg.std() > 20
    assert np.abs(bg[1:, :-1] - bg[:-1, 1:]).mean() < 3          # 45° 方向には変化しない
    assert np.abs(bg[1:, 1:] - bg[:-1, :-1]).mean() > 20
    body = img[330:460, 330:400]                                 # 人物の暗い側
    assert np.abs(body[1:, :-1] - body[:-1, 1:]).mean() > 10     # 重ねがけなのでどの方向にも変化する
    assert np.abs(body[1:, 1:] - body[:-1, :-1]).mean() > 10


def test_stats_match_the_pixels(live):
    page = live["page"]
    open_image(page, "gradient.png")
    img = render(page, deep_merge(BASE, {"watermark": {"enabled": False}}))
    s = page.evaluate("() => window.inkdeus.stats()")
    out = s["output"]["all"]
    assert out["blackRatio"] == pytest.approx((img < 128).mean(), abs=0.03)
    assert out["meanDensity"] == pytest.approx(ink(img).mean(), abs=0.03)
    assert s["output"]["person"]["blackRatio"] == out["blackRatio"] and s["output"]["background"]["blackRatio"] is None
    assert s["output"]["background"]["areaRatio"] == 0 and s["hasMask"] is False
    lum = synth.gradient(768, 128)[0, :, 0] / 255.0
    lin = np.where(lum <= 0.04045, lum / 12.92, ((lum + 0.055) / 1.055) ** 2.4)
    lab = 1.16 * np.where(lin > 0.008856, np.cbrt(lin), 7.787 * lin + 16 / 116) - 0.16
    assert out["solidRatio"] == pytest.approx((lab < 0.15).mean(), abs=0.02)
    layers = s["output"]["person"]["layers"]
    assert layers[0]["coverage"] == pytest.approx((lab < 0.72).mean(), abs=0.02)
    assert layers[1]["coverage"] == pytest.approx((lab < 0.45).mean(), abs=0.02)
    summary = s["input"]["all"]["luminance"]
    assert summary["median"] == pytest.approx(np.median(lab), abs=0.02)
    assert summary["mean"] == pytest.approx(lab.mean(), abs=0.02)
    assert summary["p10"] < summary["p25"] < summary["median"] < summary["p75"] < summary["p90"]
    assert s["input"]["all"]["adjusted"] == summary               # 補正なしなら同じ
    assert out["lineRatio"] == 0                                  # なだらかなグラデーションに線は出ない
    open_image(page, "halves.png")
    render(page, BASE)
    assert 0 < page.evaluate("() => window.inkdeus.stats().output.all.lineRatio") < 0.05
    # 無効にした層は統計でも設定上の番号で分かる
    render(page, deep_merge(BASE, {"layers": [layer(enabled=False), layer(threshold=0.9)]}))
    assert [x["layer"] for x in page.evaluate("() => window.inkdeus.stats().output.person.layers")] == [1]


def test_region_tabs_in_the_panel(live):
    page = live["page"]
    open_image(page, "scene.png")
    render(page, BASE)
    get = "() => window.inkdeus.getParams().regions"
    page.locator('.tabs button[data-tab="background"]').click()
    assert page.locator("#bgLinkedNote").count() == 1 and page.locator("#bgControls.disabled").count() == 0
    page.locator("#btnUnlink").click()                           # 背景だけ別の設定にする
    regions = page.evaluate(get)
    assert regions["background"]["linked"] is False
    assert regions["background"]["layers"] == regions["person"]["layers"]      # 人物の設定を写して切り離す
    page.locator("#selPreset").select_option("hatch1")           # 背景タブのスタイルは背景に当たる
    regions = page.evaluate(get)
    assert len(regions["background"]["layers"]) == 1 and len(regions["person"]["layers"]) == 2
    page.locator('#bgControls details.sub summary').click()      # 背景の明るさ補正
    number = page.locator('#bgControls .row[data-key="brightness"] input[type="number"]')
    number.fill("0.3")
    number.dispatch_event("change")
    regions = page.evaluate(get)
    assert regions["background"]["levels"]["brightness"] == 0.3 and regions["person"]["levels"]["brightness"] == 0
    page.locator('#bgControls .layer[data-layer="0"]').get_by_title("ソロ表示（この層だけを見る）").click()
    assert page.evaluate("() => window.inkdeus.state.solo") == {"region": "background", "index": 0}
    page.locator("#btnLink").click()                             # 人物と同じに戻す
    assert page.evaluate(get)["background"]["linked"] is True
    assert page.evaluate("() => window.inkdeus.state.solo") is None
    page.locator('.tabs button[data-tab="person"]').click()
    page.locator("#selPreset").select_option("crosshatch-ultrafine")
    assert [l["spacing"] for l in page.evaluate(get)["person"]["layers"]] == [1.0] * 4
    assert page.errors == []
