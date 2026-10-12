"""漫画のページ（PNG）を、印刷向けの PDF 1つにまとめる。

画像は可逆（Flate）で埋め込む。Pillow の PDF 出力はグレースケール・カラーを JPEG にしてしまうので使わず、
必要な分だけの小さな PDF の書き出しをここに持つ（依存は Pillow と標準ライブラリだけ）。

  - ページの大きさ = 仕上がり（TrimBox）＋ 塗り足し。MediaBox と BleedBox は「仕上がり＋塗り足し」
  - 絵は「仕上がり＋塗り足し」の枠に、収める（余白は白）か埋める（はみ出しは切る）かで置く
  - 色はグレースケール（8bit）・白黒2値（1bit）・そのまま（RGB）
  - 綴じ方向は /ViewerPreferences /Direction、見開きの初期表示は /PageLayout
"""

from __future__ import annotations

import io
import math
import re
import warnings
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image

MM_PER_INCH = 25.4
PT_PER_INCH = 72.0
SIZES = {"B5": (182.0, 257.0), "A5": (148.0, 210.0), "A4": (210.0, 297.0), "B6": (128.0, 182.0)}
FITS = ("contain", "cover")
COLORS = ("gray", "bw", "rgb")
BINDINGS = ("right", "left")
LAYOUTS = {"single": "SinglePage", "spread": "TwoPageLeft", "spread-cover": "TwoPageRight"}
DPI_RANGE = (72.0, 2400.0)
SIDE_MM_RANGE = (10.0, 2000.0)
BLEED_RANGE = (0.0, 20.0)
MAX_PIXELS = 400_000_000          # 1ページの画素数の上限（これを超える設定は断る）
MAX_PAGES = 9999
NEAR_PX = 2                       # 拡大縮小後の大きさとの差がこの画素数以内なら、拡大縮小しない（絵をぼかさない）


class PdfError(ValueError):
    """利用者に見せる理由つきのエラー"""


@dataclass(frozen=True)
class Settings:
    size: str = "B5"               # SIZES の名前か "custom"
    widthMm: float = 182.0         # 仕上がりの幅・高さ（縦置きで書く。横長のページでは入れ替わる）
    heightMm: float = 257.0
    matchAspect: bool = False      # 仕上がりをページの縦横比に合わせる（指定の大きさに余白なしで収まる最大）
    dpi: float = 600.0
    bleedMm: float = 0.0
    fit: str = "contain"
    color: str = "gray"
    threshold: int = 128           # 白黒2値のしきい値（これ未満が黒）
    binding: str = "right"
    layout: str = "single"
    title: str = ""

    def info(self) -> dict:
        return asdict(self)


def _number(body: dict, key: str, default: float, lo: float, hi: float, label: str) -> float:
    value = body.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PdfError(f"{label}は数値で指定してください")
    if not lo <= value <= hi:
        raise PdfError(f"{label}は {lo:g}〜{hi:g} の範囲で指定してください")
    return float(value)


def _choice(body: dict, key: str, default: str, choices, label: str) -> str:
    value = body.get(key, default)
    if value not in choices:
        raise PdfError(f"{label}は {' / '.join(choices)} のどれかです")
    return value


def normalize(body: dict | None) -> Settings:
    """入力を確かめて Settings にする（おかしければ PdfError）"""
    body = {} if body is None else body
    if not isinstance(body, dict):
        raise PdfError("設定は JSON オブジェクトで指定してください")
    size = body.get("size", "B5")
    if isinstance(size, str) and size.upper() in SIZES:
        size = size.upper()
        width, height = SIZES[size]
    elif size == "custom":
        width = _number(body, "widthMm", 0.0, *SIDE_MM_RANGE, "仕上がりの幅(mm)")
        height = _number(body, "heightMm", 0.0, *SIDE_MM_RANGE, "仕上がりの高さ(mm)")
    else:
        raise PdfError(f"仕上がりサイズは {' / '.join(SIZES)} / custom のどれかです")
    threshold = body.get("threshold", 128)
    if isinstance(threshold, bool) or not isinstance(threshold, int) or not 1 <= threshold <= 254:
        raise PdfError("しきい値は 1〜254 の整数で指定してください")
    title = body.get("title", "")
    if not isinstance(title, str) or len(title) > 200:
        raise PdfError("題名は 200 文字までの文字列で指定してください")
    settings = Settings(
        size=size, widthMm=width, heightMm=height, matchAspect=body.get("matchAspect") is True,
        dpi=_number(body, "dpi", 600.0, *DPI_RANGE, "解像度(dpi)"),
        bleedMm=_number(body, "bleedMm", 0.0, *BLEED_RANGE, "塗り足し(mm)"),
        fit=_choice(body, "fit", "contain", FITS, "はめ込み方"),
        color=_choice(body, "color", "gray", COLORS, "色"),
        threshold=threshold,
        binding=_choice(body, "binding", "right", BINDINGS, "綴じ方向"),
        layout=_choice(body, "layout", "single", tuple(LAYOUTS), "見開き表示"),
        title=title)
    w, h = pixel_size(settings.widthMm + settings.bleedMm * 2, settings.heightMm + settings.bleedMm * 2, settings.dpi)
    if w * h > MAX_PIXELS:
        raise PdfError(f"1ページが {w}×{h} px になります。大きすぎるので、解像度か仕上がりサイズを下げてください")
    return settings


