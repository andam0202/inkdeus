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
                raise CtlError("その名前の画像（または設定）はありません", detail) from e
            raise CtlError(detail.get("error") or f"サーバがエラーを返しました (HTTP {e.code})", detail) from e
        except (urllib.error.URLError, OSError) as e:
            raise CtlError(f"サーバに接続できません: {self.base}（inkdeus を起動していますか？）") from e

    def name(self, given: str | None) -> str:
        """対象の画像名。指定が無ければ、いまブラウザで開いている画像"""
        if given:
            return given
        current = self.call("GET", "/api/state").get("current")
        if not current:
            names = [i["name"] for i in self.call("GET", "/api/images")["images"]]
            raise CtlError("ブラウザで開いている画像がありません。--name で指定してください", {"images": names})
        return current


def _path(kind: str, name: str) -> str:
    return f"/api/{kind}/{quote(name, safe='')}"


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
    return c.call("GET", _path("params", c.name(args.name)) + "?resolve=1")


def cmd_patch(c: Client, args) -> dict:
    try:
        patch = json.loads(args.patch)
    except ValueError as e:
        raise CtlError(f"パッチが JSON として読めません: {e}") from e
    if not isinstance(patch, dict):
        raise CtlError("パッチは JSON オブジェクトである必要があります")
    return _brief(c.call("PATCH", _path("params", c.name(args.name)), patch))


def cmd_set(c: Client, args) -> dict:
    try:
        value = json.loads(args.value)
    except ValueError:
        value = args.value                      # JSON でなければ文字列として扱う
    return _brief(c.call("POST", _path("set", c.name(args.name)), {"key": args.key, "value": value}))


def cmd_preset(c: Client, args) -> dict:
    return _brief(c.call("POST", _path("style", c.name(args.name)), {"style": args.style, "region": args.region}))


def cmd_presets(c: Client, args) -> dict:
    return c.call("GET", "/api/styles")


def cmd_stats(c: Client, args) -> dict:
    name = c.name(args.name)
    result = c.call("GET", _path("stats", name))
    if not args.wait:
        return result

    def fresh():
        r = c.call("GET", _path("stats", name))
        return r if r["fresh"] else None

    if result["clients"] == 0:
        raise CtlError("ブラウザが開いていないため統計は届きません（設定ファイルは更新済みで、次に開いたとき反映されます）",
                       result, code=2)
    if result["current"] != name:
        raise CtlError(f"ブラウザで開いているのは {result['current']} です。先に inkdeus ctl open {name} してください",
                       result, code=2)
    result = _wait(fresh, args.timeout)
    if result is None:
        raise CtlError(f"{args.timeout:g} 秒待ちましたが、いまの設定に対する統計が届きません",
                       c.call("GET", _path("stats", name)), code=3)
    return result


def cmd_open(c: Client, args) -> dict:
    r = c.call("POST", "/api/command", {"type": "open", "name": args.image})
    if r["clients"] == 0:
        raise CtlError("ブラウザが開いていないため切り替えられません", r, code=2)

    def switched():
        s = c.call("GET", "/api/state")
        return s if s.get("current") == args.image else None

    state = _wait(switched, args.timeout)
    if state is None:
        raise CtlError(f"{args.timeout:g} 秒待ちましたが、ブラウザが {args.image} に切り替わりません", code=3)
    return state


def cmd_export(c: Client, args) -> dict:
    name = c.name(args.name)
    before = c.call("GET", _path("exported", name)).get("seq", 0)
    r = c.call("POST", "/api/command", {"type": "export", "name": name})
    if r["clients"] == 0:
        raise CtlError("ブラウザが開いていないため書き出せません（書き出しはブラウザが行います）", r, code=2)

    def done():
        e = c.call("GET", _path("exported", name))
        return e if e.get("seq", 0) > before else None

    result = _wait(done, args.timeout, interval=0.5)
    if result is None:
        raise CtlError(f"{args.timeout:g} 秒待ちましたが、書き出しが終わりません", code=3)
    return {"name": name, **result}


def cmd_export_all(c: Client, args) -> dict:
    if args.status:
        return c.call("GET", "/api/batch")
    if args.abort:
        r = c.call("POST", "/api/command", {"type": "export-all-abort"})
        return {"aborting": r["clients"] > 0, "batch": r["batch"]}
    before = c.call("GET", "/api/batch")
    if before.get("running"):
        raise CtlError("一括書き出しは既に進行中です（進み具合は export-all --status、中止は --abort）", before, code=2)
    r = c.call("POST", "/api/command", {"type": "export-all", "force": args.force})
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

    def with_name(p):
        p.add_argument("--name", help="対象の画像名（省略時はブラウザで開いている画像）")
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
    p.add_argument("--timeout", type=float, default=60.0)
    p = with_name(add("export", cmd_export, "ブラウザに PNG の書き出しを指示し、保存先を返す"))
    p.add_argument("--timeout", type=float, default=300.0)
    p = add("export-all", cmd_export_all,
            "ブラウザに全画像の書き出しを指示する。各画像は保存済みの設定で、設定の無い画像は"
            "いま画面に出ている設定で書き出す。書き出し済みで設定が変わっていない画像は飛ばす")
    p.add_argument("--force", action="store_true", help="書き出し済みの画像も作り直す")
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
