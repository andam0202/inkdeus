"""部分マスク・人物マスクの手直し（画面のツール）。実際のブラウザで塗って、出力画素を確かめる。画像はすべて合成"""

from __future__ import annotations

import json

import numpy as np
import pytest
from PIL import Image

import synth
from conftest import BLANK, deep_merge, layer, open_page, pixels, serve
from inkdeus import cli
from inkdeus.projects import create_project
from inkdeus.server import create_app
from test_paint_ui import drag

W, H = 256, 192
PERSON = (slice(20, 172), slice(40, 216))         # 人物: y 20〜172, x 40〜216
# 人物 = 白（何も描かない）/ 背景 = 真っ黒（全面ベタ）/ 髪 = 水平のハッチ（半分くらいの濃さ）/ 肌 = 人物と同じ（白）
PARAMS = deep_merge(BLANK, {"regions": {
    "background": {"linked": False, "mode": "keep", "grow": 0, "solid": {"enabled": True, "threshold": 1.0}, "layers": []},
    "hair": {"linked": False, "solid": {"enabled": False}, "layers": [layer(threshold=0.99, angle=0, spacing=4, width=2)]},
}})


def kind(img: np.ndarray, box) -> str:
    """その範囲が何で描かれているか: white（人物）/ black（背景）/ hatch（髪）"""
    mean = float(img[box].mean())
    return "white" if mean > 250 else "black" if mean < 5 else "hatch" if 60 < mean < 200 else f"mixed({mean:.0f})"


@pytest.fixture()
def live(tmp_path, browser, monkeypatch):
    monkeypatch.setenv("INKDEUS_PROJECTS", str(tmp_path / "projects"))
    monkeypatch.setattr("inkdeus.server.default_local_config", lambda: tmp_path / "none.toml")
    images, masks, parts = tmp_path / "images", tmp_path / "masks", tmp_path / "parts"
    synth.save(images / "a.png", synth.flat(W, H, 110))
    synth.save(images / "b.png", synth.flat(W, H, 110))
    person = np.zeros((H, W), np.uint8)
    person[PERSON] = 255
    hair = np.zeros((H, W), np.uint8)
    hair[20:80, 40:216] = 255                                    # 髪: 人物の上の帯
    skin = np.zeros((H, W), np.uint8)
    skin[80:140, 40:216] = 255                                   # 肌: その下の帯（y 140〜172 は、どの部分でもない人物）
    for name in ("a", "b"):
        synth.save(masks / f"{name}.png", person)
        synth.save(parts / "hair" / f"{name}.png", hair)
        synth.save(parts / "skin" / f"{name}.png", skin)
    project, _ = create_project("book", images, masks, root=tmp_path / "projects", parts=parts)
    app = create_app(project.source, project.masks, project.out, project=project.info(), parts_dir=project.parts,
                     local_config=tmp_path / "none.toml", config_dir=tmp_path / "config")
    server, url = serve(app)
    originals = {p: p.read_bytes() for p in [*masks.glob("*.png"), *parts.glob("*/*.png")]}
    pages = []

    def open_(query: str = "?image=a.png", **kw):
        page = open_page(browser, url + query, **kw)
        pages.append(page)
        return page

    yield {"open": open_, "url": url, "out": project.out, "originals": originals, "client": app.test_client(),
           "tmp": tmp_path}
    for page in pages:
        if not page.is_closed():
            page.close()
    server.shutdown()


def show(page, params=PARAMS) -> np.ndarray:
    page.evaluate("(p) => { window.inkdeus.setParams(p); }", params)
    return pixels(page)


def stroke(page, target: str, mode: str, a, b, radius: float = 10) -> np.ndarray:
    assert page.evaluate("([t, m, pts]) => window.inkdeus.maskedit.stroke(t, m, pts)",
                         [target, mode, [[*a, radius], [*b, radius]]]) is True
    return pixels(page)


def saved(page) -> None:
    page.evaluate("() => window.inkdeus.maskedit.flush()")
    page.wait_for_function("() => window.inkdeus.saveStatus().state === 'saved'")


HAIR, SKIN, REST, OUT = (slice(30, 70), slice(60, 200)), (slice(90, 130), slice(60, 200)), \
    (slice(148, 166), slice(60, 200)), (slice(60, 130), slice(4, 30))


