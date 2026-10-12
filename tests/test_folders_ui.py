"""白黒化の画面での複数の画像フォルダ（実際のブラウザで確かめる）"""

from __future__ import annotations

import json
from urllib.parse import quote

import pytest

import synth
from conftest import open_page, serve
from inkdeus import cli
from inkdeus.folders import FolderDef
from inkdeus.server import create_app
from test_export_flow import BLACK, WHITE, api, mean, put_params, wait_for  # noqa: F401


@pytest.fixture()
def multi(tmp_path, browser):
    """既定のフォルダ（a, b）と、同じ名前の画像を持つ「二巻」（a, c, d）"""
    one, two = tmp_path / "one", tmp_path / "two"
    for name in ("a.png", "b.png"):
        synth.save(one / name, synth.flat(96, 64, 110))
    for name in ("a.png", "c.png", "d.png"):
        synth.save(two / name, synth.flat(48, 32, 110))
    out = tmp_path / "out"
    server, url = serve(create_app(one, out_dir=out, folders=[FolderDef("二巻", two)],
                                   project={"name": "本", "path": str(tmp_path)}))
    pages = []

    def open_(query: str = "", auto_export: bool = False, context=None):
        if context is None:
            page = open_page(browser, url + query, auto_export=auto_export)
        else:
            # 同じ context（localStorage を共有）でページを開く。conftest.open_page と同じ手順
            page = context.new_page()
            page.set_default_timeout(600_000)
            page.add_init_script("try { if (localStorage.getItem('inkdeus.autoExport') === null) "
                                 "localStorage.setItem('inkdeus.autoExport', '0'); } catch (e) {}")
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.goto(url + query)
            page.wait_for_function("window.inkdeus && window.inkdeus.ready")
            page.evaluate("() => window.inkdeus.ready")
            page.errors = errors
        pages.append(page)
        return page

    yield {"open": open_, "url": url, "port": server.server_port, "out": out}
    for page in pages:
        page.close()
    server.shutdown()


def where(page) -> dict:
    return page.evaluate("() => ({folder: window.inkdeus.folder(), name: window.inkdeus.info().name,"
                         " width: window.inkdeus.info().width, images: window.inkdeus.images(),"
                         " header: document.getElementById('projectName').textContent,"
                         " select: document.querySelector('.gal-folder').value})")


def switch(page, folder: str) -> dict:
    page.select_option(".gal-folder", folder)
    page.wait_for_function("(f) => window.inkdeus.folder() === f && !!window.inkdeus.info().name"
                           " && document.getElementById('busy').hidden", arg=folder)
    return where(page)


def set_unit(page, unit) -> None:
    page.evaluate("async (u) => { const p = window.inkdeus.getParams(); p.unit = u; window.inkdeus.setParams(p);"
                  " await window.inkdeus.save(); }", unit)


def test_switching_folders_and_last_opened_image(multi, browser):
    context = browser.new_context(viewport={"width": 1200, "height": 800})      # localStorage をこのテストの中で引き継ぐ
    try:
        page = multi["open"](context=context)
        s = where(page)
        assert (s["folder"], s["name"], s["images"], s["header"], s["select"]) == ("default", "a.png", ["a.png", "b.png"], "本 / 既定", "default")
        assert page.evaluate("() => Array.from(document.querySelectorAll('.gal-folder option')).map(o => o.textContent)") == [
            "既定（2）", "二巻（3）"]
        assert page.evaluate("() => document.querySelector('.gal-folder-row').hidden") is False
        s = switch(page, "二巻")
        assert (s["name"], s["images"], s["header"], s["width"]) == ("a.png", ["a.png", "c.png", "d.png"], "本 / 二巻", 48)
        assert sorted(page.evaluate("() => Array.from(document.querySelectorAll('.gal-item')).map(c => c.dataset.name)")) == [
            "a.png", "c.png", "d.png"]
        assert quote("source@二巻", safe="") in page.evaluate("() => document.querySelector('.gal-item img').dataset.src")
        state = api(multi, "api/state")
        assert (state["current"], state["currentFolder"]) == ("a.png", "二巻")
        # 前後の移動はいまのフォルダの中で
        page.keyboard.press("]")
        page.wait_for_function("() => window.inkdeus.info().name === 'c.png'")
        assert page.evaluate("() => Array.from(document.querySelectorAll('#selImage option')).map(o => o.value)") == [
            "a.png", "c.png", "d.png"]
        # フォルダごとに「最後に開いていた画像」へ戻る
        s = switch(page, "default")
        assert (s["name"], s["width"]) == ("a.png", 96)
        page.evaluate("() => window.inkdeus.open('b.png')")
        assert switch(page, "二巻")["name"] == "c.png"
        assert switch(page, "default")["name"] == "b.png"
        assert page.errors == []
        # 開き直すと、最後に開いていたフォルダと画像から始まる
        switch(page, "二巻")
        again = multi["open"](context=context)
        s = where(again)
        assert (s["folder"], s["name"]) == ("二巻", "c.png")
        # ctl でフォルダを指定して開く
        assert cli.main(["ctl", "--port", str(multi["port"]), "open", "b.png", "--folder", "default"]) == 0
        again.wait_for_function("() => window.inkdeus.folder() === 'default' && window.inkdeus.info().name === 'b.png'")
    finally:
        context.close()


