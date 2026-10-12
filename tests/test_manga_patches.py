"""上流へ差分を当てる仕組み（inkdeus manga install）: 合成の「上流」git リポジトリで確かめる"""

from __future__ import annotations

import json
import logging

import pytest

import synth
from inkdeus import cli, manga
from inkdeus.server import create_app
from test_manga import git, make_upstream


@pytest.fixture()
def upstream(tmp_path):
    return make_upstream(tmp_path / "upstream")


def make_patch(repo, patches, commit: str, name: str, rel: str, text: str) -> None:
    """repo の rel を text に書き換える差分を作り、patches/<commit>/<name>.patch に置く（作業ツリーは元へ戻す）"""
    path = repo / rel
    path.write_text(text, encoding="utf-8", newline="\n")
    diff = git(repo, "diff", "--no-color") + "\n"
    git(repo, "checkout", "--", rel)
    folder = patches / commit
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.patch").write_text(diff, encoding="utf-8", newline="\n")


def app_js(info: dict) -> str:
    from pathlib import Path

    return (Path(info["path"]) / "js" / "app.js").read_text(encoding="utf-8")


def test_patches_are_applied_in_order(tmp_path, upstream):
    repo, commit = upstream
    patches = tmp_path / "patches"
    # 2 つ目は、1 つ目を当てた後の内容に対する差分（順に当てないと当たらない）
    make_patch(repo, patches, commit, "0001-first", "js/app.js", "window.fakeEditor = true;\nwindow.first = 1;\n")
    (repo / "js" / "app.js").write_text("window.fakeEditor = true;\nwindow.first = 1;\n", encoding="utf-8", newline="\n")
    git(repo, "commit", "--quiet", "-am", "tmp")
    make_patch(repo, patches, commit, "0002-second", "js/app.js",
               "window.fakeEditor = true;\nwindow.first = 1;\nwindow.second = 2;\n")
    git(repo, "reset", "--quiet", "--hard", commit)
    said: list[str] = []
    info = manga.install(ref=commit, source=repo, root=tmp_path / "cache", say=said.append, patch_root=patches)
    assert info["patches"] == {"applied": ["0001-first", "0002-second"], "available": ["0001-first", "0002-second"],
                               "pending": [], "failed": None, "disabled": False}
    assert app_js(info) == "window.fakeEditor = true;\nwindow.first = 1;\nwindow.second = 2;\n"
    assert not any("警告" in line for line in said)
    assert manga.pending_notice(info) is None
    # 上流のクローン自体は変わらない
    assert (repo / "js" / "app.js").read_text(encoding="utf-8") == "window.fakeEditor = true;\n"
    assert manga.status(tmp_path / "cache", patches)["patches"]["applied"] == ["0001-first", "0002-second"]


def test_failed_patch_rolls_everything_back(tmp_path, upstream):
    repo, commit = upstream
    patches = tmp_path / "patches"
    make_patch(repo, patches, commit, "0001-ok", "css/main.css", "body { margin: 1px; }\n")
    (patches / commit / "0002-broken.patch").write_text(
        "diff --git a/js/app.js b/js/app.js\n--- a/js/app.js\n+++ b/js/app.js\n@@ -1 +1 @@\n"
        "-window.somethingElse = true;\n+window.changed = true;\n", encoding="utf-8", newline="\n")
    make_patch(repo, patches, commit, "0003-never", "manifest.json", "{\"x\": 1}\n")
    said: list[str] = []
    info = manga.install(ref=commit, source=repo, root=tmp_path / "cache", say=said.append, patch_root=patches)
    state = info["patches"]
    assert state["applied"] == [] and state["pending"] == ["0001-ok", "0002-broken", "0003-never"]
    assert state["failed"]["name"] == "0002-broken" and state["failed"]["rolledBack"] == ["0001-ok"]
    assert state["failed"]["reason"]
    # 当てる前の状態に戻っている（1 つ目も取り消されている）し、そのまま使える
    from pathlib import Path

    base = Path(info["path"])
    assert (base / "css" / "main.css").read_text(encoding="utf-8") == "body { margin: 0; }\n"
    assert (base / "manifest.json").read_text(encoding="utf-8") == "{}\n"
    assert app_js(info) == "window.fakeEditor = true;\n" and info["installed"]
    assert any("警告" in line and "0002-broken" in line for line in said)
    assert "0002-broken" in manga.pending_notice(info)


