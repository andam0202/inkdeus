"""漫画の構成で使うローカルフォント（利用者のPCにあるフォントファイル）の一覧と配信。

フォントファイルは inkdeus のリポジトリには含まれない。利用者が自分で用意した物を、設定で指定したフォルダから読むだけ:

    [fonts]
    dirs = ["<フォントのあるフォルダ>", …]
    include = ["*.ttf", …]          # ファイル名の glob。省略時は dirs 内の全フォント
    [fonts.names]                   # 任意: ファイル名 → 表示名
    "Example.ttf" = "表示名"

全プロジェクト共通の inkdeus.local.toml と、プロジェクトの inkdeus.project.toml の両方に書ける（合算する）。
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
import struct
import tomllib
from dataclasses import dataclass
from pathlib import Path

FORMATS = {".ttf": ("truetype", "font/ttf"), ".otf": ("opentype", "font/otf"), ".ttc": ("collection", "font/collection"),
           ".woff": ("woff", "font/woff"), ".woff2": ("woff2", "font/woff2")}


@dataclass(frozen=True)
class Font:
    id: str            # URL に使える識別子（ファイルの場所から決まる）
    name: str          # 表示名（CSS の font-family としても使う）
    path: Path
    format: str        # CSS の format(...)
    mime: str

    def info(self) -> dict:
        return {"id": self.id, "name": self.name, "file": self.path.name, "format": self.format,
                "size": self.path.stat().st_size, "url": f"/api/fonts/file/{self.id}",
                "css": f"/api/fonts/css?family={self.name}"}


# ---- フォントファイルの name テーブル（ファミリ名）を読む。標準ライブラリだけで済ませる ----

def _decode(platform: int, encoding: int, raw: bytes) -> str | None:
    try:
        if platform in (0, 3):
            return raw.decode("utf-16-be")
        if platform == 1:
            return raw.decode({0: "mac_roman", 1: "shift_jis"}.get(encoding, "mac_roman"))
    except UnicodeDecodeError:
        return None
    return None


def read_names(path: Path) -> dict:
    """{"family": 表示用のファミリ名, "subfamily": ウェイトなど}。読めなければ空の dict。
    日本語名があれば日本語名を、無ければ英語名を使う。.ttc は先頭のフォントだけを見る"""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            if head[:4] == b"ttcf":
                (first,) = struct.unpack(">I", f.read(4)[:4]) if len(head) >= 12 else (0,)
                f.seek(first)
                head = f.read(12)
            if head[:4] not in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1"):
                return {}
            (count,) = struct.unpack(">H", head[4:6])
            table = None
            for _ in range(min(count, 4096)):
                tag, _sum, offset, length = struct.unpack(">4sIII", f.read(16))
                if tag == b"name":
                    table = (offset, length)
            if table is None or table[1] > 4 << 20:
                return {}
            f.seek(table[0])
            data = f.read(table[1])
    except (OSError, struct.error):
        return {}
    try:
        _fmt, n, strings = struct.unpack(">HHH", data[:6])
        found: dict[tuple[int, str], str] = {}
        for i in range(n):
            platform, encoding, lang, name_id, length, offset = struct.unpack(">6H", data[6 + i * 12: 18 + i * 12])
            if name_id not in (1, 2, 16, 17):
                continue
            text = _decode(platform, encoding, data[strings + offset: strings + offset + length])
            if not text or not text.strip("\x00 "):
                continue
            japanese = (platform == 3 and lang == 0x0411) or (platform == 1 and lang == 11)
            english = (platform == 3 and lang == 0x0409) or (platform == 1 and lang == 0) or platform == 0
            kind = "ja" if japanese else "en" if english else "other"
            found.setdefault((name_id, kind), text.strip("\x00 "))
    except (struct.error, IndexError):
        return {}

    def pick(*ids: int) -> str | None:
        for kind in ("ja", "en", "other"):
            for name_id in ids:
                if (name_id, kind) in found:
                    return found[(name_id, kind)]
        return None

    out = {}
    family = pick(16, 1)
    if family:
        out["family"] = family
    sub = pick(17, 2)
    if sub:
        out["subfamily"] = sub
    return out


def clean_name(name: str) -> str:
    """表示名を、CSS の font-family と URL にそのまま使える形にする"""
    name = re.sub(r'[\x00-\x1f"\\&:;{}<>#?%]', " ", str(name))
    return re.sub(r"\s+", " ", name).strip()[:80]


# ---- 設定から一覧を作る ----

def _read_section(path: Path | None) -> tuple[dict | None, Path | None]:
    if path is None:
        return None, None
    try:
        with open(path, "rb") as f:
            section = tomllib.load(f).get("fonts")
    except (OSError, tomllib.TOMLDecodeError):
        return None, None
    return (section, Path(path).parent) if isinstance(section, dict) else (None, None)


def resolve(config_files: list[Path | None]) -> tuple[list[Font], list[str]]:
    """設定ファイル（共通 → プロジェクトの順など）からフォントの一覧を作る。戻り値は (一覧, 気になる点)"""
    problems: list[str] = []
    picked: list[tuple[Path, str | None]] = []        # (ファイル, 上書きの表示名)
    seen: set[Path] = set()
    for config in config_files:
        section, base = _read_section(config)
        if section is None:
            continue
        dirs = section.get("dirs") or []
        include = section.get("include")
        names = section.get("names") if isinstance(section.get("names"), dict) else {}
        if not isinstance(dirs, list) or not all(isinstance(d, str) for d in dirs):
            problems.append(f"{config}: fonts.dirs はフォルダの並びで書いてください")
            continue
        if include is not None and (not isinstance(include, list) or not all(isinstance(p, str) for p in include)):
            problems.append(f"{config}: fonts.include はファイル名（glob）の並びで書いてください")
            continue
        files: list[Path] = []
        for d in dirs:
            folder = Path(d).expanduser()
            folder = (folder if folder.is_absolute() else base / folder).resolve()
            if not folder.is_dir():
                problems.append(f"フォントのフォルダが見つかりません: {folder}")
                continue
            files += sorted((p for p in folder.iterdir() if p.suffix.lower() in FORMATS and p.is_file()),
                            key=lambda p: p.name.casefold())
        if include is not None:
            for pattern in include:
                if not any(fnmatch.fnmatch(p.name.casefold(), pattern.casefold()) for p in files):
                    problems.append(f"include の {pattern!r} に合うフォントがありません")
            files = [p for p in files if any(fnmatch.fnmatch(p.name.casefold(), pat.casefold()) for pat in include)]
        for p in files:
            if p in seen:
                continue
            seen.add(p)
            override = names.get(p.name)
            picked.append((p, override if isinstance(override, str) and clean_name(override) else None))

    # 表示名を決める。同じ名前が複数あるとき（ウェイト違いなど）はサブファミリ名を足して区別する
    rows = []
    for p, override in picked:
        meta = {} if override else read_names(p)
        rows.append({"path": p, "name": clean_name(override or meta.get("family") or p.stem) or p.stem,
                     "sub": clean_name(meta.get("subfamily", "")), "fixed": bool(override)})
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["name"]] = counts.get(r["name"], 0) + 1
    for r in rows:
        if counts[r["name"]] > 1 and not r["fixed"] and r["sub"]:
            r["name"] = f"{r['name']} {r['sub']}"
    used: set[str] = set()
    fonts = []
    for r in rows:
        name = r["name"]
        if name in used:                                # それでも重なるなら、ファイル名で区別する
            name = clean_name(f"{name} {r['path'].stem}")
        n = 2
        while name in used:
            name = f"{r['name']} {n}"
            n += 1
        used.add(name)
        fmt, mime = FORMATS[r["path"].suffix.lower()]
        fonts.append(Font(hashlib.sha1(str(r["path"]).encode("utf-8")).hexdigest()[:16], name, r["path"], fmt, mime))
    return fonts, problems


def css_for(fonts: list[Font]) -> str:
    """@font-face の並び。ブラウザは、そのフォントを使う文字が出てきたときに初めて本体を取りに行く"""
    return "".join(
        f'@font-face{{font-family:"{f.name}";src:url("/api/fonts/file/{f.id}") format("{f.format}");'
        "font-display:swap;}\n" for f in fonts)
