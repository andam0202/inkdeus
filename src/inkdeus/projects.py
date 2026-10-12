"""プロジェクト（1つのディレクトリ = 元画像・マスク・出力先の組）の作成・一覧・読み込み。

元画像とマスクは既定では外部フォルダへの参照（絶対パス）で、コピーしない。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .assets import MODES as ASSET_MODES
from .assets import AssetError
from .assets import validate_name as validate_asset_name
from . import folders as folders_mod
from . import parts as parts_mod
from .server import PARAMS_SUFFIX, is_image, read_local_config

PROJECT_FILE = "inkdeus.project.toml"
DEFAULT_ROOT = "~/inkdeus-projects"


class ProjectError(Exception):
    """利用者に見せるエラー"""


@dataclass(frozen=True)
class Project:
    name: str
    path: Path
    source: Path
    masks: Path | None
    out: Path
    created: str
    note: str
    folders: tuple = ()          # 追加の画像フォルダ（folders.FolderDef）。既定のフォルダは source / masks
    parts: Path | None = None    # 既定のフォルダの、部分マスク（髪・肌・服など）の置き場

    @property
    def config(self) -> Path:
        return self.path / PROJECT_FILE

    @property
    def manga(self) -> Path:
        """漫画の構成データ（保存した漫画・書き出したページ）の置き場"""
        return self.path / "manga"

    def info(self) -> dict:
        return {"name": self.name, "path": str(self.path)}


def projects_root(environ: dict | None = None, local_config: Path | None = None) -> Path:
    """プロジェクト置き場。環境変数 INKDEUS_PROJECTS → inkdeus.local.toml の projects_root → 既定"""
    env = os.environ if environ is None else environ
    value = env.get("INKDEUS_PROJECTS") or read_local_config(local_config).get("projects_root") or DEFAULT_ROOT
    return Path(str(value)).expanduser().resolve()


def validate_name(name: str) -> str:
    bad = not name or name != name.strip() or name in (".", "..") or name.startswith(".")
    if bad or any(ch in name for ch in '/\\:*?"<>|') or any(ord(ch) < 32 for ch in name):
        raise ProjectError(f"プロジェクト名として使えません: {name!r}（パス区切りや .. は使えません）")
    return name


def list_images(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if is_image(p))


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)        # TOML の基本文字列は JSON と同じエスケープで書ける


def _write_config(path: Path, name: str, created: str, source: Path | str, masks: Path | str | None,
                  out: str, note: str, assets: list[dict] | None = None, fonts: dict | None = None,
                  folders: list[dict] | None = None, parts: Path | str | None = None) -> None:
    lines = [
        f"name = {_toml_string(name)}",
        f"created = {_toml_string(created)}",
        f"source = {_toml_string(str(source))}",
    ]
    if masks is not None:
        lines.append(f"masks = {_toml_string(str(masks))}")
    if parts is not None:                                  # 既定のフォルダの部分マスクの置き場
        lines.append(f"parts = {_toml_string(str(parts))}")
    lines.append(f"out = {_toml_string(out)}")
    if note:
        lines.append(f"note = {_toml_string(note)}")
    if isinstance(fonts, dict):                            # 漫画の構成で使うローカルフォント（手で書いた物をそのまま残す）
        lines += ["", "[fonts]"]
        for key in ("dirs", "include"):
            if isinstance(fonts.get(key), list):
                lines.append(f"{key} = [{', '.join(_toml_string(str(v)) for v in fonts[key])}]")
        if isinstance(fonts.get("names"), dict) and fonts["names"]:
            lines += ["", "[fonts.names]"]
            lines += [f"{_toml_string(str(k))} = {_toml_string(str(v))}" for k, v in fonts["names"].items()]
    for entry in folders or []:                            # 追加の画像フォルダ（書かれている値のまま残す）
        lines += ["", "[[folders]]"]
        for key in ("name", "images", "masks", "parts", "note"):
            if isinstance(entry.get(key), str) and (entry[key] or key in ("name", "images")):
                lines.append(f"{key} = {_toml_string(entry[key])}")
    for entry in assets or []:                             # 漫画の構成で使う追加の素材ソース
        lines += ["", "[[assets]]"]
        for key in ("name", "path", "mode"):
            if isinstance(entry.get(key), str):
                lines.append(f"{key} = {_toml_string(entry[key])}")
        if isinstance(entry.get("recursive"), bool):
            lines.append(f"recursive = {'true' if entry['recursive'] else 'false'}")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _copy_images(src: Path, dst: Path, only_png_masks: bool = False) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for p in list_images(src):
        if only_png_masks and p.suffix.lower() != ".png":
            continue
        shutil.copy2(p, dst / p.name)


def _parts_dir(path: Path | str | None) -> Path | None:
    if not path:
        return None
    try:
        return parts_mod.check_dir(path)
    except parts_mod.PartsError as e:
        raise ProjectError(str(e)) from e


def create_project(name: str, images: Path | str, masks: Path | str | None = None, copy: bool = False,
                   note: str = "", force: bool = False, root: Path | None = None,
                   parts: Path | str | None = None) -> tuple[Project, dict]:
    """プロジェクトを作る。戻り値は (Project, 枚数などの要約)。
    parts は部分マスクの置き場（参照するだけ。--copy でもコピーしない）"""
    validate_name(name)
    root = projects_root() if root is None else Path(root)
    images = Path(images).expanduser().resolve()
    if not images.is_dir():
        raise ProjectError(f"画像フォルダが見つかりません: {images}")
    files = list_images(images)
    if not files:
        raise ProjectError(f"画像が1枚もありません: {images}")
    masks_dir = None
    if masks:
        masks_dir = Path(masks).expanduser().resolve()
        if not masks_dir.is_dir():
            raise ProjectError(f"マスクフォルダが見つかりません: {masks_dir}")
    parts_ref: Path | str | None = _parts_dir(parts)

    path = root / name
    config = path / PROJECT_FILE
    created = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    assets: list[dict] = []
    fonts: dict | None = None
    extra: list[dict] = []
    if config.exists():
        if not force:
            raise ProjectError(f"プロジェクトは既にあります: {name}（設定だけ更新するなら --force）")
        try:
            with open(config, "rb") as f:
                old = tomllib.load(f)
            created = str(old.get("created", created))                    # 作成日時は引き継ぐ
            assets = _raw_assets(old)                                      # 追加の素材ソースも引き継ぐ
            extra = _raw_folders(old)                                      # 追加の画像フォルダも引き継ぐ
            fonts = old.get("fonts") if isinstance(old.get("fonts"), dict) else None
            if parts_ref is None and isinstance(old.get("parts"), str) and old["parts"]:
                parts_ref = old["parts"]                                   # 部分マスクの置き場も引き継ぐ
        except (OSError, tomllib.TOMLDecodeError):
            pass
    path.mkdir(parents=True, exist_ok=True)

    source: Path = images
    mask_ref: Path | None = masks_dir
    if copy:
        _copy_images(images, path / "source")
        source = Path("source")
        if masks_dir is not None:
            _copy_images(masks_dir, path / "masks", only_png_masks=True)
            mask_ref = Path("masks")
    (path / "out").mkdir(exist_ok=True)
    _write_config(config, name, created, source, mask_ref, "out", note, assets, fonts, extra, parts_ref)

    with_mask = sum(1 for p in files if masks_dir is not None and (masks_dir / f"{p.stem}.png").is_file())
    summary = {"name": name, "path": str(path), "images": len(files),
               "masks": with_mask if masks_dir is not None else None, "copied": copy}
    return load_project(str(path), root=root, check=True), summary


def _raw_assets(data: dict) -> list[dict]:
    raw = data.get("assets")
    return [dict(e) for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []


def _read_raw(project: Project) -> dict:
    try:
        with open(project.config, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ProjectError(f"{project.config} を読めません: {e}") from e


def _raw_folders(data: dict) -> list[dict]:
    raw = data.get("folders")
    return [dict(e) for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []


_KEEP = object()


def _rewrite_assets(project: Project, data: dict, assets: list[dict], folders: list[dict] | None = None,
                    parts=_KEEP) -> None:
    """設定ファイルを、素材ソース（と画像フォルダ・部分マスクの置き場）だけ差し替えて書き直す
    （他の項目は書かれている値のまま）"""
    if parts is _KEEP:
        parts = data.get("parts") if isinstance(data.get("parts"), str) and data["parts"] else None
    _write_config(project.config, str(data.get("name") or project.name), str(data.get("created", "")),
                  data["source"], data.get("masks") if isinstance(data.get("masks"), str) else None,
                  str(data.get("out") or "out"), str(data.get("note", "")), assets,
                  data.get("fonts") if isinstance(data.get("fonts"), dict) else None,
                  _raw_folders(data) if folders is None else folders, parts)


def add_folder(project: Project, name: str, images: Path | str, masks: Path | str | None = None,
               note: str = "", parts: Path | str | None = None) -> dict:
    """プロジェクトに画像フォルダを足す（同じ名前があればエラー）"""
    try:
        folders_mod.validate_name(name)
    except folders_mod.FolderError as e:
        raise ProjectError(str(e)) from e
    images_dir = Path(images).expanduser().resolve()
    if not images_dir.is_dir():
        raise ProjectError(f"画像フォルダが見つかりません: {images_dir}")
    if not list_images(images_dir):
        raise ProjectError(f"画像が1枚もありません: {images_dir}")
    masks_dir = None
    if masks:
        masks_dir = Path(masks).expanduser().resolve()
        if not masks_dir.is_dir():
            raise ProjectError(f"マスクフォルダが見つかりません: {masks_dir}")
    data = _read_raw(project)
    existing = _raw_folders(data)
    if any(e.get("name") == name for e in existing):
        raise ProjectError(f"その名前の画像フォルダは既にあります: {name}")
    entry = {"name": name, "images": str(images_dir)}
    if masks_dir is not None:
        entry["masks"] = str(masks_dir)
    parts_dir = _parts_dir(parts)
    if parts_dir is not None:
        entry["parts"] = str(parts_dir)
    if note:
        entry["note"] = note
    _rewrite_assets(project, data, _raw_assets(data), existing + [entry])
    return entry


def set_parts(project: Project, path: Path | str | None, folder: str | None = None) -> Path | None:
    """画像フォルダ（省略・"default" = 既定のフォルダ）の部分マスクの置き場を登録する。path が None なら登録を外す。
    戻り値は登録した置き場（外したときは None）"""
    parts_dir = _parts_dir(path)
    data = _read_raw(project)
    if folder in (None, "", folders_mod.DEFAULT):
        _rewrite_assets(project, data, _raw_assets(data), parts=str(parts_dir) if parts_dir else None)
        return parts_dir
    existing = _raw_folders(data)
    entry = next((e for e in existing if e.get("name") == folder), None)
    if entry is None:
        known = ", ".join(str(e.get("name")) for e in existing) or "なし"
        raise ProjectError(f"その名前の画像フォルダはありません: {folder}（登録済み: {known}）")
    if parts_dir is None:
        entry.pop("parts", None)
    else:
        entry["parts"] = str(parts_dir)
    _rewrite_assets(project, data, _raw_assets(data), existing)
    return parts_dir


def folder_def(project: Project, folder: str | None = None) -> folders_mod.FolderDef:
    """画像フォルダの定義（省略・"default" = 既定のフォルダ）"""
    if folder in (None, "", folders_mod.DEFAULT):
        return folders_mod.FolderDef(folders_mod.DEFAULT, project.source, project.masks, "", project.parts)
    found = next((d for d in project.folders if d.name == folder), None)
    if found is None:
        known = ", ".join(d.name for d in project.folders) or "なし"
        raise ProjectError(f"その名前の画像フォルダはありません: {folder}（登録済み: {known}）")
    return found


def parts_summary(project: Project, folder: str | None = None) -> dict:
    """部分マスクの集計（部分名と、画像ごとのマスクの有無の数）"""
    d = folder_def(project, folder)
    stems = [p.stem for p in list_images(d.images)] if d.images.is_dir() else []
    return {"project": project.name, "folder": d.name, **parts_mod.summary(d.parts, stems)}


def remove_folder(project: Project, name: str, delete_out: bool = False) -> Path | None:
    """画像フォルダの登録を外す。delete_out で <out>/@<名前>/（設定と書き出し）も消す。戻り値は消した場所"""
    data = _read_raw(project)
    existing = _raw_folders(data)
    kept = [e for e in existing if e.get("name") != name]
    if len(kept) == len(existing):
        known = ", ".join(str(e.get("name")) for e in existing) or "なし"
        raise ProjectError(f"その名前の画像フォルダはありません: {name}（登録済み: {known}）")
    _rewrite_assets(project, data, _raw_assets(data), kept)
    target = folders_mod.out_dir(project.out, folders_mod.validate_name(name))
    if delete_out and target.is_dir() and target.parent == project.out:
        shutil.rmtree(target)
        return target
    return None


def _count(images: Path, masks: Path | None, out: Path) -> dict:
    row = {"images": 0, "masks": 0, "configured": 0, "exported": 0, "missing": not images.is_dir(), "latest": 0.0}
    if row["missing"]:
        return row
    files = list_images(images)
    row["images"] = len(files)
    for p in files:
        if masks is not None and (masks / f"{p.stem}.png").is_file():
            row["masks"] += 1
        for key, target in (("configured", out / f"{p.stem}{PARAMS_SUFFIX}"), ("exported", out / f"{p.stem}.png")):
            if target.is_file():
                row[key] += 1
                row["latest"] = max(row["latest"], target.stat().st_mtime)
    return row


def folder_rows(project: Project) -> list[dict]:
    """画像フォルダごとの枚数（既定が先頭）"""
    defs = [folders_mod.FolderDef(folders_mod.DEFAULT, project.source, project.masks, "", project.parts),
            *project.folders]
    rows = []
    for d in defs:
        out = folders_mod.out_dir(project.out, d.name)
        rows.append({"name": d.name, "images_dir": str(d.images), "masks_dir": str(d.masks) if d.masks else None,
                     "parts_dir": str(d.parts) if d.parts else None,
                     "out": str(out), "note": d.note, **_count(d.images, d.masks, out)})
    return rows


def add_asset_source(project: Project, name: str, path: Path | str, mode: str = "panel",
                     recursive: bool = False) -> dict:
    """プロジェクトに素材ソースを登録する（同じ名前があれば置き換える）"""
    try:
        validate_asset_name(name)
    except AssetError as e:
        raise ProjectError(str(e)) from e
    if mode not in ASSET_MODES:
        raise ProjectError(f"mode は {' / '.join(ASSET_MODES)} のどちらかです: {mode!r}")
    folder = Path(path).expanduser().resolve()
    if not folder.is_dir():
        raise ProjectError(f"素材のフォルダが見つかりません: {folder}")
    data = _read_raw(project)
    entry = {"name": name, "path": str(folder), "mode": mode, "recursive": bool(recursive)}
    assets = [e for e in _raw_assets(data) if e.get("name") != name] + [entry]
    _rewrite_assets(project, data, assets)
    return entry


def remove_asset_source(project: Project, name: str) -> None:
    data = _read_raw(project)
    assets = _raw_assets(data)
    kept = [e for e in assets if e.get("name") != name]
    if len(kept) == len(assets):
        known = ", ".join(str(e.get("name")) for e in assets) or "なし"
        raise ProjectError(f"その名前の素材ソースはありません: {name}（登録済み: {known}）")
    _rewrite_assets(project, data, kept)


def _resolve_dir(base: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return (p if p.is_absolute() else base / p).resolve()


def load_project(name_or_path: str, root: Path | None = None, check: bool = True) -> Project:
    """名前（プロジェクト置き場の下）またはパスからプロジェクトを読む。check=True で参照先も確かめる"""
    root = projects_root() if root is None else Path(root)
    cand = Path(name_or_path).expanduser()
    if cand.is_file() and cand.name == PROJECT_FILE:
        cand = cand.parent
    if (cand / PROJECT_FILE).is_file():
        path = cand.resolve()
    else:
        looks_like_path = cand.is_absolute() or any(sep in name_or_path for sep in ("/", "\\"))
        if looks_like_path:
            raise ProjectError(f"プロジェクトが見つかりません: {name_or_path}（{PROJECT_FILE} がありません）")
        path = root / validate_name(name_or_path)
        if not (path / PROJECT_FILE).is_file():
            known = ", ".join(p["name"] for p in list_projects(root)) or "なし"
            raise ProjectError(f"プロジェクトが見つかりません: {name_or_path}（置き場: {root} / 既存: {known}）")
    try:
        with open(path / PROJECT_FILE, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ProjectError(f"{path / PROJECT_FILE} を読めません: {e}") from e
    if not isinstance(data.get("source"), str):
        raise ProjectError(f"{path / PROJECT_FILE} に source がありません")
    project = Project(
        name=str(data.get("name") or path.name),
        path=path,
        source=_resolve_dir(path, data["source"]),
        masks=_resolve_dir(path, data["masks"]) if isinstance(data.get("masks"), str) else None,
        out=_resolve_dir(path, str(data.get("out") or "out")),
        created=str(data.get("created", "")),
        note=str(data.get("note", "")),
        folders=tuple(folders_mod.parse_entries(data.get("folders"), path)[0]),
        parts=_resolve_dir(path, data["parts"]) if isinstance(data.get("parts"), str) and data["parts"] else None,
    )
    if check:
        if not project.source.is_dir():
            raise ProjectError(f"プロジェクト「{project.name}」の元画像フォルダが見つかりません: {project.source}\n"
                               f"フォルダを戻すか、inkdeus new {project.name} --images <dir> --force で参照先を更新してください")
        if not list_images(project.source):
            raise ProjectError(f"プロジェクト「{project.name}」の元画像フォルダに画像がありません: {project.source}")
        if project.masks is not None and not project.masks.is_dir():
            raise ProjectError(f"プロジェクト「{project.name}」のマスクフォルダが見つかりません: {project.masks}")
    return project


def summarize(project: Project) -> dict:
    """一覧用の枚数（参照先が無い場合は missing=True）"""
    row = {"name": project.name, "path": str(project.path), "source": str(project.source),
           "masks_dir": str(project.masks) if project.masks else None, "out": str(project.out),
           "created": project.created, "note": project.note,
           "images": 0, "masks": 0, "configured": 0, "exported": 0, "updated": None, "missing": False}
    if not project.source.is_dir():
        row["missing"] = True
        return row
    # 枚数は全フォルダの合計。内訳は folders に入れる（フォルダが1つだけなら従来と同じ数）
    per = folder_rows(project)
    latest = (project.path / PROJECT_FILE).stat().st_mtime
    for key in ("images", "masks", "configured", "exported"):
        row[key] = sum(f[key] for f in per)
    latest = max([latest, *(f["latest"] for f in per)])
    row["folders"] = [{k: f[k] for k in ("name", "images", "masks", "configured", "exported", "missing")} for f in per]
    row["updated"] = dt.datetime.fromtimestamp(latest).astimezone().isoformat(timespec="seconds")
    return row


def list_projects(root: Path | None = None) -> list[dict]:
    root = projects_root() if root is None else Path(root)
    if not root.is_dir():
        return []
    rows = []
    for d in sorted(root.iterdir()):
        if not (d / PROJECT_FILE).is_file():
            continue
        try:
            rows.append(summarize(load_project(str(d), root=root, check=False)))
        except ProjectError as e:
            rows.append({"name": d.name, "path": str(d), "error": str(e)})
    return rows
