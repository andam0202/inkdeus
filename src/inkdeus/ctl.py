"""稼働中の inkdeus サーバを外から操作する CLI（inkdeus ctl …）。出力は JSON。

設定を書き換えると、ブラウザで開いている画面に自動で反映される。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import quote


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8770


class CtlError(Exception):
    def __init__(self, message: str, detail: dict | None = None, code: int = 1):
        super().__init__(message)
        self.detail = detail or {}
        self.code = code


class Client:
    def __init__(self, host: str, port: int):
        self.base = f"http://{host}:{port}"

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            try:
                detail = json.loads(text)
            except ValueError:
                detail = {}
            if e.code == 404:
                raise CtlError("その名前の画像（または設定・バージョン）はありません", detail) from e
            raise CtlError(detail.get("error") or f"サーバがエラーを返しました (HTTP {e.code})", detail) from e
        except (urllib.error.URLError, OSError) as e:
            raise CtlError(f"サーバに接続できません: {self.base}（inkdeus を起動していますか？）") from e

    def target(self, args) -> tuple[str, str, str]:
        """対象の (画像名, バージョン, フォルダ)。
        --folder が無ければ、いまブラウザで開いているフォルダ。--name が無ければ、そのフォルダで開いている画像。
        --version が無ければ、いまブラウザで開いているバージョン（開いていない画像を指したときは既定のバージョン）"""
        given = getattr(args, "name", None)
        version = getattr(args, "version", None)
        folder = getattr(args, "folder", None)
        if given and version is not None and folder is not None:
            return given, version, folder
        state = self.call("GET", "/api/state")
        shown = state.get("currentFolder") or DEFAULT_FOLDER
        folder = folder or shown
        current = state.get("current") if folder == shown else None
        if not given and not current:
            query = f"?folder={quote(folder, safe='')}" if folder != DEFAULT_FOLDER else ""
            names = [i["name"] for i in self.call("GET", "/api/images" + query)["images"]]
            where = "" if folder == shown else f"フォルダ「{folder}」で"
            raise CtlError(f"{where}ブラウザで開いている画像がありません。--name で指定してください", {"images": names})
        name = given or current
        if version is None:
            version = (state.get("currentVersion") or "") if name == current else ""
        return name, version, folder

    def name(self, args) -> tuple[str, str]:
        """バージョンを取らないコマンド用: (画像名, フォルダ)"""
        name, _version, folder = self.target(args)
        return name, folder


DEFAULT_FOLDER = "default"


def _path(kind: str, name: str, version: str = "", folder: str = "", **extra) -> str:
    query = {**({"version": version} if version else {}),
             **({"folder": folder} if folder and folder != DEFAULT_FOLDER else {}), **extra}
    base = f"/api/{kind}/{quote(name, safe='')}"
    return base + ("?" + "&".join(f"{k}={quote(str(v), safe='')}" for k, v in query.items()) if query else "")


def _brief(result: dict) -> dict:
    """変更系の応答から、設定の全文を除いた要約を作る"""
    return {k: v for k, v in result.items() if k != "params"}


def _wait(predicate, timeout: float, interval: float = 0.25):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value is not None:
            return value
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


def cmd_state(c: Client, args) -> dict:
    return c.call("GET", "/api/state")


def cmd_get(c: Client, args) -> dict:
    return c.call("GET", _path("params", *c.target(args), resolve=1))


def cmd_patch(c: Client, args) -> dict:
    try:
        patch = json.loads(args.patch)
    except ValueError as e:
        raise CtlError(f"パッチが JSON として読めません: {e}") from e
    if not isinstance(patch, dict):
        raise CtlError("パッチは JSON オブジェクトである必要があります")
    return _brief(c.call("PATCH", _path("params", *c.target(args)), patch))


def cmd_set(c: Client, args) -> dict:
    try:
        value = json.loads(args.value)
    except ValueError:
        value = args.value                      # JSON でなければ文字列として扱う
    return _brief(c.call("POST", _path("set", *c.target(args)), {"key": args.key, "value": value}))


def cmd_preset(c: Client, args) -> dict:
    return _brief(c.call("POST", _path("style", *c.target(args)), {"style": args.style, "region": args.region}))


def cmd_presets(c: Client, args) -> dict:
    return c.call("GET", "/api/styles")


def cmd_stats(c: Client, args) -> dict:
    name, version, folder = c.target(args)
    result = c.call("GET", _path("stats", name, version, folder))
    if not args.wait:
        return result

    def fresh():
        r = c.call("GET", _path("stats", name, version, folder))
        return r if r["fresh"] else None

    if result["clients"] == 0:
        raise CtlError("ブラウザが開いていないため統計は届きません（設定ファイルは更新済みで、次に開いたとき反映されます）",
                       result, code=2)
    if (result.get("currentFolder") or DEFAULT_FOLDER) != folder:
        raise CtlError(f"ブラウザで開いているフォルダは「{result.get('currentFolder')}」です。"
                       f"先に inkdeus ctl open {name} --folder {folder} してください", result, code=2)
    if result["current"] != name:
        raise CtlError(f"ブラウザで開いているのは {result['current']} です。先に inkdeus ctl open {name} してください",
                       result, code=2)
    if (result.get("currentVersion") or "") != version:
        shown = result.get("currentVersion") or "既定"
        hint = f"inkdeus ctl open {name}" + (f" --version {version}" if version else ' --version ""')
        raise CtlError(f"ブラウザで開いているバージョンは「{shown}」です。先に {hint} してください", result, code=2)
    result = _wait(fresh, args.timeout)
    if result is None:
        raise CtlError(f"{args.timeout:g} 秒待ちましたが、いまの設定に対する統計が届きません",
                       c.call("GET", _path("stats", name, version, folder)), code=3)
    return result


def cmd_open(c: Client, args) -> dict:
    # --version が無ければ「指定なし」（その画像を開いていればそのまま、別の画像からなら既定のバージョン）
    folder = args.folder or c.call("GET", "/api/state").get("currentFolder") or DEFAULT_FOLDER
    r = c.call("POST", "/api/command", {"type": "open", "name": args.image, "version": args.version,
                                        "folder": folder})
    if r["clients"] == 0:
        raise CtlError("ブラウザが開いていないため切り替えられません", r, code=2)

    def switched():
        s = c.call("GET", "/api/state")
        if s.get("current") != args.image or (s.get("currentFolder") or DEFAULT_FOLDER) != folder:
            return None
        return s if args.version is None or (s.get("currentVersion") or "") == args.version else None

    state = _wait(switched, args.timeout)
    if state is None:
        raise CtlError(f"{args.timeout:g} 秒待ちましたが、ブラウザが {args.image} に切り替わりません", code=3)
    return state


def cmd_export(c: Client, args) -> dict:
    name, version, folder = c.target(args)
    before = c.call("GET", _path("exported", name, version, folder)).get("seq", 0)
    r = c.call("POST", "/api/command", {"type": "export", "name": name, "version": version, "folder": folder})
    if r["clients"] == 0:
        raise CtlError("ブラウザが開いていないため書き出せません（書き出しはブラウザが行います）", r, code=2)

    def done():
        e = c.call("GET", _path("exported", name, version, folder))
        return e if e.get("seq", 0) > before else None

    result = _wait(done, args.timeout, interval=0.5)
    if result is None:
        raise CtlError(f"{args.timeout:g} 秒待ちましたが、書き出しが終わりません", code=3)
    return {"folder": folder, "name": name, "version": version, **result}


def cmd_versions(c: Client, args) -> dict:
    name, folder = c.name(args)
    return c.call("GET", _path("versions", name, "", folder))


def cmd_version(c: Client, args) -> dict:
    name, folder = c.name(args)
    if args.action == "new":
        body = {"version": args.version_name}
        if args.source is not None:
            body["copyFrom"] = args.source
        return c.call("POST", _path("versions", name, "", folder), body)
    if args.action == "rename":
        return c.call("PATCH", _path("versions", name, "", folder), {"version": args.old, "to": args.new})
    return c.call("DELETE", _path("versions", name, args.version_name, folder))


def _paint_path(name: str, version: str, folder: str, target: str = "") -> str:
    path = _path("paint", name, version, folder)
    if not target:
        return path
    base, sep, query = path.partition("?")
    return f"{base}/{quote(target, safe='')}{sep}{query}"


def cmd_paint(c: Client, args) -> dict:
    """消しゴム・ペン（人が画面で描いた物）の一覧と消去。描くのは人が画面で行う"""
    name, version, folder = c.target(args)
    if args.action == "list":
        return c.call("GET", _paint_path(name, version, folder))
    return c.call("DELETE", _paint_path(name, version, folder, args.target))


def cmd_folders(c: Client, args) -> dict:
    return c.call("GET", "/api/folders")


def cmd_export_all(c: Client, args) -> dict:
    if args.status:
        return c.call("GET", "/api/batch")
    if args.abort:
        r = c.call("POST", "/api/command", {"type": "export-all-abort"})
        return {"aborting": r["clients"] > 0, "batch": r["batch"]}
    before = c.call("GET", "/api/batch")
    if before.get("running"):
        raise CtlError("一括書き出しは既に進行中です（進み具合は export-all --status、中止は --abort）", before, code=2)
    r = c.call("POST", "/api/command", {"type": "export-all", "force": args.force,
                                        "defaultOnly": args.default_only, "allFolders": args.all_folders})
    if r["clients"] == 0:
        raise CtlError("ブラウザが開いていないため書き出せません（書き出しはブラウザが行います）", r, code=2)

    def started():
        b = c.call("GET", "/api/batch")
        return b if b.get("seq", 0) > before.get("seq", 0) else None

    batch = _wait(started, min(args.timeout, 30.0))
    if batch is None:
        raise CtlError("ブラウザが一括書き出しを始めません（白黒化の画面を開いていますか？）", code=3)
    if not args.wait:
        return batch

    def finished():
        b = c.call("GET", "/api/batch")
        return b if not b.get("running") else None

    batch = _wait(finished, args.timeout, interval=1.0)
    if batch is None:
        raise CtlError(f"{args.timeout:g} 秒待ちましたが、一括書き出しが終わりません",
                       c.call("GET", "/api/batch"), code=3)
    if batch.get("failed"):
        raise CtlError(f"{batch['failed']} 枚の書き出しに失敗しました", batch, code=1)
    return batch


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="inkdeus ctl",
        description="稼働中の inkdeus サーバを操作する。設定の変更は開いているブラウザにすぐ反映される。出力は JSON")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"サーバのポート（既定 {DEFAULT_PORT}）")
    ap.add_argument("--host", default=DEFAULT_HOST, help=f"サーバのホスト（既定 {DEFAULT_HOST}）")
    # 接続先はサブコマンドの後ろにも書ける（inkdeus ctl state --port 8771）。
    # サブコマンド側は「指定されたときだけ」値を持たせ、前に書いた値を既定値で上書きしないようにする
    target = argparse.ArgumentParser(add_help=False)
    target.add_argument("--port", type=int, default=argparse.SUPPRESS, help="サーバのポート（サブコマンドの前でも後ろでも可）")
    target.add_argument("--host", default=argparse.SUPPRESS, help="サーバのホスト（サブコマンドの前でも後ろでも可）")
    sub = ap.add_subparsers(dest="command", required=True, metavar="<command>")

    def add(name, func, help_text, **kw):
        p = sub.add_parser(name, help=help_text, description=help_text, parents=[target], **kw)
        p.set_defaults(func=func)
        return p

    def with_name(p, version=True):
        p.add_argument("--name", help="対象の画像名（省略時はブラウザで開いている画像）")
        p.add_argument("--folder", default=None, metavar="F",
                       help="対象の画像フォルダ（省略時はブラウザで開いているフォルダ。既定のフォルダは default）")
        if version:
            p.add_argument("--version", default=None, metavar="V",
                           help='対象のバージョン（省略時はブラウザで開いているバージョン。既定のバージョンは ""）')
        return p

    add("state", cmd_state, "現在の画像・プロジェクト・フォルダなど")
    with_name(add("get", cmd_get, "設定JSON を表示（保存済みが無ければ既定値）"))
    p = with_name(add(
        "patch", cmd_patch,
        "JSON Merge Patch (RFC 7396) で設定を部分更新する。オブジェクトは統合されるが、"
        "配列（layers）は丸ごと置換になる（1層だけ変えるなら set を使う）。検証に通らなければ何も変えない"))
    p.add_argument("patch", help='例: \'{"regions": {"background": {"levels": {"brightness": 0.25}}}}\'')
    p = with_name(add("set", cmd_set, "1項目だけ変更する（キーはドット区切り。配列は番号）"))
    p.add_argument("key", help="例: regions.background.levels.brightness / regions.person.layers.0.angle")
    p.add_argument("value", help="JSON として読める値（数値・true/false・\"文字列\"）。読めなければ文字列")
    p = with_name(add("preset", cmd_preset, "名前付きスタイルを領域に適用する（名前は別名でも可）"))
    p.add_argument("style", help="スタイル名（一覧は presets）")
    p.add_argument("--region", choices=["person", "background", "both"], default="person")
    add("presets", cmd_presets, "名前付きスタイルの一覧と説明")
    p = with_name(add("stats", cmd_stats, "描画結果の統計（黒率・濃度・ベタ・層ごとの面積率・輝度の分布）"))
    p.add_argument("--wait", action="store_true", help="いまの設定(rev)に対する統計が届くまで待つ")
    p.add_argument("--timeout", type=float, default=60.0)
    p = add("open", cmd_open, "ブラウザで開く画像を切り替える")
    p.add_argument("image", help="画像名")
    p.add_argument("--version", default=None, metavar="V", help='開くバージョン（既定のバージョンは ""）')
    p.add_argument("--folder", default=None, metavar="F", help="画像フォルダ（省略時はいま開いているフォルダ）")
    p.add_argument("--timeout", type=float, default=60.0)
    p = with_name(add("export", cmd_export, "ブラウザに PNG の書き出しを指示し、保存先を返す"))
    p.add_argument("--timeout", type=float, default=300.0)
    add("folders", cmd_folders, "画像フォルダ（画像セット）の一覧と枚数。current がいま開いているフォルダ")
    with_name(add("versions", cmd_versions, "画像のバージョン（設定の違う白黒版）の一覧"), version=False)
    p = add("version", cmd_version, "バージョンを作る（設定を複製）・名前を変える・削除する")
    vsub = p.add_subparsers(dest="action", required=True, metavar="<new|rename|delete>")
    q = with_name(vsub.add_parser("new", parents=[target], help="設定を複製して新しいバージョンを作る"), version=False)
    q.add_argument("version_name", metavar="V", help="新しいバージョンの名前（英数字・かな漢字・_ - で 32 文字まで）")
    q.add_argument("--from", dest="source", default=None, metavar="元",
                   help='複製元のバージョン（省略時は既定のバージョン）')
    q = with_name(vsub.add_parser("rename", parents=[target], help="バージョンの名前を変える"), version=False)
    q.add_argument("old", metavar="旧")
    q.add_argument("new", metavar="新")
    q = with_name(vsub.add_parser("delete", parents=[target], help="バージョンを削除する（設定と PNG を消す）"),
                  version=False)
    q.add_argument("version_name", metavar="V")
    p = add("paint", cmd_paint, "消しゴム・ペン（人が画面で描いた物）の一覧と消去")
    psub = p.add_subparsers(dest="action", required=True, metavar="<list|clear>")
    with_name(psub.add_parser("list", parents=[target], help="保存されている対象（どの層・ベタ・線画・すべて・ペン）の一覧"))
    q = with_name(psub.add_parser("clear", parents=[target],
                                  help="対象の消しゴム（またはペンで描いた物）を消す。開いている画面にも反映される"))
    q.add_argument("target", metavar="<対象|all-targets>",
                   help="layer-<層の id> / solid-person / solid-background / lines / all / pen。all-targets で全部")
    p = add("export-all", cmd_export_all,
            "ブラウザに全画像・全バージョンの書き出しを指示する。各画像は保存済みの設定で、設定の無い画像は"
            "いま画面に出ている設定で書き出す。書き出し済みで設定が変わっていない物は飛ばす")
    p.add_argument("--force", action="store_true", help="書き出し済みの画像も作り直す")
    p.add_argument("--default-only", action="store_true", help="既定のバージョンだけを書き出す")
    p.add_argument("--all-folders", action="store_true", help="すべての画像フォルダを書き出す（既定は、いま開いているフォルダ）")
    p.add_argument("--wait", action="store_true", help="終わるまで待って結果（件数）を返す")
    p.add_argument("--status", action="store_true", help="進み具合だけを表示する（何も始めない）")
    p.add_argument("--abort", action="store_true", help="進行中の一括書き出しを中止する")
    p.add_argument("--timeout", type=float, default=7200.0)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.func(Client(args.host, args.port), args)
    except CtlError as e:
        print(json.dumps({"error": str(e), **e.detail}, ensure_ascii=False, indent=2), file=sys.stderr)
        return e.code
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
