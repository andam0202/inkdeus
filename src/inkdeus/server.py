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

    def params_path(self, entry: Entry) -> Path:
        return self.out_dir / f"{entry.stem}{PARAMS_SUFFIX}"

    def export_path(self, entry: Entry) -> Path:
        return self.out_dir / f"{entry.stem}.png"


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

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=256)
        with self._lock:
            self._clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def publish(self, event: dict) -> int:
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass
        return len(clients)

    def count(self) -> int:
        with self._lock:
            return len(self._clients)


class Session:
    """稼働中の状態: いま開いている画像、設定ファイルの版、統計、書き出しの記録"""

    def __init__(self, lib: Library):
        self.lib = lib
        self.hub = Hub()
        self.lock = threading.RLock()
        self.current: str | None = None
        self.revs: dict[str, str | None] = {}                 # 設定ファイル名 → 最後に知っている rev
        self._stamps: dict[str, tuple[int, int]] = {}         # 設定ファイル名 → (mtime_ns, size)
        self.stats: dict[str, dict] = {}
        self.exports: dict[str, dict] = {}
        self._stop = threading.Event()
        self._primed = False                                  # 最初の走査（起動時の状態の記録）が済んだか

    # ---- 設定ファイル ----

    def read_params(self, entry: Entry) -> tuple[bytes | None, str | None]:
        try:
            data = self.lib.params_path(entry).read_bytes()
        except OSError:
            return None, None
        return data, rev_of(data)

    def resolved(self, entry: Entry) -> dict:
        """いまの設定（ファイルが無ければ既定値）を整えて返す"""
        data, rev = self.read_params(entry)
        raw = None
        problems: list[str] = []
        if data is not None:
            try:
                raw = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                problems.append("設定ファイルが JSON として読めないため既定値を使いました")
        params, warns = schema.normalize(raw)
        return {"name": entry.name, "rev": rev, "exists": data is not None,
                "params": params, "warnings": problems + warns}

    def write_params(self, entry: Entry, params: dict, source: str | None) -> str:
        """設定を書き込み、ブラウザへ知らせる。source は書いた本人（ブラウザの ID）で、外部からなら None"""
        data = (json.dumps(params, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        path = self.lib.params_path(entry)
        with self.lock:
            _atomic_write(path, data)
            rev = rev_of(data)
            self.revs[path.name] = rev
            try:
                st = path.stat()
                self._stamps[path.name] = (st.st_mtime_ns, st.st_size)
            except OSError:
                pass
        self.hub.publish({"type": "params", "name": entry.name, "rev": rev, "source": source})
        return rev

    # ---- 設定ファイルの外部変更の監視 ----

    def scan(self) -> list[dict]:
        """out フォルダの設定ファイルを調べ、サーバ以外が書き換えた物をイベントにして返す（配信もする）"""
        by_file = {self.lib.params_path(e).name: e for e in self.lib.entries().values()}
        seen: dict[str, tuple[int, int]] = {}
        try:
            with os.scandir(self.lib.out_dir) as it:
                for item in it:
                    if item.name in by_file:
                        st = item.stat()
                        seen[item.name] = (st.st_mtime_ns, st.st_size)
        except OSError:
            pass
        events = []
        with self.lock:
            for fname, entry in by_file.items():
                stamp = seen.get(fname)
                if stamp == self._stamps.get(fname):
                    continue
                if stamp is None:
                    self._stamps.pop(fname, None)
                    rev = None
                else:
                    self._stamps[fname] = stamp
                    try:
                        rev = rev_of((self.lib.out_dir / fname).read_bytes())
                    except OSError:
                        continue
                changed = self.revs.get(fname) != rev
                self.revs[fname] = rev
                if changed and self._primed:
                    events.append({"type": "params", "name": entry.name, "rev": rev, "source": None})
            self._primed = True
        for ev in events:
            self.hub.publish(ev)
        return events

    def start_watching(self) -> None:
        self.scan()                                   # 起動時点の状態を覚える（イベントは出さない）

        def loop():
            while not self._stop.wait(WATCH_INTERVAL):
                try:
                    self.scan()
                except Exception:  # noqa: BLE001 — 監視は止めない
                    pass

        threading.Thread(target=loop, name="inkdeus-watch", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


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
               cache_root: Path | str | None = None) -> Flask:
    """アプリを作る。project はプロジェクトの情報（任意）。
    local_config は共通設定ファイル（省略時はリポジトリ直下の inkdeus.local.toml）、
    cache_root は漫画エディタのキャッシュ置き場（省略時は INKDEUS_CACHE か既定の場所）"""
    lib = Library(Path(input_path), Path(mask_dir) if mask_dir else None,
                  Path(out_dir) if out_dir else None)
    web = find_web_dir()
    session = Session(lib)
    project_dir = Path(project["path"]) if project and project.get("path") else None
    catalog = assets_mod.Catalog(
        lib,
        project_file=project_dir / "inkdeus.project.toml" if project_dir else None,
        local_file=Path(local_config) if local_config else default_local_config())
    manga_dir = (project_dir / "manga") if project_dir else lib.out_dir / "manga"
    manga_site = manga_mod.MangaSite(Path(cache_root) if cache_root else None)
    app = Flask(__name__, static_folder=str(web), static_url_path="")
    app.config["MAX_CONTENT_LENGTH"] = MAX_EXPORT_BYTES
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
    app.config["INKDEUS_LIBRARY"] = lib
    app.config["INKDEUS_SESSION"] = session
    app.config["INKDEUS_ASSETS"] = catalog
    app.config["INKDEUS_MANGA_DIR"] = manga_dir
    if watch:
        session.start_watching()

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
            "images": len(lib.entries()),
            "clients": session.hub.count(),
            "project": project,
            "source": str(lib.folder),
            "masks": str(lib.mask_dir) if lib.mask_dir else None,
            "out": str(lib.out_dir),
            "manga": str(manga_dir),
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

    @app.get("/api/assets/<source>/image/<path:rel>")
    def api_assets_image(source: str, rel: str):
        item = asset_item(source, rel)
        limit = int_arg("max", None, *assets_mod.MAX_SIDE_RANGE)
        if limit is None:
            return send_file(item.path, conditional=True)
        return send_derived(item, "max", limit)

    @app.get("/api/assets/<source>/thumb/<path:rel>")
    def api_assets_thumb(source: str, rel: str):
        item = asset_item(source, rel)
        return send_derived(item, "thumb", int_arg("h", 160, *assets_mod.THUMB_RANGE))

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

    @app.get("/api/images")
    def api_images():
        items = []
        for e in lib.entries().values():
            w, h = lib.size(e)
            items.append({
                "name": e.name, "width": w, "height": h,
                "hasMask": lib.mask_path(e) is not None,
                "hasParams": lib.params_path(e).is_file(),
            })
        return jsonify({"images": items, "outDir": str(lib.out_dir),
                        "maskDir": str(lib.mask_dir) if lib.mask_dir else None})

    @app.get("/api/image/<path:name>")
    def api_image(name: str):
        return send_file(lib.get(name).path)

    @app.get("/api/mask/<path:name>")
    def api_mask(name: str):
        p = lib.mask_path(lib.get(name))
        if p is None:
            abort(404)
        return send_file(p, mimetype="image/png")

    # ---- 設定 ----

    @app.route("/api/params/<path:name>", methods=["GET", "PUT", "PATCH"])
    def api_params(name: str):
        entry = lib.get(name)
        if request.method == "GET":
            if request.args.get("resolve"):
                return jsonify(session.resolved(entry))
            data, rev = session.read_params(entry)
            if data is None:
                abort(404)
            resp = Response(data, mimetype="application/json")
            resp.headers["X-Inkdeus-Rev"] = rev
            return resp
        body = json_body()
        if request.method == "PUT":
            # ブラウザ自身の保存（ブラウザ側で整えてあるのでそのまま書く）
            rev = session.write_params(entry, body, request.headers.get("X-Inkdeus-Client"))
            return jsonify({"path": str(lib.params_path(entry)), "rev": rev})
        # PATCH: 外部からの部分更新（JSON Merge Patch）。検証に通らなければ何も書かない
        base = session.resolved(entry)["params"]
        try:
            params, _ = schema.normalize(schema.merge_patch(base, body), strict=True)
        except schema.ValidationError as e:
            return fail(e.problems)
        rev = session.write_params(entry, params, None)
        return jsonify({"name": entry.name, "rev": rev, "params": params})

    @app.post("/api/set/<path:name>")
    def api_set(name: str):
        entry = lib.get(name)
        body = json_body()
        if not isinstance(body.get("key"), str) or "value" not in body:
            return fail(["key と value が必要です"])
        base = session.resolved(entry)["params"]
        try:
            params, _ = schema.normalize(schema.set_path(base, body["key"], body["value"]),
                                         strict=True)
        except schema.ValidationError as e:
            return fail(e.problems)
        rev = session.write_params(entry, params, None)
        return jsonify({"name": entry.name, "rev": rev, "params": params})

    @app.post("/api/style/<path:name>")
    def api_style(name: str):
        entry = lib.get(name)
        body = json_body()
        base = session.resolved(entry)["params"]
        try:
            style = schema.resolve_style(str(body.get("style", "")))
            params = schema.apply_style(base, str(body.get("style", "")), str(body.get("region", "person")))
            params, _ = schema.normalize(params, strict=True)
        except schema.ValidationError as e:
            return fail(e.problems)
        rev = session.write_params(entry, params, None)
        return jsonify({"name": entry.name, "rev": rev, "style": style,
                        "region": body.get("region", "person"), "params": params})

    @app.get("/api/styles")
    def api_styles():
        return jsonify({"styles": schema.list_styles(), "default": schema.load()["defaultStyle"]})

    # ---- 書き出し ----

    @app.post("/api/export/<path:name>")
    def api_export(name: str):
        entry = lib.get(name)
        data = request.get_data(cache=False)
        if not data.startswith(PNG_SIGNATURE):
            abort(400, description="PNG ではありません")
        path = lib.export_path(entry)
        _atomic_write(path, data)
        with session.lock:
            seq = session.exports.get(entry.name, {}).get("seq", 0) + 1
            session.exports[entry.name] = {"seq": seq, "path": str(path), "bytes": len(data),
                                           "time": time.time(), "rev": request.headers.get("X-Inkdeus-Rev")}
        return jsonify({"path": str(path), "bytes": len(data)})

    @app.get("/api/exported/<path:name>")
    def api_exported(name: str):
        entry = lib.get(name)
        return jsonify(session.exports.get(entry.name, {"seq": 0}))

    # ---- 外部からの操作（状態・イベント・統計・指示） ----

    @app.route("/api/state", methods=["GET", "POST"])
    def api_state():
        if request.method == "POST":
            body = json_body()
            current = body.get("current")
            if current is not None and current not in lib.entries():
                abort(404)
            session.current = current
        return jsonify(info())

    @app.get("/api/events")
    def api_events():
        q = session.hub.subscribe()

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
        entry = lib.get(name)
        if request.method == "POST":
            body = json_body()
            body["name"] = entry.name
            body["receivedAt"] = time.time()
            session.stats[entry.name] = body
            return jsonify({"ok": True})
        _, rev = session.read_params(entry)
        stats = session.stats.get(entry.name)
        return jsonify({
            "name": entry.name, "currentRev": rev, "stats": stats,
            "fresh": stats is not None and stats.get("rev") == rev,
            "clients": session.hub.count(), "current": session.current,
        })

    @app.post("/api/command")
    def api_command():
        body = json_body()
        kind = body.get("type")
        if kind not in ("open", "export"):
            abort(400, description="type は open か export です")
        entry = lib.get(str(body.get("name", "")))
        clients = session.hub.publish({"type": kind, "name": entry.name})
        return jsonify({"type": kind, "name": entry.name, "clients": clients})

    return app
