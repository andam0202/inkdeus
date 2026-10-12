"""白黒化の画面でのバージョン（実際のブラウザで確かめる）と、ctl からの操作"""

from __future__ import annotations

import json
from urllib.parse import quote

import synth
from conftest import open_page
from test_ctl import browser as fake_browser  # noqa: F401 — ブラウザの代役（ctl のテスト用）
from test_ctl import ctl, live as ctl_live  # noqa: F401
from test_export_flow import BLACK, WHITE, Events, api, live, mean, put_params, wait_for  # noqa: F401


def state_of(page) -> dict:
    return page.evaluate("() => ({version: window.inkdeus.version(), params: window.inkdeus.getParams(),"
                         " names: window.inkdeus.versions().map((v) => v.name),"
                         " select: document.getElementById('selVersion').value,"
                         " history: window.inkdeus.historyInfo()})")


def test_new_version_exports_its_own_png(live):
    put_params(live, "a.png", WHITE)
    page = live["open"]("?image=a.png", auto_export=True)
    out = live["out"]
    wait_for((out / "a.png").is_file)
    assert page.evaluate("() => document.getElementById('verBox').hidden") is False
    bar = page.evaluate("() => { const t = document.getElementById('topbar');"
                        " return [t.scrollWidth <= t.clientWidth, t.getBoundingClientRect().height]; }")
    assert bar[0] and bar[1] < 48                                      # ヘッダは1行に収まる
    assert state_of(page)["names"] == [""] and state_of(page)["select"] == ""
    # いまの設定を複製して新しいバージョンを作ると、そのバージョンが開く
    page.evaluate("() => window.inkdeus.createVersion('黒')")
    s = state_of(page)
    assert s["version"] == "黒" and s["names"] == ["", "黒"] and s["select"] == "黒"
    copied = json.loads((out / "a@黒.inkdeus.json").read_text(encoding="utf-8"))
    assert copied == s["params"] and copied["lines"]["enabled"] is False        # いまの設定の複製
    assert api(live, "api/state")["currentVersion"] == "黒"
    assert "@黒" in page.title()
    wait_for((out / "a@黒.png").is_file)                               # 開いた時点で書き出される
    assert mean(out / "a@黒.png") > 250
    # 設定を変えると、このバージョンの PNG だけが変わる
    before = (out / "a.png").stat().st_mtime_ns
    page.evaluate("(p) => window.inkdeus.setParams(p)", BLACK)
    wait_for(lambda: mean(out / "a@黒.png") < 5)
    page.wait_for_function("() => window.inkdeus.autoExport().label === '書き出し済み'")
    assert mean(out / "a.png") > 250 and (out / "a.png").stat().st_mtime_ns == before
    rows = {v["name"]: v for v in api(live, "api/versions/a.png")["versions"]}
    assert rows["黒"]["hasExport"] and rows["黒"]["exportFresh"] and rows[""]["exportFresh"]
    # 手動の書き出しも、開いているバージョンが対象
    r = page.evaluate("() => window.inkdeus.exportPNG()")
    assert r["path"].endswith("a@黒.png") and r["version"] == "黒"
    # 画像一覧のサムネイルに、名前付きバージョンの数が出る
    assert page.evaluate("() => document.querySelector('.gal-item[data-name=\"a.png\"] "
                         ".gal-badge[data-kind=\"versions\"]').textContent") == "+1"
    assert page.evaluate("() => document.querySelector('.gal-item[data-name=\"b.png\"] "
                         ".gal-badge[data-kind=\"versions\"]')") is None
    assert page.errors == []


