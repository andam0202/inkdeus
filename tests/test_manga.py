"""漫画の構成: 漫画エディタの取得（キャッシュ）・/manga/ の配信と書き換え・漫画データの保存 API。

実物の漫画エディタは使わない。取得元は、ここで作る最小の合成リポジトリ（アクセス解析・service worker の登録・
manifest を模した行を持つ index.html）。実物を使う結合テストは test_manga_bridge.py。
"""

from __future__ import annotations

import io
import json
import struct
import subprocess

import pytest
from PIL import Image

import synth
from inkdeus import cli, manga
from inkdeus.server import create_app

FAKE_INDEX = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>fake editor</title>
    <link rel="manifest" href="manifest.json">
    <script async src="third/GoogleAnalytics/googleAnalytics.js"></script>
    <link rel="stylesheet" href="css/main.css?v=1">
</head>
<body>
    <div id="app"></div>
    <script>
        function boot() { document.getElementById('app').textContent = 'booted'; }
        boot();

// ========== Analytics ==========
        window.dataLayer = window.dataLayer || [];
        function gtag() { dataLayer.push(arguments); }
        gtag('js', new Date());
        gtag('config', 'G-FAKE000000', { 'cookie_domain': 'example.invalid' });
        function trackCustomEvent(a, b) { gtag('event', b, { 'event_category': a }); }
        window.addEventListener('load', function () { trackCustomEvent('page_load', 'page_load'); });
    </script>
    <script src="js/app.js?v=1" defer></script>
    <script src="js/core/service/worker-register.js?v=1" defer></script>
