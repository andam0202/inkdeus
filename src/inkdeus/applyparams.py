"""設定の一括適用: ある画像の設定を、選んだ複数の画像へまとめて写す。

ここにあるのは、サーバにもコマンドにも依存しない部分だけ:
  - 適用する範囲（scope）の読み取り
  - 設定の合成（範囲に入っているキーだけを、適用元の値に置き換える）
  - 適用先の指定（画像名 / all / unconfigured / glob）の展開
  - 1 回の適用を 1 つの単位として残す記録（<out>/.backup/_apply/apply-<時刻>.json）と、その取り消し

消しゴム・ペン・部分マスクの手直しは画像固有なので、写さない。
"""

from __future__ import annotations

import copy
import fnmatch
import json
import shutil
import time
from pathlib import Path

from . import backup as backup_mod

# 範囲の名前。parts は「部分ごとのトーン」（parts:<部分名> で部分を選ぶ。parts だけなら、適用元にある部分の全部）
PLAIN_SCOPES = ("input", "lines", "watermark", "output")
REGION_SCOPES = ("person", "background")
SCOPE_NAMES = (*PLAIN_SCOPES[:2], *REGION_SCOPES, "parts", *PLAIN_SCOPES[2:], "unit")
# 既定の範囲: unit（画像倍率）以外の全部。unit は画像の大きさで自動なので、頼まれたときだけ写す
DEFAULT_SCOPE = tuple(n for n in SCOPE_NAMES if n != "unit")
SCOPE_LABELS = {"input": "入力", "lines": "線画", "person": "人物のトーン", "background": "背景のトーン",
                "parts": "部分ごとのトーン", "watermark": "透かし", "output": "出力", "unit": "画像倍率"}
SELECTORS = ("all", "unconfigured")
JOURNAL_DIR = "_apply"
JOURNAL_PREFIX = "apply-"
JOURNAL_KEEP = 50
PAINT_SUFFIX = ".paint"
PARAMS_SUFFIX = ".inkdeus.json"


