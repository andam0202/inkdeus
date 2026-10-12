"""操作用 CLI（inkdeus ctl …）: 実際にサーバを立てて HTTP で話す"""

from __future__ import annotations

import io
import json
import threading
import urllib.request

import pytest
from PIL import Image

import synth
from conftest import serve
from inkdeus import cli
from inkdeus.server import create_app


@pytest.fixture()
def live(tmp_path):
    images, masks = tmp_path / "images", tmp_path / "masks"
    img, mask = synth.scene(64, 48)
    synth.save(images / "scene.png", img)
    synth.save(masks / "scene.png", mask)
    synth.save(images / "plain.png", synth.flat(32, 16, 255))
    project = {"name": "demo", "path": str(tmp_path)}
    server, url = serve(create_app(images, mask_dir=masks, out_dir=tmp_path / "out", project=project))
    yield {"port": server.server_port, "url": url, "images": images, "out": tmp_path / "out"}
    server.shutdown()


@pytest.fixture()
def ctl(live, capsys):
    def run(*args, expect=0):
        capsys.readouterr()
        code = cli.main(["ctl", "--port", str(live["port"]), *args])
        captured = capsys.readouterr()
        assert code == expect, captured.err or captured.out
        return json.loads(captured.out if code == 0 else captured.err)
    return run


class FakeBrowser(threading.Thread):
    """ブラウザの代役: イベントを受けて、統計・状態・書き出しをサーバへ返す"""

    def __init__(self, url: str, current: str):
        super().__init__(daemon=True)
        self.url, self.current = url, current
        self.seen: list[dict] = []
        self.ready = threading.Event()
        self.stream = None

    def post(self, path, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.url + path, data=data, method="POST",
                                     headers={"Content-Type": content_type})
        urllib.request.urlopen(req, timeout=10).read()

    def run(self):
        self.post("api/state", {"current": self.current})
        self.stream = urllib.request.urlopen(self.url + "api/events", timeout=60)
        try:
            for raw in self.stream:
                line = raw.decode("utf-8").strip()
                if line.startswith(":"):
                    self.ready.set()
                if not line.startswith("data: "):
                    continue
                ev = json.loads(line[6:])
                self.seen.append(ev)
                if ev["type"] == "params" and ev["name"] == self.current:
                    self.post(f"api/stats/{ev['name']}", {"rev": ev["rev"], "output": {"all": {"blackRatio": 0.5}}})
                elif ev["type"] == "open":
                    self.current = ev["name"]
                    self.post("api/state", {"current": self.current})
                elif ev["type"] == "export":
                    buf = io.BytesIO()
                    Image.new("L", (4, 4), 200).save(buf, "PNG")
                    self.post(f"api/export/{ev['name']}", buf.getvalue(), "image/png")
        except (OSError, ValueError, AttributeError):       # 接続を閉じられたら終わる
            pass

    def close(self):
        if self.stream is not None:
            self.stream.close()


@pytest.fixture()
def browser(live):
    b = FakeBrowser(live["url"], "scene.png")
    b.start()
    assert b.ready.wait(10)
    yield b
    b.close()


def test_state_includes_project_and_folders(ctl, live):
    s = ctl("state")
    assert s["project"]["name"] == "demo" and s["current"] is None and s["images"] == 2
    assert s["source"] == str(live["images"]) and s["out"] == str(live["out"]) and s["masks"].endswith("masks")
    assert s["clients"] == 0


def test_name_is_required_without_browser(ctl):
    err = ctl("get", expect=1)
    assert "--name" in err["error"] and set(err["images"]) == {"scene.png", "plain.png"}
    assert ctl("get", "--name", "nope.png", expect=1)["error"]


