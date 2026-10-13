"""データを失わないための仕組み（漫画の画面）: 保存の状態、タブが隠れたときの前倒し保存、保存の失敗と再試行、
以前の版、閉じる前の確認、サーバの更新の知らせ。実物の漫画エディタ（上流）が要る（無ければ skip）"""

from __future__ import annotations

import pytest

from test_manga_bridge import (  # noqa: F401 — manga_env / ctx は fixture
    apply_template, ctx, images_on_canvas, manga_env, open_manga,
)

# 閉じる・再読み込みの前に引き止めるか。連携スクリプトの判断（status().unsaved）と、実際の beforeunload の扱いの両方を見る
# （漫画エディタ自身にも「保存していない変更」の確認があるので、beforeunload は「引き止める」側だけを確かめる）
ASK = """() => { const e = new Event('beforeunload', {cancelable: true}); window.dispatchEvent(e);
    return [window.inkdeusBridge.status().unsaved, e.defaultPrevented]; }"""
HIDE = """() => { Object.defineProperty(document, 'hidden', {get: () => true, configurable: true});
    document.dispatchEvent(new Event('visibilitychange')); }"""


def save_state(page) -> dict:
    return page.evaluate("() => window.inkdeusBridge.status().save")


@pytest.fixture()
def mpage(ctx, manga_env):
    page, status = open_manga(ctx, manga_env)
    assert status["ok"]
    yield page
    page.close()


def test_save_state_early_save_and_retry(mpage, manga_env):
    page = mpage
    auto = manga_env.project / "manga" / "autosave.lz4"
    page.evaluate("() => window.inkdeusBridge.setAutosave({enabled: true, seconds: 3600})")
    assert save_state(page) == {"state": "saved", "label": "保存済み"}
    assert page.evaluate(ASK)[0] is False                            # 変更が無ければ引き止めない
    apply_template(page)                                             # ページが増える（ページの切り替えは、すぐ保存される）
    panel = page.evaluate("() => window.inkdeusBridge.panels()[0].guid")
    page.evaluate("(g) => window.inkdeusBridge.placeAsset('source', 'scene.png', {panel: g})", panel)
    page.wait_for_function("() => !isProjectBusy() && !window.inkdeusBridge.status().busy")
    page.evaluate("() => window.inkdeusBridge.autosaveNow()")
    page.wait_for_function("() => window.inkdeusBridge.status().save.state === 'saved'")
    page.wait_for_timeout(2500)                                      # 見張り（2 秒ごと）が一巡しても、保存済みのまま
    assert save_state(page)["state"] == "saved" and page.evaluate(ASK)[0] is False
    page.evaluate("() => window.inkdeusBridge.placeAsset('sfx', 'impact/don.png')")     # 同じページの中での変更
    page.wait_for_function("() => window.inkdeusBridge.status().save.state === 'dirty'")
    assert page.locator(".ikb-save-state").inner_text() == "未保存の変更あり"
    before = auto.stat().st_mtime_ns
    page.wait_for_timeout(2500)
    assert auto.stat().st_mtime_ns == before                         # まだ時間が来ていない（3600 秒後）
    # タブが隠れたら、待ち時間を待たずに保存する
    page.evaluate(HIDE)
    page.wait_for_function("() => window.inkdeusBridge.status().save.state === 'saved'")
    assert auto.stat().st_mtime_ns > before and auto.stat().st_size > 0
    assert page.locator(".ikb-save-state").inner_text() == "保存済み" and page.evaluate(ASK)[0] is False
    page.evaluate("() => Object.defineProperty(document, 'hidden', {get: () => false, configurable: true})")
    # 保存していない変更があるときに閉じようとすると、引き止める（その間に保存も進める）
    before = auto.stat().st_mtime_ns
    page.evaluate("() => window.inkdeusBridge.placeAsset('sfx', 'impact/don.png')")
    page.wait_for_function("() => window.inkdeusBridge.status().save.state === 'dirty'")
    assert page.evaluate(ASK) == [True, True]
    page.wait_for_function("() => window.inkdeusBridge.status().save.state === 'saved'")
    assert auto.stat().st_mtime_ns > before

    # 保存に失敗したら、変更を持ったまま、間隔を広げながら再試行する
    page.route("**/api/manga/project/autosave", lambda route: route.abort() if route.request.method == "PUT" else route.continue_())
    page.evaluate("() => window.inkdeusBridge.placeAsset('sfx', 'impact/don.png')")
    page.wait_for_function("() => window.inkdeusBridge.status().save.state === 'dirty'")
    saved_at = auto.stat().st_mtime_ns
    assert page.evaluate("() => window.inkdeusBridge.autosaveNow()") is None
    assert save_state(page) == {"state": "failed", "label": "保存に失敗（再試行中）"}
    assert "自動保存に失敗しました" in page.evaluate("() => document.querySelector('.ikb-msg').textContent")
    first = page.evaluate("() => window.inkdeusBridge.status().autosave.retryMs")
    assert first == 2000 and page.evaluate(ASK) == [True, True]
    page.wait_for_function("(n) => window.inkdeusBridge.status().autosave.retryMs > n", arg=first)   # 再試行して、また失敗
    assert auto.stat().st_mtime_ns == saved_at
    page.unroute("**/api/manga/project/autosave")
    page.wait_for_function("() => window.inkdeusBridge.status().save.state === 'saved'")             # つながったら保存される
    assert auto.stat().st_mtime_ns > saved_at
    assert "自動保存できました" in page.evaluate("() => document.querySelector('.ikb-msg').textContent")
    assert page.evaluate("() => window.inkdeusBridge.status().autosave.failed") is False