def test_painting_parts_and_the_person(live):
    page = live["open"]()
    assert page.evaluate("() => window.inkdeus.maskedit.ok()") is True
    base = show(page)
    page.evaluate("() => window.inkdeus.save()")
    assert [kind(base, b) for b in (HAIR, SKIN, REST, OUT)] == ["hatch", "white", "white", "black"]
    # 髪に塗る: その範囲が髪の設定で描かれ、元いた部分（肌）からは外れる。人物の外には塗れない
    img = stroke(page, "hair", "add", (10, 110), (250, 110))
    band = (slice(104, 116), slice(60, 200))
    assert kind(img, band) == "hatch" and kind(img, (slice(84, 96), slice(60, 200))) == "white"
    assert kind(img, (slice(104, 116), slice(4, 30))) == "black" and kind(img, (slice(104, 116), slice(226, 252))) == "black"
    assert page.evaluate("([x, y]) => window.inkdeus.regionAt(x, y)", [120, 110]) == "hair"
    assert page.evaluate("([x, y]) => window.inkdeus.regionAt(x, y)", [120, 90]) == "skin"
    assert page.evaluate("([x, y]) => window.inkdeus.regionAt(x, y)", [20, 110]) == "background"
    # 髪から消す: どの部分でもない人物の領域になる（人物の設定で描かれる）
    img = stroke(page, "hair", "remove", (60, 50), (200, 50))
    assert kind(img, (slice(44, 56), slice(70, 190))) == "white" and kind(img, (slice(24, 36), slice(70, 190))) == "hatch"
    assert page.evaluate("([x, y]) => window.inkdeus.regionAt(x, y)", [120, 50]) == "person"
    # 人物から外す: 背景の設定で描かれ、そこにあった部分も外れる
    img = stroke(page, "person", "remove", (60, 30), (200, 30), 6)
    assert kind(img, (slice(27, 33), slice(70, 190))) == "black"
    assert page.evaluate("([x, y]) => window.inkdeus.regionAt(x, y)", [120, 30]) == "background"
    img = stroke(page, "person", "remove", (60, 158), (200, 158), 6)
    assert kind(img, (slice(155, 161), slice(70, 190))) == "black"
    # 人物に加える: 人物の外だった所が、人物になる
    img = stroke(page, "person", "add", (10, 140), (30, 140), 6)
    assert kind(img, (slice(137, 143), slice(12, 28))) == "white"
    # 人物の外に塗ってあった髪（さっきの筆のはみ出し）は、そこが人物になれば髪として出る
    img = stroke(page, "person", "add", (10, 110), (30, 110), 4)
    assert kind(img, (slice(108, 112), slice(12, 28))) == "hatch"
    saved(page)
    final = pixels(page)
    state = page.evaluate("() => window.inkdeus.maskedit.state()")
    assert state["targets"] == ["hair", "person"] and state["onServer"] == ["hair", "person"] and state["tileSaves"] >= 4
    assert page.evaluate("() => window.inkdeus.stats().maskEdits") == ["hair", "person"]
    # 元のマスクのファイルは変わっていない。手直しは 3 値の PNG として、画像ごとに保存される
    assert all(p.read_bytes() == data for p, data in live["originals"].items())
    edit = np.asarray(Image.open(live["out"] / "a.maskedit" / "hair.png"))
    assert edit.shape == (H, W) and set(np.unique(edit)) == {0, 128, 255}
    assert (edit[104:116, 20:240] == 255).all() and (edit[44:56, 70:190] == 0).all()
    # 再読み込みしても、別のバージョンでも、同じ結果
    again = live["open"]()
    assert (pixels(again) == final).all() and again.evaluate("() => window.inkdeus.maskedit.state().targets") == ["hair", "person"]
    again.evaluate("() => window.inkdeus.createVersion('v2')")
    assert (pixels(again) == final).all()
    assert again.evaluate("() => window.inkdeus.gallery()") is not None
    badges = again.evaluate("""() => Array.from(document.querySelectorAll('#gallery .gal-badge[data-kind="maskedit"]'))
        .map(b => b.textContent)""")
    assert badges == ["マスク直"]                                # 画像一覧の印（手直しのある a.png だけ）
    # 書き出しに反映される（PNG を書き出す）
    out = again.evaluate("() => window.inkdeus.exportPNG()")
    assert (np.asarray(Image.open(out["path"])) == final).all()
    # サーバの合成（inkdeus maskedit export と同じ決まり）とも一致する
    for target, region in (("hair", "hair"), ("skin", "skin")):
        got = np.asarray(Image.open(__import__("io").BytesIO(
            live["client"].get(f"/api/maskedit-composed/a.png/{target}").data)))
        ys, xs = np.nonzero(got)
        assert all(again.evaluate("([x, y]) => window.inkdeus.regionAt(x, y)", [int(x), int(y)]) == region
                   for x, y in list(zip(xs, ys))[::997])


