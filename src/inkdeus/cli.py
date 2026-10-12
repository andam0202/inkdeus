"""コマンドライン入口: uv run inkdeus <画像 or フォルダ>"""

from __future__ import annotations

import argparse
from pathlib import Path

from .server import create_app, resolve_owner


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="inkdeus",
        description="カラーのイラストを白黒の漫画原稿風に手動調整するエディタ（ブラウザで開く）")
    ap.add_argument("input", help="画像ファイル、または画像の入ったフォルダ")
    ap.add_argument("--mask-dir", help="人物マスクのフォルダ（<画像と同名>.png、白=人物）")
    ap.add_argument("--out-dir", help="書き出しと設定JSONの保存先（既定: <入力フォルダ>/inkdeus_out）")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--owner", action="store_true",
                    help="所有者モード（透かしのオン/オフスイッチを出す）")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    owner = resolve_owner(flag=args.owner)
    try:
        app = create_app(Path(args.input), args.mask_dir, args.out_dir, owner=owner)
    except (FileNotFoundError, ValueError) as e:
        print(f"エラー: {e}")
        return 1
    lib = app.config["INKDEUS_LIBRARY"]
    print(f"inkdeus: http://{args.host}:{args.port}/")
    print(f"  入力: {lib.folder}{'/' + lib.single if lib.single else ''}")
    print(f"  出力: {lib.out_dir}")
    if lib.mask_dir:
        print(f"  マスク: {lib.mask_dir}")
    if owner:
        print("  所有者モード")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("  注意: ループバック以外で待ち受けます（同じネットワークから読み書きできます）")
    app.run(host=args.host, port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
