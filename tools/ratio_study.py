#!/usr/bin/env python3
"""压缩方案对比测量：在当前抓帧上跑各种变换 + 编码，输出压缩率与 PSNR。

用法：python3 tools/ratio_study.py frame.pgm

输出用来填 docs/TEST_REPORT.md 的压缩表。所有方案都在主机上按硬件同款算法实现，
所以数字可直接对应到 RTL 的行为。
"""
import sys
import numpy as np
from PIL import Image

sys.path.insert(0, __file__.rsplit('/', 1)[0])
import rle_codec as R
import lz77_codec as L


def psnr(a, b):
    mse = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    return 99.0 if mse < 1e-9 else 10 * np.log10(255.0 ** 2 / mse)


def delta_1d(v):
    d = np.empty_like(v)
    d[0] = (v[0] - 128) % 256
    d[1:] = (v[1:] - v[:-1]) % 256
    return d.astype(np.uint8)


def undelta_1d(d):
    return (128 + np.cumsum(d.astype(np.int64))) % 256


def gauss(img, sigma=0.85):
    from scipy import ndimage
    return np.clip(ndimage.gaussian_filter(img.astype(np.float64), sigma, mode='nearest'), 0, 255)


def rle_bit7(d):
    """bit7 格式：0xxxxxxx=字面量段，1xxxxxxx=重复段(val+1 次, 后跟 1 字节值)。"""
    out = bytearray()
    lit = bytearray()
    i, n = 0, len(d)

    def flush():
        nonlocal lit
        while lit:
            c = lit[:128]
            out.append(len(c) - 1)
            out.extend(c)
            lit = lit[len(c):]

    while i < n:
        j = i
        while j + 1 < n and d[j + 1] == d[i] and j - i < 127:
            j += 1
        run = j - i + 1
        if run >= 3:
            flush()
            out.append(0x80 | (run - 1))
            out.append(int(d[i]))
            i = j + 1
        else:
            for k in range(run):
                lit.append(int(d[i]))
            i += run
            if len(lit) >= 128:
                flush()
    flush()
    return bytes(out)


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    img = np.asarray(Image.open(sys.argv[1]).convert('L'))
    flat = img.ravel().astype(np.int32)
    gau = np.round(gauss(img)).ravel().astype(np.int32)

    print('帧 %dx%d  均值 %.1f  标准差 %.1f' % (img.shape[1], img.shape[0], img.mean(), img.std()))
    print()
    print('%-34s %10s %9s %8s' % ('方案', '压缩后', '压缩率', 'PSNR'))
    print('-' * 66)

    for name, v in [('原始灰度', flat), ('3x3 高斯', gau)]:
        d = delta_1d(v)
        ref = flat if name == '原始灰度' else flat
        for enc_name, fn in [('差分RLE-v1', R.rle_encode), ('差分RLE-bit7', rle_bit7)]:
            e = fn(d.tobytes())
            print('%-34s %10d %8.2f%% %7.1f' % (
                '%s %s' % (name, enc_name), len(e), (1 - len(e) / len(v)) * 100, psnr(ref, undelta_1d(d))))
        e = L.lz77_encode(d.tobytes())
        print('%-34s %10d %8.2f%% %7.1f' % (
            '%s 差分LZ77' % name, len(e), (1 - len(e) / len(v)) * 100, psnr(ref, undelta_1d(d))))


if __name__ == '__main__':
    main()
