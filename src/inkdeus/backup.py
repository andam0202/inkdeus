"""世代バックアップと、壊れにくい書き込み。

上書き保存の直前に、いまのファイルを <置き場>/<キー>/<時刻><拡張子> へ退避する。
置き場はプロジェクト単位の <out>/.backup/（自分用スタイルの共通分だけは <設定フォルダ>/.backup/）。
キーは保存先の構成をそのまま写した相対パス:

    [@<フォルダ>/]<名前>[@<バージョン>].inkdeus.json     画像ごとの設定
    [@<フォルダ>/]<名前>[@<バージョン>].paint/<対象>.png  消しゴム・ペン
    [@<フォルダ>/]<名前>.maskedit/<対象>.png              部分マスクの手直し
    _manga/<保存名>.lz4                                    漫画データ（autosave を含む）
    _project/inkdeus.project.toml, _project/styles.json    プロジェクトの設定・このプロジェクトのスタイル

1 つの退避は「そのとき上書きされる直前の中身」。まだファイルが無かったとき（新しく作られるとき）は、
中身の無い印（<時刻>.absent）を残す（そこへ戻すと、ファイルが消える）。

間引き: 直近 keep_recent 個は全部残し、それより古い物は「1 時間に 1 個を 24 時間ぶん、1 日に 1 個を 30 日ぶん」
だけ残す。1 ファイルあたりの個数と、置き場全体の大きさ（既定 2GB。超えたら古い物から消す）にも上限がある。
"""

from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

DIR = ".backup"
ABSENT = ".absent"
BROKEN_MARK = ".broken-"
STAMP_RE = re.compile(r"(\d{8})-(\d{6})-(\d{3})(?:-(\d+))?")
KINDS = ("params", "paint", "maskedit", "manga", "project", "styles")
PROJECT_PREFIX = "_project"
MANGA_PREFIX = "_manga"


class BackupError(Exception):
    """利用者に見せるエラー"""


@dataclass(frozen=True)
class Config:
    enabled: bool = True
    keep_recent: int = 10            # 直近のこの個数は、間引かずに全部残す
    hourly_hours: int = 24           # それより古い物は、この時間ぶん「1 時間に 1 個」
    daily_days: int = 30             # さらに古い物は、この日数ぶん「1 日に 1 個」。それより古い物は消す
    max_per_file: int = 200          # 1 ファイルあたりの個数の上限
    max_total_mb: int = 2048         # 置き場全体の上限（超えたら古い物から消す）


def read_config(local: dict | None) -> Config:
    """inkdeus.local.toml の [backup]。使えない値は既定のまま"""
    raw = (local or {}).get("backup")
    cfg = Config()
    if not isinstance(raw, dict):
        return cfg
    if isinstance(raw.get("enabled"), bool):
        cfg = replace(cfg, enabled=raw["enabled"])
    for key, lo, hi in (("keep_recent", 1, 1000), ("hourly_hours", 0, 24 * 365), ("daily_days", 0, 3650),
                        ("max_per_file", 1, 100000), ("max_total_mb", 1, 1 << 20)):
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi:
            cfg = replace(cfg, **{key: value})
    return cfg


# ---- 壊れにくい書き込み ----

