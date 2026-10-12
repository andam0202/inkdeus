"""自分用スタイル（気に入った層の組み合わせを、名前を付けて保存した物）。

保存先は 2 か所:
- 利用者の設定フォルダ <設定フォルダ>/styles.json（全プロジェクト共通。出どころ "user"）
  設定フォルダは環境変数 INKDEUS_CONFIG、無ければ ~/.config/inkdeus
- プロジェクトのフォルダ直下の styles.json（そのプロジェクト専用。出どころ "project"）
同じ名前が両方にあれば、プロジェクト専用が優先。

形式:
    {"version": 1, "styles": [{"name", "aliases": [], "description", "target": "any|person|background",
                               "includeLevels": bool, "data": {"solid", "layers", "levels"?, "mode"?},
                               "createdAt", "updatedAt"}]}
data の中身は、ある領域の solid と layers（層の id は持たない。適用のとき新しく振る）。
includeLevels のときは levels（明るさ補正）も、背景から保存したときは mode（背景の描き方）も持つ。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from . import schema

FORMAT = 1
FILE = "styles.json"
NAME_RE = re.compile(r"[\w\-()（）]{1,32}")            # 英数・かな漢字と、記号 _ - ( )（全角の括弧も可）
TARGETS = ("any", "person", "background")
SCOPES = ("user", "project")
MAX_STYLES = 200
MAX_TEXT = 200
ENTRY_KEYS = {"name", "aliases", "description", "target", "includeLevels", "data", "createdAt", "updatedAt"}
DATA_KEYS = {"solid", "layers", "levels", "mode"}


class StyleError(ValueError):
    """利用者に見せるエラー（problems に理由の一覧）"""

    def __init__(self, problems: list[str] | str, status: int = 400):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        self.status = status
        super().__init__("; ".join(self.problems))


def config_dir(environ: dict | None = None) -> Path:
    """利用者の設定フォルダ。環境変数 INKDEUS_CONFIG → ~/.config/inkdeus"""
    env = os.environ if environ is None else environ
    return Path(str(env.get("INKDEUS_CONFIG") or "~/.config/inkdeus")).expanduser().resolve()


def key_of(name: Any) -> str:
    """名前の照合用の形（全角/半角・大文字小文字・空白・_ の違いを無視。組み込みのスタイル名と同じ規則）"""
    return schema._style_key(name)


def builtin_keys() -> dict[str, str]:
    """組み込みのスタイルの名前・表示名・別名（照合用の形）→ スタイルのキー"""
    out: dict[str, str] = {}
    for key, style in schema.load()["styles"].items():
        for n in (key, style["label"], *style.get("aliases", []), *style.get("aliasesNonBackground", [])):
            out.setdefault(key_of(n), key)
    return out


def check_name(name: Any, what: str = "スタイル名") -> str:
    if not isinstance(name, str) or NAME_RE.fullmatch(name) is None:
        raise StyleError(f"{what}として使えません: {name!r}（英数字・かな漢字・_ - ( ) で 1〜32 文字）")
    hit = builtin_keys().get(key_of(name))
    if hit is not None:
        raise StyleError(f"{what}「{name}」は、組み込みのスタイル「{hit}」の名前・別名と重なるので使えません")
    return name


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def clean_data(data: Any, include_levels: bool) -> dict:
    """保存する中身を検証して整える（層の id は落とす）。使えなければ StyleError"""
    s = schema.load()
    if not isinstance(data, dict):
        raise StyleError("data がオブジェクトではありません")
    problems = [f"未知のキー: data.{k}" for k in data if k not in DATA_KEYS]
    out: dict = {"solid": schema._norm_fields(data.get("solid"), s["solid"], "data.solid", problems)}
    layers = schema._norm_layers(data.get("layers"), "data.layers", problems, [], "layers" in data)
    out["layers"] = [{k: v for k, v in layer.items() if k != "id"} for layer in layers]
    if include_levels:
        out["levels"] = schema._norm_fields(data.get("levels"), s["levels"], "data.levels", problems)
    if "mode" in data:
        out["mode"] = schema._norm_value(data["mode"], s["background"]["mode"], "data.mode", problems, True)
    if problems:
        raise StyleError(problems)
    return out


def clean_entry(raw: Any, stamp: str | None = None) -> dict:
    """1 件を検証して整える。使えなければ StyleError"""
    if not isinstance(raw, dict):
        raise StyleError("スタイルがオブジェクトではありません")
    problems = [f"未知のキー: {k}" for k in raw if k not in ENTRY_KEYS]
    name = check_name(raw.get("name"))
    aliases = raw.get("aliases", [])
    if not isinstance(aliases, list) or len(aliases) > 16:
        raise StyleError("aliases は別名の並び（16 個まで）で指定してください")
    aliases = [check_name(a, "別名") for a in aliases]
    keys = [key_of(n) for n in (name, *aliases)]
    if len(set(keys)) != len(keys):
        raise StyleError("名前と別名が重なっています")
    target = raw.get("target", "any")
    if target not in TARGETS:
        problems.append(f"target は {' / '.join(TARGETS)} のどれかです: {target!r}")
    description = raw.get("description", "")
    if not isinstance(description, str):
        problems.append("description は文字列で指定してください")
        description = ""
    include = raw.get("includeLevels", False)
    if not isinstance(include, bool):
        problems.append("includeLevels は true / false で指定してください")
        include = False
    if problems:
        raise StyleError(problems)
    data = clean_data(raw.get("data"), include)
    stamp = stamp or now()
    created = raw.get("createdAt") if isinstance(raw.get("createdAt"), str) else stamp
    updated = raw.get("updatedAt") if isinstance(raw.get("updatedAt"), str) else stamp
    return {"name": name, "aliases": aliases, "description": description[:MAX_TEXT], "target": target,
            "includeLevels": include, "data": data, "createdAt": created, "updatedAt": updated}


def read_file(path: Path | None) -> tuple[list[dict], list[str]]:
    """styles.json を読む。壊れていても落ちない（読めた物だけ返し、理由を一覧で返す）"""
    if path is None:
        return [], []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], []
    except (OSError, UnicodeDecodeError) as e:
        return [], [f"{path} を読めません: {e}"]
    try:
        raw = json.loads(text)
    except ValueError:
        return [], [f"{path} が JSON として読めません（壊れています）"]
    styles = raw.get("styles") if isinstance(raw, dict) else None
    if not isinstance(styles, list):
        return [], [f"{path} に styles の並びがありません"]
    found: list[dict] = []
    problems: list[str] = []
    seen: set[str] = set()
    for i, item in enumerate(styles[:MAX_STYLES]):
        try:
            entry = clean_entry(item)
        except StyleError as e:
            label = item.get("name") if isinstance(item, dict) else None
            problems.append(f"{path} の styles[{i}]（{label!r}）は使えません: {e}")
            continue
        keys = {key_of(n) for n in (entry["name"], *entry["aliases"])}
        if keys & seen:
            problems.append(f"{path} の styles[{i}]（{entry['name']}）は、名前が先の物と重なるので使いません")
            continue
        seen |= keys
        found.append(entry)
    return found, problems


def definition(entry: dict) -> dict:
    """schema.apply_style に渡す形（組み込みのスタイルの定義と同じ形）"""
    data = entry["data"]
    out = {"solid": data["solid"], "layers": data["layers"], "backgroundMode": data.get("mode", "keep")}
    if entry.get("includeLevels") and isinstance(data.get("levels"), dict):
        out["levels"] = data["levels"]
    return out


def from_region(params: dict, region: str, with_levels: bool) -> tuple[dict, str]:
    """設定の、ある領域（person / background / 部分名）から、保存する中身を作る。戻り値は (data, target)。
    人物と同じ設定で描いている背景・部分は、実際に描かれている設定（人物の物）を保存する"""
    regions = params["regions"]
    if region not in regions:
        raise StyleError(f"その領域の設定がありません: {region}（{' / '.join(regions)}）")
    own = regions[region]
    tone = regions["person"] if own.get("linked") is True else own
    data = {"solid": dict(tone["solid"]), "layers": [{k: v for k, v in layer.items() if k != "id"}
                                                     for layer in tone["layers"]]}
    if with_levels:
        data["levels"] = dict(tone["levels"])
    target = "any"
    if region == "background":
        data["mode"] = own["mode"]
        if own["mode"] != "keep":
            target = "background"                    # 「白」「線だけ」は背景にしか意味が無い
    return data, target


class Store:
    """自分用スタイルの読み書き（利用者共通とプロジェクト専用）。ファイルは毎回読み直す"""

    def __init__(self, user_dir: Path | None = None, project_dir: Path | None = None):
        self.files: dict[str, Path | None] = {
            "user": (Path(user_dir) if user_dir else config_dir()) / FILE,
            "project": Path(project_dir) / FILE if project_dir else None,
        }
        self._lock = threading.Lock()

    def path(self, scope: str) -> Path:
        if scope not in SCOPES:
            raise StyleError(f"scope は {' / '.join(SCOPES)} のどちらかです: {scope!r}")
        path = self.files[scope]
        if path is None:
            raise StyleError("プロジェクト専用のスタイルは、プロジェクトを開いているときだけ使えます")
        return path

    def load(self, scope: str) -> tuple[list[dict], list[str]]:
        return read_file(self.files.get(scope))

    def rows(self) -> tuple[list[dict], list[str]]:
        """一覧（プロジェクト専用 → 利用者共通の順）。同じ名前が両方にあれば、利用者共通の方に shadowed が付く"""
        rows: list[dict] = []
        problems: list[str] = []
        taken: set[str] = set()
        for scope in ("project", "user"):
            entries, bad = self.load(scope)
            problems += bad
            for e in entries:
                key = key_of(e["name"])
                rows.append({**e, "origin": scope, "label": e["name"], "shadowed": key in taken,
                             "layers": len(e["data"]["layers"]), "solid": e["data"]["solid"]["enabled"],
                             "backgroundMode": e["data"].get("mode")})
                if scope == "project":
                    taken.add(key)
        return rows, problems

    def find(self, name: str, scope: str | None = None) -> tuple[dict, str] | None:
        """名前（別名でも可）から (スタイル, 出どころ) を引く。プロジェクト専用が優先"""
        want = key_of(name)
        for s in ((scope,) if scope else ("project", "user")):
            for e in self.load(s)[0]:
                if want in (key_of(n) for n in (e["name"], *e["aliases"])):
                    return e, s
        return None

    def _write(self, scope: str, entries: list[dict]) -> None:
        path = self.path(scope)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # 壊れたファイルを上書きする前に、写しを残す
                os.replace(path, path.with_name(path.name + ".broken"))
        data = {"version": FORMAT, "styles": entries}
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, path)

    def save(self, raw: dict, scope: str = "user") -> tuple[dict, bool]:
        """保存する（同じ名前があれば上書き）。戻り値は (保存した物, 上書きしたか)"""
        self.path(scope)
        entry = clean_entry({k: v for k, v in raw.items() if k not in ("createdAt", "updatedAt")})
        keys = {key_of(n) for n in (entry["name"], *entry["aliases"])}
        with self._lock:
            entries, _ = self.load(scope)
            if len(entries) >= MAX_STYLES:
                raise StyleError(f"スタイルが多すぎます（{MAX_STYLES} 個まで）")
            at = next((i for i, e in enumerate(entries) if key_of(e["name"]) == key_of(entry["name"])), None)
            for i, e in enumerate(entries):
                if i != at and keys & {key_of(n) for n in (e["name"], *e["aliases"])}:
                    raise StyleError(f"名前・別名が、既にあるスタイル「{e['name']}」と重なっています")
            if at is None:
                entries.append(entry)
            else:
                entry["createdAt"] = entries[at]["createdAt"]
                entries[at] = entry
            self._write(scope, entries)
        return entry, at is not None

    def _pick(self, name: str, scope: str | None) -> tuple[dict, str]:
        want = key_of(name)
        for s in ((scope,) if scope else ("project", "user")):
            if s not in SCOPES:
                raise StyleError(f"scope は {' / '.join(SCOPES)} のどちらかです: {s!r}")
            for e in self.load(s)[0]:
                if key_of(e["name"]) == want:
                    return e, s
        raise StyleError(f"その名前の自分用スタイルはありません: {name}", status=404)

    def delete(self, name: str, scope: str | None = None) -> tuple[dict, str]:
        with self._lock:
            entry, found = self._pick(name, scope)
            self._write(found, [e for e in self.load(found)[0] if e["name"] != entry["name"]])
        return entry, found

    def rename(self, name: str, to: str, scope: str | None = None) -> tuple[dict, str]:
        check_name(to)
        with self._lock:
            entry, found = self._pick(name, scope)
            entries = self.load(found)[0]
            for e in entries:
                if e["name"] != entry["name"] and key_of(to) in (key_of(n) for n in (e["name"], *e["aliases"])):
                    raise StyleError(f"名前が、既にあるスタイル「{e['name']}」と重なっています")
            for e in entries:
                if e["name"] == entry["name"]:
                    e["name"] = to
                    e["aliases"] = [a for a in e["aliases"] if key_of(a) != key_of(to)]
                    e["updatedAt"] = now()
                    entry = e
            self._write(found, entries)
        return entry, found
