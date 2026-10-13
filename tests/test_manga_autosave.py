"""漫画の画面の自動保存: 上流（漫画エディタ）自身の自動保存と復元の確認を止めて inkdeus の保存に一本化する、
ブラウザの中に残っている上流の自動保存の扱い、保存までの間隔、「前回の続き」の選び方。
実物の漫画エディタ（上流）が要る（無ければ skip）"""

from __future__ import annotations

import pytest

from test_manga_bridge import (  # noqa: F401 — manga_env / ctx は fixture
    UPSTREAM_DIALOG, apply_template, ctx, manga_env, open_manga,
)

pytestmark = pytest.mark.manga          # 実物の漫画エディタ（上流のクローン）が要る

# 上流の自動保存の置き場（ブラウザの中 = IndexedDB）に、データがあるか
HAS_UPSTREAM_DATA = """async () => !!(await localforage.createInstance(
    {name: 'autoSaveStorage', storeName: 'projectAutoSave'}).getItem('metadata'))"""
# 読み込む前に、長い間隔のタイマー（上流の自動保存は 60 秒ごと）が仕掛けられるのを記録する
WATCH_TIMERS = """(() => { window.__longTimers = []; const real = window.setInterval;
    window.setInterval = function (fn, ms, ...rest) { if (ms >= 10000) window.__longTimers.push(ms);
        return real.call(window, fn, ms, ...rest); }; })()"""
KEEP = "try { localStorage.setItem('inkdeus.manga.upstreamAutosave', 'keep'); } catch (e) {}"
FORGET = """() => { localStorage.removeItem('inkdeus.manga.upstreamAutosave');
    localStorage.removeItem('inkdeus.manga.upstreamOldSeen'); localStorage.removeItem('inkdeus.manga.autosaveSeconds'); }"""


def mode(page) -> dict:
    return page.evaluate("() => window.inkdeusBridge.status().upstreamAutosave")


def old_block(page) -> dict:
    return page.evaluate("""() => { const b = document.querySelector('.ikb-upstream-old');
        return {hidden: b.hidden, text: b.textContent, acts: Array.from(b.querySelectorAll('button')).map(x => x.dataset.act)}; }""")


def leave_upstream_data(ctx, manga_env) -> int:  # noqa: F811
    """上流の自動保存を止めない設定の画面で、上流に自動保存させる（止める前の版を使っていた状態を作る）。ページ数を返す"""
    page, status = open_manga(ctx, manga_env, prepare=lambda p: p.add_init_script(KEEP))
    try:
        assert status["ok"] and mode(page)["mode"] == "kept"
        page.evaluate("() => window.inkdeusBridge.setAutosave({enabled: false})")
        apply_template(page)
        page.evaluate("() => AutoSaveManager.save()")
        page.wait_for_function(HAS_UPSTREAM_DATA)
        return page.evaluate("() => btmGetGuids().length")
    finally:
        page.evaluate("() => localStorage.removeItem('inkdeus.manga.upstreamAutosave')")
        page.close()


@pytest.fixture()
def clean(ctx, manga_env):  # noqa: F811
    """上流の自動保存のデータ（ブラウザの中）と、この機能の覚え書きが無い状態から始める
    （ブラウザコンテキストはモジュールで共有している。漫画の画面を開かずに、同じ出どころの軽いページから消す）"""
    page = ctx.new_page()
    try:
        page.goto(manga_env.url + "api/config")
        page.evaluate("""() => new Promise((resolve) => { const r = indexedDB.deleteDatabase('autoSaveStorage');
            r.onsuccess = r.onerror = r.onblocked = () => resolve(); })""")
        page.evaluate(FORGET)
    finally:
        page.close()


