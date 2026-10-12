"""自分用スタイル（実際のブラウザで保存・適用して、出力画素を確かめる）。画像はすべて合成"""

from __future__ import annotations

import json

import numpy as np
import pytest

import synth
from conftest import BLANK, deep_merge, layer, no_ids, open_page, pixels, serve
from inkdeus import cli
from inkdeus.server import create_app

W, H = 320, 240


@pytest.fixture()
def live(tmp_path, browser):
    images, masks, parts = tmp_path / "images", tmp_path / "masks", tmp_path / "parts"
    img, mask = synth.scene(W, H)
    for name in ("fig", "other"):
        synth.save(images / f"{name}.png", img if name == "fig" else img[:, ::-1].copy())
        synth.save(masks / f"{name}.png", mask if name == "fig" else mask[:, ::-1].copy())
    pm = synth.part_masks(W, H)
    synth.save_parts(parts, "fig", pm)
    (tmp_path / "proj").mkdir()
    project = {"name": "スタイルの本", "path": str(tmp_path / "proj")}
    server, url = serve(create_app(images, mask_dir=masks, out_dir=tmp_path / "out", project=project, parts_dir=parts,
                                   config_dir=tmp_path / "config"))
    pages = []

    def open_(query: str = "?image=fig.png", **kw):
        page = open_page(browser, url + query, **kw)
        pages.append(page)
        return page

    yield {"open": open_, "url": url, "port": server.server_port, "out": tmp_path / "out", "parts": pm,
           "config": tmp_path / "config" / "styles.json", "project": tmp_path / "proj" / "styles.json"}
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


def options(page) -> list[tuple[str, str]]:
    """スタイルの選択欄の中身: (見出し, 表示名) の並び"""
    return [tuple(x) for x in page.evaluate("""() => [...document.querySelectorAll('#selPreset option')].map((o) =>
        [o.parentElement.tagName === 'OPTGROUP' ? o.parentElement.label : '', o.textContent])""")]


def mine(page) -> list[tuple[str, str]]:
    return [(g, t) for g, t in options(page) if g in ("自分用", "このプロジェクト")]


def ctl(live, capsys, *args, expect=0):
    capsys.readouterr()
    code = cli.main(["ctl", "--port", str(live["port"]), *args])
    captured = capsys.readouterr()
    assert code == expect, captured.err or captured.out
    return json.loads(captured.out if code == 0 else captured.err)


# 人物: 2 層（20° と 110°）・ベタ 0.3・明るさ +0.2
FAV = deep_merge(BLANK, {
    "lines": {"enabled": True, "sigma": 1.0},
    "solid": {"enabled": True, "threshold": 0.3},
    "levels": {"brightness": 0.2},
    "layers": [layer(threshold=0.8, angle=20, spacing=5.0, width=1.2), layer(threshold=0.5, angle=110, spacing=5.0, width=1.2)],
})
PLAIN = deep_merge(BLANK, {"lines": {"enabled": True, "sigma": 1.0}, "layers": [layer(threshold=0.6, angle=45, spacing=4, width=1)]})


def save_dialog(page, name: str, description: str = "", scope: str = "user", levels: bool = False) -> None:
    """選択欄の「いまの設定をスタイルとして保存…」から、ダイアログで保存する"""
    page.locator("#selPreset").select_option("@save")
    page.locator("#styleSaveDialog[open]").wait_for()
    page.locator("#styleName").fill(name)
    page.locator("#styleDesc").fill(description)
    if scope == "project":
        page.locator("#styleScopeProject").check()
    if levels:
        page.locator("#styleLevels").check()
    page.locator("#styleSaveOK").click()
    page.locator("#styleSaveDialog").wait_for(state="detached")


