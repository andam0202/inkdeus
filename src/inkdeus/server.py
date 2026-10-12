"""静的ファイル配信とファイル入出力、外部からの操作の中継だけを行う小さなサーバ。

描画ロジックは一切持たない（すべてブラウザの WebGL2 側）。設定JSON の検証だけは
schema.py（定義は web/schema.json）で行う。
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import struct
import threading
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, Response, abort, jsonify, redirect, request, send_file, send_from_directory
from PIL import Image

from . import assets as assets_mod
from . import folders as folders_mod
from . import fonts as fonts_mod
from . import manga as manga_mod
from . import schema

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_PARAMS_BYTES = 1 << 20
MAX_EXPORT_BYTES = 1 << 30
LOCAL_CONFIG_NAME = "inkdeus.local.toml"
PARAMS_SUFFIX = ".inkdeus.json"
WATCH_INTERVAL = 0.5
HEARTBEAT_SECONDS = 3.0
MANGA_NAME_RE = re.compile(r"[\w\-]{1,64}")           # 漫画データの名前（パス区切りや . を含められない）
MANGA_AUTOSAVE = "autosave"                            # 自動保存用の予約名
MANGA_SUFFIX = ".lz4"
VERSION_SEP = "@"                                      # <stem>@<バージョン>.png / .inkdeus.json
VERSION_RE = re.compile(r"[\w\-]{1,32}")               # バージョン名（英数・かな漢字・_ -。@・空白・パス区切りは不可）


class VersionError(ValueError):
    """バージョン名として使えない（利用者に見せる理由つき）"""


def find_web_dir() -> Path:
    """フロントエンド（web/）の場所。リポジトリ直下を優先し、無ければパッケージ同梱を使う"""
    here = Path(__file__).resolve()
    for cand in (here.parents[2] / "web", here.parent / "web"):
        if (cand / "index.html").is_file():
            return cand
    raise FileNotFoundError("web/index.html が見つかりません")


def default_local_config() -> Path:
    """リポジトリ直下の inkdeus.local.toml（存在しなくてもよい）"""
    return Path(__file__).resolve().parents[2] / LOCAL_CONFIG_NAME


def read_local_config(path: Path | None = None) -> dict:
    """ローカル設定（無い・壊れている場合は空）"""
    try:
        with open(default_local_config() if path is None else Path(path), "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def is_image(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS


@dataclass(frozen=True)
class Entry:
    name: str          # 一覧に出す名前（ファイル名）
    path: Path
    stem: str          # 出力・設定ファイルの基底名


class Library:
    """入力（1ファイルまたはフォルダ）と、マスク・出力先の対応を管理する"""

    def __init__(self, input_path: Path, mask_dir: Path | None, out_dir: Path | None):
        input_path = Path(input_path).resolve()
        if input_path.is_dir():
            self.folder, self.single = input_path, None
        elif input_path.is_file():
            if input_path.suffix.lower() not in IMAGE_EXTENSIONS:
                raise ValueError(f"対応していない画像形式です: {input_path.name}")
            self.folder, self.single = input_path.parent, input_path.name
        else:
            raise FileNotFoundError(f"入力が見つかりません: {input_path}")
        self.mask_dir = Path(mask_dir).resolve() if mask_dir else None
        self.out_dir = Path(out_dir).resolve() if out_dir else self.folder / "inkdeus_out"
        if self.out_dir == self.folder:
            raise ValueError("--out-dir に入力フォルダそのものは指定できません（入力画像を上書きしないため）")
        self._dims: dict[tuple[str, int, int], tuple[int, int]] = {}

    def entries(self) -> dict[str, Entry]:
        """現在の一覧（名前 → Entry）。API はここにある名前だけを受け付ける"""
        if self.single is not None:
            files = [self.folder / self.single]
        else:
            files = sorted(p for p in self.folder.iterdir() if is_image(p))
        files = [p for p in files if p.is_file()]
        stems: dict[str, int] = {}
        for p in files:
            stems[p.stem] = stems.get(p.stem, 0) + 1
        out = {}
        for p in files:
            # 拡張子違いの同名ファイルがあるときだけ、拡張子込みの名前を基底名にする
            stem = p.stem if stems[p.stem] == 1 else p.name.replace(".", "_")
            out[p.name] = Entry(p.name, p, stem)
        return out

    def get(self, name: str) -> Entry:
        entry = self.entries().get(name)
        if entry is None:
            abort(404)
        return entry

    def size(self, entry: Entry) -> tuple[int, int]:
        st = entry.path.stat()
        key = (entry.name, st.st_mtime_ns, st.st_size)
        if key not in self._dims:
            try:
                with Image.open(entry.path) as im:
                    self._dims[key] = im.size
            except Exception:  # noqa: BLE001 — 壊れた画像は 0x0 として一覧に出す
                self._dims[key] = (0, 0)
        return self._dims[key]

    def mask_path(self, entry: Entry) -> Path | None:
        if self.mask_dir is None:
            return None
        p = self.mask_dir / f"{entry.path.stem}.png"
        return p if p.is_file() else None

    # ---- バージョン（同じ元画像から作る、設定の違う白黒版）----
    # 既定のバージョン（名前 ""）は <stem>.inkdeus.json / <stem>.png、
    # 名前付きは <stem>@<バージョン>.inkdeus.json / <stem>@<バージョン>.png に置く。

    @staticmethod
    def _base(entry: Entry, version: str = "") -> str:
        return f"{entry.stem}{VERSION_SEP}{version}" if version else entry.stem

    def params_path(self, entry: Entry, version: str = "") -> Path:
        return self.out_dir / f"{self._base(entry, version)}{PARAMS_SUFFIX}"

    def export_path(self, entry: Entry, version: str = "") -> Path:
        return self.out_dir / f"{self._base(entry, version)}.png"

    def check_version(self, entry: Entry, version: str, entries: dict[str, Entry] | None = None) -> str:
        """バージョン名を確かめる（"" は既定）。使えなければ VersionError"""
        if version == "":
            return version
        if not isinstance(version, str) or VERSION_RE.fullmatch(version) is None:
            raise VersionError("バージョン名に使えるのは英数字・かな漢字・_ - だけです（1〜32 文字）")
        stems = {e.stem for e in (self.entries() if entries is None else entries).values()}
        if self._base(entry, version) in stems:
            # <stem>@<バージョン> という名前の元画像が別にある。そのファイルはそちらの既定のバージョンとして読む
            raise VersionError(f"「{self._base(entry, version)}」という名前の元画像があるため、"
                               f"このバージョン名は使えません")
        return version

    def resolve_stem(self, stem: str, entries: dict[str, Entry] | None = None) -> tuple[Entry, str] | None:
        """出力ファイルの基底名（拡張子なし）から (元画像, バージョン) を引く。
        元画像の一覧に実在する基底名を優先する（名前に @ を含む元画像は、その画像の既定のバージョン）"""
        by_stem = {e.stem: e for e in (self.entries() if entries is None else entries).values()}
        if stem in by_stem:
            return by_stem[stem], ""
        base, sep, version = stem.rpartition(VERSION_SEP)
        if sep and base in by_stem and VERSION_RE.fullmatch(version):
            return by_stem[base], version
        return None

    def version_index(self, entries: dict[str, Entry] | None = None) -> dict[str, dict[str, dict]]:
        """out フォルダを1回だけ走査して、画像名 → {バージョン名 → {"params": stat, "export": stat}} を返す"""
        entries = self.entries() if entries is None else entries
        index: dict[str, dict[str, dict]] = {name: {} for name in entries}
        try:
            with os.scandir(self.out_dir) as it:
                items = [(i.name, i) for i in it]
        except OSError:
            return index
        for fname, item in items:
            if fname.endswith(PARAMS_SUFFIX):
                kind, stem = "params", fname[: -len(PARAMS_SUFFIX)]
            elif fname.endswith(".png"):
                kind, stem = "export", fname[:-4]
            else:
                continue
            found = self.resolve_stem(stem, entries)
            if found is None:
                continue
            try:
                if not item.is_file():
                    continue
                st = item.stat()
            except OSError:
                continue
            index[found[0].name].setdefault(found[1], {})[kind] = st
        return index

    def versions(self, entry: Entry, index: dict | None = None) -> list[dict]:
        """その画像のバージョンの一覧（既定が先頭。名前付きは名前順）"""
        found = (self.version_index() if index is None else index).get(entry.name, {})
        try:
            inputs = [entry.path.stat().st_mtime_ns]
        except OSError:
            inputs = []
        mask = self.mask_path(entry)
        if mask is not None:
            inputs.append(mask.stat().st_mtime_ns)
        rows = []
        for name in ["", *sorted(v for v in found if v)]:
            rec = found.get(name, {})
            params, export = rec.get("params"), rec.get("export")
            # 設定がまだ保存されていない物は「新しい」とは見なさない
            fresh = (export is not None and params is not None and bool(inputs)
                     and export.st_mtime_ns >= max(params.st_mtime_ns, *inputs))
            stamps = [st.st_mtime for st in (params, export) if st is not None]
            rows.append({
                "name": name, "hasParams": params is not None, "hasExport": export is not None,
                "exportFresh": fresh, "updatedAt": max(stamps) if stamps else None,
                # 白黒版（書き出した PNG）のファイル名と更新時刻（ミリ秒。サムネイルのキャッシュ回避に使う）
                "exportFile": self.export_path(entry, name).name,
                "exportVersion": export.st_mtime_ns // 1_000_000 if export is not None else None,
            })
        return rows

    def has_version(self, entry: Entry, version: str) -> bool:
        """既定のバージョンは常にある。名前付きは、設定か書き出しのファイルがあるとき"""
        return version == "" or self.params_path(entry, version).is_file() or self.export_path(entry, version).is_file()

    def export_status(self, entry: Entry, version: str = "") -> tuple[bool, bool]:
        """(書き出し済みか, その書き出しがいまの設定・元画像・マスクより新しいか)。
        設定がまだ保存されていない画像は「新しい」とは見なさない"""
        try:
            done = self.export_path(entry, version).stat().st_mtime_ns
        except OSError:
            return False, False
        try:
            inputs = [self.params_path(entry, version).stat().st_mtime_ns, entry.path.stat().st_mtime_ns]
        except OSError:
            return True, False
        mask = self.mask_path(entry)
        if mask is not None:
            inputs.append(mask.stat().st_mtime_ns)
        return True, done >= max(inputs)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def rev_of(data: bytes) -> str:
    """設定ファイルの内容から決まる版（rev）。統計がどの設定に対する物かを示すのに使う"""
    return hashlib.sha1(data).hexdigest()[:12]


class Hub:
    """ブラウザへのイベント配信（Server-Sent Events）"""

    def __init__(self):
        self._lock = threading.Lock()
        self._clients: list[queue.Queue] = []
        self._listeners: set[int] = set()         # 聞くだけの相手（漫画の構成の画面）。「開いているブラウザ」には数えない

    def subscribe(self, listener: bool = False) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=256)
        with self._lock:
            self._clients.append(q)
            if listener:
                self._listeners.add(id(q))
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)
            self._listeners.discard(id(q))

    def publish(self, event: dict) -> int:
        """全員へ配る。戻り値は、白黒化の画面（指示を実行できる相手）の数"""
        with self._lock:
            clients = list(self._clients)
            editors = sum(1 for q in clients if id(q) not in self._listeners)
        for q in clients:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass
        return editors

    def count(self) -> int:
        with self._lock:
            return sum(1 for q in self._clients if id(q) not in self._listeners)


class Session:
    """稼働中の状態: いま開いている画像、設定ファイルの版、統計、書き出しの記録"""

    def __init__(self, lib: Library, hub: Hub | None = None, folder: str = folders_mod.DEFAULT):
        self.lib = lib
        self.folder = folder                                  # この Session が受け持つ画像フォルダ
        self.hub = hub or Hub()
        self.current_folder: str = folders_mod.DEFAULT        # いまブラウザで開いている画像フォルダ（全体で1つ）
        self.lock = threading.RLock()
        self.current: str | None = None
        self.current_version: str = ""                        # いまブラウザで開いているバージョン（"" = 既定）
        self._owners: dict[str, tuple[str, str]] = {}         # 設定ファイル名 → (画像名, バージョン)
        self.revs: dict[str, str | None] = {}                 # 設定ファイル名 → 最後に知っている rev
        self._stamps: dict[str, tuple[int, int]] = {}         # 設定ファイル名 → (mtime_ns, size)
        self.stats: dict[tuple[str, str], dict] = {}          # (画像名, バージョン) → 統計
        self.exports: dict[tuple[str, str], dict] = {}
        self.batch: dict = {"seq": 0, "running": False}       # 一括書き出しの進み具合（ブラウザが知らせてくる）
        self._stop = threading.Event()
        self._primed = False                                  # 最初の走査（起動時の状態の記録）が済んだか

    # ---- 設定ファイル ----

    def read_params(self, entry: Entry, version: str = "") -> tuple[bytes | None, str | None]:
        try:
            data = self.lib.params_path(entry, version).read_bytes()
        except OSError:
            return None, None
        return data, rev_of(data)

    def resolved(self, entry: Entry, version: str = "") -> dict:
        """いまの設定（ファイルが無ければ既定値）を整えて返す"""
        data, rev = self.read_params(entry, version)
        raw = None
        problems: list[str] = []
        if data is not None:
            try:
                raw = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                problems.append("設定ファイルが JSON として読めないため既定値を使いました")
        params, warns = schema.normalize(raw)
        return {"name": entry.name, "version": version, "rev": rev, "exists": data is not None,
                "params": params, "warnings": problems + warns}

    def remember(self, path: Path, entry: Entry, version: str, rev: str | None) -> None:
        """サーバ自身が書いた（消した）設定ファイルを覚える（監視が「外部からの変更」と取り違えないように）"""
        with self.lock:
            try:
                st = path.stat()
            except OSError:
                self.revs.pop(path.name, None)
                self._stamps.pop(path.name, None)
                self._owners.pop(path.name, None)
                return
            self.revs[path.name] = rev
            self._stamps[path.name] = (st.st_mtime_ns, st.st_size)
            self._owners[path.name] = (entry.name, version)

    def write_params(self, entry: Entry, params: dict, source: str | None, version: str = "") -> str:
        """設定を書き込み、ブラウザへ知らせる。source は書いた本人（ブラウザの ID）で、外部からなら None"""
        data = (json.dumps(params, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        path = self.lib.params_path(entry, version)
        with self.lock:
            _atomic_write(path, data)
            rev = rev_of(data)
            self.remember(path, entry, version, rev)
        self.hub.publish({"type": "params", "folder": self.folder, "name": entry.name, "version": version,
                          "rev": rev, "source": source})
        return rev

    # ---- 設定ファイルの外部変更の監視 ----

    def scan(self) -> list[dict]:
        """out フォルダの設定ファイルを調べ、サーバ以外が書き換えた物をイベントにして返す（配信もする）"""
        entries = self.lib.entries()
        seen: dict[str, tuple[int, int]] = {}
        owners: dict[str, tuple[str, str]] = {}
        try:
            with os.scandir(self.lib.out_dir) as it:
                for item in it:
                    if not item.name.endswith(PARAMS_SUFFIX):
                        continue
                    found = self.lib.resolve_stem(item.name[: -len(PARAMS_SUFFIX)], entries)
                    if found is None:
                        continue
                    st = item.stat()
                    seen[item.name] = (st.st_mtime_ns, st.st_size)
                    owners[item.name] = (found[0].name, found[1])
        except OSError:
            pass
        events = []
        with self.lock:
            for fname in sorted(set(seen) | set(self._stamps)):
                stamp = seen.get(fname)
                if stamp == self._stamps.get(fname):
                    continue
                owner = owners.get(fname) or self._owners.get(fname)
                if owner is None:
                    self._stamps.pop(fname, None)
                    continue
                if stamp is None:
                    self._stamps.pop(fname, None)
                    self._owners.pop(fname, None)
                    rev = None
                else:
                    self._owners[fname] = owner
                    self._stamps[fname] = stamp
                    try:
                        rev = rev_of((self.lib.out_dir / fname).read_bytes())
                    except OSError:
                        continue
                changed = self.revs.get(fname) != rev
                self.revs[fname] = rev
                if changed and self._primed:
                    events.append({"type": "params", "folder": self.folder, "name": owner[0], "version": owner[1],
                                   "rev": rev, "source": None})
            self._primed = True
        for ev in events:
            self.hub.publish(ev)
        return events

    def start_watching(self, sessions=None) -> None:
        """設定ファイルの外部変更を見張る。sessions は見張る Session の並びを返す関数（省略時は自分だけ）"""
        sessions = sessions or (lambda: [self])
        for s in sessions():
            s.scan()                                  # 起動時点の状態を覚える（イベントは出さない）

        def loop():
            while not self._stop.wait(WATCH_INTERVAL):
                for s in sessions():                  # 稼働中に足されたフォルダは、最初の走査で状態を覚えるだけ
                    try:
                        s.scan()
                    except Exception:  # noqa: BLE001 — 監視は止めない
                        pass

        threading.Thread(target=loop, name="inkdeus-watch", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


class Registry:
    """画像フォルダの一覧。既定のフォルダ（従来の入力）に、起動引数と設定ファイルの [[folders]] の分を足す。
    設定ファイルは毎回読み直すので、稼働中に足したフォルダも取り込まれる"""

    def __init__(self, lib: Library, session: Session, static: list | None = None,
                 project_file: Path | None = None):
        self.lib, self.session = lib, session
        self.static = list(static or [])
        self.project_file = project_file
        self._lock = threading.Lock()
        self._built: dict[str, tuple[tuple, Library | None, Session | None, str]] = {}

    def defs(self) -> list:
        found, seen = [], {folders_mod.DEFAULT}
        for d in [*self.static, *folders_mod.read_toml(self.project_file)[0]]:
            if d.name not in seen:
                seen.add(d.name)
                found.append(d)
        return found

    def rows(self) -> list[tuple[str, Library | None, Session | None, str]]:
        """[(名前, Library, Session, メモ)]。既定が先頭。参照先が無いフォルダは Library が None"""
        out = [(folders_mod.DEFAULT, self.lib, self.session, "")]
        with self._lock:
            for d in self.defs():
                key = (str(d.images), str(d.masks) if d.masks else None)
                built = self._built.get(d.name)
                if built is None or built[0] != key or built[1] is None:
                    try:
                        lib = Library(d.images, d.masks, folders_mod.out_dir(self.lib.out_dir, d.name))
                        built = (key, lib, Session(lib, self.session.hub, d.name), d.note)
                    except (OSError, ValueError):
                        built = (key, None, None, d.note)
                    self._built[d.name] = built
                out.append((d.name, built[1], built[2], d.note))
        return out

    def pick(self, name: str) -> tuple[Library, Session] | None:
        for folder, lib, sess, _note in self.rows():
            if folder == name and lib is not None:
                return lib, sess
        return None

    def libraries(self) -> list[tuple[str, Library]]:
        return [(name, lib) for name, lib, _s, _n in self.rows() if lib is not None]

    def sessions(self) -> list[Session]:
        return [s for _name, _lib, s, _n in self.rows() if s is not None]


def looks_like_manga_data(data: bytes) -> bool:
    """漫画エディタの保存形式（先頭4バイト = 見出しの長さ、続いて JSON の見出し、残りが圧縮データ）か"""
    if len(data) < 6:
        return False
    (size,) = struct.unpack("<I", data[:4])
    if size < 2 or size > len(data) - 4:
        return False
    try:
        header = json.loads(data[4:4 + size].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return isinstance(header, list) and all(isinstance(f, dict) and "name" in f and "size" in f for f in header)


def create_app(input_path: Path | str, mask_dir: Path | str | None = None,
               out_dir: Path | str | None = None,
               project: dict | None = None, watch: bool = True,
               local_config: Path | str | None = None,
               cache_root: Path | str | None = None,
               folders: list | None = None) -> Flask:
    """アプリを作る。project はプロジェクトの情報（任意）。
    folders は追加の画像フォルダ（folders.FolderDef の並び。プロジェクトの設定ファイルの [[folders]] とは別に足す分）。
    local_config は共通設定ファイル（省略時はリポジトリ直下の inkdeus.local.toml）、
    cache_root は漫画エディタのキャッシュ置き場（省略時は INKDEUS_CACHE か既定の場所）"""
    lib = Library(Path(input_path), Path(mask_dir) if mask_dir else None,
                  Path(out_dir) if out_dir else None)
    web = find_web_dir()
    session = Session(lib)
    project_dir = Path(project["path"]) if project and project.get("path") else None
    registry = Registry(lib, session, folders, project_dir / "inkdeus.project.toml" if project_dir else None)
    catalog = assets_mod.Catalog(
        lib,
        project_file=project_dir / "inkdeus.project.toml" if project_dir else None,
        local_file=Path(local_config) if local_config else default_local_config(),
        folders=registry.libraries)
    manga_dir = (project_dir / "manga") if project_dir else lib.out_dir / "manga"
    # ローカルフォントの設定（共通 → プロジェクトの順に合算。毎回読み直すので、稼働中に書き足しても反映される）
    font_configs = [Path(local_config) if local_config else default_local_config(),
                    project_dir / "inkdeus.project.toml" if project_dir else None]
    manga_site = manga_mod.MangaSite(Path(cache_root) if cache_root else None)
    app = Flask(__name__, static_folder=str(web), static_url_path="")
    app.config["MAX_CONTENT_LENGTH"] = MAX_EXPORT_BYTES
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
    app.config["INKDEUS_LIBRARY"] = lib
    app.config["INKDEUS_SESSION"] = session
    app.config["INKDEUS_FOLDERS"] = registry
    app.config["INKDEUS_ASSETS"] = catalog
    app.config["INKDEUS_MANGA_DIR"] = manga_dir
    if watch:
        session.start_watching(registry.sessions)

    def pick() -> tuple[str, Library, Session]:
        """クエリ ?folder=<名前>（省略 = 既定）の画像フォルダ。無い名前は 404"""
        name = request.args.get("folder") or folders_mod.DEFAULT
        found = registry.pick(name)
        if found is None:
            abort(404)
        return name, found[0], found[1]

    def is_current(folder: str, entry: Entry) -> bool:
        return session.current == entry.name and session.current_folder == folder

    def json_body() -> dict:
        raw = request.get_data(cache=False)
        if len(raw) > MAX_PARAMS_BYTES:
            abort(413)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            abort(400, description="JSON として読めません")
        if not isinstance(data, dict):
            abort(400, description="JSON オブジェクトである必要があります")
        return data

    def fail(problems: list[str], status: int = 400):
        return jsonify({"error": "設定を受け付けられません", "problems": problems}), status

    def info() -> dict:
        return {
            "current": session.current,
            "currentFolder": session.current_folder,
            "currentVersion": session.current_version if session.current else "",
            "images": len(lib.entries()),
            "folders": [name for name, _lib in registry.libraries()],
            "clients": session.hub.count(),
            "project": project,
            "source": str(lib.folder),
            "masks": str(lib.mask_dir) if lib.mask_dir else None,
            "out": str(lib.out_dir),
            "manga": str(manga_dir),
            "batch": session.batch,
        }

    @app.errorhandler(400)
    def _bad_request(e):
        return jsonify({"error": e.description}), 400

    @app.before_request
    def _same_origin_only():
        # 他サイトのページからローカルのサーバへ書き込ませない
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("Origin")
            if origin and urlsplit(origin).netloc != request.host:
                abort(403)

    @app.after_request
    def _no_store(resp):
        path = request.path
        if path.startswith("/api/assets/") and ("/image/" in path or "/thumb/" in path):
            resp.headers["Cache-Control"] = "no-cache"      # 毎回確かめるが、変わっていなければ再送しない
        elif path.startswith("/api/fonts/file/"):
            resp.headers["Cache-Control"] = "public, max-age=86400"   # フォント本体は大きいので使い回させる
        elif path.startswith("/api/fonts/css"):
            resp.headers["Cache-Control"] = "no-cache"
        elif path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        elif path.startswith("/manga"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/")
    def index():
        return send_from_directory(web, "index.html")

    @app.get("/api/config")
    def api_config():
        return jsonify({"project": project})

    @app.get("/api/manga/status")
    def api_manga_status():
        base = manga_site.dir()
        return jsonify({"installed": base is not None, "commit": base.name if base else None,
                        "pinned": manga_mod.PINNED_COMMIT, "dir": str(manga_dir), "project": project})

    # ---- 漫画の構成: 取得済みの漫画エディタの配信（/manga/）----

    @app.get("/manga")
    def manga_redirect():
        return redirect("/manga/", code=308)

    @app.get("/manga/")
    @app.get("/manga/index.html")
    def manga_index():
        html = manga_site.index()
        if html is None:
            return Response(manga_mod.NOT_INSTALLED_HTML, mimetype="text/html")
        return Response(html, mimetype="text/html")

    @app.get("/manga/service-worker.js")
    def manga_service_worker():
        # 以前に登録された service worker を片付けるための物（上流の物は配信しない）
        return Response(manga_mod.SW_KILL_SCRIPT, mimetype="text/javascript")

    @app.get("/manga/<path:sub>")
    def manga_file(sub: str):
        path = manga_site.file(sub)
        if path is None:
            abort(404)
        return send_file(path, conditional=True)

    # ---- 漫画の構成: 素材（ソースごとの画像の一覧と縮小画像）----

    def asset_source(name: str) -> assets_mod.Source:
        src = catalog.source(name)
        if src is None:
            abort(404)
        return src

    def asset_item(source: str, rel: str) -> assets_mod.Item:
        item = catalog.find(asset_source(source), rel)
        if item is None:
            abort(404)
        return item

    def inked_fallback(source: str, rel: str) -> assets_mod.Item | None:
        """消された・名前の変わったバージョンの白黒版を指す URL には、既定のバージョン（無ければ元画像）を返す
        （保存済みの漫画データを開いたとき、絵そのものが消えてしまわないように）"""
        src = asset_source(source)
        if src.kind != "inked" or catalog.find(src, rel) is not None:
            return None
        return catalog.fallback(src, rel)

    def int_arg(name: str, default: int | None, lo: int, hi: int) -> int | None:
        raw = request.args.get(name)
        if raw is None or raw == "":
            return default
        try:
            value = int(raw)
        except ValueError:
            abort(400, description=f"{name} は整数で指定してください")
        if not lo <= value <= hi:
            abort(400, description=f"{name} は {lo}〜{hi} の範囲で指定してください")
        return value

    @app.get("/api/assets")
    def api_assets():
        return jsonify(catalog.summary())

    @app.get("/api/assets/<source>")
    def api_assets_list(source: str):
        src = asset_source(source)
        offset = int_arg("offset", 0, 0, 10 ** 9)
        limit = int_arg("limit", assets_mod.DEFAULT_LIMIT, 1, assets_mod.MAX_LIMIT)
        return jsonify(catalog.listing(src, request.args.get("q", ""), offset, limit))

    def send_derived(item: assets_mod.Item, kind: str, value: int):
        try:
            path, mime = catalog.derived(item, kind, value)
        except (assets_mod.AssetError, OSError) as e:
            abort(400, description=str(e))
        return send_file(path, mimetype=mime, conditional=True)

    @app.get("/api/assets/<source>/versions")
    def api_assets_versions(source: str):
        """画像ごとの更新時刻（ミリ秒）。表示中の画像が古くなっていないかを軽く確かめるための物"""
        return jsonify({"source": source, "versions": catalog.versions(asset_source(source))})

    @app.get("/api/assets/<source>/pair/<path:rel>")
    def api_assets_pair(source: str, rel: str):
        """元画像（source）と白黒版（inked）の対応。どちらの名前からでも引ける"""
        pair = catalog.pair(asset_source(source), rel)
        if pair is None:
            abort(404)
        return jsonify(pair)

    @app.get("/api/assets/<source>/image/<path:rel>")
    def api_assets_image(source: str, rel: str):
        substitute = inked_fallback(source, rel)
        item = substitute or asset_item(source, rel)
        limit = int_arg("max", None, *assets_mod.MAX_SIDE_RANGE)
        resp = send_file(item.path, conditional=True) if limit is None else send_derived(item, "max", limit)
        if substitute is not None:
            resp.headers["X-Inkdeus-Fallback"] = "1"
        return resp

    @app.get("/api/assets/<source>/thumb/<path:rel>")
    def api_assets_thumb(source: str, rel: str):
        item = asset_item(source, rel)
        return send_derived(item, "thumb", int_arg("h", 160, *assets_mod.THUMB_RANGE))

    # ---- 漫画の構成: ローカルフォント（利用者のPCにあるフォントファイル） ----

    @app.get("/api/fonts")
    def api_fonts():
        fonts, _ = fonts_mod.resolve(font_configs)
        return jsonify([f.info() for f in fonts])

    @app.get("/api/fonts/file/<font_id>")
    def api_font_file(font_id: str):
        fonts, _ = fonts_mod.resolve(font_configs)
        font = next((f for f in fonts if f.id == font_id), None)      # 一覧にある物だけ
        if font is None:
            abort(404)
        return send_file(font.path, mimetype=font.mime, conditional=True)

    @app.get("/api/fonts/css")
    def api_fonts_css():
        fonts, _ = fonts_mod.resolve(font_configs)
        family = request.args.get("family")
        if family is not None:
            fonts = [f for f in fonts if f.name == family]
            if not fonts:
                abort(404)
        return Response(fonts_mod.css_for(fonts), mimetype="text/css")

    # ---- 漫画の構成: 漫画データ（漫画エディタの保存形式のまま）と書き出したページ ----

    def manga_name(name: str) -> str:
        if MANGA_NAME_RE.fullmatch(name) is None:
            abort(400, description="名前に使えるのは英数字・かな漢字・_ - だけです（64 文字まで）")
        return name

    @app.get("/api/manga/projects")
    def api_manga_projects():
        rows = []
        if manga_dir.is_dir():
            for p in sorted(manga_dir.iterdir()):
                if p.suffix == MANGA_SUFFIX and p.is_file() and MANGA_NAME_RE.fullmatch(p.stem):
                    st = p.stat()
                    rows.append({"name": p.stem, "bytes": st.st_size, "mtime": st.st_mtime,
                                 "autosave": p.stem == MANGA_AUTOSAVE})
        return jsonify({"dir": str(manga_dir), "autosaveName": MANGA_AUTOSAVE, "projects": rows})

    @app.route("/api/manga/project/<name>", methods=["GET", "PUT"])
    def api_manga_project(name: str):
        path = manga_dir / (manga_name(name) + MANGA_SUFFIX)
        if request.method == "GET":
            if not path.is_file():
                abort(404)
            return send_file(path, mimetype="application/octet-stream")
        data = request.get_data(cache=False)
        if not looks_like_manga_data(data):
            abort(400, description="漫画データとして読めません")
        _atomic_write(path, data)
        return jsonify({"name": name, "path": str(path), "bytes": len(data), "mtime": path.stat().st_mtime})

    @app.get("/api/manga/pages/<name>")
    def api_manga_pages(name: str):
        folder = manga_dir / "pages" / manga_name(name)
        rows = []
        if folder.is_dir():
            for p in sorted(folder.glob("*.png")):
                if p.stem.isdigit():
                    st = p.stat()
                    rows.append({"number": int(p.stem), "file": p.name, "bytes": st.st_size, "mtime": st.st_mtime})
        return jsonify({"name": name, "dir": str(folder), "pages": rows})

    @app.post("/api/manga/pages/<name>/<int:number>.png")
    def api_manga_page(name: str, number: int):
        if not 1 <= number <= 9999:
            abort(400, description="ページ番号は 1〜9999 です")
        data = request.get_data(cache=False)
        if not data.startswith(PNG_SIGNATURE):
            abort(400, description="PNG ではありません")
        path = manga_dir / "pages" / manga_name(name) / f"{number:03d}.png"
        _atomic_write(path, data)
        return jsonify({"name": name, "number": number, "path": str(path), "bytes": len(data)})

    # ---- 画像・マスク ----

    def ver_arg(lib: Library, entry: Entry, must_exist: bool = True) -> str:
        """クエリ ?version=<名前>（省略 = 既定）。使えない名前は 400、無いバージョンは 404"""
        version = request.args.get("version", "")
        try:
            lib.check_version(entry, version)
        except VersionError as e:
            abort(400, description=str(e))
        if must_exist and not lib.has_version(entry, version):
            abort(404)
        return version

    @app.get("/api/folders")
    def api_folders():
        """画像フォルダの一覧（既定は name が "default"）と枚数"""
        rows = []
        for name, flib, _sess, note in registry.rows():
            row = {"name": name, "label": "既定" if name == folders_mod.DEFAULT else name, "note": note,
                   "missing": flib is None, "images": None, "masks": None, "out": None,
                   "counts": {"images": 0, "masks": 0, "configured": 0, "exported": 0}}
            if flib is not None:
                entries = flib.entries()
                index = flib.version_index(entries)
                row.update(images=str(flib.folder), masks=str(flib.mask_dir) if flib.mask_dir else None,
                           out=str(flib.out_dir))
                row["counts"] = {
                    "images": len(entries),
                    "masks": sum(1 for e in entries.values() if flib.mask_path(e) is not None),
                    "configured": sum(1 for e in entries if "params" in index[e].get("", {})),
                    "exported": sum(1 for e in entries if "export" in index[e].get("", {}))}
            rows.append(row)
        return jsonify({"folders": rows, "current": session.current_folder})

    @app.get("/api/images")
    def api_images():
        folder, lib, fs = pick()
        items = []
        entries = lib.entries()
        index = lib.version_index(entries)
        for e in entries.values():
            w, h = lib.size(e)
            versions = lib.versions(e, index)
            default = versions[0]
            items.append({
                # 画像単位の項目は既定のバージョンの物（名前付きは versions に並ぶ）
                "exportFile": default["exportFile"], "exportVersion": default["exportVersion"],
                "name": e.name, "width": w, "height": h,
                "hasMask": lib.mask_path(e) is not None,
                "hasParams": default["hasParams"],
                "hasExport": default["hasExport"], "exportFresh": default["exportFresh"],
                "versions": versions,
            })
        return jsonify({"folder": folder, "images": items, "outDir": str(lib.out_dir),
                        "maskDir": str(lib.mask_dir) if lib.mask_dir else None})

    @app.get("/api/image/<path:name>")
    def api_image(name: str):
        _folder, lib, _fs = pick()
        return send_file(lib.get(name).path)

    @app.get("/api/mask/<path:name>")
    def api_mask(name: str):
        _folder, lib, _fs = pick()
        p = lib.mask_path(lib.get(name))
        if p is None:
            abort(404)
        return send_file(p, mimetype="image/png")

    # ---- 設定 ----

    @app.route("/api/params/<path:name>", methods=["GET", "PUT", "PATCH"])
    def api_params(name: str):
        folder, lib, fs = pick()
        entry = lib.get(name)
        version = ver_arg(lib, entry)
        if request.method == "GET":
            if request.args.get("resolve"):
                return jsonify(fs.resolved(entry, version))
            data, rev = fs.read_params(entry, version)
            if data is None:
                abort(404)
            resp = Response(data, mimetype="application/json")
            resp.headers["X-Inkdeus-Rev"] = rev
            return resp
        body = json_body()
        if request.method == "PUT":
            # ブラウザ自身の保存（ブラウザ側で整えてあるのでそのまま書く）
            rev = fs.write_params(entry, body, request.headers.get("X-Inkdeus-Client"), version)
            return jsonify({"path": str(lib.params_path(entry, version)), "rev": rev, "version": version})
        # PATCH: 外部からの部分更新（JSON Merge Patch）。検証に通らなければ何も書かない
        base = fs.resolved(entry, version)["params"]
        try:
            params, _ = schema.normalize(schema.merge_patch(base, body), strict=True)
        except schema.ValidationError as e:
            return fail(e.problems)
        rev = fs.write_params(entry, params, None, version)
        return jsonify({"name": entry.name, "version": version, "rev": rev, "params": params})

    @app.post("/api/set/<path:name>")
    def api_set(name: str):
        folder, lib, fs = pick()
        entry = lib.get(name)
        version = ver_arg(lib, entry)
        body = json_body()
        if not isinstance(body.get("key"), str) or "value" not in body:
            return fail(["key と value が必要です"])
        base = fs.resolved(entry, version)["params"]
        try:
            params, _ = schema.normalize(schema.set_path(base, body["key"], body["value"]),
                                         strict=True)
        except schema.ValidationError as e:
            return fail(e.problems)
        rev = fs.write_params(entry, params, None, version)
        return jsonify({"name": entry.name, "version": version, "rev": rev, "params": params})

    @app.post("/api/style/<path:name>")
    def api_style(name: str):
        folder, lib, fs = pick()
        entry = lib.get(name)
        version = ver_arg(lib, entry)
        body = json_body()
        base = fs.resolved(entry, version)["params"]
        try:
            style = schema.resolve_style(str(body.get("style", "")))
            params = schema.apply_style(base, str(body.get("style", "")), str(body.get("region", "person")))
            params, _ = schema.normalize(params, strict=True)
        except schema.ValidationError as e:
            return fail(e.problems)
        rev = fs.write_params(entry, params, None, version)
        return jsonify({"name": entry.name, "version": version, "rev": rev, "style": style,
                        "region": body.get("region", "person"), "params": params})

    @app.get("/api/styles")
    def api_styles():
        return jsonify({"styles": schema.list_styles(), "default": schema.load()["defaultStyle"]})

    # ---- 書き出し ----

    @app.post("/api/export/<path:name>")
    def api_export(name: str):
        folder, lib, fs = pick()
        entry = lib.get(name)
        version = ver_arg(lib, entry)
        data = request.get_data(cache=False)
        if not data.startswith(PNG_SIGNATURE):
            abort(400, description="PNG ではありません")
        path = lib.export_path(entry, version)
        _atomic_write(path, data)
        with fs.lock:
            seq = fs.exports.get((entry.name, version), {}).get("seq", 0) + 1
            fs.exports[(entry.name, version)] = {
                "seq": seq, "version": version, "path": str(path), "bytes": len(data),
                "time": time.time(), "rev": request.headers.get("X-Inkdeus-Rev")}
        st = path.stat()
        # 書き出しを知らせる（漫画の構成の画面が、使っている白黒版を新しい物に替える）。
        # version はバージョン名、stamp は PNG の更新時刻（ミリ秒。キャッシュ回避に使う）
        session.hub.publish({"type": "exported", "folder": folder, "name": entry.name, "version": version,
                             "file": path.name, "source": assets_mod.source_name("inked", folder),
                             "mtime": st.st_mtime, "stamp": st.st_mtime_ns // 1_000_000,
                             "rev": request.headers.get("X-Inkdeus-Rev")})
        return jsonify({"path": str(path), "bytes": len(data), "mtime": st.st_mtime, "version": version})

    @app.get("/api/exported/<path:name>")
    def api_exported(name: str):
        folder, lib, fs = pick()
        entry = lib.get(name)
        return jsonify(fs.exports.get((entry.name, ver_arg(lib, entry)), {"seq": 0}))

    # ---- バージョン（同じ元画像から作る、設定の違う白黒版）----

    def version_listing(folder: str, lib: Library, entry: Entry) -> dict:
        return {"folder": folder, "name": entry.name, "versions": lib.versions(entry),
                "current": session.current_version if is_current(folder, entry) else None}

    def body_version(lib: Library, entry: Entry, body: dict, key: str, allow_default: bool) -> str:
        value = body.get(key, "" if allow_default else None)
        if not isinstance(value, str) or (value == "" and not allow_default):
            abort(400, description=f"{key} にバージョン名を指定してください")
        try:
            return lib.check_version(entry, value)
        except VersionError as e:
            abort(400, description=str(e))

    def announce(folder: str, lib: Library, entry: Entry, action: str, version: str, **extra) -> None:
        session.hub.publish({"type": "versions", "action": action, "folder": folder, "name": entry.name,
                             "version": version,
                             "file": lib.export_path(entry, version).name, **extra})

    @app.route("/api/versions/<path:name>", methods=["GET", "POST", "PATCH", "DELETE"])
    def api_versions(name: str):
        folder, lib, fs = pick()
        entry = lib.get(name)
        if request.method == "GET":
            return jsonify(version_listing(folder, lib, entry))
        if request.method == "POST":
            # 作成: copyFrom（省略 = 既定）の設定を複製する。copyExport が真なら書き出した PNG も写す
            body = json_body()
            version = body_version(lib, entry, body, "version", False)
            source = body_version(lib, entry, body, "copyFrom", True)
            if lib.has_version(entry, version):
                return jsonify({"error": f"バージョン「{version}」は既にあります"}), 409
            if not lib.has_version(entry, source):
                abort(404)
            params = fs.resolved(entry, source)["params"]
            rev = fs.write_params(entry, params, None, version)
            copied = False
            if body.get("copyExport") is True:
                try:
                    _atomic_write(lib.export_path(entry, version), lib.export_path(entry, source).read_bytes())
                    copied = True
                except OSError:
                    pass                                    # 複製元がまだ書き出されていない
            announce(folder, lib, entry, "create", version, copyFrom=source)
            return jsonify({**version_listing(folder, lib, entry), "version": version, "copyFrom": source, "rev": rev,
                            "copiedExport": copied, "file": lib.export_path(entry, version).name})
        if request.method == "PATCH":
            # 名前の変更
            body = json_body()
            version = body_version(lib, entry, body, "version", False)
            target = body_version(lib, entry, body, "to", False)
            if not lib.has_version(entry, version):
                abort(404)
            if lib.has_version(entry, target):
                return jsonify({"error": f"バージョン「{target}」は既にあります"}), 409
            with fs.lock:
                for getter in (lib.params_path, lib.export_path):
                    src, dst = getter(entry, version), getter(entry, target)
                    if src.is_file():
                        os.replace(src, dst)
                old, new = lib.params_path(entry, version), lib.params_path(entry, target)
                rev = fs.revs.get(old.name)
                fs.remember(old, entry, version, None)
                fs.remember(new, entry, target, rev)
                for table in (fs.stats, fs.exports):
                    table.pop((entry.name, version), None)
                if is_current(folder, entry) and session.current_version == version:
                    session.current_version = target
            announce(folder, lib, entry, "rename", version, to=target, toFile=lib.export_path(entry, target).name)
            return jsonify({**version_listing(folder, lib, entry), "version": target, "renamedFrom": version})
        # DELETE: 設定と PNG を消す（既定のバージョンは消せない）
        version = request.args.get("version", "")
        if version == "":
            abort(400, description="既定のバージョンは削除できません")
        try:
            lib.check_version(entry, version)
        except VersionError as e:
            abort(400, description=str(e))
        if not lib.has_version(entry, version):
            abort(404)
        with fs.lock:
            for path in (lib.params_path(entry, version), lib.export_path(entry, version)):
                path.unlink(missing_ok=True)
            fs.remember(lib.params_path(entry, version), entry, version, None)
            for table in (fs.stats, fs.exports):
                table.pop((entry.name, version), None)
            if is_current(folder, entry) and session.current_version == version:
                session.current_version = ""
        announce(folder, lib, entry, "delete", version)
        return jsonify({**version_listing(folder, lib, entry), "deleted": version})

    # ---- 外部からの操作（状態・イベント・統計・指示） ----

    @app.route("/api/state", methods=["GET", "POST"])
    def api_state():
        if request.method == "POST":
            body = json_body()
            current = body.get("current")
            folder = body.get("folder") or folders_mod.DEFAULT
            found = registry.pick(folder) if isinstance(folder, str) else None
            if found is None:
                abort(404)
            entries = found[0].entries()
            if current is not None and current not in entries:
                abort(404)
            version = body.get("version") or ""
            if current is not None and version:
                try:
                    found[0].check_version(entries[current], version, entries)
                except VersionError as e:
                    abort(400, description=str(e))
            session.current = current
            session.current_folder = folder
            session.current_version = version if current is not None else ""
        return jsonify(info())

    @app.get("/api/events")
    def api_events():
        # ?role=manga は聞くだけの相手（書き出しの通知を受ける）。指示を実行できるブラウザには数えない
        q = session.hub.subscribe(listener=request.args.get("role") == "manga")

        def stream():
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        event = q.get(timeout=HEARTBEAT_SECONDS)
                    except queue.Empty:
                        yield ": ping\n\n"          # 切断の検知も兼ねる
                        continue
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            finally:
                session.hub.unsubscribe(q)

        return Response(stream(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.route("/api/stats/<path:name>", methods=["GET", "POST"])
    def api_stats(name: str):
        folder, lib, fs = pick()
        entry = lib.get(name)
        if request.method == "POST":
            version = ver_arg(lib, entry, must_exist=False)
            if not lib.has_version(entry, version):
                # 統計を送る直前にバージョンが消された・名前が変わった。エラーにはせず受け流す
                return jsonify({"ok": False})
            body = json_body()
            body["name"] = entry.name
            body["version"] = version
            body["receivedAt"] = time.time()
            fs.stats[(entry.name, version)] = body
            return jsonify({"ok": True})
        version = ver_arg(lib, entry)
        _, rev = fs.read_params(entry, version)
        stats = fs.stats.get((entry.name, version))
        return jsonify({
            "name": entry.name, "version": version, "currentRev": rev, "stats": stats,
            "fresh": stats is not None and stats.get("rev") == rev,
            "clients": session.hub.count(), "current": session.current, "folder": folder,
            "currentFolder": session.current_folder,
            "currentVersion": session.current_version if session.current else "",
        })

    @app.route("/api/batch", methods=["GET", "POST"])
    def api_batch():
        """一括書き出しの進み具合。ブラウザが POST で知らせ、外部（ctl）が GET で読む"""
        if request.method == "POST":
            body = json_body()
            with session.lock:
                seq = session.batch.get("seq", 0)
                if body.get("started") is True:
                    seq += 1
                keys = ("running", "total", "done", "exported", "skipped", "failed", "current", "aborted",
                        "force", "defaultOnly", "allFolders", "elapsed", "remaining", "errors")
                session.batch = {"seq": seq, **{k: body[k] for k in keys if k in body}, "time": time.time()}
                session.batch["running"] = body.get("running") is True
        return jsonify(session.batch)

    @app.post("/api/command")
    def api_command():
        body = json_body()
        kind = body.get("type")
        if kind in ("export-all", "export-all-abort"):
            # 一括書き出しの開始・中止（画像名は要らない）
            event = {"type": kind, "force": body.get("force") is True,
                     "defaultOnly": body.get("defaultOnly") is True, "allFolders": body.get("allFolders") is True}
            return jsonify({**event, "clients": session.hub.publish(event), "batch": session.batch})
        if kind not in ("open", "export"):
            abort(400, description="type は open / export / export-all / export-all-abort のどれかです")
        # folder: 画像フォルダ（省略 = いまブラウザで開いているフォルダ）
        folder = body.get("folder") or session.current_folder
        found = registry.pick(folder) if isinstance(folder, str) else None
        if found is None:
            abort(404)
        lib = found[0]
        entry = lib.get(str(body.get("name", "")))
        # version: 開く・書き出すバージョン。null（省略）は「指定なし」（open はいまのまま / 既定、export は既定）
        version = body.get("version")
        if version is not None:
            if not isinstance(version, str):
                abort(400, description="version は文字列で指定してください")
            try:
                lib.check_version(entry, version)
            except VersionError as e:
                abort(400, description=str(e))
            if not lib.has_version(entry, version):
                abort(404)
        clients = session.hub.publish({"type": kind, "folder": folder, "name": entry.name, "version": version})
        return jsonify({"type": kind, "folder": folder, "name": entry.name, "version": version, "clients": clients})

    return app
