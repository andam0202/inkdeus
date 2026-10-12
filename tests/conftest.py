"""描画テスト用の共通部品: 合成画像のフォルダ、サーバ、ヘッドレス Chromium"""

from __future__ import annotations

import base64
import copy
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent))

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

    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/"


def _site(tmp_path_factory, owner: bool):
    root = tmp_path_factory.mktemp("owner" if owner else "site")
    images, masks = make_images(root)
    out = root / "out"
    server, url = serve(create_app(images, mask_dir=masks, out_dir=out, owner=owner))
    return server, Site(url, images, masks, out)


@pytest.fixture(scope="session")
def site(tmp_path_factory):
    server, s = _site(tmp_path_factory, owner=False)
    yield s
    server.shutdown()


@pytest.fixture(scope="session")
def owner_site(tmp_path_factory):
    server, s = _site(tmp_path_factory, owner=True)
    yield s
    server.shutdown()


@pytest.fixture(scope="session")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        try:
            b = pw.chromium.launch(args=CHROMIUM_ARGS)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Chromium を起動できません（uv run playwright install chromium）: {e}")
        yield b
        b.close()


def open_page(browser, url: str):
    page = browser.new_page(viewport={"width": 1200, "height": 800})
    page.set_default_timeout(600_000)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(url)
    page.wait_for_function("window.inkdeus && window.inkdeus.ready")
    page.evaluate("() => window.inkdeus.ready")
    page.errors = errors
    return page


@pytest.fixture()
def page(browser, owner_site):
    """描画の検証用。透かしを切れるよう所有者モードのサーバにつなぐ"""
    p = open_page(browser, owner_site.url)
    yield p
    p.close()


@pytest.fixture()
def plain_page(browser, site):
    """既定（所有者モードではない）サーバにつないだページ"""
    p = open_page(browser, site.url)
    yield p
    p.close()


def deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# 線画・ベタ・トーン・透かしをすべて切った「何も描かない」設定（テストはここへ必要な物だけ足す）
BLANK = {
    "version": 1,
    "unit": 1,
    "input": {"autoLevel": False, "smoothRadius": 0},
    "lines": {"enabled": False},
    "solid": {"enabled": False},
    "layers": [],
    "watermark": {"enabled": False},
    "output": {"binary": False},
}


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
