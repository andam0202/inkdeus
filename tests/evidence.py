"""失敗した結合テストの証拠を残す仕掛け。

テストが落ちた時点で、開いている全ページ（白黒化の画面・漫画の画面）について
    コンソールログ・ページエラー・通信の失敗（時刻つき）、スクリーンショット、
    連携スクリプト（window.inkdeusBridge）と白黒化の画面（window.inkdeus）の状態
を、サーバごとの
    リクエストの記録、配信したイベント（SSE）の列、購読の出入り（どれも時刻つき）
と一緒に、テストごとのフォルダへ保存する。

保存先は環境変数 INKDEUS_TEST_EVIDENCE（既定は一時フォルダの inkdeus-test-evidence/<起動時刻>-<pid>/）。
INKDEUS_TEST_EVIDENCE_ALWAYS=1 で、通ったテストの分も残す（再現できない不具合を追うとき用）。
テスト画像はすべて合成なので、スクリーンショットを残して構わない。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

KEEP = 4000                       # ページ・サーバごとに覚えておく行数
_T0 = time.time()
_lock = threading.Lock()
_pages: list["PageLog"] = []
_servers: list["ServerLog"] = []
_root: Path | None = None


def stamp(t: float | None = None) -> str:
    t = time.time() if t is None else t
    return f"{time.strftime('%H:%M:%S', time.localtime(t))}.{int(t * 1000) % 1000:03d} (+{t - _T0:8.3f}s)"


def root() -> Path:
    global _root
    if _root is None:
        given = os.environ.get("INKDEUS_TEST_EVIDENCE")
        base = Path(given) if given else Path(tempfile.gettempdir()) / "inkdeus-test-evidence"
        _root = base / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    return _root


# ---- ページ ----

BRIDGE_STATE = """() => {
  const out = {url: location.href, visibility: document.visibilityState, focus: document.hasFocus()};
  try {
    if (window.inkdeusBridge) {
      const b = window.inkdeusBridge;
      out.bridge = b.status();
      out.bridgeDebug = typeof b.debug === 'function' ? b.debug() : null;
      out.message = (document.querySelector('.ikb-msg') || {}).textContent || '';
      if (typeof canvas !== 'undefined' && canvas) {
        out.canvasObjects = canvas.getObjects().map((o) => ({type: o.type, name: o.name || null, guid: o.guid || null,
          src: o.type === 'image' && o.getSrc ? o.getSrc() : null,
          loaded: o._element ? (o._element.currentSrc || o._element.src || '').slice(0, 300) : null,
          natural: o._element ? o._element.naturalWidth : null, version: o.__ikbVersion || null,
          panel: o.relatedPoly ? o.relatedPoly.guid : null}));
        out.busy = typeof isProjectBusy === 'function' ? isProjectBusy() : null;
        out.history = typeof stateStack !== 'undefined' ? [stateStack.length, currentStateIndex] : null;
      }
    }
  } catch (e) { out.bridgeError = String(e); }
  try {
    if (window.inkdeus && typeof window.inkdeus.info === 'function') {
      out.editor = window.inkdeus.info();
      if (typeof window.inkdeus.autoExport === 'function') out.autoExport = window.inkdeus.autoExport();
      if (typeof window.inkdeus.saveStatus === 'function') out.saveStatus = window.inkdeus.saveStatus();
      const text = (id) => (document.getElementById(id) || {}).textContent || '';
      out.labels = {save: text('saveState'), export: text('exportState'), message: text('message')};
    }
  } catch (e) { out.editorError = String(e); }
  return out;
}"""


class PageLog:
    def __init__(self, page, label: str):
        self.page, self.label = page, label
        self.lines: deque[str] = deque(maxlen=KEEP)
        self.opened = time.time()
        self.closed: float | None = None
        page.on("console", lambda m: self.add(f"console.{m.type}", m.text))
        page.on("pageerror", lambda e: self.add("pageerror", str(e)))
        page.on("requestfailed", lambda r: self.add("requestfailed", f"{r.method} {r.url} — {r.failure}"))
        page.on("response", self._response)
        page.on("framenavigated", lambda f: self.add("navigated", f.url) if f == page.main_frame else None)
        page.on("close", lambda *_a: self._closed())
        page.on("crash", lambda *_a: self.add("crash", "ページが落ちました"))

    def add(self, kind: str, text: str) -> None:
        self.lines.append(f"{stamp()} [{kind}] {text}")

    def _response(self, r) -> None:
        if r.status >= 400:
            self.add("http", f"{r.status} {r.request.method} {r.url}")

    def _closed(self) -> None:
        self.closed = time.time()
        self.add("close", "ページを閉じました")


def watch_page(page, label: str | None = None) -> None:
    with _lock:
        if any(p.page is page for p in _pages):
            return
        _pages.append(PageLog(page, label or f"page{len(_pages) + 1:03d}"))
        # 閉じてから時間のたった物は捨てる（全件実行で溜めない）
        cutoff = time.time() - 600
        _pages[:] = [p for p in _pages if p.closed is None or p.closed > cutoff]


def watch_browser(browser) -> None:
    """browser.new_page / new_context を包んで、作られるページを全部見張る"""
    new_page, new_context = browser.new_page, browser.new_context

    def wrapped_page(*a, **kw):
        page = new_page(*a, **kw)
        watch_page(page)
        return page

    def wrapped_context(*a, **kw):
        context = new_context(*a, **kw)
        context.on("page", lambda p: watch_page(p))
        return context

    browser.new_page, browser.new_context = wrapped_page, wrapped_context


# ---- サーバ ----

class ServerLog:
    """リクエストの記録と、配信したイベントの列。serve() が作る"""

    def __init__(self, app, url_of):
        self.app, self.url_of = app, url_of
        self.requests: deque[str] = deque(maxlen=KEEP)
        self.events: deque[str] = deque(maxlen=KEEP)
        session = app.config.get("INKDEUS_SESSION")
        hub = getattr(session, "hub", None)
        if hub is not None:
            self._wrap_hub(hub)
        inner = app.wsgi_app

        def middleware(environ, start_response):
            began = time.time()
            path = environ.get("PATH_INFO", "") + ("?" + environ["QUERY_STRING"] if environ.get("QUERY_STRING") else "")
            seen = {}

            def start(status, headers, exc_info=None):
                seen["status"] = status.split(" ")[0]
                return start_response(status, headers, exc_info)

            try:
                return inner(environ, start)
            finally:
                self.requests.append(f"{stamp(began)} {environ.get('REQUEST_METHOD')} {path} → {seen.get('status', '?')}"
                                     f" ({(time.time() - began) * 1000:.0f} ms)")

        app.wsgi_app = middleware

    def _wrap_hub(self, hub) -> None:
        publish, subscribe, unsubscribe, send_listener = hub.publish, hub.subscribe, hub.unsubscribe, hub.send_listener

        def people() -> str:
            return f"購読 {len(hub._clients)}（うち漫画 {len(hub._listeners)}）"

        def wrapped_publish(event):
            self.events.append(f"{stamp()} publish {json.dumps(event, ensure_ascii=False)} / {people()}")
            return publish(event)

        def wrapped_subscribe(listener=False):
            q = subscribe(listener)
            self.events.append(f"{stamp()} subscribe {'manga' if listener else 'editor'} q={id(q):x} / {people()}")
            return q

        def wrapped_unsubscribe(q):
            unsubscribe(q)
            self.events.append(f"{stamp()} unsubscribe q={id(q):x} / {people()}")

        def wrapped_send(event):
            self.events.append(f"{stamp()} send_listener {json.dumps(event, ensure_ascii=False)} / {people()}")
            return send_listener(event)

        hub.publish, hub.subscribe, hub.unsubscribe, hub.send_listener = (
            wrapped_publish, wrapped_subscribe, wrapped_unsubscribe, wrapped_send)


def watch_server(app, url_of) -> ServerLog:
    log = ServerLog(app, url_of)
    with _lock:
        _servers.append(log)
        del _servers[:-40]
    return log


# ---- 保存 ----

def _safe(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", name).strip("_")[:120] or "test"


def dump(nodeid: str, phase: str, longrepr: str = "") -> Path | None:
    """いまの状態をテストごとのフォルダへ書き出す。失敗しても例外は外へ出さない"""
    try:
        folder = root() / _safe(nodeid.split("::", 1)[-1] if "::" in nodeid else nodeid)
        if phase != "call":
            folder = folder.with_name(folder.name + "." + phase)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "failure.txt").write_text(f"{nodeid} [{phase}] {stamp()}\n\n{longrepr}\n", encoding="utf-8")
        with _lock:
            pages, servers = list(_pages), list(_servers)
        for log in pages:
            base = folder / log.label
            head = f"# {log.label}  opened {stamp(log.opened)}" + (f"  closed {stamp(log.closed)}" if log.closed else "")
            state = None
            if log.closed is None:
                try:
                    head += f"  url {log.page.url}"
                    state = log.page.evaluate(BRIDGE_STATE)
                except Exception as e:  # noqa: BLE001
                    state = {"error": f"状態を読めません: {e}"}
                try:
                    log.page.screenshot(path=str(base) + ".png", timeout=15_000)
                except Exception as e:  # noqa: BLE001
                    head += f"\n# スクリーンショットを撮れません: {e}"
            elif time.time() - log.closed > 120 and not log.lines:
                continue
            (Path(str(base) + ".log")).write_text(head + "\n" + "\n".join(log.lines) + "\n", encoding="utf-8")
            if state is not None:
                Path(str(base) + ".state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str),
                                                           encoding="utf-8")
        for i, log in enumerate(servers, 1):
            url = ""
            try:
                url = log.url_of() or ""
            except Exception:  # noqa: BLE001
                pass
            port = url.rstrip("/").rsplit(":", 1)[-1] if url else f"{i:02d}"
            if not log.requests and not log.events:
                continue
            (folder / f"server-{port}.requests.log").write_text(f"# {url}\n" + "\n".join(log.requests) + "\n",
                                                                encoding="utf-8")
            (folder / f"server-{port}.events.log").write_text(f"# {url}\n" + "\n".join(log.events) + "\n",
                                                              encoding="utf-8")
        return folder
    except Exception as e:  # noqa: BLE001 — 証拠を残せなくても、テストの結果は変えない
        try:
            print(f"[evidence] 保存に失敗: {e}")
        except Exception:  # noqa: BLE001
            pass
        return None
