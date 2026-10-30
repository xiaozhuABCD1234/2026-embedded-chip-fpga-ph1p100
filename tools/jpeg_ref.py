#!/usr/bin/env python3
"""JPEG 基线（8x8 DCT + 量化 + DC 差分 + AC 之字形 RLE + 规范哈夫曼）参考实现。

用途：验证「硬件 JPEG 编码器」能拿到多少压缩率/画质，给 RTL 设计定参数。
只用灰度（Y）；encode_entropy() 输出纯熵流，交给 jpeg_file.build_jpeg() 才是
标准 JPEG 文件。

用法：python3 tools/jpeg_ref.py frame.pgm   # 打印 q=30..90 的字节数/压缩率/PSNR
"""
import sys

import numpy as np

ZZ = np.array([
     0,  1,  8, 16,  9,  2,  3, 10,
    17, 24, 32, 25, 18, 11,  4,  5,
    12, 19, 26, 33, 40, 48, 41, 34,
    27, 20, 13,  6,  7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36,
    29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46,
    53, 60, 61, 54, 47, 55, 62, 63])

LUMA_Q = np.array([
    16, 11, 10, 16,  24,  40,  51,  61,
    12, 12, 14, 19,  26,  58,  60,  55,
    14, 13, 16, 24,  40,  57,  69,  56,
    14, 17, 22, 29,  51,  87,  80,  62,
    18, 22, 37, 56,  68, 109, 103,  77,
    24, 35, 55, 64,  81, 104, 113,  92,
    49, 64, 78, 87, 103, 121, 120, 101,
    72, 92, 95, 98, 112, 100, 103,  99], dtype=np.float64).reshape(8, 8)

# JPEG 标准 DC/AC 亮度哈夫曼表（BITS, HUFFVAL）
DC_BITS = [0,1,5,1,1,1,1,1,1,0,0,0,0,0,0,0]
DC_VALS = list(range(12))
AC_BITS = [0,2,1,3,3,2,4,3,5,5,4,4,0,0,1,0x7d]
AC_VALS = [
 0x01,0x02,0x03,0x00,0x04,0x11,0x05,0x12,0x21,0x31,0x41,0x06,0x13,0x51,0x61,0x07,
 0x22,0x71,0x14,0x32,0x81,0x91,0xa1,0x08,0x23,0x42,0xb1,0xc1,0x15,0x52,0xd1,0xf0,
 0x24,0x33,0x62,0x72,0x82,0x09,0x0a,0x16,0x17,0x18,0x19,0x1a,0x25,0x26,0x27,0x28,
 0x29,0x2a,0x34,0x35,0x36,0x37,0x38,0x39,0x3a,0x43,0x44,0x45,0x46,0x47,0x48,0x49,
 0x4a,0x53,0x54,0x55,0x56,0x57,0x58,0x59,0x5a,0x63,0x64,0x65,0x66,0x67,0x68,0x69,
 0x6a,0x73,0x74,0x75,0x76,0x77,0x78,0x79,0x7a,0x83,0x84,0x85,0x86,0x87,0x88,0x89,
 0x8a,0x92,0x93,0x94,0x95,0x96,0x97,0x98,0x99,0x9a,0xa2,0xa3,0xa4,0xa5,0xa6,0xa7,
 0xa8,0xa9,0xaa,0xb2,0xb3,0xb4,0xb5,0xb6,0xb7,0xb8,0xb9,0xba,0xc2,0xc3,0xc4,0xc5,
 0xc6,0xc7,0xc8,0xc9,0xca,0xd2,0xd3,0xd4,0xd5,0xd6,0xd7,0xd8,0xd9,0xda,0xe1,0xe2,
 0xe3,0xe4,0xe5,0xe6,0xe7,0xe8,0xe9,0xea,0xf1,0xf2,0xf3,0xf4,0xf5,0xf6,0xf7,0xf8,
 0xf9,0xfa]


def build_codes(bits, vals):
    codes = {}
    code = 0
    k = 0
    for L in range(1, 17):
        for _ in range(bits[L-1]):
            codes[vals[k]] = (code, L)
            code += 1
            k += 1
        code <<= 1
    return codes


DCC = build_codes(DC_BITS, DC_VALS)
ACC = build_codes(AC_BITS, AC_VALS)


class BitWriter:
    def __init__(self):
        self.out = bytearray()
        self.acc = 0
        self.n = 0

    def put(self, val, length):
        for i in range(length - 1, -1, -1):
            self.acc = (self.acc << 1) | ((val >> i) & 1)
            self.n += 1
            if self.n == 8:
                self.out.append(self.acc)
                if self.acc == 0xFF:
                    self.out.append(0x00)     # 字节填充
                self.acc = 0
                self.n = 0

    def flush(self):
        while self.n:
            self.put(1, 1)


def category(v):
    v = abs(v)
    n = 0
    while v:
        n += 1
        v >>= 1
    return n


def scale_q(q, quality):
    if quality < 1: quality = 1
    if quality > 100: quality = 100
    s = 5000 // quality if quality < 50 else 200 - quality * 2
    t = np.floor((q * s + 50) / 100)
    return np.clip(t, 1, 255)


