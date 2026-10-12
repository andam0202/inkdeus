"""漫画のページ構成: 別プロジェクトの漫画エディタを利用者の手元に取得し、/manga/ として配信する。

対象は new-sankaku 氏の「Manga Editor Desu! Pro Edition」（GPL-3.0）。inkdeus のリポジトリには
1ファイルも含めず、`inkdeus manga install` で利用者のキャッシュへ取得する。配信時に index.html だけを
書き換え（キャッシュ上のファイルは変えない）、アクセス解析と service worker を外して、
inkdeus の連携スクリプト（web/manga-bridge/）を差し込む。
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

UPSTREAM_NAME = "manga-editor-desu"
UPSTREAM_URL = "https://github.com/new-sankaku/manga-editor-desu"
# 動作を確かめてある上流の版。連携スクリプトは上流の関数名・DOM に依存するので、既定ではこの版に固定する
PINNED_COMMIT = "04e0cf3de1f3677682f6d1831f2713db70e441ba"

CURRENT_FILE = "current"                    # いま配信する版（コミット）を1行で書いたファイル
MARKER_FILE = ".inkdeus-install.json"       # 取得が最後まで終わった印（取得元と日時）
BRIDGE_CSS = '<link rel="stylesheet" href="/manga-bridge/bridge.css">'
BRIDGE_JS = '<script src="/manga-bridge/bridge.js"></script>'

log = logging.getLogger("inkdeus.manga")


class MangaError(Exception):
    """利用者に見せるエラー"""


# ---- キャッシュ ----

def cache_root(environ: dict | None = None) -> Path:
    """キャッシュ置き場。環境変数 INKDEUS_CACHE → 既定（~/.cache/inkdeus）"""
    env = os.environ if environ is None else environ
    return Path(env.get("INKDEUS_CACHE") or "~/.cache/inkdeus").expanduser().resolve()


def upstream_root(root: Path | None = None) -> Path:
    return (cache_root() if root is None else Path(root)) / UPSTREAM_NAME


def _is_commit(text: str) -> bool:
    return re.fullmatch(r"[0-9a-f]{7,64}", text) is not None


def _installed(path: Path) -> bool:
    return (path / MARKER_FILE).is_file() and (path / "index.html").is_file()


def installed_dir(root: Path | None = None) -> Path | None:
    """配信する版のフォルダ（未導入なら None）。current に書かれた版 → 固定している版 の順に探す"""
    base = upstream_root(root)
    names = []
    try:
        names.append((base / CURRENT_FILE).read_text(encoding="utf-8").strip())
    except OSError:
        pass
    names.append(PINNED_COMMIT)
    for name in names:
        if _is_commit(name) and _installed(base / name):
            return base / name
    return None


def status(root: Path | None = None) -> dict:
    base = upstream_root(root)
    path = installed_dir(root)
    info: dict = {"installed": path is not None, "cache": str(base), "pinned": PINNED_COMMIT,
                  "upstream": UPSTREAM_URL}
    if path is not None:
        try:
            marker = json.loads((path / MARKER_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            marker = {}
        info.update(path=str(path), commit=path.name, matchesPinned=path.name == PINNED_COMMIT,
                    source=marker.get("source"), installedAt=marker.get("installedAt"))
    return info


# ---- 取得 ----

def _git(args: list[str], cwd: Path | None = None, binary: bool = False):
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False)
    except FileNotFoundError as e:
        raise MangaError("git が見つかりません（取得には git が必要です）") from e
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise MangaError(f"git {' '.join(args[:2])} に失敗しました: {detail[-1] if detail else proc.returncode}")
    return proc.stdout if binary else proc.stdout.decode("utf-8", "replace").strip()


def _export_commit(repo: Path, commit: str, dest: Path) -> None:
    """repo の commit の内容を dest へ展開する（.git は含まれない）"""
    data = _git(["-C", str(repo), "archive", "--format=tar", commit], binary=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest, filter="data")


def _from_local(clone: Path, ref: str, allow_mismatch: bool, dest: Path, say) -> str:
    clone = Path(clone).expanduser().resolve()
    if not clone.is_dir():
        raise MangaError(f"--from のフォルダが見つかりません: {clone}")
    try:
        head = _git(["-C", str(clone), "rev-parse", "HEAD"])
    except MangaError as e:
        raise MangaError(f"--from は git のクローンである必要があります: {clone}（{e}）") from e
    try:
        wanted = _git(["-C", str(clone), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    except MangaError:
        wanted = None                              # 指定の版がクローンに無い（または版の書き方が違う）
    if wanted != head:
        if not allow_mismatch:
            raise MangaError(f"--from のクローンが指定の版ではありません\n"
                             f"  指定の版: {ref}\n"
                             f"  クローンの現在の版: {head}\n"
                             f"  クローン側で git checkout {ref} するか、クローンの版のまま使うなら "
                             "--allow-mismatch を付けてください（動作は保証されません）")
        say(f"警告: クローンの現在の版 {head} は指定の版 {ref} と違いますが、そのまま使います")
    # コミット済みの内容だけを取り出す（未コミットの変更や管理外のファイルは含めない）
    _export_commit(clone, head, dest)
    return head


def _from_network(url: str, ref: str, dest: Path, say) -> str:
    say(f"取得中: {url}（{ref}）")
    with tempfile.TemporaryDirectory(prefix="inkdeus-manga-") as tmp:
        repo = Path(tmp)
        _git(["init", "--quiet"], cwd=repo)
        _git(["remote", "add", "origin", url], cwd=repo)
        _git(["fetch", "--quiet", "--depth", "1", "origin", ref], cwd=repo)
        commit = _git(["rev-parse", "FETCH_HEAD"], cwd=repo)
        _export_commit(repo, commit, dest)
    return commit


def install(ref: str = PINNED_COMMIT, source: Path | str | None = None, allow_mismatch: bool = False,
            root: Path | None = None, url: str = UPSTREAM_URL, say=print) -> dict:
    """上流を取得してキャッシュへ置く。source があればそのローカルのクローンから（ネットワーク不要）"""
    base = upstream_root(root)
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".install-", dir=base))
    try:
        if source is not None:
            commit = _from_local(Path(source), ref, allow_mismatch, work, say)
            origin = "local"
        else:
            commit = _from_network(url, ref, work, say)
            origin = url
        if not (work / "index.html").is_file():
            raise MangaError("取得した内容に index.html がありません（対象のリポジトリではないようです）")
        shutil.rmtree(work / ".git", ignore_errors=True)
        marker = {"commit": commit, "source": origin,
                  "installedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
        (work / MARKER_FILE).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
        target = base / commit
        if target.exists():
            shutil.rmtree(target)
        os.replace(work, target)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    tmp = base / (CURRENT_FILE + ".tmp")
    tmp.write_text(commit + "\n", encoding="utf-8")
    os.replace(tmp, base / CURRENT_FILE)
    if commit != PINNED_COMMIT:
        say(f"注意: 動作を確かめてある版（{PINNED_COMMIT}）とは別の版です。連携が動かないことがあります")
    return status(root)


# ---- 配信時の書き換え ----

# index.html から取り除く物。上流の実物（PINNED_COMMIT）を読んで決めたパターンで、
# 見つからないとき（上流の版が違うとき）は警告だけ出して続ける。
# 各項目は (説明, [正規表現…], 置き換え)。正規表現のどれか1つでも当たればよい。
REMOVALS: list[tuple[str, list[str], str]] = [
    # <script async src="third/GoogleAnalytics/googleAnalytics.js"></script>
    # （gtag の本体を同梱した物。版によっては googletagmanager.com から直接読む形もありうる）
    ("アクセス解析の読み込み", [
        r"[ \t]*<script\b[^>]*\bsrc=[\"'][^\"']*GoogleAnalytics/[^\"']*[\"'][^>]*>\s*</script>[ \t]*\n?",
        r"[ \t]*<script\b[^>]*\bsrc=[\"']https?://[^\"']*(?:googletagmanager|google-analytics)\.com/[^\"']*[\"'][^>]*>"
        r"\s*</script>[ \t]*\n?",
    ], ""),
    # 起動アニメーションと同じ <script> の後半にある
    #   // ========== Analytics ==========
    #   window.dataLayer = …; function gtag(){…}; gtag('config', …); function trackCustomEvent(…){…}
    #   クリック・スクロール・読み込み・離脱のたびに trackCustomEvent(…) を呼ぶリスナー
    # を、その <script> の終わりまで丸ごと外す。関数名だけは空の物を残す（他から呼ばれても落ちないように）
    ("アクセス解析の設定と送信", [
        r"//[ \t]*=+[ \t]*Analytics[ \t]*=+.*?(?=</script>)",
    ], "window.dataLayer=[];function gtag(){}function trackCustomEvent(){}\n    "),
    # <script src="js/core/service/worker-register.js?v=…" defer></script>
    # （load 時に navigator.serviceWorker.register("./service-worker.js") を呼ぶ。PWA の導入ボタンの処理も同じファイル）
    ("service worker の登録", [
        r"[ \t]*<script\b[^>]*\bsrc=[\"'][^\"']*worker-register\.js[^\"']*[\"'][^>]*>\s*</script>[ \t]*\n?",
    ], ""),
    # <link rel="manifest" href="manifest.json">
    ("PWA の manifest", [
        r"[ \t]*<link\b[^>]*\brel=[\"']manifest[\"'][^>]*>[ \t]*\n?",
    ], ""),
]


@dataclass
class Rewrite:
    html: str
    removed: list[str]
    missing: list[str]
    inserted: list[str]


def rewrite_index(html: str) -> Rewrite:
    removed, missing, inserted = [], [], []
    for label, patterns, replacement in REMOVALS:
        hit = 0
        for pattern in patterns:
            html, n = re.subn(pattern, lambda _m, r=replacement: r, html, flags=re.S | re.I)
            hit += n
        (removed if hit else missing).append(label)
    head = re.search(r"</head\s*>", html, flags=re.I)
    if head:
        html = html[:head.start()] + BRIDGE_CSS + "\n" + html[head.start():]
        inserted.append("bridge.css")
    else:
        missing.append("</head>（bridge.css の差し込み先）")
    bodies = list(re.finditer(r"</body\s*>", html, flags=re.I))
    if bodies:
        pos = bodies[-1].start()
        html = html[:pos] + BRIDGE_JS + "\n" + html[pos:]
        inserted.append("bridge.js")
    else:
        html += "\n" + BRIDGE_JS + "\n"
        inserted.append("bridge.js")
        missing.append("</body>（bridge.js は末尾に足しました）")
    return Rewrite(html, removed, missing, inserted)


# 以前に登録された service worker が残っている場合に備え、同じ URL で「自分を外して全キャッシュを消すだけ」の物を返す
SW_KILL_SCRIPT = """\
// inkdeus: この場所では service worker を使いません。残っている登録とキャッシュを片付けます。
self.addEventListener('install', function () { self.skipWaiting(); });
self.addEventListener('activate', function (event) {
  event.waitUntil((async function () {
    try {
      var keys = await caches.keys();
      await Promise.all(keys.map(function (key) { return caches.delete(key); }));
    } catch (e) { /* 消せなくても登録の解除は進める */ }
    await self.registration.unregister();
    var clients = await self.clients.matchAll({ type: 'window' });
    clients.forEach(function (client) { client.navigate(client.url); });
  })());
});
"""

NOT_INSTALLED_HTML = """\
<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>inkdeus — 漫画の構成</title>
<style>
  body { font-family: system-ui, sans-serif; background: #1b1c1f; color: #e6e6e6; line-height: 1.8;
         max-width: 46rem; margin: 3rem auto; padding: 0 1.2rem; }
  code, pre { background: #2a2c31; border-radius: 4px; padding: 2px 6px; }
  pre { padding: 10px 12px; overflow-x: auto; }
  a { color: #8ab4ff; }
</style>
</head>
<body data-inkdeus-manga="not-installed">
<h1>漫画の構成はまだ使えません</h1>
<p>漫画のページ構成には、別プロジェクトの漫画エディタ（Manga Editor Desu! Pro Edition、new-sankaku 氏、GPL-3.0）を使います。
inkdeus には含まれていないので、次のコマンドを実行してこのPCへ取得してください。</p>
<pre>uv run inkdeus manga install</pre>
<p>取得が終わったら、このページを再読み込みしてください（サーバの再起動は不要です）。</p>
<p><a href="/">白黒化の画面へ戻る</a></p>
</body>
</html>
"""

# 配信しないパス（先頭一致）。service-worker.js は別扱い（上の自己解除スクリプトを返す）
BLOCKED_PREFIXES = ("third/GoogleAnalytics/",)
BLOCKED_FILES = ("manifest.json",)


class MangaSite:
    """/manga/ の配信内容を決める（Flask には依存しない）"""

    def __init__(self, root: Path | None = None):
        self.root = root
        self._logged: set[str] = set()

    def dir(self) -> Path | None:
        return installed_dir(self.root)

    def index(self) -> str | None:
        """書き換え済みの index.html（未導入なら None）"""
        base = self.dir()
        if base is None:
            return None
        result = rewrite_index((base / "index.html").read_text(encoding="utf-8"))
        if base.name not in self._logged:                 # 何をしたかは版ごとに1回だけ出す
            self._logged.add(base.name)
            log.info("manga: %s の index.html を書き換えて配信します（除去: %s / 挿入: %s）",
                     base.name[:12], "、".join(result.removed) or "なし", "、".join(result.inserted))
            for label in result.missing:
                log.warning("manga: index.html に見つかりませんでした: %s（上流の版が違う可能性があります）", label)
        return result.html

    def file(self, sub: str) -> Path | None:
        """配信してよい静的ファイルの実パス（だめなら None）"""
        base = self.dir()
        if base is None or not sub or "\\" in sub or "\x00" in sub:
            return None
        parts = sub.split("/")
        if any(p in ("", ".", "..") for p in parts) or parts[0].startswith(".git") or parts[-1] == MARKER_FILE:
            return None
        if sub in BLOCKED_FILES or any(sub.startswith(p) for p in BLOCKED_PREFIXES):
            return None
        path = (base / sub).resolve()
        if not path.is_relative_to(base.resolve()) or not path.is_file():
            return None
        return path


# ---- CLI ----

def cmd_install(args) -> int:
    info = install(ref=args.ref, source=args.source, allow_mismatch=args.allow_mismatch)
    print(f"導入しました: {info['commit']}")
    print(f"  場所: {info['path']}")
    print("  inkdeus を起動（run / open）して、ブラウザで /manga/ を開いてください")
    return 0


def cmd_status(args) -> int:
    info = status()
    if getattr(args, "json", False):
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    if not info["installed"]:
        print("未導入です（inkdeus manga install で取得できます）")
        print(f"  キャッシュ置き場: {info['cache']}")
        print(f"  固定している版: {info['pinned']}")
        return 0
    note = "" if info["matchesPinned"] else f"（固定している版 {info['pinned']} とは別の版）"
    print(f"導入済み: {info['commit']}{note}")
    print(f"  場所: {info['path']}")
    print(f"  取得元: {info.get('source') or '不明'} / 取得日時: {info.get('installedAt') or '不明'}")
    return 0


def main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="inkdeus manga",
        description="漫画のページ構成に使う漫画エディタ（Manga Editor Desu! Pro Edition、別プロジェクト・GPL-3.0）を"
                    "このPCへ取得する。取得した物は inkdeus のサーバが /manga/ で配信する")
    sub = ap.add_subparsers(dest="command", required=True, metavar="<command>")
    p = sub.add_parser("install", help="上流を取得してキャッシュへ置く")
    p.add_argument("--ref", default=PINNED_COMMIT, help=f"取得する版（コミット）。既定: {PINNED_COMMIT}")
    p.add_argument("--from", dest="source", metavar="DIR",
                   help="取得済みのローカルのクローンから取り出す（ネットワークを使わない）")
    p.add_argument("--allow-mismatch", action="store_true",
                   help="--from のクローンに指定の版が無いとき、クローンの現在の版をそのまま使う")
    p = sub.add_parser("status", help="導入済みの版と場所を表示する")
    p.add_argument("--json", action="store_true", help="JSON で出力")
    args = ap.parse_args(argv)
    try:
        return {"install": cmd_install, "status": cmd_status}[args.command](args)
    except MangaError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
