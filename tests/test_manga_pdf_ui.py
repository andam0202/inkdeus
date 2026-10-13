"""PDF の書き出しの結合テスト: 実物の漫画エディタ（上流）で数ページ作り、PDF にまとめる"""

from __future__ import annotations

import contextlib
import io
import json
import threading
import urllib.request

import pytest

from inkdeus import cli
from test_manga_bridge import (  # noqa: F401 — manga_env / ctx は fixture
    apply_template, ctx, manga_env, open_manga)

pytestmark = pytest.mark.manga          # 実物の漫画エディタ（上流のクローン）が要る

pypdf = pytest.importorskip("pypdf")

MM = 72.0 / 25.4
UPSTREAM_SETTINGS = "() => ({dpi: document.getElementById('outputDpi').value, size: JSON.stringify(getPageSizeMm())})"


@pytest.fixture(scope="module")
def book(ctx, manga_env):  # noqa: F811
    """3 ページ（白紙 + ひな形 2 つ）の漫画を開いたページ"""
    page, status = open_manga(ctx, manga_env)
    assert status["ok"], status
    page.evaluate("() => window.inkdeusBridge.setAutosave({enabled: false})")
    apply_template(page)
    apply_template(page)
    assert page.evaluate("() => btmGetGuids().length") == 3
    yield page
    page.close()


def fetch(env, path: str) -> bytes:
    return urllib.request.urlopen(env.url + path.lstrip("/"), timeout=30).read()


def box(page, name: str) -> list[float]:
    return [float(v) for v in page[name]]


def test_pdf_of_all_pages(book, manga_env):  # noqa: F811
    assert "mangaPdf" in book.evaluate("() => window.inkdeusBridge.status().server")
    assert "pdf.js" in book.evaluate("() => window.inkdeusBridge.status().modules")
    before = book.evaluate(UPSTREAM_SETTINGS)
    here = book.evaluate("() => getCanvasGUID()")
    info = book.evaluate("""() => window.inkdeusBridge.exportPdf({name: 'book',
        settings: {size: 'B5', dpi: 150, bleedMm: 3, fit: 'cover', binding: 'right', layout: 'spread-cover'}})""")
    path = manga_env.project / "manga" / "pdf" / "book.pdf"
    assert info["path"] == str(path) and info["pages"] == 3 and info["numbers"] == [1, 2, 3]
    doc = pypdf.PdfReader(str(path))
    assert len(doc.pages) == 3
    portrait = book.evaluate("() => canvas.height >= canvas.width")
    w, h = (182, 257) if portrait else (257, 182)
    for p in doc.pages:
        assert box(p, "/MediaBox") == pytest.approx([0, 0, (w + 6) * MM, (h + 6) * MM], abs=0.01)
        assert box(p, "/TrimBox") == pytest.approx([3 * MM, 3 * MM, (w + 3) * MM, (h + 3) * MM], abs=0.01)
        im = p["/Resources"]["/XObject"]["/Im0"]
        assert (int(im["/Width"]), int(im["/Height"])) == (round((w + 6) / 25.4 * 150), round((h + 6) / 25.4 * 150))
        assert im["/ColorSpace"] == "/DeviceGray" and im["/Filter"] == "/FlateDecode"
    root = doc.trailer["/Root"]
    assert root["/ViewerPreferences"]["/Direction"] == "/R2L" and root["/PageLayout"] == "/TwoPageRight"
    # ひな形のページにはコマの枠線（黒）が入っている。白紙のページは真っ白
    blank = doc.pages[0]["/Resources"]["/XObject"]["/Im0"].get_data()
    framed = doc.pages[1]["/Resources"]["/XObject"]["/Im0"].get_data()
    assert min(blank) > 200 and min(framed) < 60
    # 漫画エディタの原稿サイズ・DPI の設定は変わらず、開いていたページへ戻っている
    assert book.evaluate(UPSTREAM_SETTINGS) == before
    assert book.evaluate("() => getCanvasGUID()") == here
    # 途中のページ画像は片付いている
    assert json.loads(fetch(manga_env, "api/manga/pdf"))["running"] == []