def test_get_patch_set(ctl, live):
    got = ctl("get", "--name", "scene.png")
    assert got["exists"] is False and got["params"]["regions"]["background"]["linked"] is True
    r = ctl("patch", '{"regions": {"background": {"linked": false, "levels": {"brightness": 0.25}}}}',
            "--name", "scene.png")
    assert r["name"] == "scene.png" and r["rev"] and "params" not in r
    r2 = ctl("set", "regions.background.levels.gamma", "1.4", "--name", "scene.png")
    assert r2["rev"] != r["rev"]
    ctl("set", "regions.background.mode", "keep", "--name", "scene.png")       # JSON でない値は文字列
    ctl("set", "regions.person.layers.0.enabled", "false", "--name", "scene.png")
    got = ctl("get", "--name", "scene.png")
    bg = got["params"]["regions"]["background"]
    assert got["exists"] is True and bg["levels"] == {"brightness": 0.25, "contrast": 1, "gamma": 1.4}
    assert got["params"]["regions"]["person"]["layers"][0]["enabled"] is False
    # ブラウザが開いていなくても設定ファイルは更新されている
    saved = json.loads((live["out"] / "scene.inkdeus.json").read_text(encoding="utf-8"))
    assert saved == got["params"]


def test_validation_errors_exit_nonzero_and_change_nothing(ctl):
    rev = ctl("patch", "{}", "--name", "scene.png")["rev"]
    err = ctl("patch", '{"lines": {"sigma": 50}, "typo": 1}', "--name", "scene.png", expect=1)
    assert any("lines.sigma" in p for p in err["problems"]) and any("typo" in p for p in err["problems"])
    assert ctl("patch", "{not json", "--name", "scene.png", expect=1)["error"]
    assert ctl("patch", "[1]", "--name", "scene.png", expect=1)["error"]
    assert ctl("set", "regions.person.layers.7.angle", "10", "--name", "scene.png", expect=1)["problems"]
    assert ctl("set", "lines.strength", "2", "--name", "scene.png", expect=1)["problems"]
    assert ctl("preset", "nope", "--name", "scene.png", expect=1)["problems"]
    assert ctl("get", "--name", "scene.png")["rev"] == rev


def test_patch_replaces_layer_arrays(ctl):
    r = ctl("patch", '{"regions": {"person": {"layers": [{"type": "hatch", "angle": 30}]}}}', "--name", "scene.png")
    assert r["rev"]
    layers = ctl("get", "--name", "scene.png")["params"]["regions"]["person"]["layers"]
    assert len(layers) == 1 and layers[0]["angle"] == 30 and layers[0]["spacing"] == 2.0


def test_presets_and_aliases(ctl):
    listed = ctl("presets")
    by_name = {s["name"]: s for s in listed["styles"]}
    assert "クロスハッチ極細" in by_name["crosshatch-ultrafine"]["aliases"] and by_name["hatch1"]["description"]
    r = ctl("preset", "ハッチ(1)", "--region", "background", "--name", "scene.png")
    assert r["style"] == "hatch1" and r["region"] == "background"
    r = ctl("preset", "クロスハッチ極細", "--name", "scene.png")
    assert r["style"] == "crosshatch-ultrafine" and r["region"] == "person"
    regions = ctl("get", "--name", "scene.png")["params"]["regions"]
    assert len(regions["background"]["layers"]) == 1 and regions["background"]["linked"] is False
    assert [l["spacing"] for l in regions["person"]["layers"]] == [1.0] * 4
    ctl("preset", "white", "--region", "both", "--name", "scene.png")
    regions = ctl("get", "--name", "scene.png")["params"]["regions"]
    assert regions["background"]["mode"] == "white" and regions["person"]["layers"] == []


def test_stats_without_browser(ctl):
    ctl("patch", "{}", "--name", "scene.png")
    r = ctl("stats", "--name", "scene.png")
    assert r["stats"] is None and r["fresh"] is False
    err = ctl("stats", "--wait", "--name", "scene.png", expect=2)
    assert "ブラウザが開いていない" in err["error"]
    assert "ブラウザが開いていない" in ctl("open", "plain.png", expect=2)["error"]
    assert "ブラウザが開いていない" in ctl("export", "--name", "scene.png", expect=2)["error"]


