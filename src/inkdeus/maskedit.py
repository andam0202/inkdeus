"""部分マスク・人物マスクの手直し（元のマスクに対する上書き）の置き場と、合成。

元のマスクのファイル（inkdeus の外で作られた物）は書き換えない。手直しは画像ごとに

    <out>/[@<フォルダ>/]<画像の基底名>.maskedit/<対象>.png      対象 = 部分名 または person

へ、8bit グレースケールの 3 値で持つ: 255 = 加える / 0 = 外す / 128 = 触っていない。
バージョンには依存しない（同じ元画像の全バージョンで共通）。

合成の決まり（画面の描画と、inkdeus maskedit export は同じ決まりに従う）:
- 人物 = 元の人物マスク（無ければ全面）に、person の手直しを重ねた物（加える → 人物 / 外す → 背景）
- 部分 k = （元の部分 k ∪ k に加えた所）から、k から外した所と、ほかの部分に加えた所を除いた物。
  同じ画素を 2 つの部分に加えてあるときは、並びが後ろの部分が勝つ。最後に、人物の内側だけに切る
  （部分どうしは排他。どの部分でもない人物の領域は、人物の設定で描かれる）
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from PIL import Image, ImageChops

SUFFIX = ".maskedit"
PERSON = "person"
ALL = "all-targets"                  # 削除でだけ使える「全部の対象」
NEUTRAL = 128                        # 触っていない
ADD_MIN = 192                        # これ以上は「加える」
REMOVE_MAX = 63                      # これ以下は「外す」
TARGET_RE = re.compile(r"[\w\-]{1,32}")


def rows_in(folder: Path, names: list[str] | None = None) -> list[dict]:
    """置き場に保存されている対象の一覧。names（その画像フォルダの部分名）を渡すと、person とその部分だけ"""
    rows = []
    try:
        with os.scandir(folder) as it:
            items = sorted(it, key=lambda i: i.name)
    except OSError:
        return rows
    for item in items:
        target = item.name[:-4] if item.name.endswith(".png") else ""
        if TARGET_RE.fullmatch(target) is None or (names is not None and target != PERSON and target not in names):
            continue
        try:
            if not item.is_file():
                continue
            st = item.stat()
        except OSError:
            continue
        rows.append({"target": target, "file": item.name, "bytes": st.st_size, "mtime": st.st_mtime,
                     "stamp": st.st_mtime_ns // 1_000_000, **({"broken": True} if st.st_size == 0 else {})})
    return rows


def _binary(path: Path | None, size: tuple[int, int], default: int) -> Image.Image:
    """マスクのファイルを、画像と同じ大きさの 0 / 255 にする（無ければ default で埋めた物）"""
    if path is None:
        return Image.new("L", size, default)
    try:
        with Image.open(path) as im:
            im = im.convert("L")
            if im.size != size:
                im = im.resize(size, Image.NEAREST)
            return im.point(lambda v: 255 if v >= 128 else 0)
    except (OSError, ValueError, SyntaxError):
        return Image.new("L", size, default)


def _edit(folder: Path, target: str, size: tuple[int, int]) -> tuple[Image.Image, Image.Image] | None:
    """手直しの (加える所, 外す所)（どちらも 0 / 255）。手直しが無ければ None"""
    path = folder / f"{target}.png"
    try:
        with Image.open(path) as im:
            im = im.convert("L")
            if im.size != size:
                im = im.resize(size, Image.NEAREST)      # 長辺 2048 相当に縮めて持っている手直しを、原寸へ
            return (im.point(lambda v: 255 if v >= ADD_MIN else 0), im.point(lambda v: 255 if v <= REMOVE_MAX else 0))
    except (OSError, ValueError, SyntaxError):
        return None


def compose(size: tuple[int, int], mask: Path | None, parts: dict[str, Path | None], folder: Path) -> dict:
    """元のマスクに手直しを合成する。戻り値は {"person": 画像 | None, "parts": {部分名: 画像}, "edited": [対象]}。
    person は、元の人物マスクも person の手直しも無ければ None（マスクの無い画像）"""
    edited = []
    person = None
    edit = _edit(folder, PERSON, size)
    if mask is not None or edit is not None:
        person = _binary(mask, size, 255)
        if edit is not None:
            edited.append(PERSON)
            person = ImageChops.darker(ImageChops.lighter(person, edit[0]), ImageChops.invert(edit[1]))
    edits = {name: _edit(folder, name, size) for name in parts}
    edited += [name for name in parts if edits[name] is not None]
    names = list(parts)
    # ほかの部分に加えた所（同じ画素を 2 つの部分に加えてあれば、並びが後ろの部分が勝つ）
    adds: dict[str, Image.Image] = {}
    later = Image.new("L", size, 0)
    for name in reversed(names):
        if edits[name] is not None:
            adds[name] = ImageChops.darker(edits[name][0], ImageChops.invert(later))
            later = ImageChops.lighter(later, edits[name][0])
    out = {}
    for name in names:
        part = _binary(parts[name], size, 0)
        if edits[name] is not None:
            part = ImageChops.darker(part, ImageChops.invert(edits[name][1]))
        for other, added in adds.items():
            if other != name:
                part = ImageChops.darker(part, ImageChops.invert(added))
        if name in adds:
            part = ImageChops.lighter(part, adds[name])
        if person is not None:
            part = ImageChops.darker(part, person)           # 人物の内側だけ
        out[name] = part
    return {"person": person, "parts": out, "edited": edited}
