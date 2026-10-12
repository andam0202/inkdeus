"""白黒化の画面の画像一覧パネル（左端）: 一覧・切り替え・絞り込み・状態の印・幅・前後の移動"""

from __future__ import annotations

import functools
import http.server
import io
import json
import os
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import synth
from conftest import BLANK, deep_merge, layer, open_page, pixels, serve
from inkdeus import cli
from inkdeus.server import create_app

NAMES = ["apple.png", "banana.png", "cherry.png", "date.png", "elder.png", "fig.png", "grape.png"]
NO_MASK = {"cherry.png", "elder.png"}
HATCH = deep_merge(BLANK, {"layers": [layer(threshold=0.9, spacing=6.0, width=2.0)]})


def png_bytes(value: int = 128) -> bytes:
    buf = io.BytesIO()
    Image.new("L", (8, 6), value).save(buf, "PNG")
    return buf.getvalue()


def request(url: str, path: str, data: bytes | None = None, method: str | None = None):
    req = urllib.request.Request(url + path, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


@pytest.fixture()
def live(tmp_path, browser):
    """7枚の合成画像。設定あり・マスクなし・書き出し済み・書き出しが古い、が混ざっている"""
    images, masks, out = tmp_path / "images", tmp_path / "masks", tmp_path / "out"
    for i, name in enumerate(NAMES):
        img, mask = synth.scene(160, 120)
        synth.save(images / name, np.clip(img.astype(np.int16) + i * 4, 0, 255).astype(np.uint8))
        if name not in NO_MASK:
            synth.save(masks / name, mask)
    server, url = serve(create_app(images, mask_dir=masks, out_dir=out, project={"name": "本", "path": str(tmp_path)}))
    for name in ("apple.png", "banana.png", "cherry.png"):                       # 設定あり
        request(url, f"api/params/{name}", json.dumps(BLANK).encode("utf-8"), "PUT")
    for name in ("apple.png", "banana.png", "elder.png"):                        # 書き出しあり
        request(url, f"api/export/{name}", png_bytes())
    later = time.time_ns() + 30_000_000_000                                      # banana は、設定がその後変わった
    os.utime(out / "banana.inkdeus.json", ns=(later, later))
    pages = []

    def open_(query: str = "", **kw):
        page = open_page(browser, url + query, **kw)
        pages.append(page)
        return page

    yield {"open": open_, "url": url, "port": server.server_port, "out": out}
    for page in pages:
        page.close()
    server.shutdown()


CELLS = """() => Array.from(document.querySelectorAll('.gal-item')).map(c => ({
    name: c.dataset.name, hidden: c.hidden, current: c.classList.contains('current'),
    missing: c.classList.contains('missing'), label: c.querySelector('.gal-name').textContent,
    badges: Array.from(c.querySelectorAll('.gal-badge')).map(b => b.dataset.kind),
    texts: Array.from(c.querySelectorAll('.gal-badge')).map(b => b.textContent),
    src: c.querySelector('img').dataset.src, loaded: c.querySelector('img').getAttribute('src')}))"""


def cells(page) -> dict[str, dict]:
    return {c["name"]: c for c in page.evaluate(CELLS)}


def shown(page) -> list[str]:
    return [c["name"] for c in page.evaluate(CELLS) if not c["hidden"]]


def current(page) -> list[str]:
    return [c["name"] for c in page.evaluate(CELLS) if c["current"]]


def chips(page) -> dict[str, str]:
    return page.evaluate("() => Object.fromEntries(Array.from(document.querySelectorAll('.gal-chip'))"
                         ".map(c => [c.dataset.filter, c.textContent]))")


def wait_badges(page, name: str, badges: list[str]) -> None:
    page.wait_for_function(
        """([name, want]) => { const c = document.querySelector(`.gal-item[data-name="${name}"]`);
            return c && Array.from(c.querySelectorAll('.gal-badge')).map(b => b.dataset.kind).join() === want; }""",
        arg=[name, ",".join(badges)], timeout=20000)


def test_gallery_lists_all_images_and_opens_on_click(live, capsys):
    page = live["open"]()
    assert page.evaluate("() => document.getElementById('gallery').hidden") is False
    assert page.evaluate("() => window.inkdeus.gallery().open") is True                # 既定は開いた状態
    got = cells(page)
    assert list(got) == NAMES and all(c["label"] == n for n, c in got.items())
    assert current(page) == ["apple.png"]
    assert got["apple.png"]["badges"] == ["params", "fresh"] and got["apple.png"]["texts"] == ["設定", "書出"]
    assert got["banana.png"]["badges"] == ["params", "stale"]
    assert got["cherry.png"]["badges"] == ["params", "nomask"]
    assert got["date.png"]["badges"] == []
    assert got["elder.png"]["badges"] == ["stale", "nomask"]                           # 白黒版はあるが、設定が未保存
    assert got["grape.png"]["badges"] == []
    assert all(c["src"].startswith("api/assets/source/thumb/") for c in got.values())
    page.wait_for_function("() => document.querySelector('.gal-item img').naturalWidth > 0")
    assert "116" not in page.evaluate("() => document.querySelector('.gal-count').textContent")
    assert page.evaluate("() => document.querySelector('.gal-count').textContent") == "7 枚中 7 枚"
    # クリックで開く（既存の切り替えと同じ経路）
    page.click(".gal-item[data-name='date.png']")
    page.wait_for_function("() => window.inkdeus.info().name === 'date.png'")
    page.wait_for_function("() => document.querySelector('.gal-item.current').dataset.name === 'date.png'")
    assert current(page) == ["date.png"]
    assert page.evaluate("() => document.getElementById('selImage').value") == "date.png"   # 既存のドロップダウンも追従
    # 外部（ctl open）で切り替わっても追従する
    assert cli.main(["ctl", "--port", str(live["port"]), "open", "grape.png", "--timeout", "20"]) == 0
    capsys.readouterr()
    page.wait_for_function("() => document.querySelector('.gal-item.current').dataset.name === 'grape.png'")
    # [ と ] で前後へ
    page.keyboard.press("[")
    page.wait_for_function("() => window.inkdeus.info().name === 'fig.png'")
    page.keyboard.press("]")
    page.wait_for_function("() => window.inkdeus.info().name === 'grape.png'")
    page.keyboard.press("]")                                                           # 端では何もしない
    page.wait_for_timeout(300)
    assert page.evaluate("() => window.inkdeus.info().name") == "grape.png" and current(page) == ["grape.png"]
    page.focus(".gal-search")                                                          # 入力欄では文字として入る
    page.keyboard.type("[")
    assert page.evaluate("() => document.querySelector('.gal-search').value") == "["
    assert page.evaluate("() => window.inkdeus.info().name") == "grape.png"
    assert page.errors == []


def test_gallery_filters_and_counts(live):
    page = live["open"]()
    assert chips(page) == {"all": "すべて 7", "noParams": "未設定 4", "noExport": "未書き出し 4", "stale": "書き出しが古い 2"}
    page.click(".gal-chip[data-filter='stale']")
    assert shown(page) == ["banana.png", "elder.png"]
    assert page.evaluate("() => document.querySelector('.gal-count').textContent") == "7 枚中 2 枚"
    assert page.evaluate("() => document.querySelector('.gal-chip.on').dataset.filter") == "stale"
    page.click(".gal-chip[data-filter='noParams']")
    assert shown(page) == ["date.png", "elder.png", "fig.png", "grape.png"]
    page.click(".gal-chip[data-filter='noExport']")
    assert shown(page) == ["cherry.png", "date.png", "fig.png", "grape.png"]
    # 絞り込み中は、その結果の中で前後へ動く
    page.evaluate("() => document.getElementById('gl').focus()")
    page.keyboard.press("]")
    page.wait_for_function("() => window.inkdeus.info().name === 'cherry.png'")       # apple は結果に無いので先頭へ
    page.keyboard.press("]")
    page.wait_for_function("() => window.inkdeus.info().name === 'date.png'")
    page.keyboard.press("[")
    page.wait_for_function("() => window.inkdeus.info().name === 'cherry.png'")
    # 名前での絞り込み（チップと両方が効く）
    page.fill(".gal-search", "AP")
    page.wait_for_function("() => document.querySelector('.gal-count').textContent === '7 枚中 1 枚'", timeout=20000)
    assert shown(page) == ["grape.png"]
    page.click(".gal-chip[data-filter='all']")
    assert shown(page) == ["apple.png", "grape.png"]
    page.fill(".gal-search", "zzz")
    page.wait_for_function("() => document.querySelector('.gal-count').textContent === '7 枚中 0 枚'", timeout=20000)
    page.fill(".gal-search", "")
    page.wait_for_function("() => document.querySelector('.gal-count').textContent === '7 枚中 7 枚'", timeout=20000)
    assert page.errors == []


def test_badges_follow_saves_and_exports(live, capsys):
    page = live["open"]("?image=date.png")
    assert cells(page)["date.png"]["badges"] == []
    page.evaluate("(p) => { window.inkdeus.setParams(p); }", HATCH)
    page.evaluate("() => window.inkdeus.save()")
    wait_badges(page, "date.png", ["params"])                                  # 設定を保存した
    assert chips(page)["noParams"] == "未設定 3"
    page.evaluate("() => window.inkdeus.exportPNG()")
    wait_badges(page, "date.png", ["params", "fresh"])                         # 書き出した
    assert chips(page)["noExport"] == "未書き出し 3"
    page.evaluate("(p) => { window.inkdeus.setParams(p); }", BLANK)
    wait_badges(page, "date.png", ["params", "stale"])                         # 設定を変えたので、白黒版は古くなった
    assert chips(page)["stale"] == "書き出しが古い 3"
    # 別の所（別のタブ・一括書き出し）での書き出し・設定変更も、その画像の分だけ反映する
    request(live["url"], "api/export/cherry.png", png_bytes())
    wait_badges(page, "cherry.png", ["params", "fresh", "nomask"])
    assert cli.main(["ctl", "--port", str(live["port"]), "set", "--name", "apple.png", "lines.strength", "0.5"]) == 0
    capsys.readouterr()
    wait_badges(page, "apple.png", ["params", "stale"])
    assert cli.main(["ctl", "--port", str(live["port"]), "set", "--name", "fig.png", "lines.strength", "0.5"]) == 0
    capsys.readouterr()
    wait_badges(page, "fig.png", ["params"])
    assert page.errors == []


def test_thumbnail_kind_switch(live):
    page = live["open"]()
    page.select_option(".gal-kind", "inked")
    got = cells(page)
    version = (live["out"] / "apple.png").stat().st_mtime_ns // 1_000_000
    assert got["apple.png"]["src"] == f"api/assets/inked/thumb/apple.png?h=120&v={version}"
    assert got["apple.png"]["missing"] is False and got["elder.png"]["missing"] is False
    # 白黒版が無い画像は、元画像を薄く出して区別する
    assert got["cherry.png"]["missing"] is True and got["cherry.png"]["src"].startswith("api/assets/source/thumb/")
    assert page.evaluate("""() => getComputedStyle(document.querySelector(".gal-item[data-name='cherry.png'] img")).opacity""") == "0.3"
    page.wait_for_function("""() => { const i = document.querySelector(".gal-item[data-name='apple.png'] img");
        return i.getAttribute('src').includes('/inked/') && i.naturalWidth > 0; }""")
    # 書き出すと、白黒版のサムネイルが新しい版になる
    page.evaluate("() => window.inkdeus.open('cherry.png')")
    page.evaluate("() => window.inkdeus.exportPNG()")
    page.wait_for_function("""() => { const c = document.querySelector(".gal-item[data-name='cherry.png']");
        return !c.classList.contains('missing') && c.querySelector('img').dataset.src.includes('/inked/thumb/cherry.png'); }""")
    first = cells(page)["cherry.png"]["src"]
    time.sleep(0.05)
    page.evaluate("(p) => { window.inkdeus.setParams(p); }", HATCH)
    page.evaluate("() => window.inkdeus.exportPNG()")
    page.wait_for_function("""(old) => document.querySelector(".gal-item[data-name='cherry.png'] img").dataset.src !== old""",
                           arg=first)
    assert page.evaluate("() => localStorage.getItem('inkdeus.gallery.kind')") == '"inked"'
    page.reload()                                                              # 種類は次回も覚えている
    page.wait_for_function("() => window.inkdeus && window.inkdeus.ready")
    page.evaluate("() => window.inkdeus.ready")
    assert page.evaluate("() => document.querySelector('.gal-kind').value") == "inked"
    assert "/inked/thumb/" in cells(page)["apple.png"]["src"]


def test_width_open_state_and_preview_integrity(live):
    page = live["open"]()                                                      # 画面は 1200x800
    page.evaluate("(p) => { window.inkdeus.setParams(p); }", HATCH)

    def measure():
        return page.evaluate("""() => { const g = document.getElementById('gallery'), c = document.getElementById('gl');
            const dpr = window.devicePixelRatio || 1;
            return {gallery: g.getBoundingClientRect().width, viewerLeft: document.getElementById('viewer').getBoundingClientRect().left,
                columns: getComputedStyle(document.querySelector('.gal-grid')).gridTemplateColumns.split(' ').length,
                canvasOk: c.width === Math.round(c.clientWidth * dpr) && c.height === Math.round(c.clientHeight * dpr),
                canvasWidth: c.clientWidth, state: window.inkdeus.gallery(),
                split: document.getElementById('splitHandle').style.left,
                stored: JSON.parse(localStorage.getItem('inkdeus.gallery.width'))}; }""")

    def settle():
        page.wait_for_function("""() => { const c = document.getElementById('gl'); const dpr = window.devicePixelRatio || 1;
            return c.width === Math.round(c.clientWidth * dpr); }""")
        return measure()

    def drag_to(x: float):
        box = page.locator(".gal-resize").bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 300)
        page.mouse.down()
        page.mouse.move(x - 30, box["y"] + 310, steps=4)
        page.mouse.move(x, box["y"] + 320, steps=4)
        page.mouse.up()
        return settle()

    before = pixels(page)
    first = settle()
    assert first["gallery"] == pytest.approx(224, abs=2) and first["columns"] == 2 and first["stored"] is None
    assert first["viewerLeft"] == pytest.approx(first["gallery"], abs=1)        # プレビューはパネルの分だけ右へ寄る
    page.click("#btnCompare")                                                  # 比較表示にしておく
    wide = drag_to(420)
    assert wide["state"]["width"] == pytest.approx(420, abs=3) and wide["columns"] > first["columns"]
    assert wide["canvasOk"] and wide["canvasWidth"] == pytest.approx(first["canvasWidth"] - 220, abs=4)
    assert wide["stored"] == wide["state"]["width"] and wide["split"] == "50%"   # 仕切りは幅に対する割合のまま
    assert drag_to(40)["state"]["width"] == 160                                # 最小 160px
    assert drag_to(1100)["state"]["width"] == 600                              # 最大は画面幅の 50%
    kept = drag_to(330)["state"]["width"]
    assert (pixels(page) == before).all()                                      # 出力の画素は変わらない
    # 比較表示のまま拡大・移動しても、座標の計算が崩れない（キャンバス内の同じ点を中心に拡大される）
    page.mouse.move(700, 400)
    page.mouse.wheel(0, -300)
    assert page.evaluate("() => window.inkdeus.state.view.zoom") > 0 and page.errors == []
    # 閉じる → プレビューが広がる。開閉の状態は覚えている
    page.click(".gal-toggle")
    closed = settle()
    assert closed["state"]["open"] is False and closed["gallery"] == pytest.approx(24, abs=2) and closed["canvasOk"]
    assert closed["canvasWidth"] > wide["canvasWidth"]
    page.reload()
    page.wait_for_function("() => window.inkdeus && window.inkdeus.ready")
    page.evaluate("() => window.inkdeus.ready")
    again = settle()
    assert again["state"]["open"] is False and again["state"]["width"] == kept
    page.click(".gal-toggle")
    opened = settle()
    assert opened["state"]["open"] is True and opened["gallery"] == pytest.approx(kept + 24, abs=2)
    assert page.evaluate("() => document.querySelector('.gal-item.current').dataset.name") == "apple.png"
    page.dblclick(".gal-resize")                                               # ダブルクリックで既定の幅
    back = settle()
    assert back["state"]["width"] == 200 and back["stored"] is None and back["columns"] == 2
    assert (pixels(page) == before).all() and page.errors == []