def test_same_named_images_are_set_and_exported_apart(multi):
    page = multi["open"]()
    out = multi["out"]
    page.evaluate("(p) => window.inkdeus.setParams(p)", WHITE)
    assert page.evaluate("() => window.inkdeus.exportPNG()")["path"] == str(out / "a.png")
    switch(page, "二巻")
    page.evaluate("(p) => window.inkdeus.setParams(p)", BLACK)
    assert page.evaluate("() => window.inkdeus.exportPNG()")["path"] == str(out / "@二巻" / "a.png")
    assert mean(out / "a.png") > 250 and mean(out / "@二巻" / "a.png") < 5
    one = json.loads((out / "a.inkdeus.json").read_text(encoding="utf-8"))
    two = json.loads((out / "@二巻" / "a.inkdeus.json").read_text(encoding="utf-8"))
    assert one["regions"]["person"]["solid"]["enabled"] is False and two["regions"]["person"]["solid"]["enabled"] is True
    # 自動書き出しも、開いているフォルダの保存先へ
    auto = multi["open"]("?folder=" + quote("二巻") + "&image=c.png", auto_export=True)
    wait_for((out / "@二巻" / "c.png").is_file)
    assert not (out / "c.png").exists()
    # バージョンもフォルダの中に作られる
    auto.evaluate("() => window.inkdeus.createVersion('v')")
    assert (out / "@二巻" / "c@v.inkdeus.json").is_file() and auto.evaluate("() => window.inkdeus.version()") == "v"
    assert page.errors == [] and auto.errors == []


def test_direct_links(multi):
    api(multi, "api/params/a.png?folder=" + quote("二巻"), json.dumps(WHITE).encode("utf-8"), "PUT")
    api(multi, "api/versions/a.png?folder=" + quote("二巻"), json.dumps({"version": "v"}).encode("utf-8"))
    page = multi["open"]("?folder=" + quote("二巻") + "&image=c.png")
    assert (where(page)["folder"], where(page)["name"]) == ("二巻", "c.png")
    page = multi["open"]("?folder=" + quote("二巻") + "&image=a.png&version=v")
    s = where(page)
    assert (s["folder"], s["name"], page.evaluate("() => window.inkdeus.version()")) == ("二巻", "a.png", "v")
    # フォルダの指定が無ければ既定のフォルダ。無いフォルダは知らせる
    assert where(multi["open"]("?image=b.png"))["folder"] == "default"
    other = multi["open"]("?folder=nope&image=a.png")
    assert where(other)["folder"] == "default"
    assert "画像フォルダが見つかりません" in other.evaluate("() => document.getElementById('messages').textContent")


def test_settings_are_not_inherited_across_folders(multi):
    page = multi["open"]()
    out = multi["out"]
    set_unit(page, 3)
    s = switch(page, "二巻")                                           # このフォルダにはまだ設定が無い
    assert s["name"] == "a.png" and page.evaluate("() => window.inkdeus.getParams().unit") is None
    assert not (out / "@二巻").exists() or not list((out / "@二巻").glob("*.json"))
    set_unit(page, 4)
    page.evaluate("() => window.inkdeus.open('c.png')")                # 同じフォルダの中では引き継ぐ
    assert page.evaluate("() => window.inkdeus.getParams().unit") == 4
    page.evaluate("() => window.inkdeus.open('a.png')")
    switch(page, "default")
    assert page.evaluate("() => window.inkdeus.getParams().unit") == 3
    # フォルダを切り替えて開いた最初の画像に設定が無ければ、そのフォルダで最後に開いていた画像の設定を引き継ぐ
    page.evaluate("() => window.inkdeus.open('d.png', '', '二巻')")
    assert where(page)["name"] == "d.png" and page.evaluate("() => window.inkdeus.getParams().unit") == 4
    assert json.loads((out / "@二巻" / "d.inkdeus.json").read_text(encoding="utf-8"))["unit"] == 4


def test_export_all_scope(multi, capsys):
    put_params(multi, "a.png", WHITE)
    page = multi["open"]("?image=b.png")
    out = multi["out"]
    r = page.evaluate("() => window.inkdeus.exportAll({})")
    assert (r["total"], r["exported"], r["failed"], r["allFolders"]) == (2, 2, 0, False)     # いまのフォルダだけ
    assert sorted(p.name for p in out.glob("*.png")) == ["a.png", "b.png"] and not (out / "@二巻").exists()
    r = page.evaluate("() => window.inkdeus.exportAll({allFolders: true})")
    assert (r["total"], r["exported"], r["skipped"], r["failed"], r["allFolders"]) == (5, 3, 2, 0, True)
    assert sorted(p.name for p in (out / "@二巻").glob("*.png")) == ["a.png", "c.png", "d.png"]
    s = where(page)
    assert (s["folder"], s["name"]) == ("default", "b.png")            # 始める前のフォルダと画像へ戻る
    page.evaluate("() => document.querySelector('#panel details.section:last-of-type').open = true")
    assert page.evaluate("() => !!document.getElementById('chkExportAllFolders')") is True
    capsys.readouterr()
    assert cli.main(["ctl", "--port", str(multi["port"]), "export-all", "--all-folders", "--force", "--wait"]) == 0
    r = json.loads(capsys.readouterr().out)
    assert (r["total"], r["exported"], r["allFolders"]) == (5, 5, True) and page.errors == []


def test_single_folder_shows_no_switch(tmp_path, browser):
    synth.save(tmp_path / "images" / "a.png", synth.flat(32, 24, 100))
    server, url = serve(create_app(tmp_path / "images", out_dir=tmp_path / "out"))
    page = open_page(browser, url)
    try:
        assert page.evaluate("() => document.querySelector('.gal-folder-row').hidden") is True
        assert page.evaluate("() => document.getElementById('projectName').hidden") is True
        assert page.evaluate("() => window.inkdeus.folders()") == ["default"] and page.errors == []
    finally:
        page.close()
        server.shutdown()