def test_upstream_autosave_and_recovery_dialog_are_stopped(ctx, manga_env, clean):  # noqa: F811
    first, status = open_manga(ctx, manga_env, prepare=lambda p: p.add_init_script(WATCH_TIMERS))
    second = None
    try:
        assert status["ok"] and mode(first) == {"mode": "stopped", "old": None}
        assert 60000 not in first.evaluate("() => window.__longTimers")     # 60 秒ごとのタイマーは仕掛けられていない
        first.evaluate("() => window.inkdeusBridge.setAutosave({enabled: false})")
        apply_template(first)
        first.evaluate("() => AutoSaveManager.save()")                     # 上流の保存の入口を呼んでも、何も書かれない
        first.evaluate("() => AutoSaveManager.restart()")                  # 設定の読み直し（タイマーの始め直し）も働かない
        first.wait_for_timeout(300)
        assert first.evaluate(HAS_UPSTREAM_DATA) is False and 60000 not in first.evaluate("() => window.__longTimers")
        # 上流の設定欄の「自動保存」は働かないので、押せなくしてある。案内は出ない（一本化できている）
        assert first.evaluate("() => document.getElementById('autoSaveCheckbox').disabled") is True
        assert first.evaluate("() => document.querySelector('.ikb-upstream-note').hidden") is True
        assert old_block(first)["hidden"] is True
        # inkdeus の自動保存は働く（プロジェクトの manga/autosave.lz4）
        auto = manga_env.project / "manga" / "autosave.lz4"
        first.evaluate("() => window.inkdeusBridge.setAutosave({enabled: true})")
        first.evaluate("() => window.inkdeusBridge.autosaveNow()")
        first.wait_for_function("() => window.inkdeusBridge.status().save.state === 'saved'")
        assert auto.stat().st_size > 0
        # 次に開いたページに、上流の復元の確認（全画面）は出ない。パネルをすぐ押せる
        second, status = open_manga(ctx, manga_env)
        assert status["ok"] and second.evaluate("(s) => document.querySelector(s) === null", UPSTREAM_DIALOG)
        second.click(".ikb-tab:has-text('白黒')", timeout=5000)
        second.wait_for_function("() => window.inkdeusBridge.status().source === 'inked'")
        assert second.evaluate("() => document.querySelector('.ikb-resume').hidden") is False   # inkdeus の「前回の続き」は出る
    finally:
        if second is not None:
            second.close()
        first.close()


def test_kept_upstream_autosave_works_as_before_and_is_announced(ctx, manga_env, clean):  # noqa: F811
    """上流の自動保存を止めない設定（inkdeus.manga.upstreamAutosave = keep）では、従来どおり二重に保存され、
    次に開いたページで上流の復元の確認が全画面で出る（その間、素材パネルは押せない）。パネルに案内が出る"""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    pages = leave_upstream_data(ctx, manga_env)
    assert pages >= 2
    second, status = open_manga(ctx, manga_env, prepare=lambda p: p.add_init_script(KEEP))
    try:
        second.wait_for_selector(UPSTREAM_DIALOG)
        assert status["ok"] and mode(second)["mode"] == "kept"            # 連携スクリプトの準備は、確認を待たずに済む
        note = second.evaluate("() => { const n = document.querySelector('.ikb-upstream-note'); return [n.hidden, n.textContent]; }")
        assert note[0] is False and note[1].startswith("漫画エディタ側の自動保存が有効です")
        assert old_block(second)["hidden"] is True                         # 上流が自分で尋ねるので、パネルでは尋ねない
        second.wait_for_selector(".ikb-tab:has-text('白黒')")
        with pytest.raises(PlaywrightTimeout, match="intercepts pointer events"):
            second.click(".ikb-tab:has-text('白黒')", timeout=3000)
        second.click(f"{UPSTREAM_DIALOG} button:has-text('Decide later')")
        second.wait_for_selector(UPSTREAM_DIALOG, state="detached")
        second.click(".ikb-tab:has-text('白黒')")
        second.wait_for_function("() => window.inkdeusBridge.status().source === 'inked'")
        assert second.evaluate("() => document.getElementById('autoSaveCheckbox').disabled") is False
    finally:
        second.evaluate("() => localStorage.removeItem('inkdeus.manga.upstreamAutosave')")
        second.close()


