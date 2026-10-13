"""描画テスト用の共通部品: 合成画像のフォルダ、サーバ、ヘッドレス Chromium"""

from __future__ import annotations

import base64
import copy
import os
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent))

# 利用者の設定フォルダ（自分用スタイルの置き場）は、テストの間は一時フォルダへ向ける（実物を読まない・書かない）
os.environ["INKDEUS_CONFIG"] = tempfile.mkdtemp(prefix="inkdeus-test-config-")

import evidence  # noqa: E402
import synth  # noqa: E402
from inkdeus.server import create_app  # noqa: E402

# ヘッドレス環境では GPU が無いので、ソフトウェア描画（SwiftShader）で WebGL2 を動かす
CHROMIUM_ARGS = [
    "--use-gl=angle",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
    "--ignore-gpu-blocklist",
]


@dataclass
class Site:
    url: str
    images: Path
    masks: Path
    out: Path


def make_images(root: Path) -> tuple[Path, Path]:
    images, masks = root / "images", root / "masks"
    synth.save(images / "white.png", synth.flat(256, 256, 255))
    synth.save(images / "gray.png", synth.flat(256, 256, 110))
    synth.save(images / "dark.png", synth.flat(128, 128, 20))
    synth.save(images / "gradient.png", synth.gradient(768, 128))
    synth.save(images / "halves.png", synth.halves(256, 256))
    img, mask = synth.scene(640, 480)
    synth.save(images / "scene.png", img)
    synth.save(masks / "scene.png", mask)
    return images, masks


def serve(app):
    import logging

    from werkzeug.serving import make_server

    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    holder = {}
    evidence.watch_server(app, lambda: holder.get("url"))      # 失敗したときに、リクエストとイベントの列を残す
    server = make_server("127.0.0.1", 0, app, threaded=True)
    holder["url"] = f"http://127.0.0.1:{server.server_port}/"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/"


def _site(tmp_path_factory, label: str):
    root = tmp_path_factory.mktemp(label)
    images, masks = make_images(root)
    out = root / "out"
    server, url = serve(create_app(images, mask_dir=masks, out_dir=out))
    return server, Site(url, images, masks, out)


@pytest.fixture(scope="session")
def site(tmp_path_factory):
    """透かしまわりの検証用のサーバ（main_site とは出力先を分ける）"""
    server, s = _site(tmp_path_factory, "site")
    yield s
    server.shutdown()


@pytest.fixture(scope="session")
def main_site(tmp_path_factory):
    """描画の検証用のサーバ"""
    server, s = _site(tmp_path_factory, "main")
    yield s
    server.shutdown()


def browser_env() -> dict:
    """ヘッドレス Chromium に渡す環境変数。フォントの探し先を、OS に標準で入っている場所だけにする。

    漫画エディタ（上流）は起動のたびに、200 個ほどのフォント名を 1 つずつ描いて「入っているか」を調べる。
    利用者のフォント設定（fontconfig）に遅い置き場（ネットワーク越し・別 OS のフォントフォルダなど）が入っていると、
    1 つ調べるのに数十ミリ秒かかり、漫画の画面を開くたびに十数秒を使う（結合テストの時間の大半がこれだった）。
    テストの結果が、その PC に入っているフォントに左右されないようにする意味もある。
    INKDEUS_TEST_SYSTEM_FONTS=1 で、利用者のフォント設定をそのまま使う"""
    env = dict(os.environ)
    if os.environ.get("INKDEUS_TEST_SYSTEM_FONTS") == "1" or not sys.platform.startswith("linux"):
        return env
    folder = Path(tempfile.gettempdir()) / "inkdeus-test-fontconfig"
    conf = folder / "fonts.conf"
    text = ('<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n<fontconfig>\n'
            "  <dir>/usr/share/fonts</dir>\n  <dir>/usr/local/share/fonts</dir>\n"
            f"  <cachedir>{folder / 'cache'}</cachedir>\n</fontconfig>\n")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if not conf.is_file() or conf.read_text(encoding="utf-8") != text:
            tmp = folder / f"fonts.conf.{os.getpid()}.tmp"       # 並列のワーカーが同時に書いても、壊れた物を読ませない
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, conf)
    except OSError:
        return env
    env["FONTCONFIG_FILE"] = str(conf)
    return env


@pytest.fixture(scope="session")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        try:
            b = pw.chromium.launch(args=CHROMIUM_ARGS, env=browser_env())
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Chromium を起動できません（uv run playwright install chromium）: {e}")
        evidence.watch_browser(b)                 # 作られる全ページのコンソール・エラー・通信の失敗を覚えておく
        yield b
        b.close()


# ---- 印（範囲を絞って回すための目印）と、並列実行の割り振り ----
# ui     ブラウザ（ヘッドレス Chromium）を使うテスト。fixture から自動で付く
# server ブラウザを使わないテスト（サーバ・コマンド・純粋な関数）。自動で付く
# manga  実物の漫画エディタ（上流のクローン）が要る結合テスト。各ファイルの pytestmark で付ける
# slow   1 本で何十秒もかかる物（大きな画像など）。各テストに付ける
#
# 並列（pytest-xdist）では、同じファイルのテストを同じワーカーで、書いた順に回す（--dist loadgroup）。
# モジュール単位の fixture（漫画の画面・サーバ）を共有し、前のテストの結果を使うテストがあるため。
# ファイルが XDIST_SPLIT_PARAM = "<fixture 名>" を持つときは、その fixture の値ごとに別の組にする。

