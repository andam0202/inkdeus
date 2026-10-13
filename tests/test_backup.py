"""世代バックアップ（退避・間引き・上限・復元）と、壊れにくい書き込み。サーバ・CLI を通した確認は test_backup_api.py"""

from __future__ import annotations

import datetime as dt
import os

import pytest

from inkdeus import backup
from inkdeus.backup import BackupError, Config, Store

T0 = dt.datetime(2026, 3, 10, 12, 0, 0).timestamp()
HOUR, DAY = 3600.0, 86400.0


def save(store: Store, path, key: str, text: str, now: float):
    """「上書きの直前に退避してから書く」を 1 回行う"""
    data = text.encode("utf-8")
    entry = store.stash(path, key, new_data=data, now=now)
    backup.atomic_write(path, data)
    return entry


def texts(store: Store, key: str) -> list[str]:
    return ["<absent>" if e.absent else e.path.read_text(encoding="utf-8") for e in store.entries(key)]


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "out" / backup.DIR)


def test_stash_keeps_the_previous_content_and_skips_identical_ones(store, tmp_path):
    path = tmp_path / "out" / "a.inkdeus.json"
    key = "a.inkdeus.json"
    first = save(store, path, key, "v1", T0)                     # まだ無かった → 「無かった」の印
    assert first.absent and texts(store, key) == ["<absent>"]
    assert save(store, path, key, "v1", T0 + 1) is None           # 同じ中身を書き直すだけ → 退避しない
    assert save(store, path, key, "v2", T0 + 2).path.read_text(encoding="utf-8") == "v1"
    assert save(store, path, key, "v3", T0 + 3).path.read_text(encoding="utf-8") == "v2"
    assert texts(store, key) == ["v2", "v1", "<absent>"]          # 新しい順
    names = [e.name for e in store.entries(key)]
    assert names[0].endswith(".json") and names[-1].endswith(".absent")
    assert [e.stamp for e in store.entries(key)][0] == backup.stamp_name(T0 + 3)
    # 直前の退避と同じ中身なら、中身を指定しない退避（削除の前など）でも重ねない
    path.write_text("v2", encoding="utf-8")
    assert store.stash(path, key, now=T0 + 4) is None
    assert store.stash(path, key, force=True, now=T0 + 5) is not None      # 復元の前の退避は必ず残す
    # 同じミリ秒に重ねても、名前の順 = 退避した順
    save(store, path, key, "v4", T0 + 5)
    save(store, path, key, "v5", T0 + 5)
    assert texts(store, key)[:2] == ["v4", "v2"]
    # 切ってあれば何もしない
    off = Store(store.root, Config(enabled=False))
    assert off.stash(path, key, new_data=b"zzz") is None and len(off.entries(key)) == len(store.entries(key))


def test_thinning_keeps_recent_then_hourly_then_daily(tmp_path):
    store = Store(tmp_path / backup.DIR, Config(keep_recent=3, hourly_hours=24, daily_days=30))
    path, key = tmp_path / "f.json", "f.json"
    path.write_text("0", encoding="utf-8")
    now = T0 + 40 * DAY
    # 40 日前から 10 分おきに 1 個ずつ（最後は「いま」の 10 分前）。間引きは最後にまとめて行う
    loose = Store(store.root, Config(keep_recent=10 ** 6, max_per_file=10 ** 6))
    n, t = 0, T0
    while t < now:
        n += 1
        save(loose, path, key, str(n), t)
        t += 600 if t > now - 1.5 * DAY else 12 * HOUR            # 古い所は 12 時間おき（数を抑える）
    before = len(store.entries(key))
    removed = store.prune_key(key, now)
    rows = store.entries(key)
    assert removed == before - len(rows) > 0
    ages = [now - e.time for e in rows]
    assert ages == sorted(ages)
    assert ages[:3] == pytest.approx([600, 1200, 1800], abs=1)    # 直近 3 個は全部残る
    hourly = [a for a in ages[3:] if a <= 24 * HOUR]
    daily = [a for a in ages[3:] if 24 * HOUR < a <= 30 * DAY]
    assert 22 <= len(hourly) <= 25                                # 1 時間に 1 個を 24 時間ぶん
    assert all(b - a >= 0.5 * HOUR for a, b in zip(hourly, hourly[1:]))
    assert 28 <= len(daily) <= 30                                 # 1 日に 1 個を 30 日ぶん
    assert all(b - a >= 0.5 * DAY for a, b in zip(daily, daily[1:]))
    assert max(ages) <= 30 * DAY                                  # それより古い物は消える
    assert store.prune_key(key, now) == 0                         # もう一度やっても変わらない


