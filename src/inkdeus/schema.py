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
import re
import secrets
import unicodedata
from pathlib import Path
from typing import Any

REGIONS = ("person", "background")
TONE_KEYS = ("levels", "solid", "layers")
# 層の id（消しゴムのマスクがどの層の物かを決める名前）。短い乱数文字列で、並べ替えても変わらない。
# 形は web/schema.json の layerId と同じ
LAYER_ID_RE = re.compile(r"[a-z0-9]{4,16}")
# 部分（髪・肌・服など。部分マスクのサブフォルダ名）の名前。regions.<部分名> として設定を持てる
PART_NAME_RE = re.compile(r"[\w\-]{1,32}")
_ID_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789"


def new_layer_id(taken: set[str] | None = None) -> str:
    while True:
        value = "".join(secrets.choice(_ID_CHARS) for _ in range(8))
        if taken is None or value not in taken:
            return value


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


def reserved_parts() -> tuple[str, ...]:
    return tuple(load()["parts"]["reserved"])


def is_part_name(name: Any) -> bool:
    """部分の名前として使えるか（1〜32 文字の英数・かな漢字・_ -。person / background / all は予約）"""
    return isinstance(name, str) and PART_NAME_RE.fullmatch(name) is not None and name not in reserved_parts()


def part_keys(raw: Any) -> list[str]:
    """設定（整える前でもよい）の regions にある、部分の項目の名前"""
    regions = raw.get("regions") if isinstance(raw, dict) else None
    if not isinstance(regions, dict):
        return []
    return [k for k, v in regions.items() if k not in REGIONS and is_part_name(k) and isinstance(v, dict)]


def region_names(raw: Any) -> list[str]:
    """層を持つ領域の名前（人物・背景・部分）"""
    return [*REGIONS, *part_keys(raw)]


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _norm_value(v: Any, f: dict, path: str, problems: list[str], present: bool) -> Any:
    default = f["def"]
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


def _norm_fields(raw: Any, fields: dict, path: str, problems: list[str],
                 extra: tuple[str, ...] = ()) -> dict:
    if raw is not None and not isinstance(raw, dict):
        problems.append(f"{path}: オブジェクトではありません")
        raw = None
    src = raw or {}
    for key in src:
        if key not in fields and key not in extra:
            problems.append(f"未知のキー: {path}.{key}")
    return {key: _norm_value(src.get(key), f, f"{path}.{key}", problems, key in src)
            for key, f in fields.items()}


def _norm_layers(raw: Any, path: str, problems: list[str], fallback: list, present: bool,
                 taken: set[str] | None = None) -> list:
    """層の並びを整える。id の無い層には新しい id を振る（taken は、同じ設定の中で既に使われている id）"""
    s = load()
    taken = set() if taken is None else taken
    layers = raw if present else fallback
    if not isinstance(layers, list):
        problems.append(f"{path}: 配列ではありません")
        layers = fallback
    if len(layers) > s["maxLayers"]:
        problems.append(f"{path}: {len(layers)} 層あります（最大 {s['maxLayers']} 層）")
        layers = layers[: s["maxLayers"]]
    out = []
    for i, layer in enumerate(layers):
        fields = _norm_fields(layer, s["layer"], f"{path}[{i}]", problems, ("id",))
        given = layer.get("id") if isinstance(layer, dict) else None
        if given is not None and given != "" and not (isinstance(given, str) and LAYER_ID_RE.fullmatch(given)):
            problems.append(f"{path}[{i}].id: 値 {json.dumps(given, ensure_ascii=False)} は使えません"
                            f"（英小文字と数字で 4〜16 文字）")
            given = None
        if not given or given in taken:               # 無い・重複している（複製された層）ときは振り直す
            given = new_layer_id(taken)
        taken.add(given)
        out.append({"id": given, **fields})
    return out


def _norm_region_tone(src: dict, out: dict, path: str, problems: list[str], taken: set[str]) -> None:
    s = load()
    style = s["styles"][s["defaultStyle"]]
    out["levels"] = _norm_fields(src.get("levels"), s["levels"], f"{path}.levels", problems)
    out["solid"] = _norm_fields(src["solid"] if "solid" in src else style["solid"], s["solid"],
                                f"{path}.solid", problems)
    out["layers"] = _norm_layers(src.get("layers"), f"{path}.layers", problems, style["layers"], "layers" in src,
                                 taken)


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


