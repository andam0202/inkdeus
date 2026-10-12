"""画像フォルダ（＝画像セット）の定義。プロジェクトは複数の画像フォルダを持てる。

既定のフォルダ（名前 "default"）は、従来の source / masks の組。追加分は設定ファイルの
    [[folders]]
    name = "<名前>"      # 1〜32 文字の英数・かな漢字・_ -。"default" は予約
    images = "<dir>"
    masks = "<dir>"       # 任意
    note = "…"            # 任意
で、保存先は <out>/@<名前>/ 以下（既定のフォルダは従来どおり <out>/ 直下）。
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT = "default"
NAME_RE = re.compile(r"[\w\-]{1,32}")
OUT_PREFIX = "@"                     # <out>/@<フォルダ名>/


class FolderError(ValueError):
    """利用者に見せるエラー"""


@dataclass(frozen=True)
class FolderDef:
    name: str
    images: Path
    masks: Path | None = None
    note: str = ""


def validate_name(name) -> str:
    if not isinstance(name, str) or NAME_RE.fullmatch(name) is None:
        raise FolderError(f"フォルダ名として使えません: {name!r}（英数字・かな漢字・_ - で 1〜32 文字）")
    if name == DEFAULT:
        raise FolderError(f"「{DEFAULT}」は既定のフォルダの名前なので使えません")
    return name


def out_dir(out_root: Path, name: str) -> Path:
    """そのフォルダの保存先（設定JSON・書き出した PNG などの置き場所はここで決める）"""
    return out_root if name == DEFAULT else out_root / f"{OUT_PREFIX}{name}"


def _resolve(base: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return (p if p.is_absolute() else base / p).resolve()


def parse_entries(raw, base: Path) -> tuple[list[FolderDef], list[str]]:
    """toml の [[folders]] を読む。使えない項目は飛ばして、理由を返す"""
    found: list[FolderDef] = []
    problems: list[str] = []
    if raw is None:
        return found, problems
    if not isinstance(raw, list):
        return found, ["folders は [[folders]] の並びで書いてください"]
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        where = f"folders[{i}]"
        if not isinstance(entry, dict):
            problems.append(f"{where}: 表（name / images …）ではありません")
            continue
        try:
            name = validate_name(entry.get("name"))
        except FolderError as e:
            problems.append(f"{where}: {e}")
            continue
        if name in seen:
            problems.append(f"{where}: 同じ名前のフォルダが既にあります: {name}")
            continue
        if not isinstance(entry.get("images"), str) or not entry["images"]:
            problems.append(f"{where}（{name}）: images がありません")
            continue
        seen.add(name)
        masks = entry.get("masks")
        found.append(FolderDef(name, _resolve(base, entry["images"]),
                               _resolve(base, masks) if isinstance(masks, str) and masks else None,
                               str(entry.get("note", ""))))
    return found, problems


def read_toml(path: Path | None) -> tuple[list[FolderDef], list[str]]:
    if path is None:
        return [], []
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return [], []
    return parse_entries(data.get("folders"), Path(path).parent)


def parse_add(spec: str) -> FolderDef:
    """起動引数 --add <名前>=<画像dir>[,<マスクdir>]"""
    name, sep, rest = spec.partition("=")
    if not sep or not rest:
        raise FolderError(f"--add は <名前>=<画像フォルダ>[,<マスクフォルダ>] の形で指定してください: {spec!r}")
    images, _, masks = rest.partition(",")
    return FolderDef(validate_name(name), Path(images).expanduser().resolve(),
                     Path(masks).expanduser().resolve() if masks else None)
