"""プロジェクト全体の書き出しと取り込み（1 つの zip）。

zip の中身:
    manifest.json                  目録（中身の一覧とハッシュ、inkdeus の版、フォルダの名前と構成）
    out/…                          設定（*.inkdeus.json）、消しゴム・ペン（*.paint/）、部分マスクの手直し（*.maskedit/）
                                   追加の画像フォルダの分は out/@<フォルダ>/…
    manga/*.lz4                    漫画データ
    project/styles.json            このプロジェクトのスタイル
    sources/<フォルダ>/images|masks|parts/…   元画像・人物マスク・部分マスク（--with-sources のときだけ）

既定では、元画像・マスク・書き出した PNG・PDF・バックアップは含めない（作り直せる物・大きい物）。
目録に元画像フォルダの絶対パスは入れない（フォルダ名と、zip の中の相対構成だけ）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

from . import __version__ as INKDEUS_VERSION
from . import backup as backup_mod
from . import folders as folders_mod
from . import parts as parts_mod
from .projects import (PROJECT_FILE, Project, ProjectError, _toml_string, list_images, load_project, projects_root,
                       validate_name)
from .server import PAINT_SUFFIX, PARAMS_SUFFIX
from .styles import FILE as STYLES_FILE

FORMAT = 1
MANIFEST = "manifest.json"
MASKEDIT_SUFFIX = ".maskedit"
ROOTS = ("out", "manga", "project", "sources")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _out_files(out: Path, with_exports: bool, with_backups: bool) -> list[tuple[Path, str]]:
    """保存先の中で、持ち出す物（実パス, out からの相対パス）"""
    found: list[tuple[Path, str]] = []
    if not out.is_dir():
        return found

    def walk(folder: Path, rel: str, depth: int) -> None:
        for item in sorted(folder.iterdir(), key=lambda p: p.name):
            name = item.name
            here = f"{rel}{name}"
            if item.is_dir():
                if name == backup_mod.DIR:
                    if with_backups and depth == 0:
                        found.extend((p, f"{here}/{p.relative_to(item).as_posix()}")
                                     for p in sorted(item.rglob("*")) if p.is_file() and not p.name.endswith(".tmp"))
                elif name.endswith((PAINT_SUFFIX, MASKEDIT_SUFFIX)):
                    found.extend((p, f"{here}/{p.name}") for p in sorted(item.iterdir())
                                 if p.is_file() and not p.name.endswith(".tmp") and backup_mod.BROKEN_MARK not in p.name)
                elif name.startswith(folders_mod.OUT_PREFIX) and depth == 0:
                    walk(item, here + "/", depth + 1)             # 追加の画像フォルダの保存先
                # .cache（縮小画像）・manga（プロジェクトなしで起動したときの漫画データ）は、ここでは持ち出さない
            elif name.endswith(PARAMS_SUFFIX) or (with_exports and name.lower().endswith(".png")):
                found.append((item, here))

    walk(out, "", 0)
    return found


def export_project(project: Project, dest: Path | str, with_exports: bool = False, with_backups: bool = False,
                   with_sources: bool = False) -> dict:
    """プロジェクトを 1 つの zip に書き出す。戻り値は目録"""
    dest = Path(dest).expanduser().resolve()
    files: list[tuple[Path, str]] = [(p, f"out/{rel}") for p, rel in _out_files(project.out, with_exports, with_backups)]
    if project.manga.is_dir():
        for p in sorted(project.manga.iterdir()):
            if p.is_file() and p.suffix == ".lz4":
                files.append((p, f"manga/{p.name}"))
        if with_exports:
            for sub in ("pages", "pdf"):
                if (project.manga / sub).is_dir():
                    files.extend((p, f"manga/{sub}/{p.relative_to(project.manga / sub).as_posix()}")
                                 for p in sorted((project.manga / sub).rglob("*")) if p.is_file())
    if (project.path / STYLES_FILE).is_file():
        files.append((project.path / STYLES_FILE, f"project/{STYLES_FILE}"))

    defs = [folders_mod.FolderDef(folders_mod.DEFAULT, project.source, project.masks, "", project.parts),
            *project.folders]
    folder_rows = []
    for d in defs:
        row = {"name": d.name, "note": d.note,
               # 絶対パスは入れない。フォルダの名前（最後の 1 要素）だけを、付け替えるときの手がかりとして残す
               "imagesName": d.images.name, "masksName": d.masks.name if d.masks else None,
               "partsName": d.parts.name if d.parts else None,
               "images": len(list_images(d.images)) if d.images.is_dir() else 0, "sources": False}
        if with_sources and d.images.is_dir():
            base = f"sources/{d.name}"
            files.extend((p, f"{base}/images/{p.name}") for p in list_images(d.images))
            if d.masks is not None and d.masks.is_dir():
                files.extend((p, f"{base}/masks/{p.name}") for p in sorted(d.masks.glob("*.png")) if p.is_file())
            for part in parts_mod.scan(d.parts)[0]:
                files.extend((p, f"{base}/parts/{part}/{p.name}")
                             for p in sorted((d.parts / part).glob("*.png")) if p.is_file())
            row["sources"] = True
        folder_rows.append(row)

    entries = [{"path": rel, "bytes": p.stat().st_size, "sha256": _sha256(p)} for p, rel in files]
    manifest = {
        "format": FORMAT, "inkdeus": INKDEUS_VERSION,
        "created": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "project": {"name": project.name, "note": project.note, "created": project.created},
        "options": {"withExports": with_exports, "withBackups": with_backups, "withSources": with_sources},
        "folders": folder_rows, "files": entries,
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            for p, rel in files:
                # PNG・圧縮済みの漫画データは、圧縮し直しても縮まない
                z.write(p, rel, zipfile.ZIP_STORED if p.suffix.lower() in (".png", ".lz4", ".jpg", ".jpeg", ".webp", ".pdf")
                        else zipfile.ZIP_DEFLATED)
        shutil.move(str(tmp), str(dest))
    finally:
        tmp.unlink(missing_ok=True)
    return {**manifest, "zip": str(dest), "bytes": dest.stat().st_size}


def _safe_member(name: str) -> PurePosixPath | None:
    """zip の中の名前を確かめる（絶対パス・.. ・知らない場所は受け付けない）"""
    path = PurePosixPath(name)
    if path.is_absolute() or "\\" in name or any(part in ("", ".", "..") for part in path.parts):
        return None
    if not path.parts or path.parts[0] not in ROOTS or ":" in name:
        return None
    return path


def read_manifest(source: Path | str) -> dict:
    try:
        with zipfile.ZipFile(source) as z:
            manifest = json.loads(z.read(MANIFEST).decode("utf-8"))
    except (OSError, KeyError, zipfile.BadZipFile, ValueError) as e:
        raise ProjectError(f"inkdeus のプロジェクトの zip として読めません: {source}（{e}）") from e
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or not isinstance(manifest.get("files"), list):
        raise ProjectError(f"対応していない形式の zip です（format: {manifest.get('format') if isinstance(manifest, dict) else '?'}）")
    return manifest


def import_project(source: Path | str, name: str, images: Path | str | None = None,
                   masks: Path | str | None = None, root: Path | None = None) -> tuple[Project, dict]:
    """zip からプロジェクトを作る。戻り値は (Project, 要約)。
    元画像を含まない zip のときは images で場所を指定する（無ければ「元画像の場所が未設定」のプロジェクトになる）"""
    validate_name(name)
    root = projects_root() if root is None else Path(root)
    source = Path(source).expanduser().resolve()
    manifest = read_manifest(source)
    target = root / name
    if target.exists():
        raise ProjectError(f"その名前のプロジェクト（またはフォルダ）は既にあります: {target}")
    images_dir = masks_dir = None
    if images:
        images_dir = Path(images).expanduser().resolve()
        if not images_dir.is_dir():
            raise ProjectError(f"画像フォルダが見つかりません: {images_dir}")
    if masks:
        masks_dir = Path(masks).expanduser().resolve()
        if not masks_dir.is_dir():
            raise ProjectError(f"マスクフォルダが見つかりません: {masks_dir}")

    wanted = {e["path"]: e for e in manifest["files"] if isinstance(e, dict) and isinstance(e.get("path"), str)}
    folder_rows = [f for f in manifest.get("folders", []) if isinstance(f, dict) and isinstance(f.get("name"), str)]
    has_sources = {f["name"] for f in folder_rows if f.get("sources") is True}
    work = root / f".{name}.importing"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        with zipfile.ZipFile(source) as z:
            members = {i.filename: i for i in z.infolist() if not i.is_dir() and i.filename != MANIFEST}
            lost = sorted(set(wanted) - set(members))
            if lost:
                raise ProjectError(f"zip に、目録にあるファイルが入っていません: {', '.join(lost[:5])}")
            for rel, entry in wanted.items():
                path = _safe_member(rel)
                if path is None:
                    raise ProjectError(f"zip の中に、受け付けられない名前があります: {rel!r}")
                dst = work / _place(path)
                dst.parent.mkdir(parents=True, exist_ok=True)
                h = hashlib.sha256()
                with z.open(members[rel]) as src, open(dst, "wb") as out:
                    for chunk in iter(lambda: src.read(1 << 20), b""):
                        h.update(chunk)
                        out.write(chunk)
                if h.hexdigest() != entry.get("sha256"):
                    raise ProjectError(f"zip の中身が目録と合いません（壊れているか、書き換えられています）: {rel}")
        (work / "out").mkdir(exist_ok=True)
        lines = [f"name = {_toml_string(name)}",
                 f"created = {_toml_string(str(manifest.get('project', {}).get('created') or ''))}"]
        default = next((f for f in folder_rows if f["name"] == folders_mod.DEFAULT), {})
        local = folders_mod.DEFAULT in has_sources
        src_ref = "source" if local else (str(images_dir) if images_dir else "")
        lines.append(f"source = {_toml_string(src_ref)}")
        mask_ref = "masks" if local and (work / "masks").is_dir() else (str(masks_dir) if masks_dir else None)
        if mask_ref:
            lines.append(f"masks = {_toml_string(mask_ref)}")
        if local and (work / "parts").is_dir():
            lines.append(f"parts = {_toml_string('parts')}")
        lines.append('out = "out"')
        note = str(manifest.get("project", {}).get("note") or "")
        if note:
            lines.append(f"note = {_toml_string(note)}")
        pending = []
        for f in folder_rows:
            if f["name"] == folders_mod.DEFAULT:
                continue
            try:
                folders_mod.validate_name(f["name"])
            except folders_mod.FolderError:
                continue
            base = work / "folders" / f["name"]
            if f["name"] in has_sources and (base / "images").is_dir():
                lines += ["", "[[folders]]", f"name = {_toml_string(f['name'])}",
                          f"images = {_toml_string(f'folders/{f['name']}/images')}"]
                if (base / "masks").is_dir():
                    lines.append(f"masks = {_toml_string(f'folders/{f['name']}/masks')}")
                if (base / "parts").is_dir():
                    lines.append(f"parts = {_toml_string(f'folders/{f['name']}/parts')}")
                if f.get("note"):
                    lines.append(f"note = {_toml_string(str(f['note']))}")
            else:
                pending.append(f["name"])             # 元画像が無い。inkdeus folders add で場所を決める
        backup_mod.atomic_write(work / PROJECT_FILE, ("\n".join(lines) + "\n").encode("utf-8"))
        with open(work / PROJECT_FILE, "rb") as fh:
            tomllib.load(fh)                          # 書いた設定が読めることを確かめる
        work.rename(target)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    project = load_project(str(target), root=root, check=False)
    summary = {"name": name, "path": str(target), "files": len(wanted), "unset": project.unset,
               "sourcesIncluded": sorted(has_sources), "pendingFolders": pending,
               "imagesName": default.get("imagesName"), "from": manifest.get("project", {}).get("name")}
    return project, summary


def _place(path: PurePosixPath) -> Path:
    """zip の中の場所 → プロジェクトの中の場所"""
    parts = path.parts
    if parts[0] == "project":
        return Path(*parts[1:])                       # project/styles.json → styles.json
    if parts[0] == "sources":
        folder, kind, rest = parts[1], parts[2] if len(parts) > 2 else "", parts[3:]
        names = {"images": "source", "masks": "masks", "parts": "parts"}
        if kind not in names or not rest:
            raise ProjectError(f"zip の中に、受け付けられない名前があります: {path.as_posix()!r}")
        if folder == folders_mod.DEFAULT:
            return Path(names[kind], *rest)
        return Path("folders", folder, kind, *rest)
    return Path(*parts)                               # out/… と manga/… はそのまま