def normalize(raw: Any, strict: bool = False, parts: Any = None) -> tuple[dict, list[str]]:
    """設定を整えて (params, problems) を返す。strict=True で問題があれば ValidationError。
    parts は「知っている部分名」（部分マスクの置き場にある部分）。regions.<部分名> の項目は、
    知っている名前か、部分の項目の形（linked を持つ）をしていれば残す（その画像に無い部分の設定も、
    別の画像へ引き継いだときのために残す）。strict で parts を渡したときは、知っている名前だけを通す"""
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
    params["input"] = _norm_fields(raw.get("input"), sections["input"], "input", problems)
    params["lines"] = _norm_fields(raw.get("lines"), sections["lines"], "lines", problems)

    regions = raw.get("regions")
    if regions is not None and not isinstance(regions, dict):
        problems.append("regions: オブジェクトではありません")
        regions = None
    regions = regions or {}
    known = set(parts or ())
    kept: list[str] = []
    for key, value in regions.items():
        if key in REGIONS:
            continue
        shaped = isinstance(value, dict) and "linked" in value
        ok = is_part_name(key) and (key in known or (shaped and not (strict and parts is not None)))
        if ok:
            kept.append(key)
        elif is_part_name(key) and parts is not None:
            problems.append(f"未知のキー: regions.{key}（部分マスクのある部分: {', '.join(sorted(known)) or 'なし'}）")
        else:
            problems.append(f"未知のキー: regions.{key}")
    taken: set[str] = set()                        # 層の id は、人物・背景・部分を通して重複させない
    person = _norm_fields(regions.get("person"), {}, "regions.person", problems, TONE_KEYS)
    _norm_region_tone(regions["person"] if isinstance(regions.get("person"), dict) else {},
                      person, "regions.person", problems, taken)
    background = _norm_fields(regions.get("background"), s["background"], "regions.background",
                              problems, TONE_KEYS)
    _norm_region_tone(regions["background"] if isinstance(regions.get("background"), dict) else {},
                      background, "regions.background", problems, taken)
    params["regions"] = {"person": person, "background": background}
    for key in kept:
        # 部分: linked が true の間は人物と同じ設定で描く（false で、この項目の levels / solid / layers が有効）
        src = regions[key] if isinstance(regions[key], dict) else {}
        part = _norm_fields(regions[key], s["part"], f"regions.{key}", problems, TONE_KEYS)
        _norm_region_tone(src, part, f"regions.{key}", problems, taken)
        params["regions"][key] = part

    params["watermark"] = _norm_fields(raw.get("watermark"), sections["watermark"], "watermark", problems)
    params["output"] = _norm_fields(raw.get("output"), sections["output"], "output", problems)
    if strict and problems:
        raise ValidationError(problems)
    return params, problems


def default_params() -> dict:
    return normalize({})[0]


def _layer_lists(raw: Any):
    """設定（整える前でもよい）の中の、層の並び（人物・背景・部分）を順に返す"""
    regions = raw.get("regions") if isinstance(raw, dict) else None
    for name in region_names(raw):
        region = regions.get(name) if isinstance(regions, dict) else None
        layers = region.get("layers") if isinstance(region, dict) else None
        if isinstance(layers, list):
            yield name, layers


def layer_ids(raw: Any) -> set[str]:
    """設定に含まれる層の id（人物・背景・部分のすべて）"""
    return {layer["id"] for _, layers in _layer_lists(raw) for layer in layers
            if isinstance(layer, dict) and isinstance(layer.get("id"), str) and layer["id"]}


def lacks_layer_ids(raw: Any) -> bool:
    """id の無い層（層の id が入る前に保存された設定）があるか。層の並びが無い設定も「無い」に数える"""
    lists = list(_layer_lists(migrate(raw) if isinstance(raw, dict) else raw))
    if len([name for name, _ in lists if name in REGIONS]) < len(REGIONS):
        return True
    return any(not (isinstance(layer, dict) and layer.get("id")) for _, layers in lists for layer in layers)


def inherit_layer_ids(raw: Any, base: dict) -> Any:
    """層を丸ごと差し替える変更（JSON Merge Patch の配列）で id が書かれていない層に、
    同じ位置にあった層の id を引き継がせる（その層の消しゴムのマスクが外れないように）"""
    out = copy.deepcopy(raw)
    used = layer_ids(out)
    old = dict(_layer_lists(base))
    for name, layers in _layer_lists(out):
        for i, layer in enumerate(layers):
            if not isinstance(layer, dict) or layer.get("id"):
                continue
            before = old.get(name, [])
            given = before[i].get("id") if i < len(before) and isinstance(before[i], dict) else None
            if given and given not in used:
                layer["id"] = given
                used.add(given)
    return out


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


def new_part(params: dict, linked: bool = True) -> dict:
    """部分の項目を新しく作る。人物の levels / solid / layers を写す（切り離した瞬間に見た目が変わらないように）。
    層には新しい id を振る"""
    person = params["regions"]["person"]
    taken = layer_ids(params)
    layers = []
    for layer in person.get("layers", []):
        made = {**layer, "id": new_layer_id(taken)}
        taken.add(made["id"])
        layers.append(made)
    return {"linked": linked, "levels": copy.deepcopy(person.get("levels", {})),
            "solid": copy.deepcopy(person.get("solid", {})), "layers": layers}