def test_switching_keeps_settings_and_history_apart(live):
    put_params(live, "a.png", WHITE)
    page = live["open"]("?image=a.png")
    page.evaluate("() => window.inkdeus.createVersion('v1')")
    assert state_of(page)["history"] == {"length": 1, "pos": 0}
    for unit in (2, 3):
        page.evaluate("(u) => { const p = window.inkdeus.getParams(); p.unit = u; window.inkdeus.setParams(p); }", unit)
    assert state_of(page)["history"] == {"length": 3, "pos": 2}
    # 既定へ切り替える（選択欄から）。設定は既定の物で、履歴も別
    page.select_option("#selVersion", "")
    page.wait_for_function("() => window.inkdeus.version() === '' && !document.getElementById('busy').hidden === false")
    s = state_of(page)
    assert s["params"]["unit"] == 1 and s["history"] == {"length": 1, "pos": 0}
    page.evaluate("() => { const p = window.inkdeus.getParams(); p.unit = 4; window.inkdeus.setParams(p); }")
    page.evaluate("() => window.inkdeus.undo()")
    assert state_of(page)["params"]["unit"] == 1                       # 既定の履歴だけが戻る
    page.evaluate("() => window.inkdeus.redo()")
    # v1 へ戻ると、v1 の設定と履歴がそのまま
    page.evaluate("() => window.inkdeus.openVersion('v1')")
    s = state_of(page)
    assert s["version"] == "v1" and s["params"]["unit"] == 3 and s["history"] == {"length": 3, "pos": 2}
    page.evaluate("() => window.inkdeus.undo()")
    assert state_of(page)["params"]["unit"] == 2
    page.evaluate("() => window.inkdeus.save()")
    out = live["out"]
    assert json.loads((out / "a@v1.inkdeus.json").read_text(encoding="utf-8"))["unit"] == 2
    assert json.loads((out / "a.inkdeus.json").read_text(encoding="utf-8"))["unit"] == 4
    # 外部（ctl など）からの変更は、開いているバージョンの物だけが画面に入る
    api(live, "api/set/a.png", json.dumps({"key": "unit", "value": 5}).encode("utf-8"))
    api(live, "api/set/a.png?version=v1", json.dumps({"key": "unit", "value": 6}).encode("utf-8"))
    page.wait_for_function("() => window.inkdeus.getParams().unit === 6")
    # 名前の変更と削除
    page.evaluate("() => window.inkdeus.renameVersion('v1', 'v2')")
    assert state_of(page)["version"] == "v2" and state_of(page)["names"] == ["", "v2"]
    assert (out / "a@v2.inkdeus.json").is_file() and not (out / "a@v1.inkdeus.json").exists()
    assert api(live, "api/state")["currentVersion"] == "v2"
    page.evaluate("() => { const p = window.inkdeus.getParams(); p.unit = 7; window.inkdeus.setParams(p); }")
    page.evaluate("() => window.inkdeus.deleteVersion('v2')")           # 未保存の変更があっても、消した物を書き戻さない
    s = state_of(page)
    assert s["version"] == "" and s["names"] == [""] and s["params"]["unit"] == 5
    page.wait_for_timeout(1200)
    assert not list(out.glob("a@*"))
    assert page.evaluate("""() => document.querySelector('#selVersion option[value="@delete"]').disabled""") is True
    # 選択欄の「操作」から作る・名前を変える・削除する（名前は入力のダイアログで聞く）
    answers = ["画面から", "改名", None]
    page.on("dialog", lambda d: d.accept(answers.pop(0)) if d.type == "prompt" else d.accept())
    page.select_option("#selVersion", "@new")
    page.wait_for_function("() => window.inkdeus.version() === '画面から'")
    page.select_option("#selVersion", "@rename")
    page.wait_for_function("() => window.inkdeus.version() === '改名' && document.getElementById('selVersion').value === '改名'")
    page.select_option("#selVersion", "@delete")
    page.wait_for_function("() => window.inkdeus.version() === '' && window.inkdeus.versions().length === 1")
    assert not list(out.glob("a@*"))
    # 使えない名前は断られる
    for bad in ("a b", "a@b", "x" * 33):
        assert "バージョン名" in page.evaluate(
            "(n) => window.inkdeus.createVersion(n).then(() => '', (e) => e.message)", bad)
    assert page.errors == []


