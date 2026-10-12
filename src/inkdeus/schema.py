"""設定JSONの検証・既定値の補完・旧形式の移行・スタイルの適用（サーバ / ctl 用）。

キー・既定値・値域・名前付きスタイルの定義は web/schema.json が真実の源で、
ブラウザ側（web/js/schema.js）も同じファイルを読む。ここは同じ規則の Python 版。
ブラウザ側は「丸めて警告」、こちらの strict=True は「エラーにする」（外部からの変更の打ち間違いを通さない）。
"""

from __future__ import annotations

import copy
import functools
import json
import math
import unicodedata
from pathlib import Path
from typing import Any

REGIONS = ("person", "background")
TONE_KEYS = ("levels", "solid", "layers")


class ValidationError(ValueError):
    """設定として受け付けられない（problems に理由の一覧）"""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def schema_path() -> Path:
    here = Path(__file__).resolve()
    for cand in (here.parents[2] / "web" / "schema.json", here.parent / "web" / "schema.json"):
        if cand.is_file():
            return cand
    raise FileNotFoundError("web/schema.json が見つかりません")


@functools.cache
def load() -> dict:
    with open(schema_path(), encoding="utf-8") as f:
        return json.load(f)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _field_default(f: dict, owner: bool) -> Any:
    return f["ownerDef"] if owner and "ownerDef" in f else f["def"]


def _norm_value(v: Any, f: dict, path: str, problems: list[str], owner: bool, present: bool) -> Any:
    default = _field_default(f, owner)
    if not present:
        return default
    kind = f["type"]
    if kind == "bool" and isinstance(v, bool):
        return v
    if kind == "range" and _is_number(v):
        c = min(f["max"], max(f["min"], v))
        if c != v:
            problems.append(f"{path}: {v} は範囲外です（{f['min']}〜{f['max']}）")
        return c
    if kind == "select" and any(key == v for key, _ in f["options"]):
        return v
    if kind == "text" and isinstance(v, str):
        return v[: f["maxLength"]]
    choices = "（" + " / ".join(key for key, _ in f["options"]) + "）" if kind == "select" else ""
    problems.append(f"{path}: 値 {json.dumps(v, ensure_ascii=False)} は使えません{choices}")
    return default


def _norm_fields(raw: Any, fields: dict, path: str, problems: list[str], owner: bool,
                 extra: tuple[str, ...] = ()) -> dict:
    if raw is not None and not isinstance(raw, dict):
        problems.append(f"{path}: オブジェクトではありません")
        raw = None
    src = raw or {}
    for key in src:
        if key not in fields and key not in extra:
            problems.append(f"未知のキー: {path}.{key}")
    return {key: _norm_value(src.get(key), f, f"{path}.{key}", problems, owner, key in src)
            for key, f in fields.items()}


def _norm_layers(raw: Any, path: str, problems: list[str], fallback: list, present: bool) -> list:
    s = load()
    layers = raw if present else fallback
    if not isinstance(layers, list):
        problems.append(f"{path}: 配列ではありません")
        layers = fallback
    if len(layers) > s["maxLayers"]:
        problems.append(f"{path}: {len(layers)} 層あります（最大 {s['maxLayers']} 層）")
        layers = layers[: s["maxLayers"]]
    return [_norm_fields(layer, s["layer"], f"{path}[{i}]", problems, False) for i, layer in enumerate(layers)]


def _norm_region_tone(src: dict, out: dict, path: str, problems: list[str]) -> None:
    s = load()
    style = s["styles"][s["defaultStyle"]]
    out["levels"] = _norm_fields(src.get("levels"), s["levels"], f"{path}.levels", problems, False)
    out["solid"] = _norm_fields(src["solid"] if "solid" in src else style["solid"], s["solid"],
                                f"{path}.solid", problems, False)
    out["layers"] = _norm_layers(src.get("layers"), f"{path}.layers", problems, style["layers"], "layers" in src)


def migrate(raw: Any) -> Any:
    """旧形式（version 1: トップレベルに solid / layers / background）を領域別の形へ移す"""
    if not isinstance(raw, dict):
        return raw
    old = "solid" in raw or "layers" in raw or "background" in raw or raw.get("version") == 1
    if not old or "regions" in raw:
        return raw
    rest = {k: v for k, v in raw.items() if k not in ("solid", "layers", "background")}
    person = {k: raw[k] for k in ("solid", "layers") if k in raw}
    rest["version"] = load()["version"]
    rest["regions"] = {"person": person, "background": raw.get("background", {})}
    return rest