</body>
</html>
"""


def git(repo, *args) -> str:
    cmd = ["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid",
           "-c", "commit.gpgsign=false", *args]
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


def make_upstream(root, index: str = FAKE_INDEX):
    """最小の「上流」を git リポジトリとして作る。戻り値は (フォルダ, コミット)"""
    files = {
        "index.html": index,
        "css/main.css": "body { margin: 0; }\n",
        "js/app.js": "window.fakeEditor = true;\n",
        "js/core/service/worker-register.js": "navigator.serviceWorker.register('./service-worker.js');\n",
        "third/GoogleAnalytics/googleAnalytics.js": "/* fake analytics */\n",
        "manifest.json": "{}\n",
        "service-worker.js": "/* fake cache-first worker */\n",
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    git(root, "init", "--quiet")
    git(root, "add", "-A")
    git(root, "commit", "--quiet", "-m", "fake upstream")
    return root, git(root, "rev-parse", "HEAD")


@pytest.fixture()
def upstream(tmp_path):
    return make_upstream(tmp_path / "upstream")


@pytest.fixture()
def images(tmp_path):
    folder = tmp_path / "images"
    synth.save(folder / "a.png", synth.flat(32, 16, 200))
    return folder


def app_client(images, cache):
    return create_app(images, local_config=images / "none.toml", cache_root=cache, watch=False).test_client()


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


# ---- 取得 ----

def test_install_from_local_clone(tmp_path, upstream):
    repo, commit = upstream
    (repo / "untracked.txt").write_text("not committed", encoding="utf-8")
    cache = tmp_path / "cache"
    messages: list[str] = []
    info = manga.install(ref=commit, source=repo, root=cache, say=messages.append)
    target = cache / "manga-editor-desu" / commit
    assert info["installed"] and info["commit"] == commit and info["path"] == str(target)
    assert (target / "index.html").read_text(encoding="utf-8") == FAKE_INDEX        # キャッシュ上の物は書き換えない
    assert (target / "js" / "app.js").is_file()
    assert not (target / ".git").exists()                                           # .git は残さない
    assert not (target / "untracked.txt").exists()                                  # コミット済みの内容だけ
    assert json.loads((target / manga.MARKER_FILE).read_text(encoding="utf-8"))["commit"] == commit
    assert manga.installed_dir(cache) == target
    assert info["matchesPinned"] is False
    assert any("別の版" in m for m in messages)                                     # 固定している版ではない旨を知らせる
    # 入れ直しても壊れない
    assert manga.install(ref=commit, source=repo, root=cache, say=messages.append)["commit"] == commit


def test_install_checks_the_commit(tmp_path, upstream):
    repo, commit = upstream
    cache = tmp_path / "cache"
    with pytest.raises(manga.MangaError) as e:
        manga.install(source=repo, root=cache, say=lambda _m: None)                 # 既定は固定している版
    assert manga.PINNED_COMMIT in str(e.value) and commit in str(e.value) and "--allow-mismatch" in str(e.value)
    assert manga.installed_dir(cache) is None
    assert not list((cache / "manga-editor-desu").glob(".install-*"))                # 作業用フォルダを残さない
    messages: list[str] = []
    info = manga.install(source=repo, allow_mismatch=True, root=cache, say=messages.append)
    assert info["commit"] == commit and any("警告" in m for m in messages)
    with pytest.raises(manga.MangaError):
        manga.install(source=tmp_path / "nowhere", root=cache, say=lambda _m: None)
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(manga.MangaError):
        manga.install(source=plain, allow_mismatch=True, root=cache, say=lambda _m: None)   # git のクローンではない


def test_pinned_commit_is_a_full_hash():
    assert len(manga.PINNED_COMMIT) == 40 and manga._is_commit(manga.PINNED_COMMIT)


def test_cli_install_and_status(tmp_path, upstream, capsys, monkeypatch):
    repo, commit = upstream
    monkeypatch.setenv("INKDEUS_CACHE", str(tmp_path / "cache"))
    code, out, _ = run(capsys, "manga", "status")
    assert code == 0 and "未導入" in out and manga.PINNED_COMMIT in out
    code, _, err = run(capsys, "manga", "install", "--from", str(repo))
    assert code == 1 and "--allow-mismatch" in err
    code, out, _ = run(capsys, "manga", "install", "--from", str(repo), "--ref", commit)
    assert code == 0 and commit in out
    code, out, _ = run(capsys, "manga", "status")
    assert code == 0 and "導入済み" in out and commit in out and str(tmp_path / "cache") in out
    code, out, _ = run(capsys, "manga", "status", "--json")
    assert json.loads(out)["commit"] == commit
    assert manga.cache_root() == (tmp_path / "cache").resolve()


# ---- 配信と書き換え ----

def test_rewrite_removes_analytics_and_service_worker():
    result = manga.rewrite_index(FAKE_INDEX)
    html = result.html
    assert result.missing == [] and len(result.removed) == len(manga.REMOVALS)
    assert result.inserted == ["bridge.css", "bridge.js"]
    for gone in ("GoogleAnalytics", "gtag('", "G-FAKE000000", "dataLayer.push", "worker-register", 'rel="manifest"',
                 "trackCustomEvent('page_load'"):
        assert gone not in html
    assert "function gtag(){}" in html and "function trackCustomEvent(){}" in html   # 呼ばれても落ちない空の関数
    assert "boot();" in html and 'src="js/app.js?v=1"' in html and "css/main.css" in html   # 他は残す
    assert html.index(manga.BRIDGE_CSS) < html.index("</head>")
    assert html.index('src="js/app.js?v=1"') < html.index(manga.BRIDGE_JS) < html.index("</body>")
    assert html.count("<script") == html.count("</script>")


def test_rewrite_reports_what_it_could_not_find():
    result = manga.rewrite_index("<html><head></head><body><p>hello</p></body></html>")
    assert result.removed == [] and len(result.missing) == len(manga.REMOVALS)
    assert manga.BRIDGE_CSS in result.html and manga.BRIDGE_JS in result.html
    bare = manga.rewrite_index("<p>no head, no body</p>")
    assert bare.html.rstrip().endswith(manga.BRIDGE_JS) and any("</head>" in m for m in bare.missing)


def test_rewrite_handles_external_gtag():
    html = ('<html><head><script async src="https://www.googletagmanager.com/gtag/js?id=G-X"></script></head>'
            "<body></body></html>")
    result = manga.rewrite_index(html)
    assert "googletagmanager" not in result.html and "アクセス解析の読み込み" in result.removed


def test_manga_not_installed_page(tmp_path, images):
    client = app_client(images, tmp_path / "cache")
    r = client.get("/manga/")
    assert r.status_code == 200 and "inkdeus manga install" in r.get_data(as_text=True)
    assert "no-cache" in r.headers["Cache-Control"]
    assert client.get("/manga/js/app.js").status_code == 404
    assert client.get("/api/manga/status").get_json()["installed"] is False
    r = client.get("/manga")
    assert r.status_code == 308 and r.headers["Location"].endswith("/manga/")


def test_manga_is_served_rewritten(tmp_path, images, upstream, caplog):
    repo, commit = upstream
    cache = tmp_path / "cache"
    client = app_client(images, cache)
    assert "inkdeus manga install" in client.get("/manga/").get_data(as_text=True)
    manga.install(ref=commit, source=repo, root=cache, say=lambda _m: None)
    with caplog.at_level("INFO", logger="inkdeus.manga"):
        r = client.get("/manga/")                                    # 稼働中に導入しても、再起動なしで配信される
        again = client.get("/manga/index.html")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and r.mimetype == "text/html" and html == again.get_data(as_text=True)
    assert "GoogleAnalytics" not in html and "gtag('config'" not in html
    assert "worker-register" not in html and 'rel="manifest"' not in html
    assert manga.BRIDGE_CSS in html and manga.BRIDGE_JS in html
    logged = [rec.getMessage() for rec in caplog.records]
    assert len([m for m in logged if "書き換えて配信" in m]) == 1     # 何をしたかは1回だけ記録する
    assert "service worker の登録" in logged[0] and "bridge.js" in logged[0]
    status = client.get("/api/manga/status").get_json()
    assert status["installed"] is True and status["commit"] == commit and status["pinned"] == manga.PINNED_COMMIT

    r = client.get("/manga/js/app.js?v=1")
    assert r.status_code == 200 and b"fakeEditor" in r.data and "no-cache" in r.headers["Cache-Control"]
    assert client.get("/manga/css/main.css").status_code == 200
    # service worker は「自分を外してキャッシュを消すだけ」の物に差し替える
    r = client.get("/manga/service-worker.js")
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and "javascript" in r.mimetype
    assert "registration.unregister()" in body and "caches.delete" in body and "fake cache-first" not in body
    assert "fetch" not in body                                       # 取得には一切関わらない
    for blocked in ("/manga/third/GoogleAnalytics/googleAnalytics.js", "/manga/manifest.json",
                    "/manga/" + manga.MARKER_FILE, "/manga/nope.js", "/manga/js"):
        assert client.get(blocked).status_code == 404, blocked


def test_manga_rejects_path_traversal(tmp_path, images, upstream):
    repo, commit = upstream
    cache = tmp_path / "cache"
    manga.install(ref=commit, source=repo, root=cache, say=lambda _m: None)
    (cache / "manga-editor-desu" / "secret.txt").write_text("secret", encoding="utf-8")
    site = manga.MangaSite(cache)
    assert site.file("js/app.js") is not None
    for bad in ("../secret.txt", "js/../../secret.txt", "js\\..\\..\\secret.txt", "/etc/hostname", "", "js//app.js",
                ".git/config", "third/GoogleAnalytics/googleAnalytics.js", "manifest.json"):
        assert site.file(bad) is None, bad
    client = app_client(images, cache)
    for url in ("/manga/../secret.txt", "/manga/%2e%2e/secret.txt", "/manga/js/%2e%2e/%2e%2e/secret.txt",
                "/manga/..%2fsecret.txt"):
        r = client.get(url)
        assert r.status_code in (301, 308, 404) and b"secret" not in r.data, url


def test_bridge_files_are_served(tmp_path, images):
    client = app_client(images, tmp_path / "cache")
    r = client.get("/manga-bridge/bridge.js")
    assert r.status_code == 200 and b"inkdeusBridge" in r.data
    assert client.get("/manga-bridge/bridge.css").status_code == 200
    assert b'href="/manga/"' in client.get("/").data               # 本体の画面から行き来できる


# ---- 漫画データの保存 ----

def manga_bytes(pages: int = 1, payload: bytes = b"compressed-bytes") -> bytes:
    """漫画エディタの保存形式と同じ枠（見出しの長さ・JSON の見出し・本体）を持つデータ"""
    header = json.dumps([{"name": f"lz4_part_{i}.lz4", "size": 10 + i} for i in range(pages)]).encode("utf-8")
    return struct.pack("<I", len(header)) + header + payload


def png_bytes(value: int = 128) -> bytes:
    buf = io.BytesIO()
    Image.new("L", (8, 6), value).save(buf, "PNG")
    return buf.getvalue()


def test_manga_project_roundtrip(tmp_path, images):
    client = app_client(images, tmp_path / "cache")
    folder = images / "inkdeus_out" / "manga"                      # プロジェクトなしの起動では <out>/manga/
    listing = client.get("/api/manga/projects").get_json()
    assert listing["projects"] == [] and listing["dir"] == str(folder) and listing["autosaveName"] == "autosave"
    assert client.get("/api/manga/project/第1話").status_code == 404
    data = manga_bytes(2)
    r = client.put("/api/manga/project/第1話", data=data)
    assert r.status_code == 200 and r.get_json()["bytes"] == len(data)
    assert (folder / "第1話.lz4").read_bytes() == data
    assert not list(folder.glob("*.tmp"))
    r = client.get("/api/manga/project/第1話")
    assert r.status_code == 200 and r.data == data and r.headers["Cache-Control"] == "no-store"
    newer = manga_bytes(3, b"x" * 50)
    assert client.put("/api/manga/project/第1話", data=newer).status_code == 200          # 上書き
    assert client.get("/api/manga/project/第1話").data == newer
    assert client.put("/api/manga/project/autosave", data=data).status_code == 200
    (folder / "notes.txt").write_text("ignored", encoding="utf-8")
    rows = {p["name"]: p for p in client.get("/api/manga/projects").get_json()["projects"]}
    assert set(rows) == {"第1話", "autosave"}
    assert rows["第1話"]["bytes"] == len(newer) and rows["第1話"]["autosave"] is False and rows["第1話"]["mtime"] > 0
    assert rows["autosave"]["autosave"] is True


def test_manga_project_rejects_bad_names_and_data(tmp_path, images):
    client = app_client(images, tmp_path / "cache")
    data = manga_bytes()
    for name in ("a.b", "..", "a b", "x" * 65, "%2e%2e", "a%2fb", "a:b", "名前!"):
        r = client.put(f"/api/manga/project/{name}", data=data)
        assert r.status_code in (400, 404, 405), name
    assert not (images / "inkdeus_out" / "manga").exists() or not list((images / "inkdeus_out" / "manga").iterdir())
    for bad in (b"", b"abc", b"\xff\xff\xff\xffrest", struct.pack("<I", 4) + b"oops" + b"data",
                struct.pack("<I", 2) + b"{}" + b"data"):
        assert client.put("/api/manga/project/ok", data=bad).status_code == 400
    assert client.put("/api/manga/project/ok", data=data, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.put("/api/manga/project/ok", data=data,
                      headers={"Origin": "http://localhost", "Host": "localhost"}).status_code == 200


def test_manga_pages(tmp_path, images):
    client = app_client(images, tmp_path / "cache")
    assert client.get("/api/manga/pages/第1話").get_json()["pages"] == []
    r = client.post("/api/manga/pages/第1話/2.png", data=png_bytes(10))
    assert r.status_code == 200
    saved = images / "inkdeus_out" / "manga" / "pages" / "第1話" / "002.png"
    assert r.get_json()["path"] == str(saved) and saved.read_bytes() == png_bytes(10)
    assert client.post("/api/manga/pages/第1話/1.png", data=png_bytes(20)).status_code == 200
    assert client.post("/api/manga/pages/第1話/2.png", data=png_bytes(30)).status_code == 200      # 上書き
    listing = client.get("/api/manga/pages/第1話").get_json()
    assert [p["number"] for p in listing["pages"]] == [1, 2] and listing["pages"][1]["file"] == "002.png"
    assert listing["dir"] == str(saved.parent) and saved.read_bytes() == png_bytes(30)
    assert client.post("/api/manga/pages/第1話/3.png", data=b"not a png").status_code == 400
    assert client.post("/api/manga/pages/第1話/0.png", data=png_bytes()).status_code == 400
    assert client.post("/api/manga/pages/a.b/1.png", data=png_bytes()).status_code == 400
    assert client.post("/api/manga/pages/第1話/x.png", data=png_bytes()).status_code in (404, 405)
    assert client.post("/api/manga/pages/第1話/1.png", data=png_bytes(),
                       headers={"Origin": "http://evil.example"}).status_code == 403


def test_manga_data_goes_to_the_project_folder(tmp_path, images):
    from inkdeus.projects import create_project

    project, _ = create_project("p1", images, root=tmp_path / "projects")
    app = create_app(project.source, project.masks, project.out, project=project.info(),
                     local_config=tmp_path / "none.toml", cache_root=tmp_path / "cache", watch=False)
    client = app.test_client()
    assert client.put("/api/manga/project/x", data=manga_bytes()).status_code == 200
    assert (project.path / "manga" / "x.lz4").is_file() and project.manga == project.path / "manga"
    assert client.post("/api/manga/pages/x/1.png", data=png_bytes()).status_code == 200
    assert (project.path / "manga" / "pages" / "x" / "001.png").is_file()
    assert client.get("/api/state").get_json()["manga"] == str(project.path / "manga")
    assert client.get("/api/manga/status").get_json()["project"] == project.info()


# ---- 連携スクリプト（対応していない版に差し込まれたとき） ----

def test_bridge_reports_unsupported_version(tmp_path, images, upstream, browser):
    """必要な関数を持たないページ（ここでは合成の「上流」）では、機能を無効にしてその旨を表示する"""
    from conftest import serve

    repo, commit = upstream
    cache = tmp_path / "cache"
    manga.install(ref=commit, source=repo, root=cache, say=lambda _m: None)
    server, url = serve(create_app(images, local_config=images / "none.toml", cache_root=cache, watch=False))
    page = browser.new_page(viewport={"width": 1200, "height": 800})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(url + "manga/")
        status = page.evaluate("() => window.inkdeusBridge.ready")
        assert status["ok"] is False and not any(status["features"].values())
        assert "putImageInFrame" in status["missing"]["place"] and "lz4Compressor" in status["missing"]["save"]
        message = page.evaluate("() => document.querySelector('.ikb-msg').textContent")
        assert "対応していない版です" in message and "putImageInFrame" in message
        assert page.evaluate("() => document.getElementById('app').textContent") == "booted"   # 元のページは動いている
        assert page.evaluate("() => window.fakeEditor") is True
        # 素材の一覧は出るが、配置・保存・書き出しは使えない
        page.wait_for_selector(".ikb-item[data-path='a.png']")
        for call in ("placeAsset('source', 'a.png')", "save('x')", "exportPage()", "open('x', {force: true})"):
            with pytest.raises(Exception, match="対応していない版"):
                page.evaluate(f"() => window.inkdeusBridge.{call}")
        disabled = page.evaluate("""() => Array.from(document.querySelectorAll('.ikb-file button:not(.ikb-icon)'))
            .map(b => b.disabled)""")
        assert disabled and all(disabled)
        page.click(".ikb-item[data-path='a.png']")                # 押しても例外を外へ漏らさず、パネル内に表示する
        page.wait_for_function("() => document.querySelector('.ikb-msg').dataset.kind === 'error'")
        assert errors == []
    finally:
        page.close()
        server.shutdown()


def test_panel_width_can_be_dragged_and_is_remembered(tmp_path, upstream, browser):
    """素材パネルの幅: 左端のつまみのドラッグ・列数・保存と復元・既定に戻す・重ねて表示"""
    from conftest import serve

    repo, commit = upstream
    cache = tmp_path / "cache"
    manga.install(ref=commit, source=repo, root=cache, say=lambda _m: None)
    folder = tmp_path / "many"
    for i in range(12):
        synth.save(folder / f"img{i:02d}.png", synth.flat(40, 30, 20 * i))
    server, url = serve(create_app(folder, local_config=tmp_path / "none.toml", cache_root=cache, watch=False))
    context = browser.new_context(viewport={"width": 1400, "height": 800})
    page = context.new_page()

    def measure():
        return page.evaluate("""() => { const cs = (e) => getComputedStyle(e);
            const grid = document.querySelector('.ikb-grid');
            return {panel: document.querySelector('#inkdeus-bridge').getBoundingClientRect().width - 26,
                    columns: cs(grid).gridTemplateColumns.split(' ').length,
                    padding: parseFloat(cs(document.body).paddingRight),
                    stored: JSON.parse(localStorage.getItem('inkdeus.manga.panelWidth')),
                    width: window.inkdeusBridge.status().width, float: window.inkdeusBridge.status().float}; }""")

    def drag_to(x: float):
        box = page.locator(".ikb-resize").bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 200)
        page.mouse.down()
        page.mouse.move(x + 40, box["y"] + 210, steps=4)
        page.mouse.move(x, box["y"] + 220, steps=4)
        page.mouse.up()

    try:
        page.goto(url + "manga/")
        page.evaluate("() => window.inkdeusBridge.ready")
        page.wait_for_selector(".ikb-item[data-path='img11.png']")
        first = measure()
        assert first["panel"] == 300 and first["columns"] == 3 and first["padding"] == 326 and first["stored"] is None
        drag_to(1400 - 26 - 560)                                      # 幅 560px まで広げる
        wide = measure()
        assert wide["panel"] == pytest.approx(560, abs=3) and wide["width"] == pytest.approx(560, abs=3)
        assert wide["columns"] > first["columns"]                     # 広げると列が増える
        assert wide["padding"] == pytest.approx(wide["panel"] + 26, abs=1)        # 漫画エディタ側の余白も追従する
        assert wide["stored"] == wide["width"]
        drag_to(1400 - 26 - 50)                                       # 狭めすぎても最小 220px
        assert measure()["panel"] == 220 and measure()["columns"] == 2
        drag_to(20)                                                   # 広げすぎても画面幅の 70% まで
        assert measure()["panel"] == 980
        drag_to(1400 - 26 - 480)
        kept = measure()["width"]
        page.reload()                                                 # 幅は次回も復元される
        page.evaluate("() => window.inkdeusBridge.ready")
        assert measure()["panel"] == kept == pytest.approx(480, abs=3)
        # 重ねて表示: 漫画エディタの画面を押しのけない
        page.click(".ikb-head .ikb-icon")
        floating = measure()
        assert floating["float"] is True and floating["padding"] == 26 and floating["panel"] == kept
        page.reload()
        page.evaluate("() => window.inkdeusBridge.ready")
        assert measure()["float"] is True and measure()["padding"] == 26
        page.click(".ikb-head .ikb-icon")
        assert measure()["padding"] == pytest.approx(kept + 26, abs=1)
        page.dblclick(".ikb-resize")                                  # ダブルクリックで既定の幅
        back = measure()
        assert back["panel"] == 300 and back["stored"] is None and back["columns"] == 3
    finally:
        context.close()
        server.shutdown()