def test_undo_redo_clear_and_other_versions_go_stale(live):
    page = live["open"]()
    base = show(page)
    page.evaluate("() => window.inkdeus.save()")
    page.evaluate("() => window.inkdeus.createVersion('v2')")
    page.evaluate("() => window.inkdeus.openVersion('')")
    for version in ("", "v2"):
        live["client"].post(f"/api/export/a.png?version={version}", data=_png(base))
    page.evaluate("() => window.inkdeus.gallery()")
    one = stroke(page, "hair", "add", (60, 110), (200, 110))
    two = stroke(page, "skin", "add", (60, 40), (200, 40))       # 髪だった所を、肌にする（白くなる）
    assert kind(two, (slice(34, 46), slice(70, 190))) == "white" and (one != base).any() and (two != one).any()
    # 手直しは全バージョンの絵に効くので、開いていないバージョンの白黒版も「古い」になる
    versions = page.evaluate("() => window.inkdeus.versions()")
    assert [v["exportFresh"] for v in versions] == [False, False]
    # 取り消し・やり直し（1 筆 = 1 履歴。消しゴム・ペン・設定の変更と同じ並び）
    page.keyboard.press("Control+z")
    page.evaluate("() => window.inkdeus.maskedit.settle()")
    assert (pixels(page) == one).all()
    page.keyboard.press("Control+z")
    page.evaluate("() => window.inkdeus.maskedit.settle()")
    assert (pixels(page) == base).all()
    page.keyboard.press("Control+y")
    page.keyboard.press("Control+y")
    page.evaluate("() => window.inkdeus.maskedit.settle()")
    assert (pixels(page) == two).all()
    saved(page)
    assert sorted(p.name for p in (live["out"] / "a.maskedit").glob("*.png")) == ["hair.png", "skin.png"]
    # 髪に塗った所を、あとから肌に塗ると、肌が勝つ（塗った部分が必ず勝つ）
    three = stroke(page, "skin", "add", (60, 110), (200, 110))
    assert kind(three, (slice(104, 116), slice(70, 190))) == "white"
    # この対象の手直しを取り消す（取り消し Ctrl+Z で戻せる）
    assert page.evaluate("() => window.inkdeus.maskedit.clear('skin')") is True
    cleared = pixels(page)
    assert kind(cleared, (slice(34, 46), slice(70, 190))) == "hatch"            # 肌にしていた所が、髪へ戻る
    assert (cleared == base).all()                                # 髪に塗った分は、肌に塗り直したときに外れている
    page.keyboard.press("Control+z")
    page.evaluate("() => window.inkdeus.maskedit.settle()")
    assert (pixels(page) == three).all()
    # この画像の手直しを全部取り消す → サーバのファイルも片付く
    assert page.evaluate("() => window.inkdeus.maskedit.clear()") is True
    assert (pixels(page) == base).all() and page.evaluate("() => window.inkdeus.maskedit.state().targets") == []
    saved(page)
    assert not (live["out"] / "a.maskedit").exists()
    assert all(p.read_bytes() == data for p, data in live["originals"].items())


def _png(arr: np.ndarray) -> bytes:
    import io

    buf = io.BytesIO()
    Image.fromarray(arr, "L").save(buf, "PNG")
    return buf.getvalue()


