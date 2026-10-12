"""コマンドライン入口。

    inkdeus new <名前> --images <dir> [--masks <dir>]   プロジェクトを作る
    inkdeus open <名前 or パス>                         プロジェクトを開いてサーバを起動
    inkdeus projects                                    プロジェクトの一覧
    inkdeus run <画像 or フォルダ>                      プロジェクトなしで直接起動（inkdeus <path> でも可）
    inkdeus ctl …                                       稼働中のサーバを操作
    inkdeus manga install | status                      漫画の構成に使う漫画エディタを取得する（inkdeus manga --help）
    inkdeus assets add | list | remove <プロジェクト>   漫画の構成で使う素材ソース（画像フォルダ）を登録する
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import ctl, manga
from . import fonts as fonts_mod
from .assets import BUILTIN as BUILTIN_ASSETS
from .assets import MODES as ASSET_MODES
from .assets import Catalog, read_toml_assets
from .projects import (ProjectError, add_asset_source, create_project, list_projects, load_project,
                       projects_root, remove_asset_source)
from .server import create_app, default_local_config

COMMANDS = ("run", "new", "open", "projects", "ctl", "manga", "assets", "fonts")


def _server_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--port", type=int, default=8770)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--owner", action="store_true", help="廃止（無視されます）。透かしは画面のスイッチで切り替えます")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="inkdeus",
        description="カラーのイラストを白黒の漫画原稿風に手動調整するエディタ（ブラウザで開く）",
        epilog="inkdeus <画像 or フォルダ> は run の省略形。稼働中のサーバの操作は inkdeus ctl --help")
    sub = ap.add_subparsers(dest="command", required=True, metavar="<command>")

    p = sub.add_parser("run", help="プロジェクトなしで、画像ファイルまたはフォルダを直接開く")
    p.add_argument("input", help="画像ファイル、または画像の入ったフォルダ")
    p.add_argument("--mask-dir", help="人物マスクのフォルダ（<画像と同名>.png、白=人物）")
    p.add_argument("--out-dir", help="書き出しと設定JSONの保存先（既定: <入力フォルダ>/inkdeus_out）")
    _server_options(p)

    p = sub.add_parser("new", help="プロジェクトを作る（元画像は参照するだけでコピーしない）")
    p.add_argument("name", help="プロジェクト名（プロジェクト置き場の下のフォルダ名になる）")
    p.add_argument("--images", required=True, help="元画像のフォルダ")
    p.add_argument("--masks", help="人物マスクのフォルダ（<画像と同名>.png、白=人物）")
    p.add_argument("--copy", action="store_true", help="画像とマスクをプロジェクト内にコピーする")
    p.add_argument("--note", default="", help="メモ")
    p.add_argument("--force", action="store_true", help="既にあるプロジェクトの設定を更新する")

    p = sub.add_parser("open", help="プロジェクトを開いてサーバを起動する")
    p.add_argument("project", help="プロジェクト名、またはプロジェクトのフォルダ")
    _server_options(p)

    p = sub.add_parser("projects", help="プロジェクトの一覧")
    p.add_argument("--json", action="store_true", help="JSON で出力")

    sub.add_parser("ctl", help="稼働中のサーバを操作する（inkdeus ctl --help）", add_help=False)
    sub.add_parser("manga", help="漫画の構成に使う漫画エディタを取得する（inkdeus manga --help）", add_help=False)

    p = sub.add_parser("assets", help="漫画の構成で使う素材ソース（画像フォルダ）をプロジェクトに登録する")
    asub = p.add_subparsers(dest="assets_command", required=True, metavar="<command>")
    a = asub.add_parser("add", help="素材ソースを登録する（同じ名前があれば置き換える）")
    a.add_argument("project", help="プロジェクト名、またはプロジェクトのフォルダ")
    a.add_argument("--name", required=True, help="ソース名（素材パネルのタブ名になる）")
    a.add_argument("--path", required=True, help="画像の入ったフォルダ")
    a.add_argument("--mode", choices=ASSET_MODES, default="panel",
                   help="panel = コマにはめ込む画像（既定） / free = ページ上へ自由に置く素材（擬音など）")
    a.add_argument("--recursive", action="store_true", help="サブフォルダも含める（フォルダ名で見出し分けされる）")
    a = asub.add_parser("list", help="素材ソースの一覧")
    a.add_argument("project", help="プロジェクト名、またはプロジェクトのフォルダ")
    a.add_argument("--json", action="store_true", help="JSON で出力")
    p = sub.add_parser("fonts", help="漫画の構成で使うローカルフォント（設定の [fonts]）を確かめる")
    fsub = p.add_subparsers(dest="fonts_command", required=True, metavar="<command>")
    f = fsub.add_parser("list", help="設定から見つかったフォントの一覧（表示名とファイル）")
    f.add_argument("--project", help="プロジェクト名（そのプロジェクトの [fonts] も合わせて見る）")
    f.add_argument("--json", action="store_true", help="JSON で出力")
    a = asub.add_parser("remove", help="素材ソースの登録を外す（フォルダの中身には触らない）")
    a.add_argument("project", help="プロジェクト名、またはプロジェクトのフォルダ")
    a.add_argument("--name", required=True, help="ソース名")
    return ap


def serve(app, host: str, port: int, project=None) -> int:
    lib = app.config["INKDEUS_LIBRARY"]
    print(f"inkdeus: http://{host}:{port}/")
    if project is not None:
        print(f"  プロジェクト: {project.name}（{project.path}）")
    print(f"  入力: {lib.folder}{'/' + lib.single if lib.single else ''}（{len(lib.entries())} 枚）")
    print(f"  出力: {lib.out_dir}")
    if lib.mask_dir:
        print(f"  マスク: {lib.mask_dir}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("  注意: ループバック以外で待ち受けます（同じネットワークから読み書きできます）")
    print(f"  漫画の構成: http://{host}:{port}/manga/" + ("" if manga.installed_dir() else
                                                              "（未導入。inkdeus manga install で取得）"))
    sys.stdout.flush()
    logger = logging.getLogger("inkdeus")                 # /manga/ の書き換えの記録などを端末に出す
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    app.run(host=host, port=port, threaded=True)
    return 0


def cmd_run(args) -> int:
    try:
        app = create_app(Path(args.input), args.mask_dir, args.out_dir)
    except (FileNotFoundError, ValueError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    return serve(app, args.host, args.port)


def cmd_new(args) -> int:
    project, summary = create_project(args.name, args.images, args.masks, copy=args.copy,
                                      note=args.note, force=args.force)
    print(f"プロジェクトを作りました: {project.name}")
    print(f"  場所: {project.path}")
    print(f"  元画像: {project.source}（{summary['images']} 枚{'・コピー済み' if args.copy else '・参照'}）")
    if project.masks is not None:
        print(f"  マスク: {project.masks}（同名マスクあり {summary['masks']} / {summary['images']} 枚）")
    print(f"  出力: {project.out}")
    print(f"開くには: inkdeus open {project.name}")
    return 0


def cmd_open(args) -> int:
    project = load_project(args.project)
    try:
        app = create_app(project.source, project.masks, project.out, project=project.info())
    except (FileNotFoundError, ValueError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    return serve(app, args.host, args.port, project)


def cmd_projects(args) -> int:
    rows = list_projects()
    if args.json:
        print(json.dumps({"root": str(projects_root()), "projects": rows}, ensure_ascii=False, indent=2))
        return 0
    print(f"プロジェクト置き場: {projects_root()}")
    if not rows:
        print("  プロジェクトはまだありません（inkdeus new <名前> --images <dir> で作れます）")
        return 0
    for r in rows:
        if "error" in r:
            print(f"  {r['name']}: 読めません（{r['error']}）")
        elif r["missing"]:
            print(f"  {r['name']}: 元画像フォルダが見つかりません（{r['source']}）")
        else:
            print(f"  {r['name']}: 画像 {r['images']} / マスク {r['masks']} / 設定済み {r['configured']} / "
                  f"書き出し済み {r['exported']} / 最終更新 {r['updated']}")
    return 0


def cmd_assets(args) -> int:
    project = load_project(args.project, check=False)
    if args.assets_command == "add":
        entry = add_asset_source(project, args.name, args.path, args.mode, args.recursive)
        print(f"素材ソースを登録しました: {entry['name']}（{project.name}）")
        print(f"  場所: {entry['path']} / mode: {entry['mode']}"
              f"{' / サブフォルダも含める' if entry['recursive'] else ''}")
        return 0
    if args.assets_command == "remove":
        remove_asset_source(project, args.name)
        print(f"素材ソースの登録を外しました: {args.name}（{project.name}）")
        return 0
    local_file = default_local_config()
    rows = []
    for name, label in BUILTIN_ASSETS.items():
        rows.append({"name": name, "label": label, "mode": "panel", "recursive": False, "origin": "builtin",
                     "path": str(project.source if name == "source" else project.out)})
    seen = set(BUILTIN_ASSETS)
    problems: list[str] = []
    for path, origin in ((project.config, "project"), (local_file, "local")):
        found, bad = read_toml_assets(path, origin)
        problems += bad
        for src in found:
            if src.name in seen:
                continue
            seen.add(src.name)
            count = len(Catalog._scan(src.root, src.recursive))
            rows.append({"name": src.name, "label": src.label, "mode": src.mode, "recursive": src.recursive,
                         "origin": origin, "path": str(src.root), "missing": not src.root.is_dir(), "count": count})
    if args.json:
        print(json.dumps({"project": project.name, "sources": rows, "problems": problems},
                         ensure_ascii=False, indent=2))
        return 0
    origins = {"builtin": "既定", "project": "プロジェクト", "local": "共通（inkdeus.local.toml）"}
    print(f"素材ソース（{project.name}）:")
    for r in rows:
        extra = "" if "count" not in r else ("・フォルダが見つかりません" if r["missing"] else f"・{r['count']} 枚")
        print(f"  {r['name']}: {r['mode']}{'・サブフォルダも含める' if r['recursive'] else ''}"
              f"・{origins[r['origin']]}{extra}  {r['path']}")
    for msg in problems:
        print(f"  注意: {msg}")
    return 0


def cmd_fonts(args) -> int:
    configs = [default_local_config()]
    if args.project:
        configs.append(load_project(args.project, check=False).config)
    fonts, problems = fonts_mod.resolve(configs)
    if args.json:
        print(json.dumps({"fonts": [f.info() | {"path": str(f.path)} for f in fonts], "problems": problems},
                         ensure_ascii=False, indent=2))
        return 0
    if not fonts:
        print("フォントは登録されていません（inkdeus.local.toml かプロジェクトの設定に [fonts] を書きます）")
    for f in fonts:
        print(f"  {f.name}  —  {f.path}")
    for msg in problems:
        print(f"  注意: {msg}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "ctl":
        return ctl.main(argv[1:])
    if argv and argv[0] == "manga":
        return manga.main(argv[1:])
    # inkdeus <path> は、path が実在するときだけ run の省略形として扱う
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-") and Path(argv[0]).exists():
        argv = ["run", *argv]
    args = build_parser().parse_args(argv)
    try:
        return {"run": cmd_run, "new": cmd_new, "open": cmd_open, "projects": cmd_projects,
                "assets": cmd_assets, "fonts": cmd_fonts}[args.command](args)
    except ProjectError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
