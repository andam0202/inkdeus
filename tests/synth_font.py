"""テスト用のフォントを合成する（実在のフォントはコミットしない。fontTools はテスト用の依存）"""

from __future__ import annotations

from pathlib import Path


def make_font(path: Path, family: str, style: str = "Regular", japanese: str | None = None,
              chars: str = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 あいうnew") -> Path:
    """全部の文字が「塗りつぶしの四角」のフォント（画素で見分けられる）"""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    def box():
        pen = TTGlyphPen(None)
        pen.moveTo((50, -200))
        pen.lineTo((50, 800))
        pen.lineTo((950, 800))
        pen.lineTo((950, -200))
        pen.closePath()
        return pen.glyph()

    codes = sorted({ord(c) for c in chars})
    names = [".notdef"] + [f"u{c:04X}" for c in codes]
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(names)
    fb.setupCharacterMap({c: f"u{c:04X}" for c in codes})
    fb.setupGlyf({n: box() for n in names})
    fb.setupHorizontalMetrics({n: (1000, 50) for n in names})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    family_names = {"en": family}
    if japanese:
        family_names["ja"] = japanese
    fb.setupNameTable({"familyName": family_names, "styleName": {"en": style}})
    fb.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    fb.setupPost()
    path.parent.mkdir(parents=True, exist_ok=True)
    fb.save(str(path))
    return path
