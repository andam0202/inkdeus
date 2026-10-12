"""テスト・デモ用の合成画像を作る（バイナリはコミットせず、毎回ここで生成する）

    uv run python tests/synth.py demo_images     # デモ用に images/ と masks/ を書き出す
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image


def flat(w: int, h: int, value: int) -> np.ndarray:
    return np.full((h, w, 3), value, np.uint8)


def gradient(w: int, h: int) -> np.ndarray:
    """左が黒、右が白の横方向の線形グラデーション"""
    row = np.linspace(0, 255, w).round().astype(np.uint8)
    return np.repeat(np.repeat(row[None, :, None], h, axis=0), 3, axis=2)


def halves(w: int, h: int) -> np.ndarray:
    """左半分が白、右半分が黒"""
    img = np.full((h, w, 3), 255, np.uint8)
    img[:, w // 2:] = 0
    return img


def figure_mask(w: int, h: int) -> np.ndarray:
    """人物っぽいシルエット（頭の円＋胴の楕円）。True=人物"""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    cx = w * 0.5
    head = (xx - cx) ** 2 + (yy - h * 0.30) ** 2 <= (h * 0.14) ** 2
    body = ((xx - cx) / (w * 0.20)) ** 2 + ((yy - h * 0.78) / (h * 0.36)) ** 2 <= 1.0
    return head | body


def part_masks(w: int, h: int) -> dict[str, np.ndarray]:
    """人物のシルエット（figure_mask）を 髪・肌・服 に分けた部分マスク。True=その部分。
    互いに重ならず、どの部分にも属さない人物の領域（あご・胴の下の方）も残る"""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    mask = figure_mask(w, h)
    head = (xx - w * 0.5) ** 2 + (yy - h * 0.30) ** 2 <= (h * 0.14) ** 2
    hair = mask & (yy < h * 0.24)
    skin = head & ~hair & (yy < h * 0.40)
    clothes = mask & ~head & (yy >= h * 0.50) & (yy < h * 0.90)
    return {"hair": hair, "skin": skin, "clothes": clothes}


def save_parts(root: Path, stem: str, parts: dict[str, np.ndarray]) -> None:
    """部分マスクを <root>/<部分名>/<stem>.png に書く（8bit、255 = その部分）"""
    for name, arr in parts.items():
        save(root / name / f"{stem}.png", arr)


def scene(w: int, h: int) -> tuple[np.ndarray, np.ndarray]:
    """背景（グラデーション＋矩形）の前に人物っぽいシルエットを置いたカラー画像とそのマスク"""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    img = np.empty((h, w, 3), np.float32)
    img[..., 0] = 90 + 120 * xx / w          # 背景: 横方向に色が変わる
    img[..., 1] = 110 + 90 * yy / h
    img[..., 2] = 200 - 110 * xx / w
    img[int(h * 0.55):int(h * 0.9), int(w * 0.05):int(w * 0.25)] = (40, 50, 60)     # 暗い箱
    img[int(h * 0.10):int(h * 0.30), int(w * 0.72):int(w * 0.95)] = (245, 240, 225)  # 明るい窓
    mask = figure_mask(w, h)
    cx = w * 0.5
    # 人物: 左上から光が当たったような明暗（明るい肌 → 影 → 濃い影）
    shade = np.clip(((xx - cx) / (w * 0.2) + (yy - h * 0.3) / (h * 0.9)) * 0.5 + 0.5, 0.0, 1.0)
    tone = 235 - 215 * shade
    for c, k in enumerate((1.0, 0.92, 0.86)):
        img[..., c] = np.where(mask, tone * k, img[..., c])
    hair = mask & (yy < h * 0.24)
    img[hair] = (25, 22, 30)
    return np.clip(img, 0, 255).astype(np.uint8), mask


def save(path: Path, arr: np.ndarray, fast: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if arr.dtype == bool:
        arr = arr.astype(np.uint8) * 255
    Image.fromarray(arr).save(path, compress_level=1 if fast else 6)
    return path


def write_demo(root: Path) -> None:
    img, mask = scene(1280, 960)
    save(root / "images" / "scene.png", img)
    save(root / "masks" / "scene.png", mask)
    save(root / "images" / "gradient.png", gradient(1280, 320))
    print(f"wrote demo images to {root}")


if __name__ == "__main__":
    write_demo(Path(sys.argv[1] if len(sys.argv) > 1 else "demo_images"))
