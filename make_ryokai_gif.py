#!/usr/bin/env python3
"""Slack絵文字用: 赤い「了解」が小刻みに揺れる128x128透過アニメGIFを生成する。"""

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# --- 設定 ---
SIZE = 128
SS = 2
N = 12
FPS = 12
TEXT = "了解"
COLOR = (224, 30, 30)   # #E01E1E 赤
AMP = 4
FREQ_X = 3
FREQ_Y = 2
OUT = Path(r"D:\voice\ryokai-shake.gif")

# 日本語太字フォント候補 (path, ttcのindex)
FONT_CANDIDATES = [
    (r"C:\Windows\Fonts\YuGothB.ttc", 0),     # 游ゴシック Bold
    (r"C:\Windows\Fonts\meiryob.ttc", 0),     # メイリオ Bold
    (r"C:\Windows\Fonts\YuGothM.ttc", 0),     # 游ゴシック Medium
    (r"C:\Windows\Fonts\msgothic.ttc", 0),    # MS ゴシック
]


def load_font(px: int) -> ImageFont.FreeTypeFont:
    for p, idx in FONT_CANDIDATES:
        if Path(p).exists():
            return ImageFont.truetype(p, px, index=idx)
    return ImageFont.load_default()


def fit_font_size(text: str, max_w: int, max_h: int) -> ImageFont.FreeTypeFont:
    probe = Image.new("RGBA", (10, 10))
    d = ImageDraw.Draw(probe)
    size = 8
    best = load_font(size)
    while size < 400:
        f = load_font(size)
        l, t, r, b = d.textbbox((0, 0), text, font=f)
        if (r - l) > max_w or (b - t) > max_h:
            break
        best, size = f, size + 2
    return best


def build_frame(i: int, big: int, font: ImageFont.FreeTypeFont) -> Image.Image:
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    phase = 2 * math.pi * i / N
    dx = AMP * SS * math.sin(FREQ_X * phase)
    dy = AMP * SS * math.cos(FREQ_Y * phase)

    l, t, r, b = d.textbbox((0, 0), TEXT, font=font)
    tw, th = r - l, b - t
    cx = (big - tw) / 2 - l + dx
    cy = (big - th) / 2 - t + dy
    d.text((cx, cy), TEXT, font=font, fill=COLOR + (255,))
    return img


def to_transparent_p(frame: Image.Image) -> Image.Image:
    frame = frame.resize((SIZE, SIZE), Image.LANCZOS)
    r, g, b, a = frame.split()
    mask = a.point(lambda v: 255 if v >= 128 else 0)
    rgb = Image.merge("RGB", (r, g, b))
    p = rgb.quantize(colors=255, method=Image.MEDIANCUT)
    TRANS = 255
    p.paste(TRANS, (0, 0), Image.eval(mask, lambda v: 255 - v))
    p.info["transparency"] = TRANS
    return p


def main() -> None:
    big = SIZE * SS
    font = fit_font_size(TEXT, int(big * 0.92), int(big * 0.92))
    frames = [to_transparent_p(build_frame(i, big, font)) for i in range(N)]
    frames[0].save(
        OUT, save_all=True, append_images=frames[1:],
        duration=round(1000 / FPS), loop=0,
        transparency=255, disposal=2, optimize=False,
    )
    kb = OUT.stat().st_size / 1024
    print(f"OK -> {OUT}  {SIZE}x{SIZE}  {N}frames@{FPS}fps  {kb:.1f}KB")


if __name__ == "__main__":
    main()