def test_save_and_apply_to_other_images_and_regions(live):
    page = live["open"]()
    assert page.evaluate("() => window.inkdeus.userStyles.ok()") is True
    want_fig = show(page, FAV)
    # 選択欄: 組み込みの下に、保存と管理の項目（まだ自分用スタイルは無い）
    groups = options(page)
    assert ("", "ハッチ1層") in groups and ("自分用スタイル", "いまの設定をスタイルとして保存…") in groups
    assert ("自分用スタイル", "自分用スタイルを管理…") in groups and mine(page) == []
    save_dialog(page, "お気に入り", "斜めの 2 層")
    assert mine(page) == [("自分用", "お気に入り")]
    assert (pixels(page) == want_fig).all()                              # 保存しただけでは、絵は変わらない
    saved = json.loads(live["config"].read_text(encoding="utf-8"))["styles"]
    assert [s["name"] for s in saved] == ["お気に入り"] and saved[0]["description"] == "斜めの 2 層"
    assert saved[0]["includeLevels"] is False and "levels" not in saved[0]["data"] and saved[0]["target"] == "any"
    assert [l["angle"] for l in saved[0]["data"]["layers"]] == [20, 110] and "id" not in saved[0]["data"]["layers"][0]
    assert not live["project"].exists()
    # 別の画像へ: 人物に当てると、同じ設定で描いたのと同じ絵になる（明るさ補正は含めていないので、合わせておく）
    page.evaluate("() => window.inkdeus.open('other.png')")
    want_other = show(page, FAV)
    base = show(page, deep_merge(PLAIN, {"levels": {"brightness": 0.2}}))
    assert (base != want_other).any()
    page.locator("#selPreset").select_option("@user:お気に入り")
    assert (settled(page) == want_other).all()
    r = regions(page)["person"]
    assert [l["angle"] for l in r["layers"]] == [20, 110] and r["solid"]["threshold"] == 0.3 and r["levels"]["brightness"] == 0.2
    # 取り消せる・やり直せる（組み込みのスタイルと同じ経路）
    page.evaluate("() => window.inkdeus.undo()")
    assert (pixels(page) == base).all()
    page.evaluate("() => window.inkdeus.redo()")
    assert (pixels(page) == want_other).all()
    # 背景へ: 背景は切り離され、スタイルの層とベタになる
    show(page, deep_merge(PLAIN, {"levels": {"brightness": 0.2}}))
    page.locator('.tabs button[data-tab="background"]').click()
    page.locator("#btnUnlink").click()
    page.locator("#selPreset").select_option("@user:お気に入り")
    got = settled(page)
    bg = regions(page)["background"]
    assert bg["linked"] is False and bg["mode"] == "keep" and [l["angle"] for l in bg["layers"]] == [20, 110]
    manual = deep_merge(PLAIN, {"levels": {"brightness": 0.2}, "background": {
        "linked": False, "levels": {"brightness": 0.2}, "solid": {"enabled": True, "threshold": 0.3},
        "layers": [layer(threshold=0.8, angle=20, spacing=5.0, width=1.2), layer(threshold=0.5, angle=110, spacing=5.0, width=1.2)]}})
    assert (got == show(page, manual)).all()
    # 部分へ: 髪だけがスタイルの見た目になる（人物に当てたときの髪と同じ）
    page.evaluate("() => window.inkdeus.open('fig.png')")
    plain = show(page, deep_merge(PLAIN, {"levels": {"brightness": 0.2}}))
    page.locator('.tabs button[data-tab="hair"]').click()
    page.locator("#btnUnlinkPart").click()
    assert ("自分用", "お気に入り") in mine(page)
    page.locator("#selPreset").select_option("@user:お気に入り")
    got = settled(page)
    hair = live["parts"]["hair"]
    assert regions(page)["hair"]["linked"] is False and [l["angle"] for l in regions(page)["hair"]["layers"]] == [20, 110]
    assert (got[hair] == want_fig[hair]).all() and (got[~hair] == plain[~hair]).all() and (want_fig[hair] != plain[hair]).any()
    assert page.errors == []


def test_including_levels_or_not(live):
    page = live["open"]()
    show(page, FAV)
    save_dialog(page, "補正なし")
    save_dialog(page, "補正つき", levels=True)
    assert mine(page) == [("自分用", "補正なし"), ("自分用", "補正つき（明るさ補正つき）")]
    rows = {r["name"]: r for r in page.evaluate("() => window.inkdeus.userStyles.list()")}
    assert rows["補正つき"]["data"]["levels"]["brightness"] == 0.2 and "levels" not in rows["補正なし"]["data"]
    page.evaluate("() => window.inkdeus.open('other.png')")
    dark = deep_merge(PLAIN, {"levels": {"brightness": -0.3, "gamma": 0.7}})
    show(page, dark)
    # 含めない: 層とベタだけが替わり、明るさ補正はその画像の物のまま
    assert page.evaluate("() => window.inkdeus.userStyles.apply('user', '補正なし', 'person')") is True
    without = pixels(page)
    assert regions(page)["person"]["levels"] == {"brightness": -0.3, "contrast": 1, "gamma": 0.7}
    assert (without == show(page, deep_merge(FAV, {"levels": {"brightness": -0.3, "gamma": 0.7}}))).all()
    # 含める: 明るさ補正も保存した値になり、元の画像と同じ設定で描いた絵と同じ
    show(page, dark)
    page.evaluate("() => window.inkdeus.userStyles.apply('user', '補正つき', 'person')")
    with_levels = pixels(page)
    assert regions(page)["person"]["levels"] == {"brightness": 0.2, "contrast": 1, "gamma": 1}
    assert (with_levels == show(page, FAV)).all() and (with_levels != without).any()
    assert page.errors == []