def seed_parts(raw: Any, base: dict) -> Any:
    """変更（JSON Merge Patch など）で新しく現れた部分の項目に、書かれていない levels / solid / layers を
    人物の設定から補う（既定値ではなく「いまの人物と同じ」から始まるように）"""
    if not isinstance(raw, dict) or not isinstance(raw.get("regions"), dict):
        return raw
    before = set(part_keys(base))
    fresh = [k for k in part_keys(raw) if k not in before]
    if not fresh:
        return raw
    out = copy.deepcopy(raw)
    for key in fresh:
        seed = new_part({"regions": {**base["regions"], **{k: out["regions"][k] for k in part_keys(out)
                                                           if k != key}}})
        given = out["regions"][key]
        merged = {**{k: v for k, v in seed.items() if k != "linked"}, **given}
        for k in ("levels", "solid"):                       # 一部だけ書かれた levels / solid は、人物の値に重ねる
            if isinstance(given.get(k), dict) and isinstance(seed.get(k), dict):
                merged[k] = {**seed[k], **given[k]}
        out["regions"][key] = merged
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
                if i == 1 and keys[0] == "regions" and is_part_name(key):
                    # まだ設定の無い部分。人物の設定を写した項目を作る（検証は normalize が行う）
                    node[key] = new_part(out)
                    node = node[key]
                    continue
                raise ValidationError([f"未知のキー: {where}"])
            else:
                node = node[key]
        else:
            raise ValidationError([f"{where}: この下にキーはありません"])
    return out


def _style_key(name: str) -> str:
    text = unicodedata.normalize("NFKC", str(name)).lower()
    return "".join(ch for ch in text if not ch.isspace() and ch != "_")


def resolve_style(name: str, region: str | None = None) -> str | None:
    """スタイル名（キー・表示名・別名）からキーを引く。
    region が人物・部分のときは、その向きの別名（aliasesNonBackground。「白」「白抜き」→ blank）を先に見る"""
    want = _style_key(name)
    if region is not None and region not in ("background", "both"):
        for key, style in load()["styles"].items():
            if any(_style_key(n) == want for n in style.get("aliasesNonBackground", [])):
                return key
    for key, style in load()["styles"].items():
        names = [key, style["label"], *style.get("aliases", [])]
        if any(_style_key(n) == want for n in names):
            return key
    return None


def check_region(region: Any, parts: Any = None, both: bool = True) -> str:
    """スタイルの適用先（person / background / both / 部分名）を確かめる。
    parts を渡したときは、部分名はその中にある物だけを通す"""
    if region in REGIONS or (both and region == "both"):
        return region
    if is_part_name(region) and (parts is None or region in set(parts)):
        return region
    names = " / ".join(["person", "background", *(["both"] if both else []), *sorted(set(parts or ()))])
    raise ValidationError([f"region は {names}{'' if parts is not None else ' / 部分名'} のどれかです: {region}"])


def style_data(name: str, region: str = "person") -> tuple[str, dict]:
    """スタイル名から (キー, 定義) を引く。無ければ ValidationError"""
    key = resolve_style(name, region)
    if key is None:
        known = ", ".join(load()["styles"])
        raise ValidationError([f"スタイルが見つかりません: {name}（{known}）"])
    return key, load()["styles"][key]


def apply_style(params: dict, name: str, region: str = "person", parts: Any = None,
                style: dict | None = None) -> dict:
    """名前付きスタイルを領域（person / background / both / 部分名）に適用した新しい設定を返す。
    部分に当てると、その部分は人物との連動（linked）が切れる。
    parts は適用先にできる部分名（省略時は、既に設定に入っている部分だけ）。
    style を渡すと、名前から引く代わりにその定義（solid / layers / backgroundMode / levels）を使う"""
    check_region(region, part_keys(params) if parts is None else parts)
    if style is None:
        _, style = style_data(name, region)
    out = copy.deepcopy(params)

    taken = layer_ids(out)

    def fill(target: dict) -> None:
        # 層は入れ替わるので、新しい id を振る（前の層の消しゴムのマスクは引き継がない）
        target["solid"] = {**target["solid"], **copy.deepcopy(style["solid"])}
        target["layers"] = _norm_layers(style["layers"], "layer", [], [], True, taken)
        if isinstance(style.get("levels"), dict):          # 自分用スタイルは、明るさ補正を含められる
            target["levels"] = {**target["levels"], **copy.deepcopy(style["levels"])}

    if region in ("person", "both"):
        fill(out["regions"]["person"])
    if region in ("background", "both"):
        bg = out["regions"]["background"]
        if bg["linked"]:
            bg["levels"] = copy.deepcopy(out["regions"]["person"]["levels"])
        bg["linked"] = False
        bg["mode"] = style.get("backgroundMode", "keep")
        fill(bg)
    if region not in (*REGIONS, "both"):
        part = out["regions"].get(region)
        if not isinstance(part, dict):
            part = out["regions"][region] = new_part(out)
        elif part.get("linked", True):
            part["levels"] = copy.deepcopy(out["regions"]["person"]["levels"])
        part["linked"] = False
        fill(part)
    return out


def list_styles() -> list[dict]:
    return [{"name": key, "label": st["label"], "aliases": st.get("aliases", []),
             "aliasesNonBackground": st.get("aliasesNonBackground", []),
             "description": st.get("description", ""), "backgroundMode": st["backgroundMode"],
             "layers": len(st["layers"]), "solid": st["solid"]["enabled"], "origin": "builtin"}
            for key, st in load()["styles"].items()]
