"""部分マスク（髪・肌・服などの領域を示すマスク）の置き場の読み取り。

部分マスクは inkdeus の外で作った物を読むだけ（inkdeus 自体は作らない）。置き方:

    <部分マスクの置き場>/<部分名>/<画像と同じ stem>.png

8bit 単チャンネル・元画像と同じ縦横・255 = その部分 / 0 = それ以外。部分名は置き場の直下のサブフォルダ名から拾う
（1〜32 文字の英数・かな漢字・_ -。person / background / all は予約で、あれば無視する）。1つの置き場につき 6 部分まで。
"""

from __future__ import annotations

from pathlib import Path

from . import schema


class PartsError(ValueError):
    """利用者に見せるエラー"""


def max_parts() -> int:
    return int(schema.load()["parts"]["max"])


def scan(root: Path | None) -> tuple[list[str], list[str]]:
    """置き場の直下のサブフォルダから部分名を拾う。戻り値は (部分名（名前順・上限まで）, 注意の一覧)"""
    if root is None:
        return [], []
    try:
        dirs = sorted(p.name for p in Path(root).iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return [], [f"部分マスクの置き場が見つかりません: {root}"]
    names: list[str] = []
    warnings: list[str] = []
    reserved = schema.reserved_parts()
    for name in dirs:
        if name in reserved:
            warnings.append(f"「{name}」は予約された名前なので、部分としては使いません")
        elif not schema.is_part_name(name):
            warnings.append(f"「{name}」は部分名として使えません（英数字・かな漢字・_ - で 1〜32 文字）")
        else:
            names.append(name)
    limit = max_parts()
    if len(names) > limit:
        warnings.append(f"部分が {len(names)} 個あります。名前順で先頭の {limit} 個だけ使います"
                        f"（使わない: {', '.join(names[limit:])}）")
        names = names[:limit]
    return names, warnings


def mask_path(root: Path | None, part: str, stem: str) -> Path | None:
    """その画像（stem）の、その部分のマスク。無ければ None"""
    if root is None:
        return None
    p = Path(root) / part / f"{stem}.png"
    return p if p.is_file() else None


def check_dir(path: Path | str) -> Path:
    """登録する置き場を確かめる（フォルダであること）"""
    folder = Path(path).expanduser().resolve()
    if not folder.is_dir():
        raise PartsError(f"部分マスクの置き場が見つかりません: {folder}")
    return folder


def summary(root: Path | None, stems: list[str]) -> dict:
    """置き場の集計: 部分名と、画像（stem の並び）ごとのマスクの有無"""
    names, warnings = scan(root)
    rows = []
    for name in names:
        found = [s for s in stems if mask_path(root, name, s) is not None]
        rows.append({"name": name, "masks": len(found), "missing": len(stems) - len(found)})
    return {"dir": str(root) if root else None, "parts": rows, "images": len(stems), "warnings": warnings}