def test_limits_per_file_and_in_total(tmp_path):
    store = Store(tmp_path / backup.DIR, Config(keep_recent=50, max_per_file=5))
    path = tmp_path / "f.bin"
    for i in range(12):
        save(store, path, "f.bin", f"content-{i}", T0 + i)
    assert len(store.entries("f.bin")) == 5 and texts(store, "f.bin")[0] == "content-10"
    # 全体の上限: 超えたら、全部のキーを通して古い物から消す
    big = Store(tmp_path / "big", Config(keep_recent=50, max_total_mb=1))
    for i in range(6):
        for name in ("a.bin", "b.bin"):
            p = tmp_path / name
            data = os.urandom(200_000)
            big.stash(p, name, new_data=data, now=T0 + i * 10 + (1 if name == "b.bin" else 0))
            backup.atomic_write(p, data)
    assert big.total_bytes() <= 1 << 20
    kept = sorted(e.time for k in big.keys() for e in big.entries(k))
    assert kept and kept[0] > T0 + 10                             # 古い物から消えている
    assert {k: len(big.entries(k)) for k in big.keys()} == {"a.bin": 2, "b.bin": 3}
    report = big.prune(T0 + 100)
    assert report["bytesAfter"] == big.total_bytes() <= 1 << 20


def test_restore_keeps_the_current_one_and_can_be_undone(store, tmp_path):
    path, key = tmp_path / "out" / "a.inkdeus.json", "a.inkdeus.json"
    for i, text in enumerate(["v1", "v2", "v3"]):
        save(store, path, key, text, T0 + i * 10)
    assert texts(store, key) == ["v2", "v1", "<absent>"] and path.read_text(encoding="utf-8") == "v3"
    # 番号（1 がいちばん新しい）・時刻・退避の名前で選べる
    assert store.pick(key, "1").path.read_text(encoding="utf-8") == "v2"
    assert store.pick(key, "2").path.read_text(encoding="utf-8") == "v1"
    assert store.pick(key, backup.stamp_name(T0 + 10)).path.read_text(encoding="utf-8") == "v1"
    assert store.pick(key, dt.datetime.fromtimestamp(T0 + 5).strftime("%Y-%m-%dT%H:%M:%S")).path.read_text(encoding="utf-8") == "v1"
    with pytest.raises(BackupError, match="1〜3"):
        store.pick(key, "9")
    with pytest.raises(BackupError, match="後の変更はありません"):
        store.pick(key, T0 + 1000)
    with pytest.raises(BackupError, match="読めません"):
        store.pick(key, "yesterday")
    # 戻す: いまの物（v3）も退避される
    assert store.restore(store.pick(key, "2"), path, now=T0 + 100) is True
    assert path.read_text(encoding="utf-8") == "v1" and texts(store, key)[0] == "v3"
    assert store.restore(store.pick(key, "3"), path, now=T0 + 101) is False        # 同じ中身（v1）へは何もしない
    # 取り消し: 戻した時刻の「直前の状態」= 戻す前の物
    assert store.restore(store.state_at(key, T0 + 100), path, now=T0 + 200) is True
    assert path.read_text(encoding="utf-8") == "v3"
    # 「まだ無かった」時点へ戻すと、ファイルが消える（消す前の物は残る）
    assert store.restore(store.entries(key)[-1], path, now=T0 + 300) is True
    assert not path.exists() and texts(store, key)[0] == "v3"
    assert store.restore(store.entries(key)[0], path, now=T0 + 400) is True and path.read_text(encoding="utf-8") == "v3"