def test_stats_wait_gets_stats_for_the_new_rev(ctl, browser):
    assert ctl("state")["current"] == "scene.png" and ctl("state")["clients"] == 1
    rev = ctl("set", "lines.sigma", "1.2")["rev"]            # --name なし = 開いている画像
    r = ctl("stats", "--wait", "--timeout", "10")
    assert r["fresh"] is True and r["stats"]["rev"] == rev and r["currentRev"] == rev
    rev2 = ctl("preset", "hatch1", "--region", "background")["rev"]
    assert ctl("stats", "--wait", "--timeout", "10")["stats"]["rev"] == rev2
    # 開いていない画像の統計は待っても届かないので、すぐそう伝える
    ctl("patch", "{}", "--name", "plain.png")
    assert "scene.png" in ctl("stats", "--wait", "--name", "plain.png", expect=2)["error"]


def test_open_and_export_through_the_browser(ctl, browser, live):
    assert ctl("open", "plain.png", "--timeout", "10")["current"] == "plain.png"
    r = ctl("export", "--timeout", "10")
    assert r["name"] == "plain.png" and r["path"] == str(live["out"] / "plain.png") and r["seq"] == 1
    assert (live["out"] / "plain.png").is_file()
    assert ctl("export", "--name", "scene.png", "--timeout", "10")["path"].endswith("scene.png")


def test_stats_wait_times_out(ctl, live):
    silent = FakeBrowser(live["url"], "plain.png")           # scene.png の統計は返さない代役
    silent.start()
    assert silent.ready.wait(10)
    try:
        ctl("open", "scene.png", "--timeout", "10")
        silent.current = "plain.png"                          # 以後の params イベントには応えない
        ctl("patch", '{"unit": 2}', "--name", "scene.png")
        err = ctl("stats", "--wait", "--timeout", "1", "--name", "scene.png", expect=3)
        assert "届きません" in err["error"]
    finally:
        silent.close()


def test_port_and_host_before_or_after_the_subcommand(live, capsys):
    """--port / --host は、サブコマンドの前でも後ろでも同じ意味"""
    port = str(live["port"])

    def run(*argv):
        capsys.readouterr()
        code = cli.main(["ctl", *argv])
        captured = capsys.readouterr()
        return code, json.loads(captured.out if code == 0 else captured.err)

    expected = run("--port", port, "state")
    assert expected[0] == 0 and expected[1]["project"]["name"] == "demo"
    assert run("state", "--port", port) == expected
    assert run("--host", "127.0.0.1", "state", "--port", port) == expected
    assert run("state", "--host", "127.0.0.1", "--port", port) == expected
    assert run("--port", "1", "state", "--port", port) == expected          # 両方に書いたら後ろが勝つ
    # 引数を取るサブコマンドでも、どの位置でもよい
    assert run("get", "--name", "scene.png", "--port", port)[0] == 0
    assert run("set", "--port", port, "--name", "scene.png", "lines.sigma", "1.2")[0] == 0
    assert run("--port", port, "get", "--name", "scene.png")[1]["params"]["lines"]["sigma"] == 1.2
    assert run("set", "lines.sigma", "1.3", "--name", "scene.png", "--port", port)[0] == 0
    assert run("get", "--port", port, "--name", "scene.png")[1]["params"]["lines"]["sigma"] == 1.3


def test_port_and_host_defaults():
    from inkdeus.ctl import build_parser

    for argv in (["state"], ["stats", "--wait"]):
        args = build_parser().parse_args(argv)
        assert (args.host, args.port) == ("127.0.0.1", 8770)
    args = build_parser().parse_args(["--port", "8771", "state"])            # 前に書いた値を既定値で潰さない
    assert (args.host, args.port) == ("127.0.0.1", 8771)
    args = build_parser().parse_args(["--host", "localhost", "state", "--port", "8772"])
    assert (args.host, args.port) == ("localhost", 8772)


def test_connection_error_is_reported(capsys):
    code = cli.main(["ctl", "--port", "1", "state"])
    assert code == 1 and "接続できません" in json.loads(capsys.readouterr().err)["error"]


def test_help_mentions_array_replacement(capsys):
    with pytest.raises(SystemExit):
        cli.main(["ctl", "patch", "--help"])
    out = capsys.readouterr().out
    assert "layers" in out and "丸ごと置換" in out