def parse_pages(spec: str | None, total: int) -> list[int]:
    """ページの範囲（例 "1-8,10"）を、昇順で重複のない番号の並びにする。空・None は全ページ"""
    if spec is None or not str(spec).strip():
        return list(range(1, total + 1))
    found: set[int] = set()
    for part in str(spec).split(","):
        m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", part)
        if m is None:
            raise PdfError(f"ページの範囲として読めません: {part.strip() or '（空）'}（例: 1-8,10）")
        first, last = int(m.group(1)), int(m.group(2) or m.group(1))
        if first < 1 or last < first:
            raise PdfError(f"ページの範囲がおかしいです: {part.strip()}")
        if last > total:
            raise PdfError(f"{last} ページはありません（全 {total} ページ）")
        found.update(range(first, last + 1))
    return sorted(found)


def pixel_size(width_mm: float, height_mm: float, dpi: float) -> tuple[int, int]:
    return (max(1, round(width_mm / MM_PER_INCH * dpi)), max(1, round(height_mm / MM_PER_INCH * dpi)))


def trim_size(settings: Settings, width: int, height: int) -> tuple[float, float]:
    """そのページ（width×height px の絵）の仕上がりの大きさ(mm)。
    絵と用紙の向きが違えば用紙の縦横を入れ替え、matchAspect なら絵の縦横比に合わせて縮める"""
    w, h = settings.widthMm, settings.heightMm
    if (width > height) != (w > h):
        w, h = h, w
    if settings.matchAspect:
        scale = min(w / width, h / height)
        w, h = width * scale, height * scale
    return w, h


def geometry(settings: Settings, width: int, height: int) -> dict:
    """ページの寸法（mm と pt）と、埋め込む画像の画素数"""
    trim_w, trim_h = trim_size(settings, width, height)
    bleed = settings.bleedMm
    full_w, full_h = trim_w + bleed * 2, trim_h + bleed * 2
    px_w, px_h = pixel_size(full_w, full_h, settings.dpi)
    return {"trimMm": [trim_w, trim_h], "fullMm": [full_w, full_h], "bleedMm": bleed,
            "pixels": [px_w, px_h]}


def _pt(mm: float) -> float:
    return mm / MM_PER_INCH * PT_PER_INCH


def _open(data: bytes) -> Image.Image:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", Image.DecompressionBombWarning)
        try:
            image = Image.open(io.BytesIO(data))
            if image.width * image.height > MAX_PIXELS:
                raise PdfError(f"画像が大きすぎます（{image.width}×{image.height} px）")
            image.load()
        except PdfError:
            raise
        except Exception as e:  # noqa: BLE001 — 壊れた PNG・大きすぎる画像
            raise PdfError(f"画像として読めません: {e}") from e
    return image