def test_direct_link_with_version_and_inheritance(live):
    put_params(live, "a.png", WHITE)
    api(live, "api/versions/a.png", json.dumps({"version": "黒"}).encode("utf-8"))
    put_params(live, "a.png?version=" + quote("黒"), {**BLACK, "unit": 3})
    page = live["open"](f"?image=a.png&version={quote('黒')}")
    s = state_of(page)
    assert s["version"] == "黒" and s["params"]["unit"] == 3 and s["select"] == "黒"
    assert api(live, "api/state") | {} and api(live, "api/state")["currentVersion"] == "黒"
    # 設定の無い画像（既定のバージョン）を開くと直前の設定を引き継ぐ。別の画像へ移ると既定のバージョンになる
    page.evaluate("() => window.inkdeus.open('b.png')")
    s = state_of(page)
    assert s["version"] == "" and s["params"]["unit"] == 3 and s["names"] == [""]
    # 無いバージョンを指した直リンクは、既定を開いて知らせる
    other = live["open"]("?image=a.png&version=nope")
    assert state_of(other)["version"] == ""
    assert "見つかりません" in other.evaluate("() => document.getElementById('messages').textContent")
    # 別のタブ・ctl でバージョンが消されたら、開いている画面は既定へ移る
    third = live["open"](f"?image=a.png&version={quote('黒')}")
    api(live, "api/versions/a.png?version=" + quote("黒"), method="DELETE")
    third.wait_for_function("() => window.inkdeus.version() === '' && window.inkdeus.versions().length === 1")
    assert "削除されました" in third.evaluate("() => document.getElementById('messages').textContent")


def test_export_all_writes_every_version(live):
    put_params(live, "a.png", WHITE)
    put_params(live, "b.png", WHITE)
    for v in ("x", "y"):
        api(live, "api/versions/a.png", json.dumps({"version": v}).encode("utf-8"))
    put_params(live, "a.png?version=x", BLACK)
    page = live["open"]("?image=b.png")
    out = live["out"]
    r = page.evaluate("() => window.inkdeus.exportAll({defaultOnly: true})")
    assert (r["total"], r["exported"], r["failed"]) == (3, 3, 0)        # a, b, c の既定だけ
    assert sorted(p.name for p in out.glob("*.png")) == ["a.png", "b.png", "c.png"]
    r = page.evaluate("() => window.inkdeus.exportAll({})")
    assert (r["total"], r["exported"], r["skipped"], r["failed"]) == (5, 2, 3, 0)
    assert sorted(p.name for p in out.glob("*.png")) == ["a.png", "a@x.png", "a@y.png", "b.png", "c.png"]
    assert mean(out / "a@x.png") < 5 and mean(out / "a@y.png") > 250 and mean(out / "a.png") > 250
    assert page.evaluate("() => window.inkdeus.info().name") == "b.png"  # 始める前の画像へ戻る
    # 画面のボタンとチェック
    page.evaluate("() => document.querySelector('#panel details.section:last-of-type').open = true")
    assert page.evaluate("() => !!document.getElementById('chkExportAllDefaultOnly')") is True
    r = page.evaluate("() => window.inkdeus.exportAll({force: true})")
    assert (r["exported"], r["skipped"]) == (5, 0)
    assert api(live, "api/batch")["total"] == 5 and page.errors == []


# ---- ctl ----