class ApplyError(Exception):
    """利用者に見せるエラー（不正な入力など）"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Scope:
    """適用する範囲。keys = 丸ごと写すキー、parts = None（部分は写さない）/ "*"（適用元にある部分の全部）/ 部分名の集合"""

    def __init__(self, keys: set[str], parts):
        self.keys = keys
        self.parts = parts

    def names(self) -> list[str]:
        out = []
        for n in SCOPE_NAMES:
            if n != "parts":
                out += [n] if n in self.keys else []
            elif self.parts == "*":
                out.append("parts")
            elif self.parts:
                out += [f"parts:{p}" for p in sorted(self.parts)]
        return out

    def part_names(self, available: list[str]) -> list[str]:
        """写す部分（適用元の画像にある部分のうち、範囲に入っている物）"""
        if self.parts is None:
            return []
        return list(available) if self.parts == "*" else [p for p in available if p in self.parts]


def parse_scope(raw) -> Scope:
    """範囲の指定（"input,lines,parts:hair" のような文字列か、その並び）を読む。None・空は既定（unit 以外の全部）"""
    if raw is None or raw == "" or raw == []:
        items = list(DEFAULT_SCOPE)
    elif isinstance(raw, str):
        items = [s.strip() for s in raw.split(",") if s.strip()]
    elif isinstance(raw, (list, tuple)) and all(isinstance(s, str) for s in raw):
        items = [s.strip() for s in raw if s.strip()]
    else:
        raise ApplyError("scope は、範囲の名前の並びで指定してください")
    keys: set[str] = set()
    parts = None
    for item in items:
        if item == "all":
            keys.update(n for n in DEFAULT_SCOPE if n != "parts")
            parts = "*"
        elif item == "parts":
            parts = "*"
        elif item.startswith("parts:"):
            name = item[len("parts:"):]
            if not name:
                raise ApplyError("parts: のあとに部分名を書いてください（例: parts:hair）")
            if parts != "*":
                parts = {*(parts or set()), name}
        elif item in SCOPE_NAMES:
            keys.add(item)
        else:
            raise ApplyError(f"範囲の名前が違います: {item}（{', '.join(SCOPE_NAMES)}, parts:<部分名>, all）")
    if not keys and parts is None:
        raise ApplyError("適用する範囲が空です")
    return Scope(keys, parts)


def parse_to_version(raw, source_version: str) -> tuple[str, str]:
    """適用先のバージョンの指定 → (種類, バージョン名)。
    default = 既定のバージョン / same = 適用元と同じ名前 / new:<名前> = その名前のバージョン（無ければ作る）"""
    text = "default" if raw in (None, "") else raw
    if not isinstance(text, str):
        raise ApplyError("toVersion は default / same / new:<名前> のどれかです")
    if text == "default":
        return "default", ""
    if text == "same":
        return "same", source_version
    if text.startswith("new:") and text[4:]:
        return "new", text[4:]
    raise ApplyError("toVersion は default / same / new:<名前> のどれかです")


def expand_targets(tokens, names: list[str], unconfigured: set[str]) -> list[str]:
    """適用先の指定を、画像名の並び（一覧の順・重複なし）にする。
    それぞれ: all = 全部 / unconfigured = 設定のまだ無い画像 / 実在する画像名 / glob（* ? [ を含む物）"""
    if isinstance(tokens, str):
        tokens = [tokens]
    if not isinstance(tokens, (list, tuple)) or not tokens or not all(isinstance(t, str) and t for t in tokens):
        raise ApplyError("適用先（to）を、画像名・all・unconfigured・glob の並びで指定してください")
    known = set(names)
    picked: set[str] = set()
    for token in tokens:
        if token in known:
            picked.add(token)
        elif token == "all":
            picked.update(names)
        elif token == "unconfigured":
            picked.update(n for n in names if n in unconfigured)
        elif any(ch in token for ch in "*?["):
            found = [n for n in names if fnmatch.fnmatchcase(n, token)]
            if not found:
                raise ApplyError(f"当てはまる画像がありません: {token}", 404)
            picked.update(found)
        else:
            raise ApplyError(f"その名前の画像はありません: {token}", 404)
    return [n for n in names if n in picked]


def merge(source: dict, target: dict, scope: Scope, source_parts: list[str], target_parts: set[str]) -> tuple[dict, list[str]]:
    """target（適用先のいまの設定）のうち、範囲に入っているキーだけを source（適用元）の値にした物を返す。
    どちらも整えた設定（schema.normalize 済み）。戻り値の 2 つ目は、適用先にマスクが無くて飛ばした部分名"""
    out = copy.deepcopy(target)
    for key in PLAIN_SCOPES:
        if key in scope.keys and key in source:
            out[key] = copy.deepcopy(source[key])
    if "unit" in scope.keys:
        out["unit"] = source.get("unit")
    src_regions = source.get("regions") if isinstance(source.get("regions"), dict) else {}
    regions = out.setdefault("regions", {})
    for key in REGION_SCOPES:
        if key in scope.keys and isinstance(src_regions.get(key), dict):
            regions[key] = copy.deepcopy(src_regions[key])
    skipped = []
    for part in scope.part_names(source_parts):
        if part not in target_parts:
            skipped.append(part)                         # 適用先の画像に、その部分のマスクが無い
            continue
        src = src_regions.get(part)
        if not isinstance(src, dict) or src.get("linked", True):
            # 適用元では、その部分は人物と同じ設定で描かれている。適用先も人物に連動させる（持っている設定は残す）
            if isinstance(regions.get(part), dict):
                regions[part]["linked"] = True
        else:
            regions[part] = copy.deepcopy(src)
    return out, skipped


# ---- 1 回の適用の記録 ----

def journal_dir(store: backup_mod.Store) -> Path:
    return store.root / JOURNAL_DIR


def new_id(store: backup_mod.Store, began: float) -> str:
    base = backup_mod.stamp_name(began)
    name, n = base, 1
    while (journal_dir(store) / f"{JOURNAL_PREFIX}{name}.json").exists():
        name = f"{base}-{n:04d}"
        n += 1
    return name


def write_journal(store: backup_mod.Store, record: dict) -> Path:
    path = journal_dir(store) / f"{JOURNAL_PREFIX}{record['id']}.json"
    backup_mod.atomic_write(path, (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    rows = sorted(journal_dir(store).glob(f"{JOURNAL_PREFIX}*.json"), key=lambda p: p.name, reverse=True)
    for old in rows[JOURNAL_KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass
    return path


def journals(store: backup_mod.Store) -> list[dict]:
    """適用の記録（新しい順）。読めない物は飛ばす"""
    out = []
    try:
        files = sorted(journal_dir(store).glob(f"{JOURNAL_PREFIX}*.json"), key=lambda p: p.name, reverse=True)
    except OSError:
        return out
    for path in files:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and isinstance(record.get("items"), list) and "id" in record:
            out.append(record)
    return out


def summary(record: dict) -> dict:
    """記録の要約（一覧に出す分）"""
    return {"id": record["id"], "time": record.get("time"),
            "label": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.get("time") or 0)),
            "from": record.get("from"), "scope": record.get("scope"), "toVersion": record.get("toVersion"),
            "count": len(record["items"]), "undone": record.get("undone")}


def pick_journal(store: backup_mod.Store, ident: str | int | None = None) -> dict:
    """取り消す適用を選ぶ。ident = None は「まだ取り消していない、いちばん新しい物」。番号（1 が最新）か id でも選べる"""
    rows = journals(store)
    if ident in (None, ""):
        live = [r for r in rows if not r.get("undone")]
        if not live:
            raise ApplyError("取り消せる適用がありません", 404)
        return live[0]
    text = str(ident)
    if text.isdigit() and len(text) <= 4:
        if not 1 <= int(text) <= len(rows):
            raise ApplyError(f"番号は 1〜{len(rows)} です（1 がいちばん新しい）: {text}" if rows else "適用の記録がありません", 404)
        found = rows[int(text) - 1]
    else:
        found = next((r for r in rows if r["id"] == text), None)
        if found is None:
            raise ApplyError(f"その適用の記録はありません: {text}", 404)
    if found.get("undone"):
        raise ApplyError("その適用は、もう取り消してあります", 409)
    return found


def undo(store: backup_mod.Store, base_out: Path, record: dict, reindex=None) -> list[dict]:
    """1 回の適用を、まとめて取り消す。各画像の設定（と、適用で捨てた層の消しゴム）を、適用の直前の状態へ戻す。
    戻す前のいまの状態も退避する。適用が作ったバージョンは、バージョンごと消す。
    reindex(paint_dir) は、消しゴム・ペンの目録を書き直す関数。戻り値は、画像ごとの結果"""
    began = float(record["time"])
    now = backup_mod.parse_stamp(backup_mod.stamp_name(time.time()))
    base_out = Path(base_out)
    results = []
    for item in record["items"]:
        key = item["key"]
        params = base_out / key
        paint_dir = params.with_name(params.name[: -len(PARAMS_SUFFIX)] + PAINT_SUFFIX)
        row = {"folder": item.get("folder"), "name": item.get("name"), "version": item.get("version", ""),
               "created": bool(item.get("created")), "restored": [], "paint": False}
        if item.get("created"):
            # 適用が作ったバージョン: 設定・消しゴムとペン・書き出した PNG を消す（消す前に退避する）
            store.stash(params, key, now=now)
            if paint_dir.is_dir():
                prefix = key[: -len(PARAMS_SUFFIX)] + PAINT_SUFFIX
                for f in sorted(paint_dir.glob("*.png")):
                    store.stash(f, f"{prefix}/{f.name}", now=now)
                shutil.rmtree(paint_dir, ignore_errors=True)
                row["paint"] = True
            removed = params.is_file()
            params.unlink(missing_ok=True)
            params.with_name(params.name[: -len(PARAMS_SUFFIX)] + ".png").unlink(missing_ok=True)
            row["restored"] = ["バージョン"] if removed else []
        else:
            targets = {key: ("設定", params)}
            for k in item.get("paintKeys") or []:
                targets[k] = ("消しゴム", base_out / k)
            row["restored"] = backup_mod.restore_at(store, targets, began, now=now)
            if "消しゴム" in row["restored"]:
                row["paint"] = True
                if reindex is not None:
                    reindex(paint_dir)
        results.append(row)
    record["undone"] = time.time()
    path = journal_dir(store) / f"{JOURNAL_PREFIX}{record['id']}.json"
    backup_mod.atomic_write(path, (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return results