def test_current_image_is_scrolled_into_view(tmp_path, browser):
    images = tmp_path / "images"
    for i in range(60):
        synth.save(images / f"img{i:03d}.png", synth.flat(32, 24, 40 + i))
    server, url = serve(create_app(images, out_dir=tmp_path / "out"))
    page = open_page(browser, url + "?image=img055.png")
    try:
        visible = """() => { const g = document.querySelector('.gal-grid').getBoundingClientRect();
            const c = document.querySelector('.gal-item.current').getBoundingClientRect();
            return c.top >= g.top - 1 && c.bottom <= g.bottom + 1; }"""
        page.wait_for_function(visible)
        assert page.evaluate("() => document.querySelector('.gal-item.current').dataset.name") == "img055.png"
        # 画面内の物だけ読み込む（遠くのサムネイルはまだ取りに行っていない）
        loaded = page.evaluate("() => Array.from(document.querySelectorAll('.gal-item img')).filter(i => i.getAttribute('src')).length")
        assert 0 < loaded < 60
        page.evaluate("() => window.inkdeus.open('img002.png')")
        page.wait_for_function(visible)
        page.click(".gal-toggle")
        page.evaluate("() => window.inkdeus.open('img050.png')")
        page.click(".gal-toggle")                                              # 開いたときにも見える位置へ
        page.wait_for_function(visible)
        assert page.errors == []
    finally:
        page.close()
        server.shutdown()