def test_no_patches_option_and_reinstall(tmp_path, upstream):
    repo, commit = upstream
    patches = tmp_path / "patches"
    make_patch(repo, patches, commit, "0001-first", "js/app.js", "window.fakeEditor = true;\nwindow.first = 1;\n")
    quiet = lambda _m: None  # noqa: E731
    info = manga.install(ref=commit, source=repo, root=tmp_path / "cache", say=quiet, patches=False, patch_root=patches)
    assert info["patches"]["applied"] == [] and info["patches"]["disabled"] is True
    assert info["patches"]["pending"] == ["0001-first"]
    assert app_js(info) == "window.fakeEditor = true;\n"
    assert manga.pending_notice(info) is None                      # 自分で外したのだから案内しない
    # 入れ直すと当たる
    info = manga.install(ref=commit, source=repo, root=tmp_path / "cache", say=quiet, patch_root=patches)
    assert info["patches"]["applied"] == ["0001-first"] and info["patches"]["disabled"] is False
    assert app_js(info).endswith("window.first = 1;\n")


def test_pending_patches_of_an_older_install_are_detected(tmp_path, upstream, caplog):
    """差分の仕組みが無い版で入れたキャッシュ（印に patches が無い）は「未適用」と分かる。自動では当てない"""
    repo, commit = upstream
    patches = tmp_path / "patches"
    quiet = lambda _m: None  # noqa: E731
    info = manga.install(ref=commit, source=repo, root=tmp_path / "cache", say=quiet, patch_root=patches)
    assert info["patches"] == {"applied": [], "available": [], "pending": [], "failed": None, "disabled": False}
    from pathlib import Path

    marker = Path(info["path"]) / manga.MARKER_FILE
    old = json.loads(marker.read_text(encoding="utf-8"))
    old.pop("patches")
    marker.write_text(json.dumps(old), encoding="utf-8")
    make_patch(repo, patches, commit, "0001-later", "js/app.js", "window.fakeEditor = true;\nwindow.later = 1;\n")
    info = manga.status(tmp_path / "cache", patches)
    assert info["patches"]["applied"] == [] and info["patches"]["pending"] == ["0001-later"]
    assert "未適用の差分が 1 個" in manga.pending_notice(info) and "manga install" in manga.pending_notice(info)
    assert app_js(info) == "window.fakeEditor = true;\n"            # 勝手には当てない


def test_cli_status_and_server_report_patches(tmp_path, upstream, capsys, monkeypatch, caplog):
    repo, commit = upstream
    monkeypatch.setenv("INKDEUS_CACHE", str(tmp_path / "cache"))
    patches = tmp_path / "patches" / manga.UPSTREAM_NAME
    make_patch(repo, patches, commit, "0001-first", "js/app.js", "window.fakeEditor = true;\nwindow.first = 1;\n")
    monkeypatch.setattr(manga, "patches_root", lambda: patches)
    assert cli.main(["manga", "install", "--ref", commit, "--from", str(repo), "--no-patches"]) == 0
    out = capsys.readouterr().out
    assert "当てていません（--no-patches）" in out and "未適用の差分: 0001-first" in out
    assert cli.main(["manga", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["patches"]["pending"] == ["0001-first"]
    # 印から「自分で外した」を消すと（= 古い版で入れた状態）、サーバの起動時に案内が出る
    from pathlib import Path

    marker = Path(manga.status()["path"]) / manga.MARKER_FILE
    old = json.loads(marker.read_text(encoding="utf-8"))
    old.pop("patches")
    marker.write_text(json.dumps(old), encoding="utf-8")
    folder = tmp_path / "images"
    synth.save(folder / "a.png", synth.flat(32, 16, 200))
    with caplog.at_level(logging.WARNING, logger="inkdeus.manga"):
        app = create_app(folder, local_config=tmp_path / "none.toml", watch=False)
    assert any("未適用の差分" in r.getMessage() for r in caplog.records)
    status = app.test_client().get("/api/manga/status").get_json()
    assert status["patches"]["pending"] == ["0001-first"] and "manga install" in status["patchNotice"]
    # 入れ直すと当たり、案内は消える
    assert cli.main(["manga", "install", "--ref", commit, "--from", str(repo)]) == 0
    assert "当てた差分: 0001-first" in capsys.readouterr().out
    assert cli.main(["manga", "status"]) == 0
    assert "当てた差分: 0001-first" in capsys.readouterr().out
    status = app.test_client().get("/api/manga/status").get_json()
    assert status["patches"]["applied"] == ["0001-first"] and status["patchNotice"] is None
    # 差分の当たった内容が配信される
    assert b"window.first = 1;" in app.test_client().get("/manga/js/app.js").data