def test_leftover_upstream_data_open_ignore_or_delete(ctx, manga_env, clean):  # noqa: F811
    pages = leave_upstream_data(ctx, manga_env)
    opened = []

    def fresh():
        while opened:                    # 前の画面は閉じる（開いたままだと、通知の接続がブラウザの同時接続の上限を使い切る）
            opened.pop().close()
        page, status = open_manga(ctx, manga_env)
        opened.append(page)
        assert status["ok"] and mode(page)["mode"] == "stopped"
        page.evaluate("() => window.inkdeusBridge.setAutosave({enabled: false})")
        return page

    try:
        # 残っているデータは消さずに、パネルの中で尋ねる（全画面の確認は出ない）
        page = fresh()
        block = old_block(page)
        assert block["hidden"] is False and block["acts"] == ["open", "ignore", "delete"]
        assert "以前の自動保存が残っています" in block["text"] and f"{pages} ページ" in block["text"]
        assert mode(page)["old"]["pages"] == pages and page.evaluate("(s) => document.querySelector(s) === null", UPSTREAM_DIALOG)
        # 無視する: 次回から尋ねない。データは残る
        page.click(".ikb-upstream-old button[data-act='ignore']")
        assert old_block(page)["hidden"] is True and page.evaluate(HAS_UPSTREAM_DATA) is True
        page = fresh()
        assert old_block(page)["hidden"] is True and mode(page)["old"]["pages"] == pages
        # （覚え書きを消して）開く: 上流の復元の画面に渡す。復元すると、ページが戻る
        page.evaluate("() => localStorage.removeItem('inkdeus.manga.upstreamOldSeen')")
        page = fresh()
        assert old_block(page)["hidden"] is False and page.evaluate("() => btmGetGuids().length") == 1
        page.click(".ikb-upstream-old button[data-act='open']")
        page.wait_for_selector(UPSTREAM_DIALOG)
        page.click(f"{UPSTREAM_DIALOG} button:has-text('Decide later')")   # あとで決める → また尋ねる
        page.wait_for_selector(UPSTREAM_DIALOG, state="detached")
        page.wait_for_function("() => document.querySelector('.ikb-upstream-old').hidden === false")
        page.click(".ikb-upstream-old button[data-act='open']")
        page.click(f"{UPSTREAM_DIALOG} button[data-choice='recover']")
        page.wait_for_function("(n) => btmGetGuids().length === n && !isProjectBusy()", arg=pages)
        page.wait_for_function("() => document.querySelector('.ikb-upstream-old').hidden === true")
        page.wait_for_function("() => window.inkdeusBridge.status().save.state === 'dirty'")   # 開いた物は、まだプロジェクトに無い
        assert page.evaluate("() => AutoSaveManager.save().then(() => true)") is True          # 開いたあとも、上流の保存は止まったまま
        page = fresh()
        assert old_block(page)["hidden"] is True                           # 開いたので、もう尋ねない
        # （覚え書きを消して）消す: 確かめてから消す
        page.evaluate("() => localStorage.removeItem('inkdeus.manga.upstreamOldSeen')")
        page = fresh()
        page.click(".ikb-upstream-old button[data-act='delete']")
        page.wait_for_selector(UPSTREAM_DIALOG)
        page.click(f"{UPSTREAM_DIALOG} .confirm-dialog-cancel")            # やめる → 消さない
        page.wait_for_selector(UPSTREAM_DIALOG, state="detached")
        assert page.evaluate(HAS_UPSTREAM_DATA) is True and old_block(page)["hidden"] is False
        page.click(".ikb-upstream-old button[data-act='delete']")
        page.click(f"{UPSTREAM_DIALOG} .confirm-dialog-ok")
        page.wait_for_function("() => document.querySelector('.ikb-upstream-old').hidden === true")
        assert page.evaluate(HAS_UPSTREAM_DATA) is False
        page = fresh()
        assert old_block(page)["hidden"] is True and mode(page)["old"] is None
    finally:
        for page in opened:
            page.close()