@pytest.hookimpl(tryfirst=True)       # xdist が組の名前を読む（同じ名前のフック）より先に、印を付けておく
def pytest_collection_modifyitems(config, items):
    for item in items:
        names = set(getattr(item, "fixturenames", ()))
        item.add_marker(pytest.mark.ui if "browser" in names else pytest.mark.server)
        group = Path(str(item.fspath)).stem
        split = getattr(item.module, "XDIST_SPLIT_PARAM", None)
        callspec = getattr(item, "callspec", None)
        if split and callspec is not None and split in callspec.params:
            group += f"[{callspec.params[split]}]"
        item.add_marker(pytest.mark.xdist_group(group))


def pytest_xdist_auto_num_workers(config):
    """-n auto のワーカー数: CPU のコア数の 1/4（2〜6。2 コア以下なら 1）。INKDEUS_TEST_WORKERS で変えられる。
    ソフトウェア描画の WebGL は、ワーカー 1 つで CPU を 3 個ほど使う。16 コアの PC での全件は、4 ワーカーで約 6 分・
    6 ワーカーで約 5 分半（コアを使い切り、ほかの作業が重くなる。それ以上増やしても、いちばん長いファイルの時間より短くはならない）"""
    given = os.environ.get("INKDEUS_TEST_WORKERS", "")
    if given.isdigit() and int(given) > 0:
        return int(given)
    cores = os.cpu_count() or 2
    return 1 if cores <= 2 else max(2, min(6, cores // 4))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """落ちた結合テストの証拠（両タブのログ・状態・スクリーンショット、サーバのリクエストとイベントの列）を残す。
    フィクスチャの片付けの前に呼ばれるので、ページはまだ開いている"""
    outcome = yield
    report = outcome.get_result()
    if report.when == "teardown" or report.skipped:
        return
    always = os.environ.get("INKDEUS_TEST_EVIDENCE_ALWAYS") == "1" and report.when == "call"
    if not (report.failed or always) or "browser" not in getattr(item, "fixturenames", ()):
        return
    folder = evidence.dump(item.nodeid, report.when, report.longreprtext if report.failed else "")
    if folder is not None and report.failed:
        report.sections.append(("inkdeus evidence", f"失敗時の記録: {folder}"))


def open_page(browser, url: str, auto_export: bool = False, prepare=None):
    """白黒化の画面を開く。自動書き出しは、それを確かめるテスト以外では切っておく
    （描画の検証の途中で、頼んでいない書き出しが走らないように）。
    prepare(page) は、読み込む前にページへ手を入れるための物（通信の差し替えなど）"""
    page = browser.new_page(viewport={"width": 1200, "height": 800})
    page.set_default_timeout(600_000)
    if prepare is not None:
        prepare(page)
    if not auto_export:
        page.add_init_script("try { if (localStorage.getItem('inkdeus.autoExport') === null) "
                             "localStorage.setItem('inkdeus.autoExport', '0'); } catch (e) {}")
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(url)
    page.wait_for_function("window.inkdeus && window.inkdeus.ready")
    page.evaluate("() => window.inkdeus.ready")
    page.errors = errors
    return page


@pytest.fixture()
def page(browser, main_site):
    """描画の検証用のページ"""
    p = open_page(browser, main_site.url)
    yield p
    p.close()


@pytest.fixture()
def plain_page(browser, site):
    """site（透かしまわりの検証用）のサーバにつないだページ"""
    p = open_page(browser, site.url)
    yield p
    p.close()


def _merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def deep_merge(base: dict, patch: dict) -> dict:
    """設定に差分を重ねる。テストを短く書くための省略形として、差分のトップレベルの
    layers / solid / levels は人物（regions.person）、background は背景（regions.background）に入れる"""
    patch = copy.deepcopy(patch)
    regions = patch.setdefault("regions", {})
    for key in ("layers", "solid", "levels"):
        if key in patch:
            regions.setdefault("person", {})[key] = patch.pop(key)
    if "background" in patch:
        regions["background"] = _merge(regions.get("background", {}), patch.pop("background"))
    return _merge(base, patch)


# 線画・ベタ・トーン・透かしをすべて切った「何も描かない」設定（テストはここへ必要な物だけ足す）
BLANK = {
    "version": 2,
    "unit": 1,
    "input": {"autoLevel": False, "smoothRadius": 0},
    "lines": {"enabled": False},
    "regions": {
        "person": {"solid": {"enabled": False}, "layers": []},
        "background": {"mode": "keep", "linked": True},
    },
    "watermark": {"enabled": False},
    "output": {"binary": False},
}


def no_ids(value):
    """層の id（乱数）を除いた写し。設定どうしを「id 以外が同じか」で比べるときに使う"""
    if isinstance(value, dict):
        return {k: no_ids(v) for k, v in value.items() if k != "id"}
    if isinstance(value, list):
        return [no_ids(v) for v in value]
    return value


def layer(**kw) -> dict:
    base = {"type": "hatch", "enabled": True, "threshold": 0.5, "soft": 0.0, "angle": 45,
            "spacing": 2.0, "width": 0.5, "widthByDarkness": 0.0, "opacity": 1.0, "phase": 0.0}
    base.update(kw)
    return base


def open_image(page, name: str) -> dict:
    return page.evaluate("(name) => window.inkdeus.open(name)", name)


def render(page, params: dict) -> np.ndarray:
    """設定を適用して描画し、出力（高さ×幅の uint8）を返す"""
    page.evaluate("(p) => window.inkdeus.setParams(p)", params)
    return pixels(page)


def pixels(page) -> np.ndarray:
    info = page.evaluate("() => window.inkdeus.info()")
    b64 = page.evaluate("() => window.inkdeus.readPixelsBase64()")
    data = np.frombuffer(base64.b64decode(b64), np.uint8)
    return data.reshape(info["height"], info["width"]).copy()


def ink(img: np.ndarray) -> np.ndarray:
    """インク量（0=白、1=黒）"""
    return 1.0 - img.astype(np.float64) / 255.0