def atomic_write(path: Path, data: bytes) -> None:
    """一時ファイルへ書いてから改名で置き換える。途中で失敗・中断しても、元のファイルはそのまま残る。
    一時ファイルの名前は毎回違う（同じ保存先への同時の書き込みが、互いの書きかけを踏まない）"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass                                  # 同期できない置き場でも、書き込み自体は続ける
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def atomic_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f"{dst.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def stamp_name(t: float) -> str:
    """時刻 → 退避の名前（ローカル時刻。YYYYmmdd-HHMMSS-ミリ秒）"""
    d = dt.datetime.fromtimestamp(t)
    return f"{d:%Y%m%d-%H%M%S}-{d.microsecond // 1000:03d}"


def parse_stamp(name: str) -> float | None:
    m = STAMP_RE.match(name)
    if m is None:
        return None
    try:
        d = dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return d.timestamp() + int(m.group(3)) / 1000.0 + (int(m.group(4)) / 1e6 if m.group(4) else 0.0)


def _order(name: str) -> tuple[str, int]:
    """退避の名前 → 並び順（時刻, 同じ時刻の中の通し番号）"""
    m = STAMP_RE.match(name)
    return (f"{m.group(1)}-{m.group(2)}-{m.group(3)}", int(m.group(4) or 0)) if m else ("", 0)


def move_broken(path: Path, now: float | None = None) -> Path | None:
    """壊れたファイルを <名前>.broken-<時刻> として残す（上書きしない）。動かせたら、その場所"""
    path = Path(path)
    if not path.exists():
        return None
    base = path.with_name(f"{path.name}{BROKEN_MARK}{stamp_name(time.time() if now is None else now)}")
    target, n = base, 1
    while target.exists():
        target = base.with_name(f"{base.name}-{n}")
        n += 1
    os.replace(path, target)
    return target


@dataclass(frozen=True)
class Entry:
    key: str
    name: str            # ファイル名（<時刻><拡張子>）
    time: float          # 退避した時刻
    path: Path
    bytes: int
    absent: bool         # 「このとき、ファイルはまだ無かった」の印

    @property
    def stamp(self) -> str:
        return STAMP_RE.match(self.name).group(0)

    def info(self) -> dict:
        return {"key": self.key, "at": self.stamp, "time": self.time, "bytes": self.bytes, "absent": self.absent,
                "label": dt.datetime.fromtimestamp(self.time).strftime("%Y-%m-%d %H:%M:%S")}


def _same(a: Path, b: Path) -> bool:
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                x, y = fa.read(1 << 20), fb.read(1 << 20)
                if x != y:
                    return False
                if not x:
                    return True
    except OSError:
        return False


class Store:
    """1 つの置き場（<out>/.backup など）"""

    def __init__(self, root: Path | str, config: Config | None = None):
        self.root = Path(root)
        self.config = config or Config()
        self._lock = threading.RLock()
        self._total: int | None = None

    # ---- 読む ----

    def entries(self, key: str) -> list[Entry]:
        """そのキーの退避（新しい順）"""
        folder = self.root / key
        rows = []
        try:
            with os.scandir(folder) as it:
                for item in it:
                    t = parse_stamp(item.name)
                    if t is None or item.name.endswith(".tmp") or not item.is_file():
                        continue
                    rows.append(Entry(key, item.name, t, Path(item.path), item.stat().st_size,
                                      item.name.endswith(ABSENT)))
        except OSError:
            return []
        return sorted(rows, key=lambda e: _order(e.name), reverse=True)

    def keys(self, prefix: str = "") -> list[str]:
        """退避のあるキーの一覧（prefix で始まる物）"""
        out = []
        base = self.root
        if not base.is_dir():
            return out
        for cur, dirs, files in os.walk(base):
            dirs.sort()
            if any(parse_stamp(f) is not None and not f.endswith(".tmp") for f in files):
                key = Path(cur).relative_to(base).as_posix()
                if key.startswith(prefix):
                    out.append(key)
        return sorted(out)

    def total_bytes(self) -> int:
        with self._lock:
            if self._total is None:
                total = 0
                for cur, _dirs, files in os.walk(self.root):
                    for f in files:
                        try:
                            total += os.stat(os.path.join(cur, f)).st_size
                        except OSError:
                            pass
                self._total = total
            return self._total

    # ---- 退避 ----

    def _new_path(self, key: str, suffix: str, now: float) -> Path:
        folder = self.root / key
        folder.mkdir(parents=True, exist_ok=True)
        base, n = stamp_name(now), 0
        # 同じミリ秒に重ねて退避しても（時計が戻っても）、並び順 = 退避した順になるよう、通し番号を足す
        latest = max((_order(e.name) for e in self.entries(key)), default=None)
        if latest is not None and latest[0] >= base:
            base, n = latest[0], latest[1] + 1
        while True:
            path = folder / f"{base}{f'-{n:04d}' if n else ''}{suffix}"
            if not path.exists():
                return path
            n += 1

    def stash(self, path: Path, key: str, new_data: bytes | None = None, force: bool = False,
              now: float | None = None) -> Entry | None:
        """上書き（削除）の直前に呼ぶ。いまのファイルを退避する。退避しなかったら None。
        new_data: これから書く中身（いまの中身と同じなら、何も変わらないので退避しない）
        force: 直前の退避と同じ中身でも退避する（復元の前の退避。取り消せるようにするため）"""
        if not self.config.enabled:
            return None
        path = Path(path)
        now = time.time() if now is None else now
        with self._lock:
            latest = next(iter(self.entries(key)), None)
            exists = path.is_file()
            if exists and new_data is not None:
                try:
                    if path.stat().st_size == len(new_data) and path.read_bytes() == new_data:
                        return None                      # 同じ中身を書き直すだけ
                except OSError:
                    pass
            if not exists:
                if latest is None and not force and new_data is None:
                    return None                          # 無い物を消すだけ
                if latest is not None and latest.absent and not force:
                    return None
                target = self._new_path(key, ABSENT, now)
                target.touch()
            else:
                if not force and latest is not None and not latest.absent and _same(latest.path, path):
                    return None                          # 直前の退避と同じ中身
                target = self._new_path(key, path.suffix or ".bin", now)
                atomic_copy(path, target)
            size = target.stat().st_size
            if self._total is not None:
                self._total += size
            self.prune_key(key, now)
            if self.total_bytes() > self.config.max_total_mb * (1 << 20):
                self.enforce_total()
            return Entry(key, target.name, parse_stamp(target.name) or now, target, size, target.name.endswith(ABSENT))

    # ---- 間引き ----

    def _remove(self, entry: Entry) -> int:
        try:
            entry.path.unlink()
        except OSError:
            return 0
        if self._total is not None:
            self._total = max(0, self._total - entry.bytes)
        return 1

    def prune_key(self, key: str, now: float | None = None) -> int:
        """そのキーの退避を間引く。消した個数を返す"""
        now = time.time() if now is None else now
        cfg = self.config
        with self._lock:
            rows = self.entries(key)
            keep = rows[:cfg.keep_recent]
            seen: set[tuple] = set()
            for e in rows[cfg.keep_recent:]:
                age = now - e.time
                if age <= cfg.hourly_hours * 3600:
                    bucket = ("h", int(e.time // 3600))
                elif age <= cfg.daily_days * 86400:
                    bucket = ("d", dt.datetime.fromtimestamp(e.time).date().toordinal())
                else:
                    continue
                if bucket not in seen:                   # 新しい順に見ているので、その区切りでいちばん新しい物が残る
                    seen.add(bucket)
                    keep.append(e)
            keep = keep[:cfg.max_per_file]
            kept = {e.name for e in keep}
            removed = sum(self._remove(e) for e in rows if e.name not in kept)
            if removed and not kept:
                try:
                    (self.root / key).rmdir()
                except OSError:
                    pass
            return removed

    def enforce_total(self) -> int:
        """置き場全体が上限を超えていたら、古い物から消す"""
        limit = self.config.max_total_mb * (1 << 20)
        with self._lock:
            self._total = None
            total = self.total_bytes()
            if total <= limit:
                return 0
            rows = sorted((e for key in self.keys() for e in self.entries(key)), key=lambda e: e.time)
            removed = 0
            for e in rows:
                if total <= limit:
                    break
                if self._remove(e):
                    total -= e.bytes
                    removed += 1
            self._total = None
            return removed

    def prune(self, now: float | None = None) -> dict:
        """全部のキーを間引き、全体の上限も確かめる"""
        with self._lock:
            before = None
            self._total = None
            before = self.total_bytes()
            removed = sum(self.prune_key(key, now) for key in self.keys())
            removed += self.enforce_total()
            self._total = None
            after = self.total_bytes()
            for cur, dirs, files in os.walk(self.root, topdown=False):     # 空になったフォルダを片付ける
                if not dirs and not files and Path(cur) != self.root:
                    try:
                        os.rmdir(cur)
                    except OSError:
                        pass
            return {"removed": removed, "bytesBefore": before, "bytesAfter": after}

    # ---- 復元 ----

    def pick(self, key: str, at: str | float) -> Entry:
        """--at の指定（番号 = 新しい順の 1 始まり / 時刻 / 退避の名前）から、戻す退避を選ぶ"""
        rows = self.entries(key)
        if not rows:
            raise BackupError(f"バックアップがありません: {key}")
        return pick_from(rows, at, key)

    def state_at(self, key: str, t: float) -> Entry | None:
        """時刻 t の直前の変更が行われる前の中身 = t 以降でいちばん古い退避（無ければ None = いまの中身のまま）"""
        later = [e for e in self.entries(key) if e.time >= t - 1e-6]
        return later[-1] if later else None

    def restore(self, entry: Entry, path: Path, now: float | None = None) -> bool:
        """退避の中身を path へ戻す。戻す前に、いまの物も退避する。何も変わらなければ False"""
        path = Path(path)
        with self._lock:
            if entry.absent:
                if not path.exists():
                    return False
                self.stash(path, entry.key, force=True, now=now)
                path.unlink()
                return True
            if path.is_file() and _same(entry.path, path):
                return False
            self.stash(path, entry.key, new_data=b"" if not path.exists() else None, force=True, now=now)
            atomic_copy(entry.path, path)
            return True


def parse_at(at: str | float | int) -> tuple[str, float | int]:
    """("index", n) か ("time", 秒) を返す"""
    if isinstance(at, (int, float)) and not isinstance(at, bool):
        return ("time", float(at))
    text = str(at).strip()
    if re.fullmatch(r"\d{1,6}", text):
        return ("index", int(text))
    t = parse_stamp(text) if STAMP_RE.fullmatch(text) else None
    if t is None:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y%m%d-%H%M%S", "%Y%m%d-%H%M", "%Y-%m-%d"):
            try:
                t = dt.datetime.strptime(text, fmt).timestamp()
                break
            except ValueError:
                continue
    if t is None:
        raise BackupError(f"--at の指定を読めません: {at!r}（番号、または 2026-01-31T12:34 / 20260131-123400-000 の形の時刻）")
    return ("time", t)


def pick_from(rows: list, at: str | float, what: str = ""):
    """新しい順の並び（.time を持つ物）から、--at の指定に当たる 1 つを選ぶ。
    時刻の指定は「その時刻の状態」= その時刻以降でいちばん古い退避"""
    kind, value = parse_at(at)
    if kind == "index":
        if not 1 <= value <= len(rows):
            raise BackupError(f"番号は 1〜{len(rows)} です（1 がいちばん新しい）: {value}")
        return rows[value - 1]
    later = [r for r in rows if r.time >= value - 1e-6]
    if not later:
        raise BackupError(f"その時刻より後の変更はありません（いまの状態が、その時刻の状態です）: {at}"
                          + (f" / {what}" if what else ""))
    return later[-1]


# ---- いくつかのファイルをまとめた「戻せる時点」 ----

def points(store: Store, labels: dict[str, str], merge_seconds: float = 2.0) -> list[dict]:
    """いくつかのキー（キー → 種類の表示名）をまとめた、戻せる時点の一覧（新しい順）。
    近い時刻の変更は 1 つの時点にまとめる。ある時点へ戻す = その時点の変更が行われる直前の状態にする"""
    rows = []
    for key, label in labels.items():
        rows += [(e, label) for e in store.entries(key)]
    rows.sort(key=lambda r: r[0].time, reverse=True)
    found: list[dict] = []
    for e, label in rows:
        # まとめるのは「別のファイルの、近い時刻の変更」だけ（同じファイルの続けての変更は、それぞれ別の時点）
        if found and found[-1]["time"] - e.time < merge_seconds and e.key not in found[-1]["keys"]:
            found[-1]["time"] = e.time
            found[-1]["at"] = e.stamp
            found[-1]["keys"].add(e.key)
            if label not in found[-1]["what"]:
                found[-1]["what"].append(label)
            found[-1]["bytes"] += e.bytes
        else:
            found.append({"at": e.stamp, "time": e.time, "what": [label], "bytes": e.bytes, "keys": {e.key}})
    for i, point in enumerate(found, 1):
        del point["keys"]
        point["number"] = i
        point["label"] = dt.datetime.fromtimestamp(point["time"]).strftime("%Y-%m-%d %H:%M:%S")
    return found


def pick_point(found: list[dict], at: str | float) -> float:
    """--at の指定から、戻す時点の時刻を決める"""
    if not found:
        raise BackupError("バックアップがありません")
    import types

    return pick_from([types.SimpleNamespace(**p) for p in found], at).time


def restore_at(store: Store, targets: dict[str, tuple[str, Path]], t: float, now: float | None = None) -> list[str]:
    """キー → (種類の表示名, 戻す先) の全部を、時刻 t の変更の直前の状態へ戻す。
    戻す前のいまの状態も（now の時刻で）退避する。戻り値は、中身が変わった物の表示名"""
    now = time.time() if now is None else now
    changed = []
    for key, (label, path) in targets.items():
        found = store.state_at(key, t)
        if found is not None and store.restore(found, path, now=now):
            changed.append(label)
    return changed
