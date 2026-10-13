"""データを失わないための仕組み（白黒化の画面）: 以前の状態に戻す、筆を置いた直後の保存、閉じる前の確認、
保存の失敗と再試行、サーバの更新の知らせ、壊れた設定ファイル、見つからない元画像の場所。画像はすべて合成"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import synth
from conftest import BLANK, deep_merge, layer, open_page, pixels, serve
from inkdeus.server import create_app
from test_paint_ui import ACROSS, BAND, MIX, drag, eraser, mix_image, paint, pen

LINES_OFF = deep_merge(MIX, {"lines": {"enabled": False}})


def wait_saved(page) -> None:
    page.wait_for_function("() => window.inkdeus.saveStatus().state === 'saved'")


def set_params(page, params) -> np.ndarray:
    page.evaluate("(p) => { window.inkdeus.setParams(p); }", params)
    return pixels(page)


@pytest.fixture()
def live(tmp_path, browser):
    images = tmp_path / "images"
    synth.save(images / "mix.png", mix_image())
    synth.save(images / "other.png", mix_image())
    out = tmp_path / "out"
    app = create_app(images, out_dir=out, local_config=tmp_path / "none.toml", config_dir=tmp_path / "config")
    server, url = serve(app)
    pages = []

    def open_(query: str = "?image=mix.png", **kw):
        page = open_page(browser, url + query, **kw)
        pages.append(page)
        return page

    yield {"open": open_, "url": url, "out": out, "app": app, "images": images}
    for page in pages:
        if not page.is_closed():
            page.close()
    server.shutdown()


# ---- 以前の状態に戻す ----

def test_restore_an_earlier_state_and_undo_it(live):
    page = live["open"]()
    assert page.evaluate("() => window.inkdeus.backups.ok()") is True
    first = set_params(page, MIX)
    wait_saved(page)
    eraser(page, "all")
    drag(page, ACROSS)
    wait_saved(page)
    erased = pixels(page)
    assert (erased[BAND] == 255).all() and (first[BAND] != 255).any()
    time.sleep(2.2)                                                # 2 秒以内の変更は、1 つの時点にまとまる
    page.evaluate("() => window.inkdeus.paint.setTool('hand')")
    second = set_params(page, LINES_OFF)
    wait_saved(page)
    pen(page, "black", size=20)
    drag(page, [(60, 30), (200, 30)])
    wait_saved(page)
    latest = pixels(page)
    assert (latest != erased).any() and (latest[25:35, 80:180] == 0).all() and second is not None
    page.evaluate("() => window.inkdeus.paint.setTool('hand')")

    # 出力パネルの「以前の状態に戻す…」から、いちばん新しい時点（最後の変更の直前）を選ぶ
    page.click("#btnRestore")
    page.wait_for_selector("#restoreDialog[open]")
    rows = page.evaluate("""() => Array.from(document.querySelectorAll('#restoreDialog .chooser-row'))
        .map(r => [r.querySelector('.chooser-label').textContent, r.querySelector('.chooser-note').textContent])""")
    assert len(rows) >= 2 and all(label[:2] == "20" for label, _ in rows)
    assert "ペン" in rows[0][1]
    # 「線画を切った」変更の時点（設定の変更のうち、いちばん新しい物。すぐ後のペンの筆と 1 つにまとまっていることもある）
    index = next(i for i, (_label, note) in enumerate(rows) if "設定" in note)
    page.click(f"#restoreDialog .chooser-row >> nth={index}")
    page.wait_for_function("() => window.inkdeus.getParams().lines.enabled === true && !window.inkdeus.paint.used('pen')")
    page.wait_for_function("() => window.inkdeus.paint.state().loading === false")
    restored = pixels(page)
    assert (restored == erased).all()                              # 設定も消しゴム・ペンも、1 つ前の状態の絵に戻る
    assert json.loads((live["out"] / "mix.inkdeus.json").read_bytes())["lines"]["enabled"] is True
    assert not (live["out"] / "mix.paint" / "pen.png").exists()
    # 知らせの「取り消す」で、戻す前の状態へ
    page.click("#messages .msg-action")
    page.wait_for_function("() => window.inkdeus.getParams().lines.enabled === false && window.inkdeus.paint.used('pen')")
    page.wait_for_function("() => window.inkdeus.paint.state().loading === false")
    assert (pixels(page) == latest).all()
    assert (live["out"] / "mix.paint" / "pen.png").exists()
    # やめる・戻せる時点が無い画像
    page.click("#btnRestore")
    page.wait_for_selector("#restoreDialog[open]")
    page.click("#restoreDialog .chooser-cancel")
    assert page.locator("#restoreDialog").count() == 0
    page.click("#btnRestore")
    page.wait_for_selector("#restoreDialog[open]")
    page.keyboard.press("Escape")
    assert page.locator("#restoreDialog").count() == 0
    assert page.evaluate("() => window.inkdeus.getParams().lines.enabled") is False      # やめれば何も変わらない


# ---- 筆を置いた直後の保存 ----

def mask_of(out: Path, target: str, stem: str = "mix") -> np.ndarray | None:
    path = out / f"{stem}.paint" / f"{target}.png"
    try:
        with Image.open(path) as im:
            return np.asarray(im.convert("RGB" if target == "pen" else "L")).copy()
    except (OSError, SyntaxError, ValueError):
        return None


def test_a_stroke_is_saved_the_moment_the_pointer_is_released(live):
    """ポインタを離した直後にタブを閉じても、開き直すと残っている"""
    page = live["open"]()
    set_params(page, MIX)
    wait_saved(page)
    assert page.evaluate("() => window.inkdeus.saveStatus().tiles") is True
    eraser(page, "all")
    pts = [page.evaluate("([x, y]) => window.inkdeus.paint.clientPoint(x, y)", p) for p in ACROSS]
    page.mouse.move(*pts[0])
    page.mouse.down()
    page.mouse.move(*pts[1], steps=6)
    page.mouse.up()
    page.close()                                                   # 1 秒後の「全体の保存」を待たずに閉じる
    deadline = time.time() + 10
    while time.time() < deadline and mask_of(live["out"], "all") is None:
        time.sleep(0.05)
    saved = mask_of(live["out"], "all")
    assert saved is not None and (saved[BAND] == 255).all() and (saved[:60] == 0).all()
    again = live["open"]()
    assert paint(again)["targets"] == ["all"] and (pixels(again)[BAND] == 255).all()
    # 続けての筆・取り消し・やり直しも、その場でサーバへ渡る（全体の PNG を作り直す保存は行われない）
    puts = []
    again.on("request", lambda r: puts.append((r.method, r.url.split("/api/")[-1])) if "/api/paint/" in r.url else None)
    before = again.evaluate("() => window.inkdeus.saveStatus().tileSaves")
    eraser(again, "all")
    drag(again, [(10, 30), (246, 30)])
    again.wait_for_function("(n) => window.inkdeus.saveStatus().tileSaves === n + 1", arg=before)
    assert (mask_of(live["out"], "all")[20:40, 30:220] == 255).all()
    again.keyboard.press("Control+z")
    again.wait_for_function("(n) => window.inkdeus.saveStatus().tileSaves === n + 2", arg=before)
    assert (mask_of(live["out"], "all")[20:40] == 0).all() and (mask_of(live["out"], "all")[BAND] == 255).all()
    again.keyboard.press("Control+y")
    again.wait_for_function("(n) => window.inkdeus.saveStatus().tileSaves === n + 3", arg=before)
    wait_saved(again)
    assert (mask_of(live["out"], "all")[20:40, 30:220] == 255).all()
    assert [m for m, _u in puts] == ["PATCH", "PATCH", "PATCH"]
    # ペン（原寸・2 チャンネル）も同じ。全部戻して空になったら、サーバのファイルも消える
    pen(again, "black", size=16)
    drag(again, [(40, 150), (200, 150)])
    again.wait_for_function("(n) => window.inkdeus.saveStatus().tileSaves === n + 4", arg=before)
    ink = mask_of(live["out"], "pen")
    assert ink.shape == (192, 256, 3) and (ink[146:154, 60:180, 0] == 255).all() and (ink[..., 1:] == 0).all()
    again.keyboard.press("Control+z")
    again.wait_for_function("(n) => window.inkdeus.saveStatus().tileSaves === n + 5", arg=before)
    wait_saved(again)
    assert mask_of(live["out"], "pen") is None
    assert [t["target"] for t in json.loads(urllib.request.urlopen(live["url"] + "api/paint/mix.png").read())["targets"]] == ["all"]
    # 開き直しても同じ絵（タイルで保存した物と、画面が持っている物が一致している）
    shown = pixels(again)
    third = live["open"]()
    assert (pixels(third) == shown).all()


ASK = """() => { const e = new Event('beforeunload', {cancelable: true}); window.dispatchEvent(e);
    return e.defaultPrevented; }"""


def test_closing_with_unsaved_changes_asks_first(live):
    """保存していない変更があるときは、閉じる・再読み込みの前に確かめる（その間に保存も進める）。
    確認のダイアログそのものはブラウザが出す物なので、ここでは「引き止めるかどうか」を beforeunload の扱いで確かめる
    （ヘッドレスのブラウザは、この確認を出さずに閉じる）"""
    page = live["open"]()
    set_params(page, MIX)
    wait_saved(page)
    assert page.evaluate(ASK) is False                             # 保存済みなら引き止めない
    requests = []
    page.on("request", lambda r: requests.append(r.method) if "/api/params/" in r.url else None)
    page.evaluate("(p) => { window.inkdeus.setParams(p); }", LINES_OFF)
    assert page.evaluate("() => window.inkdeus.saveStatus()")["state"] == "dirty"
    assert page.locator("#saveState").inner_text() == "未保存の変更あり"
    assert page.evaluate(ASK) is True                              # 引き止める
    wait_saved(page)                                               # 引き止めている間に、保存を済ませる（待ち時間を待たない）
    assert requests == ["PUT"] and page.locator("#saveState").inner_text() == "保存済み"
    assert json.loads((live["out"] / "mix.inkdeus.json").read_bytes())["lines"]["enabled"] is False
    assert page.evaluate(ASK) is False
    # 筆の保存の途中も同じ
    eraser(page, "all")
    drag(page, ACROSS)
    assert page.evaluate("() => window.inkdeus.saveStatus().state") in ("saving", "dirty")
    assert page.evaluate(ASK) is True
    wait_saved(page)
    assert page.evaluate(ASK) is False
    # タブが隠れたとき（別のタブへ移る・閉じかける）も、待ち時間を待たずに保存する
    page.evaluate("() => window.inkdeus.paint.setTool('hand')")
    page.evaluate("(p) => { window.inkdeus.setParams(p); }", MIX)
    page.evaluate("""() => { Object.defineProperty(document, 'hidden', {get: () => true, configurable: true});
        document.dispatchEvent(new Event('visibilitychange')); }""")
    page.wait_for_function("() => window.inkdeus.saveStatus().state === 'saved'", timeout=600)
    assert json.loads((live["out"] / "mix.inkdeus.json").read_bytes())["lines"]["enabled"] is True


# ---- 保存の失敗と再試行・サーバの更新の知らせ（サーバを止めたり起動し直したりする）----

LAUNCH = """
import sys
from inkdeus.server import create_app
images, out, port, tag, cfg = sys.argv[1:6]
app = create_app(images, out_dir=out, local_config=cfg, config_dir=cfg + ".d", build_tag=tag or None)
app.run(host="127.0.0.1", port=int(port), threaded=True)
"""


class Proc:
    """別プロセスのサーバ（止める・同じポートで起動し直す、を確かめるため）"""

    def __init__(self, images: Path, out: Path, tmp: Path):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.args = [str(images), str(out)]
        self.tmp = tmp
        self.url = f"http://127.0.0.1:{self.port}/"
        self.proc = None

    def start(self, tag: str = "") -> None:
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
        self.proc = subprocess.Popen([sys.executable, "-c", LAUNCH, *self.args, str(self.port), tag,
                                      str(self.tmp / "none.toml")], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                urllib.request.urlopen(self.url + "api/config", timeout=1).read()
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("サーバが起動しません")

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.kill()
            self.proc.wait(timeout=20)
            self.proc = None


@pytest.fixture()
def proc(tmp_path):
    images = tmp_path / "images"
    synth.save(images / "mix.png", mix_image())
    synth.save(images / "other.png", mix_image())
    p = Proc(images, tmp_path / "out", tmp_path)
    p.out = tmp_path / "out"
    p.start()
    yield p
    p.stop()


def test_failed_saves_are_kept_and_retried_until_the_server_is_back(proc, browser):
    page = open_page(browser, proc.url + "?image=mix.png")
    try:
        set_params(page, MIX)
        wait_saved(page)
        rect = page.evaluate("() => JSON.stringify(document.getElementById('gl').getBoundingClientRect())")
        proc.stop()                                                # サーバが落ちた
        set_params(page, LINES_OFF)
        page.wait_for_function("() => window.inkdeus.saveStatus().state === 'failed'")
        # 保存の状態の文言が変わっても、プレビューの位置は動かない（描いている最中に絵がずれない）
        assert page.evaluate("() => JSON.stringify(document.getElementById('gl').getBoundingClientRect())") == rect
        eraser(page, "all")
        drag(page, ACROSS)
        page.wait_for_function("() => window.inkdeus.saveStatus().state === 'failed'")
        assert page.locator("#saveState").inner_text() == "保存に失敗（再試行中）"
        assert "つながり次第、自動で保存します" in page.locator("#messages").inner_text()
        status = page.evaluate("() => window.inkdeus.saveStatus()")
        assert status["unsaved"] is True and status["failing"] is True
        # 保存できていない間は、別の画像へ移らない（変更を失わないため）
        page.evaluate("() => window.inkdeus.open('other.png')")
        assert page.evaluate("() => window.inkdeus.info().name") == "mix.png"
        assert "移動をやめました" in page.locator("#messages").inner_text()
        # 間隔を広げながら再試行している
        first = page.evaluate("() => window.inkdeus.saveStatus().retryMs")
        page.wait_for_function("(n) => window.inkdeus.saveStatus().retryMs > n", arg=first)
        assert json.loads((proc.out / "mix.inkdeus.json").read_bytes())["lines"]["enabled"] is True
        shown = pixels(page)
        proc.start()                                               # 復旧
        page.wait_for_function("() => window.inkdeus.saveStatus().state === 'saved'", timeout=90_000)
        assert page.locator("#saveState").inner_text() == "保存済み"
        assert "保存できました" in page.locator("#messages").inner_text()
        assert json.loads((proc.out / "mix.inkdeus.json").read_bytes())["lines"]["enabled"] is False
        saved = np.asarray(Image.open(proc.out / "mix.paint" / "all.png"))
        assert (saved[BAND] == 255).all()
        assert page.evaluate("() => Object.keys(window.inkdeus.notices())") == []      # 同じ版で起動し直しただけなら、帯は出ない
        fresh = open_page(browser, proc.url + "?image=mix.png")
        try:
            assert (pixels(fresh) == shown).all()                  # メモリに持っていた変更が、そのまま保存されている
        finally:
            fresh.close()
    finally:
        page.close()


def test_a_banner_asks_to_reload_when_the_server_was_updated(proc, browser):
    page = open_page(browser, proc.url + "?image=mix.png")
    try:
        build = page.evaluate("() => window.inkdeus.build()")
        assert build["id"] and page.evaluate("() => window.inkdeus.notices()") == {}
        assert page.locator("#notices").is_hidden()
        proc.stop()
        proc.start(tag="next-build")                               # 別の版のサーバが、同じ場所で起動した
        page.wait_for_function("() => 'reload' in window.inkdeus.notices()", timeout=90_000)
        assert page.evaluate("() => window.inkdeus.notices().reload") == "inkdeus が更新されました。再読み込みしてください"
        assert page.locator("#notices .notice").is_visible()
        assert "保存していない変更は、再読み込みの前に保存されます" in page.locator("#notices").inner_text()
        page.wait_for_timeout(1200)
        assert page.evaluate("() => window.inkdeus.build().id") == build["id"]         # 自動では再読み込みしない
        # 保存していない変更があれば、帯のボタンが先に保存してから再読み込みする
        page.evaluate("(p) => { window.inkdeus.setParams(p); }", LINES_OFF)
        with page.expect_navigation():
            page.click("#notices .notice-action")
        page.wait_for_function("window.inkdeus && window.inkdeus.ready")
        page.evaluate("() => window.inkdeus.ready")
        assert page.evaluate("() => window.inkdeus.build().id") == "next-build"
        assert page.evaluate("() => window.inkdeus.notices()") == {} and page.evaluate("() => window.inkdeus.getParams().lines.enabled") is False
    finally:
        page.close()


def test_a_page_newer_than_its_server_asks_to_restart_the_server(live, browser):
    """画面のファイルの方が新しい（この機能が入る前のサーバにつないだ）ときは、サーバの再起動を促し、新しい UI は出さない"""
    old = ["layerIds", "paint", "parts", "userStyles", "mangaPdf", "mangaCommand"]

    def as_old_server(page):
        page.route("**/api/config", lambda route: route.fulfill(json={"project": None, "features": old}))

    page = live["open"](prepare=as_old_server)
    assert page.evaluate("() => window.inkdeus.notices()") == {
        "server": "この画面は、起動中のサーバより新しい版です。サーバを再起動してください"}
    assert page.locator("#notices .notice").is_visible() and page.locator("#btnRestore").count() == 0
    status = page.evaluate("() => window.inkdeus.saveStatus()")
    assert status["tiles"] is False and page.evaluate("() => window.inkdeus.backups.ok()") is False
    # 古いサーバ相手でも、これまでどおり描いて保存できる（全体の PNG で保存する）
    set_params(page, MIX)
    wait_saved(page)
    methods = []
    page.on("request", lambda r: methods.append(r.method) if "/api/paint/" in r.url else None)
    eraser(page, "all")
    drag(page, ACROSS)
    page.wait_for_function("() => window.inkdeus.paint.state().onServer.includes('all')")
    wait_saved(page)
    assert methods == ["PUT"] and (mask_of(live["out"], "all")[BAND] == 255).all()


# ---- 壊れた設定ファイル・見つからない元画像の場所 ----

def test_a_broken_settings_file_is_not_overwritten_until_the_user_decides(live):
    page = live["open"]()
    set_params(page, LINES_OFF)
    wait_saved(page)
    set_params(page, deep_merge(LINES_OFF, {"layers": [layer(angle=30)]}))
    wait_saved(page)
    page.close()
    path = live["out"] / "mix.inkdeus.json"
    path.write_bytes(b"")                                          # 中断などで 0 バイトになった
    page = live["open"]()
    page.wait_for_selector("#brokenDialog[open]")
    assert "0 バイトか、JSON として読めません" in page.locator("#brokenDialog").inner_text()
    assert page.evaluate("() => window.inkdeus.broken().name") == "mix.png"
    page.click("#brokenDialog .chooser-cancel")                    # あとで決める: 何も保存されない
    assert "設定ファイルが壊れています" in page.evaluate("() => window.inkdeus.notices().broken")
    assert page.locator("#btnRestore").count() == 0
    page.evaluate("(p) => { window.inkdeus.setParams(p); }", MIX)
    page.wait_for_timeout(1500)
    assert path.read_bytes() == b"" and page.evaluate("() => window.inkdeus.saveStatus().state") == "none"
    # 帯のボタンからもう一度選べる。最新のバックアップから戻す
    page.click("#notices .notice-action")
    page.wait_for_selector("#brokenDialog[open]")
    buttons = page.evaluate("() => Array.from(document.querySelectorAll('#brokenDialog .chooser-buttons button')).map(b => b.textContent)")
    assert buttons[0].startswith("最新のバックアップから戻す（20") and buttons[1:] == ["既定値で始める", "あとで決める"]
    page.click("#brokenDialog button[data-value='restore']")
    page.wait_for_function("() => window.inkdeus.broken() === null && window.inkdeus.getParams().lines.enabled === false")
    assert json.loads(path.read_bytes())["lines"]["enabled"] is False          # 最後の上書きの前の版
    kept = list(live["out"].glob("mix.inkdeus.json.broken-*"))
    assert len(kept) == 1 and kept[0].read_bytes() == b"" and page.evaluate("() => window.inkdeus.notices()") == {}
    assert page.locator("#btnRestore").count() == 1
    # 既定値で始める
    page.close()
    path.write_bytes(b"{oops")
    page = live["open"]()
    page.wait_for_selector("#brokenDialog[open]")
    page.click("#brokenDialog button[data-value='reset']")
    page.wait_for_function("() => window.inkdeus.broken() === null")
    assert not path.exists() or json.loads(path.read_bytes())                  # 壊れた物は残っていない
    assert len(list(live["out"].glob("mix.inkdeus.json.broken-*"))) == 2


def test_missing_source_folder_is_shown_with_how_to_relink(tmp_path, browser):
    from inkdeus.projects import add_folder, create_project

    images, extra = tmp_path / "images", tmp_path / "extra"
    synth.save(images / "a.png", synth.flat(64, 64, 120))
    synth.save(extra / "b.png", synth.flat(64, 64, 60))
    project, _ = create_project("book", images, root=tmp_path / "projects")
    add_folder(project, "second", extra)
    import shutil

    shutil.rmtree(extra)
    app = create_app(project.source, None, project.out, project=project.info(), allow_missing=True,
                     local_config=tmp_path / "none.toml", config_dir=tmp_path / "config")
    server, url = serve(app)
    page = open_page(browser, url)
    try:
        assert page.evaluate("() => window.inkdeus.info().name") == "a.png"
        text = page.evaluate("() => window.inkdeus.notices().missing")
        assert text == "元画像の場所が見つかりません（フォルダ「second」）"
        assert "inkdeus relink book --folder second --images <dir>" in page.locator("#notices").inner_text()
        # 既定のフォルダが見つからないとき（画像は 0 枚。画面は落ちない）
        shutil.rmtree(images)
        page.reload()
        page.wait_for_function("window.inkdeus && window.inkdeus.ready")
        page.evaluate("() => window.inkdeus.ready")
        assert page.evaluate("() => window.inkdeus.notices().missing") == "元画像の場所が見つかりません（既定のフォルダ、フォルダ「second」）"
        assert "inkdeus relink book --images <dir>" in page.locator("#notices").inner_text()
        assert page.evaluate("() => window.inkdeus.images()") == []
    finally:
        page.close()
        server.shutdown()