def test_scopes_precedence_and_manage_dialog(live):
    page = live["open"]()
    show(page, FAV)
    save_dialog(page, "共通の影", scope="user")
    save_dialog(page, "本の影", "このプロジェクトだけ", scope="project")
    assert mine(page) == [("自分用", "共通の影"), ("このプロジェクト", "本の影")]
    assert [s["name"] for s in json.loads(live["project"].read_text(encoding="utf-8"))["styles"]] == ["本の影"]
    listing = page.evaluate("() => window.inkdeus.userStyles.list().map((r) => [r.name, r.origin, r.shadowed])")
    assert sorted(listing) == [["共通の影", "user", False], ["本の影", "project", False]]
    # ダイアログ: 使えない名前は保存できない。同じ名前は「上書き保存」になる
    page.locator("#selPreset").select_option("@save")
    page.locator("#styleSaveDialog[open]").wait_for()
    for bad, needle in (("ベタ", "組み込み"), ("白抜き", "組み込み"), ("a b", "使えるのは")):
        page.locator("#styleName").fill(bad)
        assert page.locator("#styleSaveOK").is_disabled() and needle in page.locator("#styleSaveNote").text_content()
    page.locator("#styleName").fill("")
    assert page.locator("#styleSaveOK").is_disabled()
    page.locator("#styleName").fill("共通の影")
    assert page.locator("#styleSaveOK").text_content() == "上書き保存" and "上書き" in page.locator("#styleSaveNote").text_content()
    page.locator("#styleScopeProject").check()
    assert page.locator("#styleSaveOK").text_content() == "保存"
    # ダイアログの中のキー操作は、画面のショートカットにならない
    page.locator("#styleName").press("e")
    assert page.evaluate("() => window.inkdeus.paint.state().tool") == "hand"
    page.locator("#styleName").fill("共通の影")
    page.locator("#styleSaveOK").click()                                 # 同じ名前をプロジェクト専用にも保存する
    page.locator("#styleSaveDialog").wait_for(state="detached")
    # 同じ名前は、プロジェクト専用が優先（選択欄には、隠れた方は出ない）
    assert mine(page) == [("このプロジェクト", "本の影"), ("このプロジェクト", "共通の影")]
    # 管理: 一覧に出どころつきで並ぶ
    page.locator("#selPreset").select_option("@manage")
    page.locator("#styleManageDialog[open]").wait_for()
    rows = page.evaluate("""() => [...document.querySelectorAll('#styleManageDialog .style-row')].map((r) =>
        [r.dataset.name, r.dataset.origin, r.querySelector('.style-origin').textContent, r.textContent.includes('隠れています')])""")
    assert rows == [["本の影", "project", "このプロジェクト", False], ["共通の影", "project", "このプロジェクト", False],
                    ["共通の影", "user", "自分用", True]]
    answers: list = []
    page.on("dialog", lambda d: d.accept(answers.pop(0)) if d.type == "prompt" else d.accept())
    row = lambda name, origin: page.locator(f'#styleManageDialog .style-row[data-name="{name}"][data-origin="{origin}"]')  # noqa: E731
    # 名前変更
    answers.append("本の影2")
    row("本の影", "project").locator(".style-rename").click()
    row("本の影2", "project").wait_for()
    assert [s["name"] for s in json.loads(live["project"].read_text(encoding="utf-8"))["styles"]] == ["本の影2", "共通の影"]
    answers.append("ベタ")                                               # 組み込みの名前には変えられない
    row("本の影2", "project").locator(".style-rename").click()
    page.wait_for_function("() => document.getElementById('messages').textContent.includes('組み込み')")
    assert row("本の影2", "project").count() == 1
    # 削除（プロジェクト専用の「共通の影」を消すと、自分用の物が出てくる）
    row("共通の影", "project").locator(".style-delete").click()
    page.wait_for_function("() => document.querySelectorAll('#styleManageDialog .style-row').length === 2")
    assert row("共通の影", "user").text_content().count("隠れています") == 0
    # 上書き保存: いまのタブの領域の設定で、中身だけ入れ替える
    page.locator("#styleManageClose").click()
    show(page, PLAIN)
    page.locator("#selPreset").select_option("@manage")
    page.locator("#styleManageDialog[open]").wait_for()
    row("共通の影", "user").locator(".style-overwrite").click()
    page.wait_for_function("() => window.inkdeus.userStyles.list().some((r) => r.name === '共通の影' && r.data.layers.length === 1)")
    saved = json.loads(live["config"].read_text(encoding="utf-8"))["styles"][0]
    assert saved["name"] == "共通の影" and [l["angle"] for l in saved["data"]["layers"]] == [45]
    # 管理の画面から当てる
    show(page, FAV)
    row("共通の影", "user").locator(".style-apply").click()
    page.locator("#styleManageDialog").wait_for(state="detached")
    assert [l["angle"] for l in regions(page)["person"]["layers"]] == [45]
    assert mine(page) == [("自分用", "共通の影"), ("このプロジェクト", "本の影2")]
    assert page.errors == []


