"""漫画の構成で使う素材（画像）の「ソース」と、その一覧・縮小画像。

ソースは名前の付いた画像置き場で、既定で2つある:
    source  プロジェクトの元画像
    inked   inkdeus で書き出した PNG（out フォルダの *.png）
加えて、プロジェクトの inkdeus.project.toml と、全プロジェクト共通の inkdeus.local.toml の
[[assets]]（name / path / mode / recursive）で追加できる。

mode は使い方の区別: "panel" = コマにはめ込む画像、"free" = コマにはめずページ上へ自由に置く素材（擬音など）。
"""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
MODES = ("panel", "free")
BUILTIN = {"source": "元画像", "inked": "白黒"}
NAME_RE = re.compile(r"[\w\-]{1,40}")
MAX_SIDE_RANGE = (16, 16384)
THUMB_RANGE = (16, 1024)
DEFAULT_LIMIT, MAX_LIMIT = 200, 1000


DEFAULT_FOLDER = "default"


def source_name(kind: str, folder: str) -> str:
    """画像フォルダの元画像・白黒のソース名（既定のフォルダは source / inked のまま）"""
    return kind if folder in ("", DEFAULT_FOLDER) else f"{kind}@{folder}"


class AssetError(Exception):
    """利用者に見せるエラー"""


@dataclass(frozen=True)
class Source:
    name: str
    label: str
    mode: str
    recursive: bool
    root: Path | None           # 既定のソース（source / inked）は None
    builtin: bool = False
    origin: str = "builtin"     # builtin / project / local
    kind: str = ""              # 既定のソースの種類（"source" = 元画像 / "inked" = 白黒）。追加のソースは ""
    folder: str = ""            # 既定のソースが属する画像フォルダ（"default" か追加フォルダの名前）


@dataclass(frozen=True)
class Item:
    rel: str                    # ソース内の相対パス（/ 区切り）。API はこれを名前として受け付ける
    path: Path
    group: str                  # サブフォルダ名（直下の物は ""）


def validate_name(name: str) -> str:
    if not isinstance(name, str) or NAME_RE.fullmatch(name) is None or name in BUILTIN:
        raise AssetError(f"ソース名として使えません: {name!r}（英数字・かな漢字・_ - で 40 文字まで。"
                         f"{' / '.join(BUILTIN)} は予約済み）")
    return name


def parse_entries(raw, base: Path, origin: str) -> tuple[list[Source], list[str]]:
    """toml の [[assets]] を読む。使えない項目は飛ばして、理由を返す"""
    sources, problems = [], []
    if raw is None:
        return sources, problems
    if not isinstance(raw, list):
        return sources, ["assets は [[assets]] の並びで書いてください"]
    for i, entry in enumerate(raw):
        where = f"assets[{i}]"
        if not isinstance(entry, dict):
            problems.append(f"{where}: 表（name / path …）ではありません")
            continue
        try:
            name = validate_name(entry.get("name"))
        except AssetError as e:
            problems.append(f"{where}: {e}")
            continue
        if not isinstance(entry.get("path"), str) or not entry["path"]:
            problems.append(f"{where}（{name}）: path がありません")
            continue
        mode = entry.get("mode", "panel")
        if mode not in MODES:
            problems.append(f"{where}（{name}）: mode は {' / '.join(MODES)} のどちらかです")
            continue
        p = Path(entry["path"]).expanduser()
        root = (p if p.is_absolute() else base / p).resolve()
        sources.append(Source(name, name, mode, entry.get("recursive") is True, root, origin=origin))
    return sources, problems


def read_toml_assets(path: Path | None, origin: str) -> tuple[list[Source], list[str]]:
    if path is None:
        return [], []
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return [], []
    return parse_entries(data.get("assets"), Path(path).parent, origin)


def _is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS and not path.name.startswith(".") and path.is_file()


def _sort_key(item: Item):
    return (item.group.casefold(), item.rel.casefold())