def test_pdf_of_a_page_range_matching_the_page_ratio(book, manga_env):  # noqa: F811
    info = book.evaluate("""() => window.inkdeusBridge.exportPdf({name: 'part', pages: '2-3',
        settings: {size: 'A5', dpi: 100, matchAspect: true, color: 'bw', binding: 'left'}})""")
    assert info["pages"] == 2 and info["numbers"] == [2, 3]
    doc = pypdf.PdfReader(io.BytesIO(fetch(manga_env, info["url"])))
    assert len(doc.pages) == 2 and doc.trailer["/Root"]["/ViewerPreferences"]["/Direction"] == "/L2R"
    size = book.evaluate("() => ({w: canvas.width, h: canvas.height})")
    for p in doc.pages:
        media = box(p, "/MediaBox")
        assert media[2] / media[3] == pytest.approx(size["w"] / size["h"], rel=0.002)        # ページの縦横比のまま
        assert max(media[2] / (148 * MM), media[3] / (210 * MM)) == pytest.approx(1.0, abs=0.001)   # 余白なしで最大
        im = p["/Resources"]["/XObject"]["/Im0"]
        assert int(im["/BitsPerComponent"]) == 1
    assert [x["resampled"] for x in info["pageInfo"]] == [False, False]      # ちょうどの倍率で書き出すので拡大縮小しない
    with pytest.raises(Exception, match="4 ページはありません"):
        book.evaluate("() => window.inkdeusBridge.exportPdf({name: 'part', pages: '1-4', settings: {dpi: 100}})")
    assert json.loads(fetch(manga_env, "api/manga/pdf"))["running"] == []


def test_pdf_dialog_and_abort(book, manga_env):  # noqa: F811
    book.evaluate("() => window.inkdeusBridge.setOpen(true)")
    book.click("#ikb-pdf")
    book.wait_for_selector("#ikb-pdf-dialog[open]")
    assert book.is_hidden("#ikb-pdf-threshold") and book.is_hidden("#ikb-pdf-dpi-custom")
    book.select_option("#ikb-pdf-size", "B6")
    book.select_option("#ikb-pdf-dpi", "custom")
    book.fill("#ikb-pdf-dpi-custom", "120")
    book.select_option("#ikb-pdf-bleed", "5")
    book.select_option("#ikb-pdf-color", "bw")
    assert book.is_visible("#ikb-pdf-threshold")
    book.fill("#ikb-pdf-pages", "9")
    book.fill("#ikb-pdf-name", "dialog")
    book.click("#ikb-pdf-ok")
    assert "9 ページはありません" in book.inner_text("#ikb-pdf-dialog .ikb-msg")       # ダイアログは閉じない
    book.fill("#ikb-pdf-pages", "1,3")
    book.click("#ikb-pdf-ok")
    book.wait_for_selector(".ikb-pdf-open[href$='dialog.pdf']")
    href = book.get_attribute(".ikb-pdf-open", "href")
    assert href == "/api/manga/pdf/dialog.pdf"
    doc = pypdf.PdfReader(io.BytesIO(fetch(manga_env, href)))
    assert len(doc.pages) == 2
    media = box(doc.pages[0], "/MediaBox")
    assert sorted(media[2:]) == pytest.approx(sorted([(128 + 10) * MM, (182 + 10) * MM]), abs=0.01)
    assert str(manga_env.project / "manga" / "pdf" / "dialog.pdf") in book.inner_text(".ikb-pdf-path")
    # 前回の設定を覚えている
    book.click("#ikb-pdf")
    assert book.input_value("#ikb-pdf-size") == "B6" and book.input_value("#ikb-pdf-dpi-custom") == "120"
    book.click("#ikb-pdf-dialog button:has-text('やめる')")
    # 中止: 途中のページ画像は片付き、PDF はできない
    with pytest.raises(Exception, match="中止しました"):
        book.evaluate("""async () => { const job = window.inkdeusBridge.exportPdf({name: 'stopped', settings: {dpi: 100}});
            window.inkdeusBridge.abortPdf(); return await job; }""")
    listing = json.loads(fetch(manga_env, "api/manga/pdf"))
    assert listing["running"] == [] and "stopped" not in [p["name"] for p in listing["pdfs"]]
    assert book.evaluate("() => window.inkdeusBridge.status().busy") is False


def run_ctl(page, env, *args) -> dict:
    """inkdeus ctl を別スレッドで動かす（ページの通信はテスト側のスレッドが中継しているので、待つ間も回し続ける）"""
    port = env.url.rstrip("/").rsplit(":", 1)[1]
    box_: dict = {}

    def work():
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            box_["code"] = cli.main(["ctl", "--port", port, *args])
        box_["out"], box_["err"] = out.getvalue(), err.getvalue()

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    for _ in range(1200):
        if not thread.is_alive():
            break
        page.wait_for_timeout(100)
    assert not thread.is_alive(), "ctl が終わりません"
    return {"code": box_["code"], **json.loads(box_["out"] if box_["code"] == 0 else box_["err"])}


def test_ctl_manga_pdf_drives_the_open_tab(book, manga_env):  # noqa: F811
    result = run_ctl(book, manga_env, "manga", "pdf", "--name", "viactl", "--size", "A4", "--dpi", "72",
                     "--pages", "2", "--wait", "--timeout", "120")
    assert result["code"] == 0, result
    assert result["pages"] == 1 and result["numbers"] == [2]
    assert result["path"] == str(manga_env.project / "manga" / "pdf" / "viactl.pdf")
    assert len(pypdf.PdfReader(result["path"]).pages) == 1