def test_background_styles_and_external_changes(live, capsys):
    page = live["open"]()
    show(page, deep_merge(FAV, {"background": {"linked": False, "mode": "white", "layers": []}}))
    page.evaluate("() => window.inkdeus.save()")
    page.locator('.tabs button[data-tab="background"]').click()
    save_dialog(page, "白い背景")
    row = page.evaluate("() => window.inkdeus.userStyles.list()[0]")
    assert row["target"] == "background" and row["data"]["mode"] == "white"
    assert mine(page) == [("自分用", "白い背景")]
    page.locator('.tabs button[data-tab="person"]').click()
    assert mine(page) == []                                              # 背景向けのスタイルは、人物・部分の選択欄には出ない
    page.locator('.tabs button[data-tab="hair"]').click()
    page.locator("#btnUnlinkPart").click()
    assert mine(page) == []
    # 外部（ctl）から保存・削除したスタイルは、開いている画面の選択欄にもすぐ出る・消える
    ctl(live, capsys, "style", "save", "ctlの影", "--region", "person")
    page.wait_for_function("() => window.inkdeus.userStyles.list().some((r) => r.name === 'ctlの影')")
    assert mine(page) == [("自分用", "ctlの影")]
    assert ctl(live, capsys, "preset", "ctlの影", "--region", "hair")["origin"] == "user"
    page.wait_for_function("() => window.inkdeus.getParams().regions.hair.layers.length === 2")
    assert no_ids(regions(page)["hair"]["layers"]) == no_ids(regions(page)["person"]["layers"])
    ctl(live, capsys, "style", "delete", "ctlの影")
    page.wait_for_function("() => !window.inkdeus.userStyles.list().some((r) => r.name === 'ctlの影')")
    assert mine(page) == []
    # 別の画像の背景へ当てると、描き方（白）も同じになる
    page.evaluate("() => window.inkdeus.open('other.png')")
    show(page, FAV)
    page.locator('.tabs button[data-tab="background"]').click()
    page.locator("#btnUnlink").click()
    page.locator("#selPreset").select_option("@user:白い背景")
    assert regions(page)["background"]["mode"] == "white"
    assert (settled(page) == show(page, deep_merge(FAV, {"background": {"linked": False, "mode": "white", "layers": []}}))).all()
    assert page.errors == []


def test_old_server_without_user_styles_shows_no_ui(live):
    def old_server(page):
        page.route("**/api/config", lambda route: route.fulfill(
            json={"project": None, "features": ["layerIds", "paint", "parts"]}))

    page = live["open"](prepare=old_server)
    assert page.evaluate("() => window.inkdeus.userStyles.ok()") is False
    groups = options(page)
    assert all(g == "" for g, _ in groups) and ("", "ベタ") in groups       # 組み込みだけ（保存・管理の項目も出ない）
    page.locator("#selPreset").select_option("hatch1")
    assert len(regions(page)["person"]["layers"]) == 1
    assert page.locator("#styleSaveDialog").count() == 0 and page.errors == []