class Catalog:
    """ソースの一覧と、各ソースの画像。設定ファイルは毎回読み直す（稼働中に追加しても反映される）"""

    def __init__(self, lib, project_file: Path | None = None, local_file: Path | None = None, folders=None):
        """folders は画像フォルダの並び [(名前, Library), …] を返す関数（省略時は既定のフォルダだけ）"""
        self.lib = lib
        self.folders = folders or (lambda: [(DEFAULT_FOLDER, lib)])
        self.project_file = project_file
        self.local_file = local_file
        self.cache_dir = lib.out_dir / ".cache" / "assets"
        self._dims: dict[tuple[str, int, int], tuple[int, int]] = {}

    # ---- ソース ----

    def sources(self) -> tuple[dict[str, Source], list[str]]:
        # 元画像と白黒は画像フォルダごとのソース。既定のフォルダは従来の名前（source / inked）のまま、
        # 追加のフォルダは source@<名前> / inked@<名前>
        out = {}
        for folder, _lib in self.folders():
            for kind, label in BUILTIN.items():
                name = source_name(kind, folder)
                shown = label if folder == DEFAULT_FOLDER else f"{label}（{folder}）"
                out[name] = Source(name, shown, "panel", False, None, builtin=True, kind=kind, folder=folder)
        problems: list[str] = []
        # プロジェクトの定義を優先し、同じ名前の共通定義は使わない
        for path, origin in ((self.project_file, "project"), (self.local_file, "local")):
            found, bad = read_toml_assets(path, origin)
            problems += bad
            for src in found:
                out.setdefault(src.name, src)
        return out, problems

    def source(self, name: str) -> Source | None:
        return self.sources()[0].get(name)

    # ---- 画像の一覧 ----

    def lib_of(self, src: Source):
        """既定のソース（元画像・白黒）が属する画像フォルダの Library"""
        for folder, lib in self.folders():
            if folder == src.folder:
                return lib
        return self.lib

    def items(self, src: Source) -> list[Item]:
        if src.kind == "source":
            return [Item(e.name, e.path, "") for e in self.lib_of(src).entries().values()]
        if src.kind == "inked":
            return self._scan(self.lib_of(src).out_dir, False, only_png=True)
        return self._scan(src.root, src.recursive)

    @staticmethod
    def _scan(root: Path | None, recursive: bool, only_png: bool = False) -> list[Item]:
        if root is None or not root.is_dir():
            return []
        found: list[Item] = []
        if recursive:
            for cur, dirs, files in os.walk(root):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                base = Path(cur)
                group = base.relative_to(root).as_posix()
                group = "" if group == "." else group
                for fname in files:
                    p = base / fname
                    if _is_image(p):
                        found.append(Item(f"{group}/{fname}" if group else fname, p, group))
        else:
            for p in root.iterdir():
                if _is_image(p) and (not only_png or p.suffix.lower() == ".png"):
                    found.append(Item(p.name, p, ""))
        return sorted(found, key=_sort_key)

    def find(self, src: Source, rel: str) -> Item | None:
        """一覧に出る物だけを返す（パスを組み立てて開くことはしない）"""
        for item in self.items(src):
            if item.rel == rel:
                return item
        return None

    def size(self, item: Item) -> tuple[int, int]:
        try:
            st = item.path.stat()
        except OSError:
            return (0, 0)
        key = (str(item.path), st.st_mtime_ns, st.st_size)
        if key not in self._dims:
            try:
                with Image.open(item.path) as im:
                    self._dims[key] = im.size
            except Exception:  # noqa: BLE001 — 壊れた画像は 0x0 として一覧に出す
                self._dims[key] = (0, 0)
        return self._dims[key]

    # ---- 元画像と白黒版（書き出した PNG）の対応 ----

    def _inked_of(self, lib, entry, version: str = "") -> dict | None:
        """その画像・バージョンの白黒版（書き出した PNG）。path はファイル名、version は更新時刻（ミリ秒）、
        name はバージョン名（"" = 既定）"""
        p = lib.export_path(entry, version)
        try:
            st = p.stat()
        except OSError:
            return None
        return {"path": p.name, "name": version, "mtime": st.st_mtime, "version": st.st_mtime_ns // 1_000_000}

    def _inked_versions(self, lib, entry, index: dict | None = None) -> list[dict]:
        """書き出し済みのバージョンの白黒版の並び（既定が先頭）"""
        rows = []
        for v in lib.versions(entry, index):
            if v["hasExport"]:
                found = self._inked_of(lib, entry, v["name"])
                if found is not None:
                    rows.append(found)
        return rows

    @staticmethod
    def _split_inked(rel: str) -> str | None:
        """白黒版のファイル名から基底名を取る（直下の .png だけ）"""
        if "/" in rel or "\\" in rel or not rel.lower().endswith(".png"):
            return None
        return rel[:-4]

    def pair(self, src: Source, rel: str) -> dict | None:
        """source か inked の名前から、元画像と白黒版の対応を返す（どの画像の物とも読めない名前は None）:
            name      元画像の名前（白黒化の画面で開く名前）
            source    {path}
            version   その名前が指すバージョン（source から引いたときは ""）
            inked     そのバージョンの白黒版（まだ無ければ None）
            default   既定のバージョンの白黒版（無ければ None）
            versions  書き出し済みの全バージョンの白黒版
            missing   inked の名前で引いたが、その PNG が無い（消された・名前の変わったバージョン）
            folder / sourceName / inkedName  画像フォルダの名前と、そのフォルダの元画像・白黒のソース名"""
        if not src.kind:
            return None
        lib = self.lib_of(src)
        source = src.kind
        where = {"folder": src.folder, "sourceName": source_name("source", src.folder),
                 "inkedName": source_name("inked", src.folder)}
        entries = lib.entries()
        version = ""
        if source == "source":
            entry = entries.get(rel)
            if entry is None:
                return None
        else:
            stem = self._split_inked(rel)
            found = lib.resolve_stem(stem, entries) if stem is not None else None
            if found is None:
                # 元画像が見当たらない書き出し済み PNG（元画像を消した等）
                item = self.find(src, rel)
                if item is None:
                    return None
                st = item.path.stat()
                inked = {"path": rel, "name": "", "mtime": st.st_mtime, "version": st.st_mtime_ns // 1_000_000}
                return {"name": None, "source": None, "version": "", "inked": inked, "default": inked,
                        "versions": [inked], "missing": False, **where}
            entry, version = found
        inked = self._inked_of(lib, entry, version)
        return {"name": entry.name, "source": {"path": entry.name}, "version": version, "inked": inked,
                "default": self._inked_of(lib, entry), "versions": self._inked_versions(lib, entry),
                "missing": source == "inked" and inked is None, **where}

    def fallback(self, src: Source, rel: str) -> Item | None:
        """無い白黒版（消された・名前の変わったバージョン）の代わりに出す画像: 既定のバージョン、無ければ元画像"""
        stem = self._split_inked(rel)
        lib = self.lib_of(src)
        found = lib.resolve_stem(stem) if stem is not None else None
        if found is None or found[1] == "":
            return None
        entry = found[0]
        default = lib.export_path(entry)
        if default.is_file():
            return Item(default.name, default, "")
        return Item(entry.name, entry.path, "") if entry.path.is_file() else None

    def versions(self, src: Source) -> dict[str, int]:
        out = {}
        for item in self.items(src):
            try:
                out[item.rel] = item.path.stat().st_mtime_ns // 1_000_000
            except OSError:
                pass
        return out

    def summary(self) -> dict:
        sources, problems = self.sources()
        rows = []
        for src in sources.values():
            row = {"name": src.name, "label": src.label, "mode": src.mode, "recursive": src.recursive,
                   "builtin": src.builtin, "origin": src.origin, "count": len(self.items(src)),
                   "kind": src.kind, "folder": src.folder if src.builtin else None}
            if src.root is not None:
                row["path"] = str(src.root)
                row["missing"] = not src.root.is_dir()
            rows.append(row)
        return {"sources": rows, "problems": problems}

    def listing(self, src: Source, q: str = "", offset: int = 0, limit: int = DEFAULT_LIMIT) -> dict:
        items = self.items(src)
        needle = q.strip().casefold()
        if needle:
            items = [i for i in items if needle in i.rel.casefold()]
        offset = max(0, offset)
        limit = max(1, min(MAX_LIMIT, limit))
        rows = []
        lib = self.lib_of(src)
        entries = lib.entries() if src.builtin else {}
        index = lib.version_index(entries) if src.kind == "source" else None
        owners: dict[str, tuple] = {}
        if src.kind == "inked":
            # 元画像ごとにまとめ、既定のバージョンを先頭にして並べる（同じ元画像のバージョンが隣り合うように）
            for item in items:
                found = lib.resolve_stem(item.rel[:-4], entries)
                if found is not None:
                    owners[item.rel] = found
            items.sort(key=lambda i: ((owners[i.rel][0].name.casefold(), owners[i.rel][1].casefold())
                                      if i.rel in owners else (i.rel.casefold(), "")))
        for item in items[offset:offset + limit]:
            w, h = self.size(item)
            try:
                st = item.path.stat()
                mtime, nbytes, version = st.st_mtime, st.st_size, st.st_mtime_ns // 1_000_000
            except OSError:
                mtime, nbytes, version = 0.0, 0, 0
            row = {"path": item.rel, "name": item.path.name, "group": item.group,
                   "width": w, "height": h, "mtime": mtime, "bytes": nbytes, "version": version,
                   "stamp": version}          # stamp = 更新時刻（ミリ秒）。白黒の一覧では version がバージョン名になる
            if src.kind == "source":
                row["editable"] = item.rel                     # 白黒化の画面で開く画像名
                row["inked"] = self._inked_of(lib, entries[item.rel]) if item.rel in entries else None
                row["versions"] = self._inked_versions(lib, entries[item.rel], index) if item.rel in entries else []
            elif src.kind == "inked":
                owner = owners.get(item.rel)
                row["editable"] = owner[0].name if owner else None      # 元画像が無ければ None
                row["base"] = owner[0].name if owner else None          # 元画像の名前
                row["version"] = owner[1] if owner else ""              # バージョン名（"" = 既定）
            rows.append(row)
        return {"source": src.name, "label": src.label, "mode": src.mode, "recursive": src.recursive,
                "kind": src.kind, "folder": src.folder if src.builtin else None,
                "total": len(items), "offset": offset, "limit": limit, "items": rows}

    # ---- 縮小画像（キャッシュつき） ----

    def derived(self, item: Item, kind: str, value: int) -> tuple[Path, str]:
        """縮小した画像の実パスと MIME 型。kind は "max"（長辺）か "thumb"（高さ）。
        縮小の必要が無ければ元ファイルを返す。結果は <out>/.cache/assets/ に置き、
        元ファイルの更新時刻とサイズが変わったら作り直す"""
        st = item.path.stat()
        w, h = self.size(item)
        if w <= 0 or h <= 0:
            raise AssetError("画像として読めません")
        if kind == "max":
            scale = value / max(w, h)
        else:
            scale = value / h
        if scale >= 1.0:
            return item.path, Image.MIME.get(_format_of(item.path), "application/octet-stream")
        key = hashlib.sha1(f"{item.path}|{kind}|{value}".encode("utf-8")).hexdigest()[:24]
        stamp = f"{st.st_mtime_ns:x}-{st.st_size:x}"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        for old in self.cache_dir.glob(f"{key}-*"):
            if old.name.startswith(f"{key}-{stamp}.") and ".tmp" not in old.name:
                return old, "image/jpeg" if old.suffix == ".jpg" else "image/png"
        size = (max(1, round(w * scale)), max(1, round(h * scale)))
        with Image.open(item.path) as im:
            im = ImageOps.exif_transpose(im)
            alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
            if alpha:
                # 透過の縁に色がにじまないよう、アルファを掛けた状態で縮めてから戻す
                out = im.convert("RGBA").convert("RGBa").resize(size, Image.LANCZOS).convert("RGBA")
            else:
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                out = im.resize(size, Image.LANCZOS, reducing_gap=3.0)
        as_jpeg = kind == "thumb" and not alpha
        target = self.cache_dir / f"{key}-{stamp}.{'jpg' if as_jpeg else 'png'}"
        tmp = self.cache_dir / f"{key}-{stamp}.{uuid.uuid4().hex}.tmp"
        if as_jpeg:
            out.convert("RGB").save(tmp, "JPEG", quality=85)
        else:
            out.save(tmp, "PNG", compress_level=3)
        for old in self.cache_dir.glob(f"{key}-*"):          # 古い版（元ファイルが変わる前の物）を片付ける
            if old != tmp and ".tmp" not in old.name:
                old.unlink(missing_ok=True)
        os.replace(tmp, target)
        return target, "image/jpeg" if as_jpeg else "image/png"


def _format_of(path: Path) -> str:
    return {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP", ".bmp": "BMP"}.get(path.suffix.lower(), "")
