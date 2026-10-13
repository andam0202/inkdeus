"""設定の一括適用（白黒化の画面）: 画像一覧の複数選択、ダイアログ、適用・取り消し、適用後の書き出し"""

from __future__ import annotations

import io
import json
import os
import time
import urllib.request

import numpy as np
import pytest
from PIL import Image

import synth
from conftest import BLANK, deep_merge, layer, open_page, serve
from inkdeus.server import create_app

NAMES = ["a.png", "b.png", "c.png", "d.png", "e.png", "f.png"]
TUNED = deep_merge(BLANK, {"lines": {"enabled": True, "strength": 0.4},
                           "layers": [layer(threshold=0.9, spacing=6.0, width=2.0, angle=10)],
                           "output": {"binary": True}})


def png_bytes(value: int = 128) -> bytes:
    buf = io.BytesIO()
    Image.new("L", (8, 6), value).save(buf, "PNG")
    return buf.getvalue()


def request(url: str, path: str, data: bytes | None = None, method: str | None = None):
    req = urllib.request.Request(url + path, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def saved(live, name: str, version: str = "") -> dict:
    query = f"?resolve=1&version={urllib.request.quote(version)}" if version else "?resolve=1"
    return request(live["url"], f"api/params/{name}{query}")["params"]


@pytest.fixture()
def live(tmp_path, browser):
    """6 枚の合成画像。a と b に髪の部分マスク。b は設定あり・書き出し済み（最新）、c は設定あり"""
    images, masks, parts, out = tmp_path / "images", tmp_path / "masks", tmp_path / "parts", tmp_path / "out"
    for i, name in enumerate(NAMES):
        img, mask = synth.scene(160, 120)
        synth.save(images / name, np.clip(img.astype(np.int16) + i * 4, 0, 255).astype(np.uint8))
        synth.save(masks / name, mask)
    hair = synth.part_masks(160, 120)["hair"]
    synth.save_parts(parts, "a", {"hair": hair})
    synth.save_parts(parts, "b", {"hair": hair})
    server, url = serve(create_app(images, mask_dir=masks, out_dir=out, parts_dir=parts,
                                   local_config=tmp_path / "none.toml"))
    for name in ("b.png", "c.png"):
        request(url, f"api/params/{name}", json.dumps(BLANK).encode("utf-8"), "PUT")
    request(url, "api/export/b.png", png_bytes())
    pages = []

    def open_(query: str = "", **kw):
        page = open_page(browser, url + query, **kw)
        pages.append(page)
        return page

    yield {"open": open_, "url": url, "out": out}
    for page in pages:
        page.close()
    server.shutdown()


def selection(page) -> list[str]:
    return page.evaluate("() => window.inkdeus.apply.selection()")


def marked(page) -> list[str]:
    return page.evaluate("() => Array.from(document.querySelectorAll('.gal-item.selected')).map(c => c.dataset.name)")


def cell(name: str) -> str:
    return f".gal-item[data-name='{name}']"


def badges(page, name: str) -> list[str]:
    return page.evaluate("(n) => Array.from(document.querySelectorAll(`.gal-item[data-name=\"${n}\"] .gal-badge`))"
                         ".map(b => b.dataset.kind)", name)


def wait_badges(page, name: str, want: list[str]) -> None:
    page.wait_for_function(
        """([n, want]) => Array.from(document.querySelectorAll(`.gal-item[data-name="${n}"] .gal-badge`))
            .map(b => b.dataset.kind).join() === want""", arg=[name, ",".join(want)], timeout=20000)


def tune(page) -> dict:
    """いま開いている画像（適用元）を、既定と違う設定にして保存する"""
    page.evaluate("(p) => { window.inkdeus.setParams(p); }", TUNED)
    page.evaluate("() => window.inkdeus.save()")
    return page.evaluate("() => window.inkdeus.getParams()")


def open_dialog(page) -> None:
    page.click(".gal-apply")
    page.wait_for_selector("#applyDialog[open]")


def run_apply(page) -> dict:
    page.click("#applyDialog .apply-run")
    page.wait_for_selector("#applyDialog .apply-result:not([hidden])")
    return page.evaluate("() => ({...document.querySelector('#applyDialog .apply-result').dataset})")


# ---- 複数選択 ----

def test_multi_select_with_ctrl_shift_and_buttons(live):
    page = live["open"]()
    assert page.evaluate("() => window.inkdeus.apply.ok()") is True
    assert page.evaluate("() => document.querySelector('.gal-select').hidden") is False
    assert selection(page) == [] and page.evaluate("() => document.querySelector('.gal-apply').hidden") is True
    # Ctrl+クリックで個別に選択（開いている画像は変わらない）。もう一度で外れる
    page.click(cell("c.png"), modifiers=["Control"])
    page.click(cell("e.png"), modifiers=["Control"])
    assert selection(page) == ["c.png", "e.png"] == marked(page)
    assert page.evaluate("() => window.inkdeus.info().name") == "a.png"
    assert page.evaluate("() => document.querySelector('.gal-sel-count').textContent") == "選択 2 枚"
    assert page.evaluate("() => document.querySelector('.gal-apply').textContent") == "いまの画像の設定を、選択した 2 枚へ適用…"
    page.click(cell("c.png"), modifiers=["Control"])
    assert selection(page) == ["e.png"]
    # Shift+クリックで範囲（最後に Ctrl+クリックした画像 = e から）
    page.click(cell("e.png"), modifiers=["Control"])
    page.click(cell("e.png"), modifiers=["Control"])
    page.click(cell("b.png"), modifiers=["Shift"])
    assert selection(page) == ["b.png", "c.png", "d.png", "e.png"]
    assert page.evaluate("() => window.inkdeus.info().name") == "a.png"
    # ふつうのクリックは、従来どおり開く（選択はそのまま）
    page.click(cell("f.png"))
    page.wait_for_function("() => window.inkdeus.info().name === 'f.png'")
    assert selection(page) == ["b.png", "c.png", "d.png", "e.png"]
    # 選択を解除 / すべて選択（いまの絞り込みの結果）
    page.click(".gal-sel-none")
    assert selection(page) == [] and marked(page) == []
    page.click(".gal-chip[data-filter='noParams']")
    page.click(".gal-sel-all")
    assert selection(page) == ["a.png", "d.png", "e.png", "f.png"]
    page.click(".gal-chip[data-filter='all']")
    assert marked(page) == ["a.png", "d.png", "e.png", "f.png"]            # 絞り込みを変えても、選択は残る
    page.fill(".gal-search", "c")
    page.wait_for_function("() => document.querySelector('.gal-count').textContent === '6 枚中 1 枚'")
    page.click(".gal-sel-all")
    assert selection(page) == ["a.png", "c.png", "d.png", "e.png", "f.png"]
    assert page.evaluate("() => window.inkdeus.gallery().selected") == selection(page)
    # 起点が無いときの Shift+クリックは、いま開いている画像から
    page.fill(".gal-search", "")
    page.wait_for_function("() => document.querySelector('.gal-count').textContent === '6 枚中 6 枚'")
    page.click(".gal-sel-none")
    page.click(cell("d.png"), modifiers=["Shift"])
    assert selection(page) == ["d.png", "e.png", "f.png"]


def test_selection_is_hidden_on_a_server_without_the_feature(live):
    """新しい画面のファイルを、設定の一括適用を知らない（起動し直していない）サーバが配っているとき"""
    def old_server(page):
        def handle(route):
            body = route.fetch().json()
            body["features"] = [f for f in body["features"] if f != "applyParams"]
            route.fulfill(json=body)
        page.route("**/api/config", handle)

    page = live["open"](prepare=old_server)
    assert page.evaluate("() => window.inkdeus.apply.ok()") is False
    assert page.evaluate("() => document.querySelector('.gal-select').hidden") is True
    page.click(cell("c.png"), modifiers=["Control"])                         # 複数選択は働かず、従来どおり開く
    page.wait_for_function("() => window.inkdeus.info().name === 'c.png'")
    assert selection(page) == [] and page.errors == []


# ---- ダイアログ・適用・取り消し ----

def test_dialog_applies_to_selected_images_and_can_be_undone(live):
    page = live["open"]()
    source = tune(page)
    page.evaluate("() => window.inkdeus.apply.select(['b.png', 'c.png', 'd.png'])")
    wait_badges(page, "b.png", ["params", "fresh", "parts"])
    assert badges(page, "d.png") == []
    open_dialog(page)
    text = page.evaluate("() => document.getElementById('applyDialog').textContent")
    assert "「a.png」の設定を、選択した 3 枚へ適用します" in text and "消しゴム・ペン・部分マスクの手直しは対象外" in text
    boxes = page.evaluate("""() => Object.fromEntries(Array.from(document.querySelectorAll('#applyDialog input[data-scope]'))
        .map(i => [i.dataset.scope, i.checked]))""")
    assert boxes == {"input": True, "lines": True, "person": True, "background": True, "parts:hair": True,
                     "watermark": True, "output": True, "unit": False}
    versions = page.evaluate("""() => Array.from(document.querySelectorAll('#applyDialog input[name="apply-version"]'))
        .map(i => [i.value, i.checked])""")
    assert versions == [["default", True], ["new", False]]                  # 既定のバージョンを開いているので「同じ名前」は出ない
    assert page.evaluate("() => document.querySelector('#applyDialog input[name=\"apply-force\"]').checked") is False
    assert page.evaluate("() => document.querySelector('#applyDialog input[name=\"apply-export\"]').checked") is False
    # 線画を外して適用する
    page.uncheck("#applyDialog input[data-scope='lines']")
    result = run_apply(page)
    assert (result["applied"], result["skipped"], result["failed"]) == ("3", "0", "0")
    listed = page.evaluate("() => Array.from(document.querySelectorAll('#applyDialog .apply-group li')).map(li => li.dataset.name)")
    assert listed == ["b.png", "c.png", "d.png"]
    assert "マスクの無い部分は飛ばした: hair" in page.evaluate("() => document.querySelector('#applyDialog li[data-name=\"c.png\"]').textContent")
    for name in ("b.png", "c.png", "d.png"):
        got = saved(live, name)
        assert got["regions"]["person"] == source["regions"]["person"] and got["output"]["binary"] is True
        assert got["lines"]["strength"] == 1.0                               # 外した範囲は、その画像のまま
        assert got["lines"]["enabled"] is (name == "d.png")                  # （設定の無かった d は、既定値のまま）
    # 印が替わる: 書き出し済みだった b は「書き出しが古い」に、設定の無かった d は「設定あり」に
    wait_badges(page, "b.png", ["params", "stale", "parts"])
    wait_badges(page, "d.png", ["params"])
    # 適用した画像を開くと、設定が替わっている
    page.click("#applyDialog .chooser-cancel")
    page.wait_for_selector("#applyDialog", state="detached")
    page.click(cell("d.png"))
    page.wait_for_function("() => window.inkdeus.info().name === 'd.png'")
    opened = page.evaluate("() => window.inkdeus.getParams()")
    assert opened["regions"]["person"]["layers"][0]["angle"] == 10 and opened["output"]["binary"] is True
    # 取り消し（1 回の適用を、まとめて戻す）。開いている画面にも反映される
    page.click(cell("a.png"))
    page.wait_for_function("() => window.inkdeus.info().name === 'a.png'")
    patch = json.dumps({"output": {"binary": False}}).encode("utf-8")
    open_dialog(page)
    page.uncheck("#applyDialog input[data-scope='lines']")
    assert run_apply(page)["skipped"] == "3"                                 # もう同じ設定なので、何も変わらない
    assert page.evaluate("() => document.querySelector('#applyDialog .apply-undo').hidden") is True
    page.click("#applyDialog .chooser-cancel")
    request(live["url"], "api/params/a.png", patch, "PATCH")                # 適用元を変えて、もう一度
    page.wait_for_function("() => window.inkdeus.getParams().output.binary === false")
    open_dialog(page)
    assert run_apply(page)["applied"] == "3"
    assert saved(live, "d.png")["output"]["binary"] is False
    page.click("#applyDialog .apply-undo")
    page.wait_for_function("() => document.querySelector('#applyDialog .apply-result').dataset.undone === '1'")
    assert "取り消しました: 3 枚" in page.evaluate("() => document.querySelector('#applyDialog .apply-summary').textContent")
    assert saved(live, "d.png")["output"]["binary"] is True and saved(live, "d.png")["lines"]["strength"] == 1.0
    # 最初の適用も取り消すと、設定の無かった画像は「設定なし」に戻る
    page.click("#applyDialog .chooser-cancel")
    undone = page.evaluate("() => window.inkdeus.apply.undo()")
    assert [x["name"] for x in undone["undone"]] == ["b.png", "c.png", "d.png"]
    wait_badges(page, "d.png", [])
    assert not (live["out"] / "d.inkdeus.json").exists()
    assert saved(live, "b.png")["output"]["binary"] is False and page.errors == []


def test_scope_must_not_be_empty_and_errors_are_shown(live):
    page = live["open"]()
    tune(page)
    page.evaluate("() => window.inkdeus.apply.select(['b.png'])")
    open_dialog(page)
    for box in page.query_selector_all("#applyDialog input[data-scope]"):
        box.uncheck()
    page.click("#applyDialog .apply-run")
    page.wait_for_selector("#applyDialog .apply-error:not([hidden])")
    assert "範囲を 1 つ以上" in page.evaluate("() => document.querySelector('#applyDialog .apply-error').textContent")
    # 新しいバージョンの名前が使えない
    page.check("#applyDialog input[data-scope='lines']")
    page.fill("#applyDialog .apply-new-name", "だ/め")
    assert page.evaluate("() => document.querySelector('#applyDialog input[value=\"new\"]').checked") is True
    page.click("#applyDialog .apply-run")
    page.wait_for_function("() => document.querySelector('#applyDialog .apply-error').textContent.includes('バージョン名')")
    assert saved(live, "b.png")["lines"]["enabled"] is False                 # 何も変わっていない
    page.keyboard.press("Escape")
    page.wait_for_selector("#applyDialog", state="detached")


def test_version_choices_same_name_and_new_name(live):
    page = live["open"]()
    tune(page)
    page.evaluate("() => window.inkdeus.createVersion('白背景')")
    page.wait_for_function("() => window.inkdeus.version() === '白背景'")
    page.evaluate("(p) => { window.inkdeus.setParams(p); }", deep_merge(TUNED, {"lines": {"strength": 0.11}}))
    page.evaluate("() => window.inkdeus.save()")
    page.evaluate("() => window.inkdeus.apply.select(['b.png', 'c.png'])")
    open_dialog(page)
    assert "「a.png @白背景」の設定" in page.evaluate("() => document.getElementById('applyDialog').textContent")
    values = page.evaluate("""() => Array.from(document.querySelectorAll('#applyDialog input[name="apply-version"]')).map(i => i.value)""")
    assert values == ["default", "same", "new"]
    page.check("#applyDialog input[value='same']")
    assert run_apply(page)["applied"] == "2"
    assert "バージョンを作成" in page.evaluate("() => document.querySelector('#applyDialog li[data-name=\"b.png\"]').textContent")
    assert saved(live, "b.png", "白背景")["lines"]["strength"] == 0.11 and saved(live, "b.png")["lines"]["enabled"] is False
    page.wait_for_function("""() => document.querySelector('.gal-item[data-name="b.png"] .gal-badge[data-kind="versions"]') !== null""")
    page.click("#applyDialog .chooser-cancel")
    # 新しい名前のバージョンへ
    open_dialog(page)
    page.fill("#applyDialog .apply-new-name", "試し")
    assert run_apply(page)["applied"] == "2"
    assert (live["out"] / "c@試し.inkdeus.json").is_file() and saved(live, "c.png", "試し")["lines"]["strength"] == 0.11
    page.click("#applyDialog .apply-undo")
    page.wait_for_function("() => document.querySelector('#applyDialog .apply-result').dataset.undone === '1'")
    assert not (live["out"] / "c@試し.inkdeus.json").exists() and (live["out"] / "c@白背景.inkdeus.json").is_file()


def test_layer_erasers_skip_then_force(live):
    page = live["open"]()
    tune(page)
    layer_id = saved(live, "c.png")["regions"]["person"]["layers"]
    assert layer_id == []                                                    # c は「何も描かない」設定。層を足して消しゴムを置く
    made = request(live["url"], "api/params/c.png", json.dumps(
        {"regions": {"person": {"layers": [layer(angle=33)]}}}).encode("utf-8"), "PATCH")["params"]
    target = "layer-" + made["regions"]["person"]["layers"][0]["id"]
    req = urllib.request.Request(live["url"] + f"api/paint/c.png/{target}", data=png_bytes(), method="PUT")
    urllib.request.urlopen(req, timeout=10).read()
    page.evaluate("() => window.inkdeus.apply.select(['c.png', 'd.png'])")
    open_dialog(page)
    result = run_apply(page)
    assert (result["applied"], result["skipped"]) == ("1", "1")
    assert "層の消しゴム" in page.evaluate("() => document.querySelector('#applyDialog li[data-name=\"c.png\"]').textContent")
    assert (live["out"] / "c.paint" / f"{target}.png").is_file()
    page.click("#applyDialog .chooser-cancel")
    open_dialog(page)
    page.check("#applyDialog input[name='apply-force']")
    result = run_apply(page)
    assert result["applied"] == "1"
    assert "層の消しゴムを捨てた: 1 個" in page.evaluate("() => document.querySelector('#applyDialog li[data-name=\"c.png\"]').textContent")
    assert not (live["out"] / "c.paint" / f"{target}.png").exists()
    page.click("#applyDialog .apply-undo")                                   # 取り消すと、消しゴムも戻る
    page.wait_for_function("() => document.querySelector('#applyDialog .apply-result').dataset.undone === '1'")
    assert (live["out"] / "c.paint" / f"{target}.png").read_bytes() == png_bytes()


# ---- 適用後に書き出す ----

def test_apply_then_export_only_the_applied_images(live):
    page = live["open"]()
    tune(page)
    out = live["out"]
    past = time.time() - 60
    os.utime(out / "b.png", (past, past))
    page.evaluate("() => window.inkdeus.apply.select(['b.png', 'd.png'])")
    open_dialog(page)
    page.check("#applyDialog input[name='apply-export']")
    page.click("#applyDialog .apply-run")
    page.wait_for_selector("#applyDialog", state="detached")                # ダイアログを閉じて、書き出しが始まる
    page.wait_for_function("() => window.inkdeus.batch().running === false && window.inkdeus.batch().done === 2", timeout=120000)
    batch = page.evaluate("() => window.inkdeus.batch()")
    assert (batch["total"], batch["exported"], batch["skipped"], batch["failed"]) == (2, 2, 0, 0)
    assert (out / "d.png").is_file() and (out / "b.png").stat().st_mtime > past + 30
    assert not (out / "c.png").exists() and not (out / "a.png").exists()    # 適用していない画像は書き出さない
    wait_badges(page, "d.png", ["params", "fresh"])
    wait_badges(page, "b.png", ["params", "fresh", "parts"])
    assert page.evaluate("() => window.inkdeus.info().name") == "a.png" and page.errors == []