def cost_of(dct_blocks, q):
    """按 JPEG 熵编码算出的字节数（不真的写流，只统计比特）"""
    bits = 0
    prev_dc = 0
    for blk in dct_blocks:
        z = np.round(blk / q).astype(np.int32).ravel()[ZZ]
        diff = z[0] - prev_dc
        prev_dc = z[0]
        c = category(diff)
        bits += DCC[c][1] + c
        run = 0
        for k in range(1, 64):
            if z[k] == 0:
                run += 1
                continue
            while run > 15:
                bits += ACC[0xF0][1]
                run -= 16
            c = category(z[k])
            sym = (run << 4) | c
            if sym not in ACC:
                return None
            bits += ACC[sym][1] + c
            run = 0
        if run:
            bits += ACC[0x00][1]
    return (bits + 7) // 8


def _prepare(img):
    """校验单通道、补边到 8 的倍数并减 128，返回 (pad, H, W, ph, pw)。"""
    img = np.asarray(img, dtype=np.float64)
    if img.ndim != 2:
        raise ValueError('只接受单通道灰度图')
    H, W = img.shape
    ph = (H + 7) // 8 * 8
    pw = (W + 7) // 8 * 8
    pad = np.full((ph, pw), 128.0, np.float64)
    pad[:H, :W] = img
    pad -= 128.0
    return pad, H, W, ph, pw


def encode_entropy(img, quality=50):
    """对整帧灰度图做完整 JPEG 编码，返回**纯熵编码字节流**（无任何标记段）。

    与 jpeg_frame.v 的输出同构：DC 差分 + AC 游程 + 标准 DC/AC 亮度表 +
    0xFF 后补 0x00；只在帧末补 1 到字节边界。可直接交给
    tools/jpeg_file.py 的 build_jpeg() 包成标准 .jpg。
    """
    pad, _, _, ph, pw = _prepare(img)
    q = scale_q(LUMA_Q, quality)
    w = BitWriter()
    prev_dc = 0
    for by in range(0, ph, 8):
        for bx in range(0, pw, 8):
            F = dct8(pad[by:by+8, bx:bx+8])
            z = np.round(F / q).astype(np.int32).ravel()[ZZ]
            diff = int(z[0]) - prev_dc
            prev_dc = int(z[0])
            c = category(diff)
            if c not in DCC:
                raise ValueError('DC 类别 %d 超出标准亮度表' % c)
            code, ln = DCC[c]
            w.put(code, ln)
            if c:
                w.put(diff - 1 if diff < 0 else diff, c)

            run = 0
            for k in range(1, 64):
                v = int(z[k])
                if v == 0:
                    run += 1
                    continue
                while run > 15:
                    code, ln = ACC[0xF0]
                    w.put(code, ln)
                    run -= 16
                c = category(v)
                sym = (run << 4) | c
                if sym not in ACC:
                    raise ValueError('AC 符号 0x%02X 不在标准亮度表中' % sym)
                code, ln = ACC[sym]
                w.put(code, ln)
                w.put(v - 1 if v < 0 else v, c)
                run = 0
            if run:
                code, ln = ACC[0x00]
                w.put(code, ln)
    w.flush()
    return bytes(w.out)


def run(img, quality):
    """返回 (熵编码字节数, PSNR)；量化表放不下时返回 (None, None)。"""
    pad, H, W, ph, pw = _prepare(img)
    blocks = []
    for by in range(0, ph, 8):
        for bx in range(0, pw, 8):
            blk = pad[by:by+8, bx:bx+8]
            F = dct8(blk)
            blocks.append(F)

    q = scale_q(LUMA_Q, quality)
    nbytes = cost_of(blocks, q)
    if nbytes is None:
        return None, None

    # 重建算 PSNR
    rec = np.zeros((ph, pw), np.float64)
    idx = 0
    for by in range(0, ph, 8):
        for bx in range(0, pw, 8):
            z = np.round(blocks[idx] / q) * q
            idx += 1
            rec[by:by+8, bx:bx+8] = idct8(z)
    rec = np.clip(rec + 128, 0, 255)[:H, :W]
    mse = np.mean((np.asarray(img, dtype=np.float64) - rec) ** 2)
    psnr = 99.0 if mse < 1e-9 else 10 * np.log10(255.0**2 / mse)
    return nbytes, psnr


def _dct_matrix():
    n = 8
    x = np.arange(n)
    k = np.arange(n).reshape(-1, 1)
    D = np.cos((2 * x + 1) * k * np.pi / (2 * n))
    C = np.where(np.arange(n) == 0, 1.0 / np.sqrt(2.0), 1.0)
    return D, C


_D, _C = _dct_matrix()


def dct8(blk):
    """标准 JPEG 前向 DCT：F = (1/4) C(u)C(v) sum f cos cos（与标准量化表配套）"""
    return 0.25 * _C[:, None] * _C[None, :] * (_D @ blk @ _D.T)


def idct8(F):
    return 0.25 * (_D.T @ (_C[:, None] * _C[None, :] * F) @ _D)


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    from PIL import Image     # 只有命令行主流程需要 PIL

    img = np.asarray(Image.open(sys.argv[1]).convert('L')).astype(np.float64)
    print('帧 %dx%d' % (img.shape[1], img.shape[0]))
    print()
    print('%8s %12s %10s %9s' % ('质量', '码流字节', '压缩率', 'PSNR'))
    print('-' * 46)
    for q in [30, 50, 60, 70, 75, 80, 90]:
        nb, p = run(img, q)
        if nb is None:
            continue
        print('%8d %12d %9.2f%% %8.1f dB' % (q, nb, (1 - nb / img.size) * 100, p))


if __name__ == '__main__':
    main()