def test_open_an_earlier_version_of_saved_manga_data(mpage, manga_env):
    page = mpage
    page.evaluate("() => window.inkdeusBridge.setAutosave({enabled: false})")
    apply_template(page)
    panels = page.evaluate("() => window.inkdeusBridge.panels()")
    page.evaluate("(g) => window.inkdeusBridge.placeAsset('source', 'scene.png', {panel: g})", panels[0]["guid"])
    page.evaluate("() => window.inkdeusBridge.save('第2話')")                     # 絵が 1 枚
    page.evaluate("(g) => window.inkdeusBridge.placeAsset('source', 'gradient.png', {panel: g})", panels[1]["guid"])
    page.evaluate("() => window.inkdeusBridge.save('第2話')")                     # 絵が 2 枚（上書き。前の版が残る）
    page_guid = page.evaluate("() => getCanvasGUID()")
    rows = page.evaluate("() => window.inkdeusBridge.listBackups('第2話')")
    assert len(rows) == 1 and rows[0]["bytes"] > 0
    saved = manga_env.project / "manga" / "第2話.lz4"
    current = saved.read_bytes()
    # 「プロジェクトから開く」の欄で保存データを選ぶと「以前の版…」が出て、一覧から開ける
    page.select_option(".ikb-saved", "第2話")
    page.wait_for_function("() => !document.querySelector('.ikb-versions-btn').hidden")
    page.click(".ikb-versions-btn")
    page.wait_for_selector(".ikb-versions .ikb-version")
    assert page.locator(".ikb-versions .ikb-version").count() == 1
    assert "以前の版" in page.locator(".ikb-versions").inner_text()
    result = page.evaluate("([n, at]) => window.inkdeusBridge.openBackup(n, at, {force: true})", ["第2話", rows[0]["at"]])
    assert result["pages"] == 2
    page.evaluate("async (g) => { if (getCanvasGUID() !== g) { await chengeCanvasByGuid(g); await btmWaitForPageReady(); } }",
                  page_guid)
    page.wait_for_function("() => !isProjectBusy() && canvas.getObjects().filter(o => o.type === 'image').length === 1")
    assert [i["src"] for i in images_on_canvas(page)] == ["/api/assets/source/image/scene.png?max=2048"]
    assert "以前の版" in page.evaluate("() => document.querySelector('.ikb-msg').textContent")
    assert saved.read_bytes() == current                                          # 開いただけでは、いまの保存データは変わらない
    assert page.evaluate("() => document.querySelector('.ikb-versions').hidden") is True
    # 壊れた保存データ（0 バイト）は、開かずに知らせる。以前の版からは開ける
    saved.write_bytes(b"")
    with pytest.raises(Exception, match="壊れています"):
        page.evaluate("() => window.inkdeusBridge.open('第2話', {force: true})")
    assert saved.read_bytes() == b""
    assert page.evaluate("([n, at]) => window.inkdeusBridge.openBackup(n, at, {force: true})",
                         ["第2話", rows[0]["at"]])["pages"] == 2
    page.evaluate("() => window.inkdeusBridge.save('第2話')")                     # 保存し直すと、壊れた物は .broken-<時刻> として残る
    assert len(list(saved.parent.glob("第2話.lz4.broken-*"))) == 1 and saved.stat().st_size > 0


def test_banner_when_the_server_changes(ctx, manga_env):
    page, status = open_manga(ctx, manga_env)
    try:
        assert status["ok"]
        page.evaluate("() => window.inkdeusBridge.setAutosave({enabled: false})")
        st = page.evaluate("() => window.inkdeusBridge.status()")
        assert st["notices"] == {} and st["build"]["id"] and page.locator("#inkdeus-banner").is_hidden()
        page.wait_for_function("() => window.inkdeusBridge.status().events.connected === true")
        hub = manga_env.app.config["INKDEUS_SESSION"].hub
        build = dict(st["build"])
        hub.publish({"type": "hello", "build": build})                             # 同じ版: 何も出ない
        hub.publish({"type": "hello", "build": {**build, "boot": "restarted000"}})  # 起動し直しただけ: 何も出ない
        page.wait_for_function("() => window.inkdeusBridge.status().build.boot === 'restarted000'")
        assert page.evaluate("() => window.inkdeusBridge.status().notices") == {}
        hub.publish({"type": "hello", "build": {**build, "id": "next-build"}})     # 別の版が起動した
        page.wait_for_function("() => 'reload' in window.inkdeusBridge.status().notices")
        assert page.locator("#inkdeus-banner").is_visible()
        assert "inkdeus が更新されました。再読み込みしてください" in page.locator("#inkdeus-banner").inner_text()
        page.wait_for_timeout(800)
        assert page.evaluate("() => window.inkdeusBridge.status().build.id") == build["id"]   # 自動では再読み込みしない
    finally:
        page.close()
    # 画面のファイルの方が新しい（古いサーバにつないだ）ときは、サーバの再起動を促す
    old = ["layerIds", "paint", "parts", "userStyles", "mangaPdf", "mangaCommand"]
    page, status = open_manga(ctx, manga_env, prepare=lambda p: p.route(
        "**/api/config", lambda route: route.fulfill(json={"project": None, "features": old})))
    try:
        assert status["ok"]
        notices = page.evaluate("() => window.inkdeusBridge.status().notices")
        assert notices == {"server": "この画面は、起動中の inkdeus のサーバより新しい版です。サーバを再起動してください"}
        assert page.locator("#inkdeus-banner").is_visible()
        assert page.evaluate("() => document.querySelector('.ikb-versions-btn').hidden") is True
    finally:
        page.close()
