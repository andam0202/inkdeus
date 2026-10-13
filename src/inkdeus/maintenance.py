"""データを守るためのコマンド: 世代バックアップ（backup）、プロジェクトの書き出し・取り込み、元画像の場所の付け替え。

    inkdeus backup list <プロジェクト> [--name <画像名>] [--folder F] [--version V] [--kind K] [--json]
    inkdeus backup restore <プロジェクト> … --at <時刻 or 番号>
    inkdeus backup prune <プロジェクト>
    inkdeus export-project <プロジェクト> -o <ファイル.zip> [--with-exports] [--with-backups] [--with-sources]
    inkdeus import-project <ファイル.zip> --name <新しい名前> [--images <dir>] [--masks <dir>]
    inkdeus relink <プロジェクト> [--folder F] --images <新しいdir> [--masks <dir>] [--parts <dir>] [--force]
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import backup as backup_mod
from . import folders as folders_mod
from . import portable
from .backup import BackupError
from .projects import PROJECT_FILE, Project, ProjectError, folder_def, list_images, load_project, relink
from .server import (MANGA_NAME_RE, MANGA_SUFFIX, PAINT_SUFFIX, PARAMS_SUFFIX, VERSION_RE, VERSION_SEP,
                     read_local_config, write_paint_index_in)
from .styles import FILE as STYLES_FILE

MASKEDIT_SUFFIX = ".maskedit"
KIND_LABELS = {"params": "設定", "paint": "消しゴム・ペン", "maskedit": "部分マスクの手直し", "manga": "漫画データ",
               "project": "プロジェクトの設定", "styles": "このプロジェクトのスタイル"}
IMAGE_KINDS = ("params", "paint", "maskedit")


def store_of(project: Project) -> backup_mod.Store:
    return backup_mod.Store(project.out / backup_mod.DIR, backup_mod.read_config(read_local_config()))


def _stem(project: Project, folder: str | None, name: str) -> str:
    """画像名 → 保存先の基底名（拡張子違いの同名ファイルがあるときだけ、拡張子込みの名前）"""
    d = folder_def(project, folder)
    files = list_images(d.images) if d.images.is_dir() else []
    same = [p for p in files if p.stem == Path(name).stem]
    if len(same) > 1 and any(p.name == name for p in same):
        return name.replace(".", "_")
    return Path(name).stem


def _prefix(folder: str | None) -> str:
    return "" if folder in (None, "", folders_mod.DEFAULT) else f"{folders_mod.OUT_PREFIX}{folder}/"


def targets_of(project: Project, store: backup_mod.Store, kinds: list[str], name: str | None,
               folder: str | None, version: str) -> dict[str, tuple[str, Path]]:
    """戻す単位: 退避のキー → (種類の表示名, 戻す先)"""
    out: dict[str, tuple[str, Path]] = {}
    base_out = folders_mod.out_dir(project.out, folder or folders_mod.DEFAULT)
    prefix = _prefix(folder)
    for kind in kinds:
        if kind in IMAGE_KINDS:
            if not name:
                raise BackupError(f"{KIND_LABELS[kind]}は --name <画像名> で画像を選んでください")
            stem = _stem(project, folder, name)
            base = f"{stem}{VERSION_SEP}{version}" if version else stem
            if kind == "params":
                out[f"{prefix}{base}{PARAMS_SUFFIX}"] = ("設定", base_out / f"{base}{PARAMS_SUFFIX}")
                continue
            folder_name = f"{base}{PAINT_SUFFIX}" if kind == "paint" else f"{stem}{MASKEDIT_SUFFIX}"
            for key in store.keys(f"{prefix}{folder_name}/"):
                target = key.rsplit("/", 1)[1]
                out[key] = (f"{KIND_LABELS[kind]}（{target.rsplit('.', 1)[0]}）", base_out / folder_name / target)
        elif kind == "manga":
            if not name or MANGA_NAME_RE.fullmatch(name) is None:
                raise BackupError("漫画データは --name <保存名> で選んでください（自動保存は autosave）")
            out[f"{backup_mod.MANGA_PREFIX}/{name}{MANGA_SUFFIX}"] = ("漫画データ", project.manga / f"{name}{MANGA_SUFFIX}")
        elif kind == "project":
            out[f"{backup_mod.PROJECT_PREFIX}/{PROJECT_FILE}"] = ("プロジェクトの設定", project.config)
        elif kind == "styles":
            out[f"{backup_mod.PROJECT_PREFIX}/{STYLES_FILE}"] = ("このプロジェクトのスタイル", project.path / STYLES_FILE)
    return out


def classify(key: str) -> dict:
    """退避のキーから、種類・フォルダ・名前・バージョンを読む"""
    folder = folders_mod.DEFAULT
    rest = key
    if rest.startswith(folders_mod.OUT_PREFIX) and "/" in rest:
        folder, rest = rest[1:].split("/", 1)
    if key.startswith(backup_mod.MANGA_PREFIX + "/"):
        return {"kind": "manga", "folder": None, "name": key.split("/", 1)[1][: -len(MANGA_SUFFIX)], "version": None}
    if key.startswith(backup_mod.PROJECT_PREFIX + "/"):
        return {"kind": "styles" if key.endswith(STYLES_FILE) else "project", "folder": None,
                "name": key.split("/", 1)[1], "version": None}
    head, _, target = rest.partition("/")
    for kind, suffix in (("params", PARAMS_SUFFIX), ("paint", PAINT_SUFFIX), ("maskedit", MASKEDIT_SUFFIX)):
        if head.endswith(suffix):
            base = head[: -len(suffix)]
            stem, sep, version = base.rpartition(VERSION_SEP)
            if not sep or VERSION_RE.fullmatch(version) is None or kind == "maskedit":
                stem, version = base, ""
            return {"kind": kind, "folder": folder, "name": stem, "version": version,
                    **({"target": target.rsplit(".", 1)[0]} if target else {})}
    return {"kind": "other", "folder": folder, "name": rest, "version": None}


def _kinds(args) -> list[str]:
    if args.kind:
        return [args.kind]
    # 種類を省いたとき: 画像名があれば、その画像・バージョンの設定と消しゴム・ペン（画面の「以前の状態に戻す」と同じ単位）
    return ["params", "paint"] if args.name else []


def cmd_backup(args) -> int:
    project = load_project(args.project, check=False)
    store = store_of(project)
    if args.backup_command == "prune":
        result = store.prune()
        print(f"バックアップを間引きました（{project.name}）: {result['removed']} 個を削除 / "
              f"{result['bytesBefore'] / 1e6:.1f} MB → {result['bytesAfter'] / 1e6:.1f} MB")
        return 0
    kinds = _kinds(args)
    version = args.version or ""
    if args.backup_command == "list" and not (args.name or args.kind in ("project", "styles")):
        # 全体の一覧（種類・フォルダで絞れる）
        rows = []
        for key in store.keys():
            info = classify(key)
            if args.kind and info["kind"] != args.kind:
                continue
            if args.folder and info["folder"] not in (None, args.folder):
                continue
            entries = store.entries(key)
            rows.append({**info, "key": key, "count": len(entries), "latest": entries[0].info()["label"],
                         "bytes": sum(e.bytes for e in entries)})
        if args.json:
            print(json.dumps({"project": project.name, "dir": str(store.root), "enabled": store.config.enabled,
                              "totalBytes": store.total_bytes(), "files": rows}, ensure_ascii=False, indent=2))
            return 0
        print(f"バックアップ（{project.name}）: {store.root}（{store.total_bytes() / 1e6:.1f} MB）")
        if not rows:
            print("  まだありません（保存のたびに、上書きされる前の版がここへ残ります）")
        for r in rows:
            where = "" if r["folder"] in (None, folders_mod.DEFAULT) else f"[{r['folder']}] "
            ver = f" @{r['version']}" if r.get("version") else ""
            target = f" / {r['target']}" if r.get("target") else ""
            print(f"  {KIND_LABELS.get(r['kind'], r['kind'])}: {where}{r['name']}{ver}{target}"
                  f" — {r['count']} 個（最新 {r['latest']}）")
        print("  画像ごとの時点の一覧は --name <画像名>、戻すには inkdeus backup restore … --at <番号 or 時刻>")
        return 0
    targets = targets_of(project, store, kinds, args.name, args.folder, version)
    found = backup_mod.points(store, {k: v[0] for k, v in targets.items()})
    if args.backup_command == "list":
        if args.json:
            print(json.dumps({"project": project.name, "name": args.name, "folder": args.folder or folders_mod.DEFAULT,
                              "version": version, "kinds": kinds, "points": found}, ensure_ascii=False, indent=2))
            return 0
        print(f"戻せる時点（{project.name} / {args.name or KIND_LABELS[kinds[0]]}"
              f"{f' @{version}' if version else ''}）— 新しい順。ある時点へ戻す = その変更が行われる直前の状態にする")
        if not found:
            print("  まだありません")
        for p in found:
            print(f"  {p['number']:>3}  {p['label']}  {' / '.join(p['what'])}")
        return 0
    # restore
    t = backup_mod.pick_point(found, args.at)
    now = time.time()
    changed = backup_mod.restore_at(store, targets, t, now=now)
    for kind in kinds:                                      # 消しゴム・ペンの目録を、戻した中身に合わせる
        if kind == "paint" and args.name:
            stem = _stem(project, args.folder, args.name)
            base = f"{stem}{VERSION_SEP}{version}" if version else stem
            write_paint_index_in(folders_mod.out_dir(project.out, args.folder or folders_mod.DEFAULT) / f"{base}{PAINT_SUFFIX}")
    label = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
    if args.json:
        print(json.dumps({"project": project.name, "restored": changed, "at": label,
                          "undo": backup_mod.stamp_name(now)}, ensure_ascii=False, indent=2))
        return 0
    if not changed:
        print(f"何も変わりませんでした（すでに {label} の時点と同じ中身です）")
        return 0
    print(f"{label} の変更の直前の状態へ戻しました（{project.name}）: {' / '.join(changed)}")
    print(f"  戻す前の状態も残してあります（取り消すには --at {backup_mod.stamp_name(now)}）")
    print("  稼働中のサーバがあれば、開いている画面へ反映されます")
    return 0


def cmd_export_project(args) -> int:
    project = load_project(args.project, check=False)
    manifest = portable.export_project(project, args.output, with_exports=args.with_exports,
                                       with_backups=args.with_backups, with_sources=args.with_sources)
    print(f"プロジェクトを書き出しました: {manifest['zip']}（{manifest['bytes'] / 1e6:.1f} MB・{len(manifest['files'])} ファイル）")
    extras = [label for flag, label in ((args.with_sources, "元画像とマスク"), (args.with_exports, "書き出した PNG・PDF"),
                                        (args.with_backups, "バックアップ")) if flag]
    print(f"  中身: 設定・消しゴムとペン・部分マスクの手直し・漫画データ・スタイル{'・' + '・'.join(extras) if extras else ''}")
    if not args.with_sources:
        print("  元画像とマスクは入っていません（取り込むときに --images で場所を指定します。含めるなら --with-sources）")
    return 0


def cmd_import_project(args) -> int:
    project, summary = portable.import_project(args.zip, args.name, images=args.images, masks=args.masks)
    print(f"プロジェクトを取り込みました: {project.name}（{summary['files']} ファイル）")
    print(f"  場所: {project.path}")
    if summary["unset"]:
        hint = f"（元の画像フォルダの名前: {summary['imagesName']}）" if summary.get("imagesName") else ""
        print(f"  元画像の場所が未設定です{hint}。inkdeus relink {project.name} --images <dir> で決めてください")
    for name in summary["pendingFolders"]:
        print(f"  画像フォルダ「{name}」の元画像は入っていません。"
              f"inkdeus folders add {project.name} --name {name} --images <dir> で場所を決めると、設定がつながります")
    return 0


def cmd_relink(args) -> int:
    project = load_project(args.project, check=False)
    report = relink(project, args.images, masks=args.masks, parts=args.parts, folder=args.folder, force=args.force)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["applied"] else 1
    label = "既定のフォルダ" if report["folder"] == folders_mod.DEFAULT else f"フォルダ「{report['folder']}」"
    print(f"付け替え先（{project.name} / {label}）: {report['to']}")
    print(f"  一致 {len(report['matched'])} / 不足 {len(report['missing'])} / 余分 {len(report['extra'])}")
    for key, title in (("missing", "不足（いまの設定・書き出しがあるのに、付け替え先に無い画像）"),
                       ("extra", "余分（付け替え先にだけある画像。新しい画像として出ます）")):
        if report[key]:
            shown = ", ".join(report[key][:10]) + (f" ほか {len(report[key]) - 10} 件" if len(report[key]) > 10 else "")
            print(f"  {title}: {shown}")
    if not report["applied"]:
        print("中止しました（不足があります。それでも付け替えるなら --force）")
        return 1
    print("付け替えました。稼働中のサーバは起動し直してください（漫画データの中の画像は、画像名で引かれるので、そのまま表示されます）")
    return 0


def add_parsers(sub) -> None:
    project_help = "プロジェクト名、またはプロジェクトのフォルダ"
    p = sub.add_parser("backup", help="保存のたびに残している前の版（世代バックアップ）を見る・戻す・間引く")
    bsub = p.add_subparsers(dest="backup_command", required=True, metavar="<list|restore|prune>")
    for command, text in (("list", "バックアップの一覧（--name で、その画像の戻せる時点の一覧）"),
                          ("restore", "バックアップから戻す（戻す前のいまの状態も残す）")):
        a = bsub.add_parser(command, help=text)
        a.add_argument("project", help=project_help)
        a.add_argument("--name", help="画像名（--kind manga のときは漫画データの保存名）")
        a.add_argument("--folder", help="画像フォルダ名（省略時は既定のフォルダ）")
        a.add_argument("--version", help="バージョン名（省略時は既定のバージョン）")
        a.add_argument("--kind", choices=backup_mod.KINDS,
                       help="種類（省略時: --name があれば設定と消しゴム・ペンをまとめて）")
        a.add_argument("--json", action="store_true", help="JSON で出力")
        if command == "restore":
            a.add_argument("--at", required=True, help="戻す時点（list の番号、または 2026-01-31T12:34 の形の時刻）")
    a = bsub.add_parser("prune", help="古いバックアップを間引く（保存のたびにも自動で行われる）")
    a.add_argument("project", help=project_help)

    p = sub.add_parser("export-project", help="プロジェクトの設定・手描き・漫画データを 1 つの zip に書き出す")
    p.add_argument("project", help=project_help)
    p.add_argument("-o", "--output", required=True, help="書き出す zip ファイル")
    p.add_argument("--with-exports", action="store_true", help="書き出した PNG・PDF も含める")
    p.add_argument("--with-backups", action="store_true", help="世代バックアップも含める")
    p.add_argument("--with-sources", action="store_true", help="元画像・人物マスク・部分マスクも含める")

    p = sub.add_parser("import-project", help="zip からプロジェクトを作る")
    p.add_argument("zip", help="inkdeus export-project で書き出した zip")
    p.add_argument("--name", required=True, help="新しいプロジェクト名")
    p.add_argument("--images", help="元画像のフォルダ（元画像を含まない zip のとき）")
    p.add_argument("--masks", help="人物マスクのフォルダ")

    p = sub.add_parser("relink", help="元画像の場所を付け替える（フォルダを動かした・別の PC へ持ってきた）")
    p.add_argument("project", help=project_help)
    p.add_argument("--folder", help="画像フォルダ名（省略時は既定のフォルダ）")
    p.add_argument("--images", required=True, help="新しい元画像のフォルダ")
    p.add_argument("--masks", help="新しい人物マスクのフォルダ（省略時はそのまま）")
    p.add_argument("--parts", help="新しい部分マスクの置き場（省略時はそのまま）")
    p.add_argument("--force", action="store_true", help="付け替え先に足りない画像があっても付け替える")
    p.add_argument("--json", action="store_true", help="JSON で出力")


COMMANDS = {"backup": cmd_backup, "export-project": cmd_export_project, "import-project": cmd_import_project,
            "relink": cmd_relink}
