"""直リンク（/?image=）・自動書き出し・全画像の書き出し（実際のブラウザで確かめる）"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from urllib.parse import quote

import numpy as np
import pytest
from PIL import Image

import synth
from conftest import BLANK, deep_merge, open_page, serve
from inkdeus import cli
from inkdeus.server import create_app

# 何も描かない設定（出力は真っ白）と、灰色の画像が真っ黒になる設定
WHITE = BLANK
BLACK = deep_merge(BLANK, {"solid": {"enabled": True, "threshold": 0.9}})


@pytest.fixture()
def live(tmp_path, browser):
    images = tmp_path / "images"
    for name in ("a.png", "b.png", "c.png"):
        synth.save(images / name, synth.flat(96, 64, 110))
    out = tmp_path / "out"
    server, url = serve(create_app(images, out_dir=out, project={"name": "本", "path": str(tmp_path)}))
    pages = []

    def open_(query: str = "", auto_export: bool = False):
        page = open_page(browser, url + query, auto_export=auto_export)
        pages.append(page)
        return page

    yield {"open": open_, "url": url, "port": server.server_port, "out": out, "images": images}
    for page in pages:
        page.close()
    server.shutdown()


def api(live, path, data: bytes | None = None, method: str | None = None):
    req = urllib.request.Request(live["url"] + path, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def put_params(live, name: str, params: dict) -> None:
    api(live, f"api/params/{name}", json.dumps(params).encode("utf-8"), "PUT")


def mean(path) -> float:
    return float(np.asarray(Image.open(path).convert("L")).mean())


class Events(threading.Thread):
    """/api/events を聞いて、届いたイベントをためる"""

    def __init__(self, url: str, query: str = ""):
        super().__init__(daemon=True)
        self.seen: list[dict] = []
        self.ready = threading.Event()
        self.stream = urllib.request.urlopen(url + "api/events" + query, timeout=120)
        self.start()
        assert self.ready.wait(10)

    def run(self):
        try:
            for raw in self.stream:
                line = raw.decode("utf-8").strip()
                if line.startswith(":"):
                    self.ready.set()
                if line.startswith("data: "):
                    self.seen.append(json.loads(line[6:]))
        except (OSError, ValueError, AttributeError):
            pass

    def of(self, kind: str) -> list[dict]:
        return [e for e in self.seen if e["type"] == kind]

    def close(self):
        self.stream.close()


def wait_for(predicate, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("時間内に条件を満たしませんでした")


# ---- 直リンク ----

def test_direct_link_opens_the_named_image(live):
    page = live["open"]("?image=" + quote("b.png"))
    assert page.evaluate("() => window.inkdeus.info().name") == "b.png"
    assert page.evaluate("() => document.getElementById('selImage').value") == "b.png"
    assert api(live, "api/state")["current"] == "b.png"
    assert page.errors == []
    # 存在しない名前なら、ふつうに起動して知らせる
    other = live["open"]("?image=nope.png")
    assert other.evaluate("() => window.inkdeus.info().name") == "a.png"
    assert "指定の画像が見つかりません" in other.evaluate("() => document.getElementById('messages').textContent")


# ---- 自動書き出し ----

def test_auto_export_after_changes_settle(live):
    put_params(live, "a.png", WHITE)
    events = Events(live["url"], "?role=manga")
    try:
        page = live["open"](auto_export=True)
        status = page.evaluate("() => window.inkdeus.autoExport()")
        assert status["enabled"] is True
        assert page.evaluate("() => document.getElementById('chkAutoExport').checked") is True
        # 白黒版がまだ無い画像は、開いた時点で書き出される
        png = live["out"] / "a.png"
        wait_for(png.is_file)
        page.wait_for_function("() => window.inkdeus.autoExport().label === '書き出し済み'")
        assert mean(png) > 250
        first = wait_for(lambda: events.of("exported"))[0]
        assert first["name"] == "a.png" and first["file"] == "a.png" and first["rev"] and first["mtime"] > 0
        assert first["version"] == png.stat().st_mtime_ns // 1_000_000
        assert api(live, "api/state")["clients"] == 1                # 聞くだけの相手は「開いているブラウザ」に数えない

        # 設定を続けて変えても、書き出しは溜まらない（落ち着いてから最新の設定で1回）
        before = page.evaluate("() => window.inkdeus.autoExport().count")
        for threshold in (0.2, 0.5, 0.9):
            page.evaluate("(p) => { window.inkdeus.setParams(p); }",
                          deep_merge(BLANK, {"solid": {"enabled": True, "threshold": threshold}}))
            page.wait_for_timeout(200)
        assert page.evaluate("() => window.inkdeus.autoExport().label") == "未反映の変更あり"
        assert page.evaluate("() => window.inkdeus.autoExport().count") == before        # まだ書き出していない
        page.wait_for_function("() => window.inkdeus.autoExport().label === '書き出し済み'")
        assert page.evaluate("() => window.inkdeus.autoExport().count") == before + 1
        assert mean(png) < 5                                                             # 最新の設定（真っ黒）
        assert len(events.of("exported")) == 2
        assert events.of("exported")[-1]["rev"] == page.evaluate("() => window.inkdeus.rev()")
        listed = {i["name"]: i for i in api(live, "api/images")["images"]}
        assert listed["a.png"]["hasExport"] and listed["a.png"]["exportFresh"]
        assert not listed["b.png"]["hasExport"] and not listed["b.png"]["exportFresh"]

        # 外部（ctl）からの設定変更でも書き出される
        assert cli.main(["ctl", "--port", str(live["port"]), "patch", json.dumps({"regions": {"person": {"solid": {
            "enabled": False}}}})]) == 0
        wait_for(lambda: len(events.of("exported")) == 3)
        assert mean(png) > 250

        # スイッチを切ると止まり、「未反映の変更あり」と出る
        page.evaluate("() => document.getElementById('chkAutoExport').click()")
        assert page.evaluate("() => window.inkdeus.autoExport().enabled") is False
        page.evaluate("(p) => { window.inkdeus.setParams(p); }", BLACK)
        page.wait_for_timeout(2600)
        assert len(events.of("exported")) == 3 and mean(png) > 250
        assert page.evaluate("() => window.inkdeus.autoExport().label") == "未反映の変更あり"
        assert page.evaluate("() => localStorage.getItem('inkdeus.autoExport')") == "0"   # 次回も覚えている
        # 手動の書き出しは従来どおり
        page.evaluate("() => window.inkdeus.exportPNG()")
        assert mean(png) < 5 and len(events.of("exported")) == 4
        assert page.evaluate("() => window.inkdeus.autoExport().label") == "書き出し済み"
        # 入れ直すと、たまっている変更が書き出される
        page.evaluate("(p) => { window.inkdeus.setParams(p); }", WHITE)
        page.evaluate("() => document.getElementById('chkAutoExport').click()")
        wait_for(lambda: len(events.of("exported")) == 5)
        assert mean(png) > 250
        assert page.errors == []
    finally:
        events.close()


def test_auto_export_runs_before_leaving_the_image(live):
    for name in ("a.png", "b.png"):
        put_params(live, name, WHITE)
    page = live["open"](auto_export=True)
    wait_for((live["out"] / "a.png").is_file)
    page.wait_for_function("() => window.inkdeus.autoExport().label === '書き出し済み'")
    page.evaluate("(p) => { window.inkdeus.setParams(p); }", BLACK)
    page.evaluate("() => window.inkdeus.open('b.png')")               # 待ち時間の途中で別の画像へ
    assert mean(live["out"] / "a.png") < 5                            # 離れる前に書き出してある
    wait_for((live["out"] / "b.png").is_file)
    assert mean(live["out"] / "b.png") > 250


# ---- 全画像の書き出し ----

def test_export_all_uses_saved_settings_and_skips_fresh_ones(live):
    put_params(live, "b.png", BLACK)                                  # b だけ設定が保存済み
    page = live["open"]()
    page.evaluate("(p) => { window.inkdeus.setParams(p); }", WHITE)   # いま画面に出ている設定（a の設定として保存される）
    page.evaluate("() => window.inkdeus.save()")
    assert page.evaluate("() => !!document.getElementById('btnExportAll')")
    result = page.evaluate("() => window.inkdeus.exportAll()")
    assert (result["exported"], result["skipped"], result["failed"], result["total"]) == (3, 0, 0, 3)
    assert result["running"] is False and result["aborted"] is False
    out = live["out"]
    assert mean(out / "a.png") > 250 and mean(out / "c.png") > 250    # 設定の無かった画像は画面の設定で
    assert mean(out / "b.png") < 5                                    # 設定のある画像はその設定で
    saved = json.loads((out / "c.inkdeus.json").read_text(encoding="utf-8"))
    assert saved["regions"]["person"]["solid"]["enabled"] is False    # その設定が c の設定として保存される
    assert json.loads((out / "b.inkdeus.json").read_text(encoding="utf-8"))["regions"]["person"]["solid"]["threshold"] == 0.9
    assert page.evaluate("() => window.inkdeus.info().name") == "a.png"            # 始める前の画像へ戻る
    assert "書き出し 3 / 飛ばし 0 / 失敗 0" in page.evaluate("() => document.getElementById('batchInfo').textContent")
    assert page.evaluate("() => document.getElementById('batchBar').hidden") is True
    state = api(live, "api/state")["batch"]
    assert state["running"] is False and state["exported"] == 3 and state["seq"] == 1

    # もう一度: すべて書き出し済みなので飛ばす
    stamps = {n: (out / n).stat().st_mtime_ns for n in ("a.png", "b.png", "c.png")}
    result = page.evaluate("() => window.inkdeus.exportAll()")
    assert (result["exported"], result["skipped"]) == (0, 3)
    assert {n: (out / n).stat().st_mtime_ns for n in stamps} == stamps
    # 設定を変えた画像だけ書き出す
    assert cli.main(["ctl", "--port", str(live["port"]), "set", "--name", "c.png",
                     "regions.person.solid", json.dumps({"enabled": True, "threshold": 0.9, "soft": 0})]) == 0
    result = page.evaluate("() => window.inkdeus.exportAll()")
    assert (result["exported"], result["skipped"]) == (1, 2) and mean(out / "c.png") < 5
    assert (out / "a.png").stat().st_mtime_ns == stamps["a.png"]
    # 「すべて作り直す」
    page.evaluate("() => document.getElementById('chkExportAllForce').click()")
    page.evaluate("() => document.getElementById('btnExportAll').click()")
    page.wait_for_function("() => window.inkdeus.batch().running === false && window.inkdeus.batch().done === 3")
    assert page.evaluate("() => window.inkdeus.batch().exported") == 3
    assert (out / "a.png").stat().st_mtime_ns > stamps["a.png"]
    assert page.errors == []


def test_export_all_can_be_aborted_and_frees_textures(live):
    page = live["open"]()
    textures = page.evaluate("""() => { const gl = document.getElementById('gl').getContext('webgl2');
        let n = 0; const make = gl.createTexture, del = gl.deleteTexture;
        gl.createTexture = function () { n += 1; return make.call(this); };
        gl.deleteTexture = function (t) { if (t) n -= 1; return del.call(this, t); };
        window.__liveTextures = () => n; return n; }""")
    assert textures == 0
    # 1枚目が終わったところで中止する
    page.evaluate("""() => { const t = setInterval(() => { if (window.inkdeus.batch().done >= 1) {
        clearInterval(t); document.getElementById('btnBatchAbort').click(); } }, 5); }""")
    result = page.evaluate("() => window.inkdeus.exportAll({force: true})")
    assert result["aborted"] is True and 1 <= result["done"] < 3 and result["exported"] == result["done"]
    assert "中止しました" in page.evaluate("() => document.getElementById('batchInfo').textContent")
    assert not (live["out"] / "c.png").exists()
    # 何枚回しても、確保したままのテクスチャは増えない（画像ごとに解放している）
    page.evaluate("() => window.inkdeus.exportAll({force: true})")
    one = page.evaluate("() => window.__liveTextures()")
    for _ in range(3):
        page.evaluate("() => window.inkdeus.exportAll({force: true})")
    assert page.evaluate("() => window.__liveTextures()") == one
    assert page.errors == []


def test_ctl_export_all(live, capsys):
    def ctl(*args, expect=0):
        capsys.readouterr()
        code = cli.main(["ctl", "--port", str(live["port"]), "export-all", *args])
        captured = capsys.readouterr()
        assert code == expect, captured.err or captured.out
        return json.loads(captured.out if code == 0 else captured.err)

    assert "ブラウザが開いていない" in ctl("--timeout", "5", expect=2)["error"]
    assert ctl("--status")["running"] is False
    page = live["open"]()
    events = Events(live["url"], "?role=manga")                       # 漫画の構成の画面が開いていても指示は1回だけ届く
    try:
        result = ctl("--wait", "--timeout", "120")
        assert (result["exported"], result["skipped"], result["failed"], result["running"]) == (3, 0, 0, False)
        assert all((live["out"] / n).is_file() for n in ("a.png", "b.png", "c.png"))
        assert sorted(e["name"] for e in events.of("exported")) == ["a.png", "b.png", "c.png"]
        assert ctl("--wait", "--timeout", "120")["skipped"] == 3
        assert ctl("--wait", "--force", "--timeout", "120")["exported"] == 3
        status = ctl("--status")
        assert status["seq"] == 3 and status["done"] == 3 and status["force"] is True
        assert api(live, "api/state")["batch"]["seq"] == 3
        started = ctl("--force", "--timeout", "30")                    # --wait なしは、始まったところで返る
        assert started["seq"] == 4
        ctl("--abort")
        page.wait_for_function("() => window.inkdeus.batch().running === false")
        assert page.errors == []
    finally:
        events.close()