def test_the_tool_in_the_toolbar_and_batch_export(live, capsys):
    page = live["open"]()
    base = show(page)
    page.evaluate("() => window.inkdeus.save()")
    # ツールバーの「部分マスク」（キー M）。対象の既定は、開いているタブの部分
    page.evaluate("() => window.inkdeus.setTab('hair')")
    assert page.locator("#toolMask").is_visible()
    page.keyboard.press("m")
    st = page.evaluate("() => window.inkdeus.paint.state()")
    assert st["tool"] == "mask" and st["maskTarget"] == "hair" and st["maskMode"] == "add"
    assert page.evaluate("() => Array.from(document.querySelectorAll('#maskTarget option')).map(o => [o.value, o.textContent])") == [
        ["hair", "髪"], ["skin", "肌"], ["person", "人物"]]
    assert page.evaluate("() => window.inkdeus.maskedit.state().view") == {"target": "hair", "lines": False}
    assert page.evaluate("() => window.inkdeus.state.highlight") == "hair"      # 対象の領域を色つきで重ねる
    page.keyboard.press("x")
    assert page.evaluate("() => window.inkdeus.paint.state().maskMode") == "remove"
    page.keyboard.press("x")
    page.evaluate("() => window.inkdeus.paint.set('mask', {size: 20, hardness: 1})")
    drag(page, [(60, 110), (200, 110)])                           # マウスでなぞる
    page.evaluate("() => window.inkdeus.maskedit.settle()")
    img = pixels(page)
    assert kind(img, (slice(104, 116), slice(70, 190))) == "hatch" and page.locator("#strokeTrail").is_hidden()
    assert page.locator("#maskMark").is_visible()
    # 線画だけ表示（見やすくするための表示。書き出しには影響しない）
    page.click("#maskLines")
    assert page.evaluate("() => window.inkdeus.maskedit.state().view.lines") is True
    out = page.evaluate("() => window.inkdeus.exportPNG()")
    assert (np.asarray(Image.open(out["path"])) == img).all()
    # 人物を選ぶと、ボタンの文言が変わる
    page.select_option("#maskTarget", "person")
    assert page.locator("#maskModeAdd").inner_text() == "人物に加える" and page.locator("#maskModeRemove").inner_text() == "人物から外す"
    page.click("#maskClearAll")
    page.evaluate("() => window.inkdeus.maskedit.settle()")
    page.keyboard.press("h")
    assert page.evaluate("() => window.inkdeus.maskedit.state().view") is None and page.evaluate("() => window.inkdeus.state.highlight") is None
    wait_pixels(page, base)                                      # （描き直しが済むのを待つ。負荷のかかった実行では、少し遅れる）
    page.keyboard.press("Control+z")                             # 全部取り消したのを、取り消す
    page.evaluate("() => window.inkdeus.maskedit.settle()")
    wait_pixels(page, img)
    saved(page)
    # 一括書き出しにも反映される（別の画像を開いた状態から）
    page.evaluate("() => window.inkdeus.open('b.png')")
    report = page.evaluate("() => window.inkdeus.exportAll({force: true})")
    assert report["failed"] == 0 and report["exported"] >= 2
    assert (np.asarray(Image.open(live["out"] / "a.png")) == img).all()
    assert (np.asarray(Image.open(live["out"] / "b.png")) == base).all()         # 手直しの無い画像はそのまま
    # 手直し済みのマスクの書き出し（外のツールへ戻す用）: 排他・人物の内側だけ
    dest = live["tmp"] / "fixed"
    assert cli.main(["maskedit", "export", "book", "-o", str(dest)]) == 0
    capsys.readouterr()
    person, hair, skin = (np.asarray(Image.open(dest / t / "a.png")) for t in ("person", "hair", "skin"))
    assert hair.shape == (H, W) and (hair[104:116, 70:190] == 255).all() and (skin[104:116, 70:190] == 0).all()
    assert not ((hair > 0) & (skin > 0)).any() and ((hair > 0) <= (person > 0)).all()
    assert (np.asarray(Image.open(dest / "hair" / "b.png")) == np.asarray(Image.open(dest / "hair" / "a.png"))).sum() > 0
    assert all(p.read_bytes() == data for p, data in live["originals"].items())


def test_the_tool_is_hidden_without_server_support(live):
    old = ["layerIds", "paint", "parts", "userStyles", "mangaPdf", "mangaCommand"]
    page = live["open"](prepare=lambda p: p.route(
        "**/api/config", lambda route: route.fulfill(json={"project": None, "features": old})))
    show(page)
    assert page.evaluate("() => window.inkdeus.maskedit.ok()") is False
    assert page.locator("#toolMask").is_hidden() and page.locator("#toolEraser").is_visible()
    page.keyboard.press("m")
    assert page.evaluate("() => window.inkdeus.paint.state().tool") == "hand"
    assert page.evaluate("([t, m, pts]) => window.inkdeus.maskedit.stroke(t, m, pts)",
                         ["hair", "add", [[60, 110, 10], [200, 110, 10]]]) is False
    assert not (live["out"] / "a.maskedit").exists()


def wait_pixels(page, want: np.ndarray, seconds: float = 20) -> None:
    import time

    deadline = time.time() + seconds
    while time.time() < deadline:
        if (pixels(page) == want).all():
            return
        page.wait_for_timeout(200)
    assert (pixels(page) == want).all()


def test_changes_from_outside_and_restoring_reach_the_open_screen(live):
    page = live["open"]()
    base = show(page)
    page.evaluate("() => window.inkdeus.save()")
    img = stroke(page, "hair", "add", (60, 110), (200, 110))
    saved(page)
    assert (img != base).any()
    # 別の所（別のタブ・コマンド）で手直しが消されたら、開いている画面も元のマスクへ戻る
    assert live["client"].delete("/api/maskedit/a.png/all-targets").get_json()["deleted"] == ["hair"]
    page.wait_for_function("() => window.inkdeus.maskedit.state().targets.length === 0")
    wait_pixels(page, base)
    assert "マスクの手直しが外部から変更されました" in page.locator("#messages").inner_text()
    # 「以前の状態に戻す」: 手直しも、設定・消しゴムとペンと一緒に戻る（消される直前の状態へ）
    points = page.evaluate("() => window.inkdeus.backups.list()")
    point = next(p for p in points if "マスクの手直し（hair）" in p["what"])
    restored = page.evaluate("(at) => window.inkdeus.backups.restore(at)", point["at"])
    assert "マスクの手直し（hair）" in restored["restored"]
    page.wait_for_function("() => window.inkdeus.maskedit.state().targets.join() === 'hair'")
    wait_pixels(page, img)
    assert all(p.read_bytes() == data for p, data in live["originals"].items())
