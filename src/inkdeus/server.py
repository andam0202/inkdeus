"""静的ファイル配信とファイル入出力だけを行う小さなサーバ。

描画ロジックは一切持たない（すべてブラウザの WebGL2 側）。
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from PIL import Image

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_PARAMS_BYTES = 1 << 20
MAX_EXPORT_BYTES = 1 << 30
LOCAL_CONFIG_NAME = "inkdeus.local.toml"


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


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_owner(flag: bool = False, environ: dict | None = None,
                  local_config: Path | None = None) -> bool:
    """所有者モードかどうか。起動引数・環境変数・ローカル設定ファイルのどれかで有効になる"""
    if flag:
        return True
    env = os.environ if environ is None else environ
    if _truthy(env.get("INKDEUS_OWNER")):
        return True
    path = default_local_config() if local_config is None else Path(local_config)
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return data.get("owner") is True


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
            files = sorted(p for p in self.folder.iterdir()
                           if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS)
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
        return self.out_dir / f"{entry.stem}.inkdeus.json"

    def export_path(self, entry: Entry) -> Path:
        return self.out_dir / f"{entry.stem}.png"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def create_app(input_path: Path | str, mask_dir: Path | str | None = None,
               out_dir: Path | str | None = None, owner: bool = False) -> Flask:
    """アプリを作る。owner は resolve_owner() で決めた値を渡す"""
    lib = Library(Path(input_path), Path(mask_dir) if mask_dir else None,
                  Path(out_dir) if out_dir else None)
    web = find_web_dir()
    app = Flask(__name__, static_folder=str(web), static_url_path="")
    app.config["MAX_CONTENT_LENGTH"] = MAX_EXPORT_BYTES
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
    app.config["INKDEUS_LIBRARY"] = lib

    @app.before_request
    def _same_origin_only():
        # 他サイトのページからローカルのサーバへ書き込ませない
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("Origin")
            if origin and urlsplit(origin).netloc != request.host:
                abort(403)

    @app.after_request
    def _no_store(resp):
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/")
    def index():
        return send_from_directory(web, "index.html")

    @app.get("/api/config")
    def api_config():
        return jsonify({"owner": bool(owner)})

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

    @app.route("/api/params/<path:name>", methods=["GET", "PUT"])
    def api_params(name: str):
        path = lib.params_path(lib.get(name))
        if request.method == "GET":
            if not path.is_file():
                abort(404)
            return send_file(path, mimetype="application/json")
        raw = request.get_data(cache=False)
        if len(raw) > MAX_PARAMS_BYTES:
            abort(413)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            abort(400, description="JSON として読めません")
        if not isinstance(data, dict):
            abort(400, description="JSON オブジェクトである必要があります")
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        _atomic_write(path, text.encode("utf-8"))
        return jsonify({"path": str(path)})

    @app.post("/api/export/<path:name>")
    def api_export(name: str):
        entry = lib.get(name)
        data = request.get_data(cache=False)
        if not data.startswith(PNG_SIGNATURE):
            abort(400, description="PNG ではありません")
        path = lib.export_path(entry)
        _atomic_write(path, data)
        return jsonify({"path": str(path), "bytes": len(data)})

    return app