def test_gallery_during_export_all(live):
    page = live["open"]()
    # 実行中に一覧を押しても切り替わらない
    page.evaluate("""() => { const t = setInterval(() => { if (window.inkdeus.batch().done >= 2) { clearInterval(t);
        window.__clicked = window.inkdeus.batch().running;
        document.querySelector(".gal-item[data-name='grape.png']").click(); } }, 5); }""")
    result = page.evaluate("() => window.inkdeus.exportAll({force: true})")
    assert result["exported"] == 7 and result["failed"] == 0 and page.evaluate("() => window.__clicked") is True
    assert page.evaluate("() => window.inkdeus.info().name") == "apple.png"     # 始める前の画像へ戻っている
    for name in NAMES:                                                          # 進行に合わせて印が付く
        wait_badges(page, name, ["params", "fresh"] + (["nomask"] if name in NO_MASK else []))
    assert chips(page) == {"all": "すべて 7", "noParams": "未設定 0", "noExport": "未書き出し 0", "stale": "書き出しが古い 0"}
    page.click(".gal-item[data-name='grape.png']")                             # 終われば、また押せる
    page.wait_for_function("() => window.inkdeus.info().name === 'grape.png'")
    assert page.errors == []


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def test_no_gallery_without_server(browser):
    web = Path(__file__).resolve().parent.parent / "web"
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(web)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    page = open_page(browser, f"http://127.0.0.1:{httpd.server_port}/")
    try:
        assert page.evaluate("() => window.inkdeus.info().server") is False
        assert page.evaluate("() => document.getElementById('gallery').hidden") is True
        assert page.evaluate("() => window.inkdeus.gallery()") is None
        assert page.evaluate("() => document.getElementById('viewer').getBoundingClientRect().left") == 0
    finally:
        page.close()
        httpd.shutdown()
