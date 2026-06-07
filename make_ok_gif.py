#!/usr/bin/env python3
"""Slack絵文字用: 赤い「OK」が小刻みに揺れる128x128透過アニメGIFを生成する。"""

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# --- 設定 ---
SIZE = 128          # 出力サイズ(px)
SS = 2              # スーパーサンプリング倍率(縁のアンチエイリアス用)
N = 12              # フレーム数
FPS = 12
TEXT = "OK"
COLOR = (224, 30, 30)   # #E01E1E 赤
AMP = 4             # 揺れ振幅(px, 出力解像度基準)
FREQ_X = 3          # 横方向の振動周波数
FREQ_Y = 2          # 縦方向の振動周波数
OUT = Path(r"D:\voice\ok-shake.gif")

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\Arialbd.ttf",
    r"C:\Windows\Fonts\segoeuib.ttf",
]


def load_font(px: int) -> ImageFont.FreeTypeFont:
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            return ImageFont.truetype(p, px)
    return ImageFont.load_default()


def fit_font_size(text: str, max_w: int, max_h: int) -> ImageFont.FreeTypeFont:
    """max_w/max_h に収まる最大フォントを二分探索的に決める。"""
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
    """高解像度(big x big)で1フレーム描画して返す。"""
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
    """RGBAフレームをP(パレット)化し、透過用インデックスを確保する。
    アルファは閾値で2値化してGIFのフリンジを防ぐ。
    """
    # 縮小
    frame = frame.resize((SIZE, SIZE), Image.LANCZOS)
    r, g, b, a = frame.split()
    # アルファ2値化(>=128で不透明)
    mask = a.point(lambda v: 255 if v >= 128 else 0)

    rgb = Image.merge("RGB", (r, g, b))
    # 透過用に1色分空けて量子化(最大255色)
    p = rgb.quantize(colors=255, method=Image.MEDIANCUT)

    TRANS = 255  # 透過に使うパレットインデックス
    # 透明部分(mask==0)をTRANSインデックスへ
    p.paste(TRANS, (0, 0), Image.eval(mask, lambda v: 255 - v))
    p.info["transparency"] = TRANS
    return p


def main() -> None:
    big = SIZE * SS
    font = fit_font_size(TEXT, int(big * 0.92), int(big * 0.92))
    frames = [to_transparent_p(build_frame(i, big, font)) for i in range(N)]

    frames[0].save(
        OUT,
        save_all=True,
        append_images=frames[1:],
        duration=round(1000 / FPS),
        loop=0,
        transparency=255,
        disposal=2,
        optimize=False,
    )
    kb = OUT.stat().st_size / 1024
    print(f"OK -> {OUT}  {SIZE}x{SIZE}  {N}frames@{FPS}fps  {kb:.1f}KB")


if __name__ == "__main__":
    main()
