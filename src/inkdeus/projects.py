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
                  out: str, note: str, assets: list[dict] | None = None) -> None:
    lines = [
        f"name = {_toml_string(name)}",
        f"created = {_toml_string(created)}",
        f"source = {_toml_string(str(source))}",
    ]
    if masks is not None:
        lines.append(f"masks = {_toml_string(str(masks))}")
    lines.append(f"out = {_toml_string(out)}")
    if note:
        lines.append(f"note = {_toml_string(note)}")
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


def create_project(name: str, images: Path | str, masks: Path | str | None = None, copy: bool = False,
                   note: str = "", force: bool = False, root: Path | None = None) -> tuple[Project, dict]:
    """プロジェクトを作る。戻り値は (Project, 枚数などの要約)"""
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

    path = root / name
    config = path / PROJECT_FILE
    created = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    assets: list[dict] = []
    if config.exists():
        if not force:
            raise ProjectError(f"プロジェクトは既にあります: {name}（設定だけ更新するなら --force）")
        try:
            with open(config, "rb") as f:
                old = tomllib.load(f)
            created = str(old.get("created", created))                    # 作成日時は引き継ぐ
            assets = _raw_assets(old)                                      # 追加の素材ソースも引き継ぐ
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
    _write_config(config, name, created, source, mask_ref, "out", note, assets)

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


def _rewrite_assets(project: Project, data: dict, assets: list[dict]) -> None:
    """設定ファイルを、素材ソースだけ差し替えて書き直す（他の項目は書かれている値のまま）"""
    _write_config(project.config, str(data.get("name") or project.name), str(data.get("created", "")),
                  data["source"], data.get("masks") if isinstance(data.get("masks"), str) else None,
                  str(data.get("out") or "out"), str(data.get("note", "")), assets)


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
    files = list_images(project.source)
    row["images"] = len(files)
    latest = (project.path / PROJECT_FILE).stat().st_mtime
    for p in files:
        if project.masks is not None and (project.masks / f"{p.stem}.png").is_file():
            row["masks"] += 1
        for key, target in (("configured", project.out / f"{p.stem}{PARAMS_SUFFIX}"),
                            ("exported", project.out / f"{p.stem}.png")):
            if target.is_file():
                row[key] += 1
                latest = max(latest, target.stat().st_mtime)
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