def test_autosave_interval_setting(ctx, manga_env, clean):  # noqa: F811
    page, status = open_manga(ctx, manga_env)
    again = None
    try:
        assert status["ok"]
        options = page.evaluate("() => Array.from(document.querySelector('.ikb-autosave-seconds').options).map(o => o.value)")
        assert options == ["10", "30", "60", "120"]
        assert page.evaluate("() => window.inkdeusBridge.status().autosave.seconds") == 30       # 既定は 30 秒
        assert page.evaluate("() => document.querySelector('.ikb-autosave-seconds').value") == "30"
        page.select_option(".ikb-autosave-seconds", "10")
        assert page.evaluate("() => window.inkdeusBridge.status().autosave.seconds") == 10
        # 選んだ間隔が経つと保存される（10 秒は待たず、その場かぎりの 1 秒で確かめる。選択肢には無い値も出せる）
        page.evaluate("() => window.inkdeusBridge.setAutosave({enabled: true, seconds: 1})")
        assert page.evaluate("() => document.querySelector('.ikb-autosave-seconds').value") == "1"
        apply_template(page)
        page.evaluate("() => window.inkdeusBridge.placeAsset('sfx', 'impact/don.png')")
        page.wait_for_function("() => window.inkdeusBridge.status().autosave.lastAt > 0")
        # 自動保存を切ると、間隔は選べない
        page.evaluate("() => document.getElementById('ikb-autosave').click()")
        assert page.evaluate("() => document.querySelector('.ikb-autosave-seconds').disabled") is True
        page.evaluate("() => document.getElementById('ikb-autosave').click()")
        # 画面で選んだ間隔は、次に開いたときも同じ（その場かぎりの値は覚えない）
        again, status = open_manga(ctx, manga_env)
        assert again.evaluate("() => window.inkdeusBridge.status().autosave.seconds") == 10
        assert again.evaluate("() => Array.from(document.querySelector('.ikb-autosave-seconds').options).map(o => o.value)") == options
    finally:
        if again is not None:
            again.close()
        page.close()


def test_resume_offers_the_newer_of_named_save_and_autosave(ctx, manga_env, clean):  # noqa: F811
    folder = manga_env.project / "manga"
    for old in folder.glob("*.lz4") if folder.is_dir() else []:
        old.unlink()
    page, status = open_manga(ctx, manga_env)
    again = None
    try:
        assert status["ok"]
        page.evaluate("() => window.inkdeusBridge.setAutosave({enabled: false})")
        apply_template(page)
        page.evaluate("() => window.inkdeusBridge.save('第2話')")                      # 2 ページ
        apply_template(page)
        page.evaluate("() => window.inkdeusBridge.setAutosave({enabled: true})")
        page.evaluate("() => window.inkdeusBridge.autosaveNow()")                      # 3 ページ（こちらが新しい）
        page.wait_for_function("() => window.inkdeusBridge.status().autosave.lastAt > 0")
        rows = {p["name"]: p for p in page.evaluate("() => window.inkdeusBridge.listSaved()")}
        assert rows["第2話"]["pages"] == 2 and rows["autosave"]["pages"] == 3
        again, status = open_manga(ctx, manga_env)
        again.evaluate("() => window.inkdeusBridge.setAutosave({enabled: false})")
        picks = again.evaluate("""() => Array.from(document.querySelectorAll('.ikb-resume .ikb-resume-pick')).map(l =>
            ({name: l.dataset.name, checked: l.querySelector('input').checked, text: l.textContent}))""")
        assert [(p["name"], p["checked"]) for p in picks] == [("autosave", True), ("第2話", False)]   # 新しい方が既定
        assert picks[0]["text"].startswith("自動保存 — ") and picks[0]["text"].endswith("・3 ページ")
        assert picks[1]["text"].startswith("第2話 — ") and picks[1]["text"].endswith("・2 ページ")
        assert again.evaluate("() => document.querySelector('.ikb-resume').dataset.name") == "autosave"
        # 名前を付けて保存した方を選んで開く
        again.check(".ikb-resume-pick[data-name='第2話'] input")
        assert again.evaluate("() => document.querySelector('.ikb-resume').dataset.name") == "第2話"
        again.click(".ikb-resume button.ikb-primary")
        again.wait_for_function("() => btmGetGuids().length === 2 && !isProjectBusy() && !window.inkdeusBridge.status().busy")
        assert again.evaluate("() => document.querySelector('.ikb-resume').hidden") is True
        assert again.evaluate("() => window.inkdeusBridge.status().name") == "第2話"
    finally:
        if again is not None:
            again.close()
        page.close()
        for made in folder.glob("*.lz4"):
            made.unlink()