def normalize(raw: Any, owner: bool = False, strict: bool = False) -> tuple[dict, list[str]]:
    """設定を整えて (params, problems) を返す。strict=True で問題があれば ValidationError"""
    s = load()
    problems: list[str] = []
    if not isinstance(raw, dict):
        if raw is not None:
            problems.append("設定がオブジェクトではありません")
        raw = {}
    raw = migrate(raw)
    sections = s["sections"]
    known = {"version", "unit", "regions", *sections}
    for key in raw:
        if key not in known:
            problems.append(f"未知のキー: {key}")
    if "version" in raw and raw["version"] != s["version"]:
        problems.append(f"version {json.dumps(raw['version'])} は未対応です")

    params: dict = {"version": s["version"], "unit": None}
    unit = raw.get("unit")
    if unit is not None:
        if _is_number(unit) and unit > 0:
            params["unit"] = min(s["unitRange"]["max"], max(s["unitRange"]["min"], unit))
            if params["unit"] != unit:
                problems.append(f"unit: {unit} は範囲外です（{s['unitRange']['min']}〜{s['unitRange']['max']}）")
        else:
            problems.append(f"unit: 値 {json.dumps(unit, ensure_ascii=False)} は使えません")
    params["input"] = _norm_fields(raw.get("input"), sections["input"], "input", problems, owner)
    params["lines"] = _norm_fields(raw.get("lines"), sections["lines"], "lines", problems, owner)

    regions = raw.get("regions")
    if regions is not None and not isinstance(regions, dict):
        problems.append("regions: オブジェクトではありません")
        regions = None
    regions = regions or {}
    for key in regions:
        if key not in REGIONS:
            problems.append(f"未知のキー: regions.{key}")
    person = _norm_fields(regions.get("person"), {}, "regions.person", problems, owner, TONE_KEYS)
    _norm_region_tone(regions["person"] if isinstance(regions.get("person"), dict) else {},
                      person, "regions.person", problems)
    background = _norm_fields(regions.get("background"), s["background"], "regions.background",
                              problems, owner, TONE_KEYS)
    _norm_region_tone(regions["background"] if isinstance(regions.get("background"), dict) else {},
                      background, "regions.background", problems)
    params["regions"] = {"person": person, "background": background}

    params["watermark"] = _norm_fields(raw.get("watermark"), sections["watermark"], "watermark", problems, owner)
    params["output"] = _norm_fields(raw.get("output"), sections["output"], "output", problems, owner)
    if strict and problems:
        raise ValidationError(problems)
    return params, problems


def default_params(owner: bool = False) -> dict:
    return normalize({}, owner)[0]


def merge_patch(target: Any, patch: Any) -> Any:
    """JSON Merge Patch (RFC 7396)。オブジェクトは再帰的に統合、null は削除、配列などは丸ごと置換"""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = merge_patch(out.get(key), value)
    return out


def set_path(params: dict, dotted: str, value: Any) -> dict:
    """ドット区切りのキー（配列は番号。例 regions.person.layers.0.angle）で1項目だけ差し替える"""
    out = copy.deepcopy(params)
    keys = [k for k in dotted.split(".") if k != ""]
    if not keys:
        raise ValidationError(["キーが空です"])
    node: Any = out
    for i, key in enumerate(keys):
        last = i == len(keys) - 1
        where = ".".join(keys[: i + 1])
        if isinstance(node, list):
            if not key.isdigit() or int(key) >= len(node):
                raise ValidationError([f"{where}: 配列の番号が正しくありません（0〜{len(node) - 1}）"])
            if last:
                node[int(key)] = value
            else:
                node = node[int(key)]
        elif isinstance(node, dict):
            if last:
                node[key] = value
            elif key not in node:
                raise ValidationError([f"未知のキー: {where}"])
            else:
                node = node[key]
        else:
            raise ValidationError([f"{where}: この下にキーはありません"])
    return out


def _style_key(name: str) -> str:
    text = unicodedata.normalize("NFKC", str(name)).lower()
    return "".join(ch for ch in text if not ch.isspace() and ch != "_")


def resolve_style(name: str) -> str | None:
    """スタイル名（キー・表示名・別名）からキーを引く"""
    want = _style_key(name)
    for key, style in load()["styles"].items():
        names = [key, style["label"], *style.get("aliases", [])]
        if any(_style_key(n) == want for n in names):
            return key
    return None


def apply_style(params: dict, name: str, region: str = "person") -> dict:
    """名前付きスタイルを領域（person / background / both）に適用した新しい設定を返す"""
    key = resolve_style(name)
    if key is None:
        known = ", ".join(load()["styles"])
        raise ValidationError([f"スタイルが見つかりません: {name}（{known}）"])
    if region not in (*REGIONS, "both"):
        raise ValidationError([f"region は person / background / both のどれかです: {region}"])
    s = load()
    style = s["styles"][key]
    out = copy.deepcopy(params)

    def fill(target: dict) -> None:
        target["solid"] = {**target["solid"], **copy.deepcopy(style["solid"])}
        target["layers"] = [_norm_fields(layer, s["layer"], "layer", [], False) for layer in style["layers"]]

    if region in ("person", "both"):
        fill(out["regions"]["person"])
    if region in ("background", "both"):
        bg = out["regions"]["background"]
        if bg["linked"]:
            bg["levels"] = copy.deepcopy(out["regions"]["person"]["levels"])
        bg["linked"] = False
        bg["mode"] = style["backgroundMode"]
        fill(bg)
    return out


def list_styles() -> list[dict]:
    return [{"name": key, "label": st["label"], "aliases": st.get("aliases", []),
             "description": st.get("description", ""), "backgroundMode": st["backgroundMode"],
             "layers": len(st["layers"]), "solid": st["solid"]["enabled"]}
            for key, st in load()["styles"].items()]
