#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""智谱 CogView / 任意「右下角烧录角标」图片的去水印器（v2：几何定位 + 镜像重建）。

v1 的教训：
  - 用「低饱和灰」找药丸不可靠 —— 药丸是半透明叠加，颜色随背景变，米色/竹影背景直接漏检；
  - 用 Laplace 扩散填 130×57 的块，平坦背景完美，但有纹理（木纹、竹叶、草地）就糊成一坨。

v2 做法：
  1. 定位不靠猜颜色，靠几何 —— 角标永远贴在右下角、尺寸与 min(W,H) 成正比；
     用「底右矩形」+「比外围更暗」双重校验，没有角标就原样返回（不会误伤干净图）。
  2. 重建用镜像 —— 把左侧同宽的一条水平翻转贴进来，接缝按行线性混合上方镜像，
     纹理能接上；平坦背景退化成同一块纯色，效果和扩散一致。

用法：
    python dewatermark.py in.jpg out.png
    python dewatermark.py in.jpg out.png --crop
"""
from __future__ import annotations

import sys

import numpy as np
from PIL import Image, ImageFilter

# 角标尺寸/定位（相对 min(W,H)）：实测 1344x768 → 128x57，1024x1024 → 148x70
W_FRAC = 0.28      # 遮罩宽 = 0.28 * min(W,H)（含余量，保证覆盖 + 抗锯齿）
H_FRAC = 0.14      # 遮罩高
MARGIN = 6         # 距右边/下边的留白像素
DARK_GATE = 8.0    # 遮罩区比外围暗多少才判定「确实有角标」
BLEND_ROWS = 10    # 顶边与上方镜像混合的行数
SMOOTH_STD = 12.0  # 镜像源亮度标准差超过它 → 源里有主体/边缘，改用裁切


def watermark_box(w: int, h: int) -> tuple[int, int, int, int]:
    m = min(w, h)
    bw, bh = int(m * W_FRAC), int(m * H_FRAC)
    x1, y1 = w - 1 - MARGIN, h - 1 - MARGIN
    x0, y0 = max(0, x1 - bw), max(0, y1 - bh)
    return x0, y0, x1, y1


def looks_watermarked(a: np.ndarray, box: tuple[int, int, int, int]) -> bool:
    """遮罩区里是否有一块明显比外围暗的像素（角标是「压暗 + 白字」叠加）。没有就不动。

    不能用整块中位数判断：遮罩比药丸大，药丸只占 1/3 面积，中位数会被背景拉平。
    改用「暗像素占比」—— 药丸内部整体压暗 25~35 个灰阶，占比稳定在 0.2 以上。
    """
    x0, y0, x1, y1 = box
    lum = a.mean(2)
    inner = lum[y0:y1 + 1, x0:x1 + 1]
    ring_parts = []
    pad = 12
    if y0 - pad >= 0:
        ring_parts.append(lum[y0 - pad:y0, x0:x1 + 1].ravel())
    if x0 - pad >= 0:
        ring_parts.append(lum[y0:y1 + 1, x0 - pad:x0].ravel())
    if not ring_parts:
        return True
    ring = float(np.median(np.concatenate(ring_parts)))
    darker = float(np.mean(inner < ring - 10.0))
    brighter = float(np.mean(inner > ring + 14.0))
    # 两种形态：半透明药丸 + 白字（浅底 → 偏暗）；直接白字烧在深底上（暗底 → 偏亮）
    return darker > 0.10 or brighter > 0.12


def mirror_patch(a: np.ndarray, box: tuple[int, int, int, int]) -> np.ndarray | None:
    """从左侧镜像取一条贴进来；上部 10 行与上方镜像混合，接住两条接缝。"""
    x0, y0, x1, y1 = box
    h, w, _ = a.shape
    bh, bw = y1 - y0 + 1, x1 - x0 + 1
    if x0 - bw < 0 and y0 - bh < 0:
        return None

    if x0 - bw >= 0:
        left = a[y0:y1 + 1, x0 - bw:x0][:, ::-1]
    else:
        left = np.repeat(a[y0:y1 + 1, x0:x0 + 1], bw, axis=1)
    patch = left.astype(np.float32)

    if y0 - bh >= 0:
        up = a[y0 - bh:y0, x0:x1 + 1][::-1, :].astype(np.float32)
        n = min(BLEND_ROWS, bh)
        for i in range(n):
            t = 0.5 * (1.0 - i / n)          # 顶行 0.5 → 向下渐减
            patch[i] = patch[i] * (1.0 - t) + up[i] * t
    return patch


def clean_image(im: Image.Image) -> tuple[Image.Image, tuple[int, int, int, int] | None, str]:
    im = im.convert("RGB")
    a = np.asarray(im).astype(np.int16)
    h, w, _ = a.shape
    box = watermark_box(w, h)
    if not looks_watermarked(a, box):
        return im, None, "none"

    x0, y0, x1, y1 = box
    patch = mirror_patch(a, box)
    if patch is None:
        patch = np.repeat(a[y0:y1 + 1, max(0, x0 - 1):max(1, x0)], x1 - x0 + 1, axis=1).astype(np.float32)

    # 色调对齐：把镜像块的局部亮度拉回角落背景水平，再羽化贴上。
    # 不做这步，暗角 / 深色背景（夜景、竹影）会被贴进一块亮区，出现明显亮方块。
    band_parts = []
    pad = 10
    if y0 - pad >= 0:
        band_parts.append(a[y0 - pad:y0, x0:x1 + 1].reshape(-1, 3))
    if x0 - pad >= 0:
        band_parts.append(a[y0:y1 + 1, x0 - pad:x0].reshape(-1, 3))
    if band_parts:
        band = np.concatenate(band_parts).astype(np.float32)
        tgt = np.median(band, axis=0)
        cur = np.median(patch.reshape(-1, 3), axis=0)
        patch = np.clip(patch + (tgt - cur) * 0.9, 0.0, 255.0)

    # 镜像源自带结构（有主体、有边缘）时，贴进来就是一块明显补丁 —— 改用裁切。
    # 只有平墙 / 渐变天空 / 纯色背景这类「贴了也看不出来」的情况才走填充。
    if float(patch.mean(2).std()) > SMOOTH_STD:
        cut = h - y0 + 2
        return im.crop((0, 0, w, max(1, h - cut))), box, "crop"

    # 掩膜 + 羽化（内部必须整块替换，只在外圈 1.6px 渐变，否则原角标边缘会透回来）
    mask = np.zeros((h, w), bool)
    mask[y0:y1 + 1, x0:x1 + 1] = True
    soft = Image.fromarray((mask * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.6))
    alpha = np.maximum(np.asarray(soft, dtype=np.float32) / 255.0, mask.astype(np.float32))
    alpha = np.clip(alpha, 0.0, 1.0)[..., None]

    out = a.astype(np.float32).copy()
    out[y0:y1 + 1, x0:x1 + 1] = patch
    blended = out * alpha + a.astype(np.float32) * (1.0 - alpha)
    return Image.fromarray(np.clip(blended, 0, 255).astype(np.uint8)), box, "fill"


def remove(src: str, dst: str) -> None:
    out, box, action = clean_image(Image.open(src))
    out.save(dst)
    if action == "none":
        print("未检测到角标，原样输出 ->", dst)
        return
    x0, y0, x1, y1 = box
    note = "裁切" if action == "crop" else "镜像填充"
    print(f"{note}：角标区 ({x0},{y0})-({x1},{y1})  ->  {out.size}  {dst}")


def crop(src: str, dst: str) -> None:
    im = Image.open(src).convert("RGB")
    a = np.asarray(im).astype(np.int16)
    h, w, _ = a.shape
    box = watermark_box(w, h)
    if not looks_watermarked(a, box):
        im.save(dst)
        print("未检测到角标，原样输出 ->", dst)
        return
    cut = h - box[1] + 2
    out = im.crop((0, 0, w, h - cut))
    out.save(dst)
    print(f"裁掉底部 {cut}px  {im.size} -> {out.size}  {dst}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(1)
    if "--crop" in sys.argv[3:]:
        crop(sys.argv[1], sys.argv[2])
    else:
        remove(sys.argv[1], sys.argv[2])