def test_points_merge_nearby_changes_and_restore_them_together(store, tmp_path):
    params, pen = tmp_path / "out" / "a.inkdeus.json", tmp_path / "out" / "a.paint" / "pen.png"
    targets = {"a.inkdeus.json": ("設定", params), "a.paint/pen.png": ("消しゴム・ペン（pen）", pen)}
    labels = {k: v[0] for k, v in targets.items()}
    save(store, params, "a.inkdeus.json", "p1", T0)
    save(store, pen, "a.paint/pen.png", "ink1", T0 + 1)           # 近い時刻の変更は、1 つの時点にまとまる
    save(store, params, "a.inkdeus.json", "p2", T0 + 60)
    save(store, pen, "a.paint/pen.png", "ink2", T0 + 120)
    save(store, params, "a.inkdeus.json", "p3", T0 + 121)
    found = backup.points(store, labels)
    assert [(p["number"], p["what"]) for p in found] == [
        (1, ["設定", "消しゴム・ペン（pen）"]), (2, ["設定"]), (3, ["消しゴム・ペン（pen）", "設定"])]
    assert found[0]["time"] == pytest.approx(T0 + 120, abs=0.01)
    # 2 番（T0+60 の変更の直前）へ戻す → 設定は p1、ペンは ink1（その時点で入っていた物）
    changed = backup.restore_at(store, targets, backup.pick_point(found, "2"), now=T0 + 500)
    assert sorted(changed) == ["消しゴム・ペン（pen）", "設定"]
    assert params.read_text(encoding="utf-8") == "p1" and pen.read_text(encoding="utf-8") == "ink1"
    # 取り消し（戻した時刻を指す）→ 戻す前の中身
    assert sorted(backup.restore_at(store, targets, T0 + 500, now=T0 + 600)) == ["消しゴム・ペン（pen）", "設定"]
    assert params.read_text(encoding="utf-8") == "p3" and pen.read_text(encoding="utf-8") == "ink2"


def test_atomic_write_leaves_the_original_when_interrupted(tmp_path, monkeypatch):
    path = tmp_path / "deep" / "f.json"
    backup.atomic_write(path, b"first")
    assert path.read_bytes() == b"first"

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(backup.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        backup.atomic_write(path, b"second-that-never-lands")
    monkeypatch.undo()
    assert path.read_bytes() == b"first"                          # 元はそのまま
    assert [p.name for p in path.parent.iterdir()] == ["f.json"]  # 書きかけの一時ファイルも残らない

    # 書き込みそのものが途中で失敗しても同じ
    class Broken(bytes):
        def __len__(self):
            raise RuntimeError("interrupted")

    real_open = open

    def flaky_open(file, mode="r", *a, **kw):
        handle = real_open(file, mode, *a, **kw)
        if "w" in mode and str(file).endswith(".tmp"):
            def write(_data):
                handle.close()
                raise KeyboardInterrupt
            handle_write = type("H", (), {"write": staticmethod(write), "flush": handle.flush, "fileno": handle.fileno,
                                          "__enter__": lambda self: self, "__exit__": lambda self, *x: handle.close()})
            return handle_write()
        return handle

    monkeypatch.setattr("builtins.open", flaky_open)
    with pytest.raises(KeyboardInterrupt):
        backup.atomic_write(path, b"third")
    monkeypatch.undo()
    assert path.read_bytes() == b"first" and [p.name for p in path.parent.iterdir()] == ["f.json"]


def test_concurrent_writers_never_leave_a_torn_file(tmp_path):
    import threading

    path = tmp_path / "f.bin"
    payloads = [bytes([i]) * 300_000 for i in range(8)]
    errors = []

    def writer(data):
        try:
            for _ in range(15):
                backup.atomic_write(path, data)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(p,)) for p in payloads]
    for t in threads:
        t.start()
    seen = set()
    while any(t.is_alive() for t in threads):
        if path.exists():
            data = path.read_bytes()
            seen.add(len(data))
            assert len(set(data)) == 1                             # 2 人の書きかけが混ざった中身は、一度も見えない
    for t in threads:
        t.join()
    assert not errors and seen == {300_000}
    assert path.read_bytes() in payloads and [p.name for p in tmp_path.iterdir()] == ["f.bin"]


def test_move_broken_and_config(tmp_path):
    path = tmp_path / "a.json"
    path.write_bytes(b"")
    kept = backup.move_broken(path, now=T0)
    assert kept.name == f"a.json.broken-{backup.stamp_name(T0)}" and not path.exists()
    path.write_bytes(b"x")
    assert backup.move_broken(path, now=T0).name.endswith("-1")   # 同じ時刻でも上書きしない
    assert backup.move_broken(tmp_path / "nothing.json") is None
    cfg = backup.read_config({"backup": {"enabled": False, "keep_recent": 3, "max_total_mb": 64, "hourly_hours": "x"}})
    assert (cfg.enabled, cfg.keep_recent, cfg.max_total_mb, cfg.hourly_hours) == (False, 3, 64, 24)
    assert backup.read_config({}) == Config() and backup.read_config({"backup": "no"}) == Config()