def compose(image: Image.Image, settings: Settings) -> tuple[Image.Image, dict]:
    """絵を「仕上がり＋塗り足し」の画素数の白い台紙へ置き、指定の色にする"""
    geo = geometry(settings, image.width, image.height)
    px_w, px_h = geo["pixels"]
    mode = "RGB" if settings.color == "rgb" else "L"
    if image.mode in ("RGBA", "LA", "P") or "transparency" in image.info:
        rgba = image.convert("RGBA")                       # 透けている所は白にする
        flat = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        flat.alpha_composite(rgba)
        image = flat
    image = image.convert(mode)
    pick = max if settings.fit == "cover" else min
    scale = pick(px_w / image.width, px_h / image.height)
    new_w, new_h = max(1, round(image.width * scale)), max(1, round(image.height * scale))
    resampled = abs(new_w - image.width) > NEAR_PX or abs(new_h - image.height) > NEAR_PX
    if resampled:
        image = image.resize((new_w, new_h), Image.LANCZOS)
    page = Image.new(mode, (px_w, px_h), 255 if mode == "L" else (255, 255, 255))
    # 中央に置く（台紙より大きい分は切られる）
    page.paste(image, ((px_w - image.width) // 2, (px_h - image.height) // 2))
    if settings.color == "bw":
        page = page.point(lambda v, t=settings.threshold: 255 if v >= t else 0).convert("1", dither=Image.NONE)
    geo.update(source=[new_w if resampled else image.width, new_h if resampled else image.height],
               resampled=resampled, scale=scale)
    return page, geo


def prepare_page(data: bytes, settings: Settings, dest: Path) -> dict:
    """1ページ分の PNG を PDF に埋め込む形（圧縮済みの画素の並び）にして dest へ書く。戻り値はページの情報"""
    page, geo = compose(_open(data), settings)
    raw = page.tobytes()                                   # "1" は行ごとにバイト境界で詰めた 1bit（1 = 白）
    packer = zlib.compressobj(6)
    with open(dest, "wb") as f:
        step = 1 << 22
        for i in range(0, len(raw), step):
            f.write(packer.compress(raw[i:i + step]))
        f.write(packer.flush())
    geo.update(file=dest.name, bytes=dest.stat().st_size,
               colorSpace="DeviceRGB" if page.mode == "RGB" else "DeviceGray",
               bits=1 if page.mode == "1" else 8)
    return geo


def _text(value: str) -> bytes:
    """PDF の文字列（UTF-16BE の16進。日本語の題名のため）"""
    return b"<" + ("﻿" + value).encode("utf-16-be").hex().upper().encode("ascii") + b">"


def _num(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


def write_pdf(dest: Path, pages: list[tuple[dict, Path]], settings: Settings) -> dict:
    """pages（ページの情報と、圧縮済みの画素のファイル）を順に並べた PDF を書く"""
    if not pages:
        raise PdfError("ページが1つも届いていません")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    offsets: dict[int, int] = {}
    count = len(pages)
    # オブジェクト番号: 1 = Catalog、2 = Pages、3 = Info、以降は1ページにつき 3 つ（Page・内容・画像）
    first = 4
    total_objects = first + count * 3
    with open(tmp, "wb") as f:
        def obj(number: int, body: bytes) -> None:
            offsets[number] = f.tell()
            f.write(f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n")

        f.write(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
        direction = "R2L" if settings.binding == "right" else "L2R"
        obj(1, (f"<< /Type /Catalog /Pages 2 0 R /PageLayout /{LAYOUTS[settings.layout]} "
                f"/ViewerPreferences << /Direction /{direction} >> >>").encode("ascii"))
        kids = " ".join(f"{first + i * 3} 0 R" for i in range(count))
        obj(2, f"<< /Type /Pages /Count {count} /Kids [{kids}] >>".encode("ascii"))
        obj(3, b"<< /Producer " + _text("inkdeus") + (b" /Title " + _text(settings.title) if settings.title else b"")
            + b" >>")
        for i, (meta, stream) in enumerate(pages):
            page_no, content_no, image_no = first + i * 3, first + i * 3 + 1, first + i * 3 + 2
            full_w, full_h = (_pt(v) for v in meta["fullMm"])
            bleed = _pt(meta["bleedMm"])
            box = f"[0 0 {_num(full_w)} {_num(full_h)}]"
            trim = f"[{_num(bleed)} {_num(bleed)} {_num(full_w - bleed)} {_num(full_h - bleed)}]"
            obj(page_no, (f"<< /Type /Page /Parent 2 0 R /MediaBox {box} /BleedBox {box} /TrimBox {trim} "
                          f"/Resources << /XObject << /Im0 {image_no} 0 R >> >> /Contents {content_no} 0 R >>"
                          ).encode("ascii"))
            content = f"q {_num(full_w)} 0 0 {_num(full_h)} 0 0 cm /Im0 Do Q".encode("ascii")
            obj(content_no, f"<< /Length {len(content)} >>\nstream\n".encode("ascii") + content + b"\nendstream")
            px_w, px_h = meta["pixels"]
            offsets[image_no] = f.tell()
            f.write((f"{image_no} 0 obj\n<< /Type /XObject /Subtype /Image /Width {px_w} /Height {px_h} "
                     f"/ColorSpace /{meta['colorSpace']} /BitsPerComponent {meta['bits']} /Filter /FlateDecode "
                     f"/Length {stream.stat().st_size} >>\nstream\n").encode("ascii"))
            with open(stream, "rb") as src:
                while chunk := src.read(1 << 20):
                    f.write(chunk)
            f.write(b"\nendstream\nendobj\n")
        xref = f.tell()
        f.write(f"xref\n0 {total_objects}\n".encode("ascii"))
        f.write(b"0000000000 65535 f \n")
        for number in range(1, total_objects):
            f.write(f"{offsets[number]:010d} 00000 n \n".encode("ascii"))
        f.write(f"trailer\n<< /Size {total_objects} /Root 1 0 R /Info 3 0 R >>\nstartxref\n{xref}\n%%EOF\n"
                .encode("ascii"))
    tmp.replace(dest)
    return {"path": str(dest), "pages": count, "bytes": dest.stat().st_size}