def test_ctl_versions_and_version_option(ctl, ctl_live, fake_browser):  # noqa: F811
    assert [v["name"] for v in ctl("versions")["versions"]] == [""]
    ctl("set", "unit", "2")
    r = ctl("version", "new", "白背景")
    assert r["version"] == "白背景" and r["copyFrom"] == "" and [v["name"] for v in r["versions"]] == ["", "白背景"]
    assert ctl("get", "--version", "白背景")["params"]["unit"] == 2
    ctl("set", "unit", "3", "--version", "白背景")
    assert ctl("get")["params"]["unit"] == 2 and ctl("get", "--version", "白背景")["version"] == "白背景"
    r = ctl("version", "new", "v2", "--from", "白背景", "--name", "scene.png")
    assert r["copyFrom"] == "白背景" and ctl("get", "--version", "v2")["params"]["unit"] == 3
    assert ctl("version", "new", "v2", expect=1)["error"]
    assert ctl("version", "new", "a b", expect=1)["error"]
    assert ctl("get", "--version", "nope", expect=1)["error"]
    # 開いているバージョンが既定の対象になる
    s = ctl("open", "scene.png", "--version", "v2")
    assert s["current"] == "scene.png" and s["currentVersion"] == "v2"
    assert ctl("state")["currentVersion"] == "v2"
    ctl("patch", '{"unit": 4}')
    assert ctl("get", "--version", "v2")["params"]["unit"] == 4 and ctl("get", "--version", "")["params"]["unit"] == 2
    assert ctl("preset", "hatch1")["version"] == "v2"
    stats = ctl("stats", "--wait", "--timeout", "10")
    assert stats["fresh"] and stats["version"] == "v2"
    assert "バージョン" in ctl("stats", "--wait", "--version", "白背景", expect=2)["error"]
    assert ctl("stats", "--version", "白背景")["stats"] is None
    # --name で別の画像を指したときは既定のバージョン
    assert ctl("get", "--name", "plain.png")["version"] == ""
    r = ctl("export", "--timeout", "10")
    assert r["version"] == "v2" and r["path"].endswith("scene@v2.png")
    assert ctl("export", "--version", "", "--timeout", "10")["path"].endswith("scene.png")
    # 名前の変更・削除
    r = ctl("version", "rename", "v2", "v3")
    assert [v["name"] for v in r["versions"]] == ["", "v3", "白背景"] and ctl("state")["currentVersion"] == "v3"
    assert (ctl_live["out"] / "scene@v3.png").is_file()
    assert ctl("version", "delete", "v3")["deleted"] == "v3"
    assert not list(ctl_live["out"].glob("scene@v3*")) and ctl("state")["currentVersion"] == ""
    assert ctl("version", "delete", "v3", expect=1)["error"]
    assert [v["name"] for v in ctl("versions", "--name", "scene.png")["versions"]] == ["", "白背景"]
    # open にバージョンを付けなければ、その画像を開いたまま（別の画像からは既定）
    ctl("open", "scene.png", "--version", "白背景")
    assert ctl("open", "scene.png")["currentVersion"] == "白背景"
    assert ctl("open", "plain.png")["currentVersion"] == ""
    assert ctl("open", "scene.png", "--version", "nope", expect=1)["error"]


def test_ctl_drives_a_real_browser_per_version(live, capsys):
    from inkdeus import cli

    def run(*args):
        capsys.readouterr()
        assert cli.main(["ctl", "--port", str(live["port"]), *args]) == 0
        return json.loads(capsys.readouterr().out)

    put_params(live, "a.png", WHITE)
    page = live["open"]("?image=a.png")
    run("version", "new", "黒")
    page.wait_for_function("() => window.inkdeus.versions().length === 2")       # 一覧に出る（開いている版はそのまま）
    assert page.evaluate("() => window.inkdeus.version()") == ""
    assert run("open", "a.png", "--version", "黒")["currentVersion"] == "黒"
    assert page.evaluate("() => window.inkdeus.version()") == "黒"
    run("patch", json.dumps(BLACK))
    stats = run("stats", "--wait")
    assert stats["version"] == "黒" and stats["stats"]["output"]["all"]["blackRatio"] > 0.99
    assert run("export")["path"].endswith("a@黒.png") and mean(live["out"] / "a@黒.png") < 5
    assert not (live["out"] / "a.png").exists()
    r = run("export-all", "--default-only", "--wait")
    assert r["total"] == 3 and r["defaultOnly"] is True and mean(live["out"] / "a.png") > 250
    run("version", "rename", "黒", "墨")
    page.wait_for_function("() => window.inkdeus.version() === '墨'")
    run("version", "delete", "墨")
    page.wait_for_function("() => window.inkdeus.version() === ''")
    assert page.errors == []
