#!/usr/bin/env python3
"""把 FPGA 输出的「纯熵编码字节流」包装成标准 JPEG 文件，并做解码验证。

FPGA 的 jpeg_frame.v 只输出熵编码比特流：DC 差分 + AC 游程 + 标准亮度
DC/AC 规范哈夫曼 + 0xFF 后补 0x00，不含 SOI/DQT/SOF0/DHT/SOS/EOI。本工具
按 T.81 / JFIF 1.01 补上这些常量标记段，得到一个 baseline、单分量、8 bit、
采样因子 1x1 的标准灰度 JPEG。

用法：
  python3 tools/jpeg_file.py wrap <entropy.bin> <out.jpg> [--width 1920 --height 1088]
  python3 tools/jpeg_file.py check <out.jpg> [<ref.png>] [--threshold 30]

wrap 只做「常量头 + 熵流 + EOI」，不重新编码、不改动熵流一个字节。
build_header()/build_jpeg() 可被板端脚本 import（默认 1920x1088，可传任意尺寸）。
"""
from __future__ import annotations

import argparse
import os
import sys
import warnings

import numpy as np

# 允许从仓库根目录或任意 CWD 直接运行：把 tools/ 放进 sys.path 以复用参考实现。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jpeg_ref import (  # noqa: E402
    AC_BITS, AC_VALS, DC_BITS, DC_VALS, LUMA_Q, ZZ,
)

# q=50 标准亮度量化表（自然光栅顺序，8x8），与 jpeg_quant_zz.v 的 qtab 一致。
q50 = np.asarray(LUMA_Q, dtype=np.uint8)


def _u16(v: int) -> bytes:
    return bytes(((v >> 8) & 0xFF, v & 0xFF))


def _seg(marker: int, payload: bytes) -> bytes:
    """一个带长度字段的标记段：FF <marker> <len:2> <payload>。"""
    return b'\xff' + bytes((marker,)) + _u16(len(payload) + 2) + payload


def _quant_bytes(dqt) -> bytes:
    """把 8x8（或 64 个自然顺序）量化表转成 DQT 里要求的之字形 64 字节。"""
    q = np.asarray(dqt, dtype=np.int64)
    if q.size != 64:
        raise ValueError('量化表必须正好 64 个元素，实际 %d' % q.size)
    q = q.reshape(8, 8)
    if q.min() < 1 or q.max() > 255:
        raise ValueError('量化表元素必须在 1..255（8 bit DQT）')
    return bytes(int(q.ravel()[int(ZZ[k])]) for k in range(64))


def _dht_bytes(is_ac: bool, bits, vals) -> bytes:
    bits = list(int(b) for b in bits)
    vals = list(int(v) for v in vals)
    if len(bits) != 16 or sum(bits) != len(vals) or len(vals) > 256:
        raise ValueError('哈夫曼表 BITS/HUFFVAL 长度不一致')
    payload = bytes(((0x10 if is_ac else 0x00),)) + bytes(bits) + bytes(vals)
    return _seg(0xC4, payload)


def build_header(width: int = 1920, height: int = 1088, dqt=None,
                 quality_table=q50) -> bytes:
    """返回从 SOI 到 SOS 段结束的完整文件头（不含熵数据与 EOI）。

    参数
    ----
    width, height : 图像尺寸，SOF0 里是 16 bit 大端。
    dqt           : 量化表；默认用 quality_table（q=50 标准亮度表）。
                    接受 8x8 或 64 个自然光栅顺序的值。
    quality_table : 默认量化表，q50。

    头结构（共 328 字节，Nf=1，表号全为 0）：
      SOI(2) APP0/JFIF(18) DQT(69) SOF0(13) DHT-DC(33) DHT-AC(183) SOS(10)
    """
    if not (1 <= width <= 65535 and 1 <= height <= 65535):
        raise ValueError('宽高必须在 1..65535')

    qbytes = _quant_bytes(quality_table if dqt is None else dqt)

    soi = b'\xff\xd8'

    # APP0：JFIF 1.01，无单位（只表示像素宽高比 1:1），无缩略图。
    app0 = _seg(0xE0, b'JFIF\x00' + bytes((1, 1, 0)) +
                _u16(1) + _u16(1) + bytes((0, 0)))

    # DQT：Pq/Tq = 0x00（8 bit 精度，表号 0），64 个之字形值。
    dqt_seg = _seg(0xDB, bytes((0x00,)) + qbytes)

    # SOF0：8 bit、单分量、1x1 采样、量化表 0。
    sof0 = _seg(0xC0, bytes((8,)) + _u16(height) + _u16(width) +
                bytes((1, 1, 0x11, 0)))

    dht_dc = _dht_bytes(False, DC_BITS, DC_VALS)
    dht_ac = _dht_bytes(True, AC_BITS, AC_VALS)

    # SOS：Ns=1，分量 1 用 DC/AC 表 0，Ss=0 Se=63 Ah=Al=0（baseline 顺序）。
    sos = _seg(0xDA, bytes((1, 1, 0x00, 0, 63, 0)))

    return soi + app0 + dqt_seg + sof0 + dht_dc + dht_ac + sos


def build_jpeg(entropy, width: int = 1920, height: int = 1088, dqt=None,
               quality_table=q50) -> bytes:
    """把纯熵编码字节流包装成严格合法的 baseline 灰度 JPEG。

    entropy       : bytes，FPGA 输出（已做 0xFF -> 0xFF00 填充）。
    width, height : 图像尺寸，默认 1920x1088。
    dqt           : 可选量化表覆盖（8x8 或 64 个自然顺序值），默认 q50。
    quality_table : 默认量化表。

    返回 SOI + 头 + entropy + EOI 的完整字节串。
    """
    if not isinstance(entropy, (bytes, bytearray, memoryview)):
        raise TypeError('entropy 必须是 bytes/bytearray/memoryview')
    entropy = bytes(entropy)
    if not entropy:
        raise ValueError('entropy 为空：FPGA 帧末应至少有一个补齐字节')

    header = build_header(width, height, dqt=dqt, quality_table=quality_table)
    return header + entropy + b'\xff\xd9'


def scan_markers(data: bytes):
    """扫描到 SOS 段结束，返回 [(marker, offset, seg_len), ...] 和头部长度。

    用于 check 时确认标志段齐全、顺序正确；遇到 SOS 立即停（后面是熵数据，
    里面的 0xFF00 不是标记）。
    """
    if data[:2] != b'\xff\xd8':
        raise ValueError('不是 JPEG：缺少 SOI')
    i, out = 2, []
    while i + 1 < len(data):
        if data[i] != 0xFF:
            raise ValueError('offset %d 处不是标记（0x%02X）' % (i, data[i]))
        start = i
        while i < len(data) and data[i] == 0xFF:
            i += 1
        if i >= len(data):
            raise ValueError('文件在标记处截断')
        marker = data[i]
        i += 1
        if marker == 0xD9:            # EOI
            out.append((marker, start, 0))
            return out, None
        if marker == 0xD8:            # 不应出现第二个 SOI
            out.append((marker, start, 0))
            continue
        if i + 2 > len(data):
            raise ValueError('文件在段长度处截断')
        seg_len = (data[i] << 8) | data[i + 1]
        if seg_len < 2 or i + seg_len > len(data):
            raise ValueError('offset %d 的段长度非法：%d' % (start, seg_len))
        out.append((marker, start, seg_len + 2))
        i += seg_len
        if marker == 0xDA:            # SOS 之后是熵数据
            return out, i
    raise ValueError('文件没有 SOS 段')


_MARKER_NAMES = {
    0xD8: 'SOI', 0xE0: 'APP0', 0xDB: 'DQT', 0xC0: 'SOF0',
    0xC4: 'DHT', 0xDA: 'SOS', 0xD9: 'EOI',
}


def _expected_marker_sequence():
    # scan_markers 从 SOI 之后开始扫、到 SOS 就停，所以这里不含 SOI/EOI。
    return [0xE0, 0xDB, 0xC0, 0xC4, 0xC4, 0xDA]


def check_header(data: bytes):
    """校验头部标记顺序 / 表号，返回 (标志名列表, 头部字节数)。

    头部字节数 = SOI..SOS 段末尾的偏移，即熵数据起点。
    """
    markers, header_end = scan_markers(data)
    seq = [m for m, _, _ in markers]
    expect = _expected_marker_sequence()
    if seq != expect:
        raise ValueError('标记顺序不对：%s，期望 %s' %
                         ([_MARKER_NAMES.get(m, hex(m)) for m in seq],
                          [_MARKER_NAMES.get(m, hex(m)) for m in expect]))
    names = [_MARKER_NAMES.get(m, hex(m)) for m in seq]
    return names, header_end


def _psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = float(np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2))
    if mse <= 0.0:
        return float('inf')
    return 10.0 * np.log10(255.0 ** 2 / mse)


def check_jpeg(path: str, ref_path: str | None = None,
               threshold: float = 30.0) -> dict:
    """用 PIL 解码并检查一个 JPEG；可选与参考 PNG 逐像素算 PSNR。

    返回 dict：尺寸、字节数、头长度、警告列表、PSNR、压缩率等。
    解码失败或（给了参考图时）PSNR 低于 threshold 会抛异常/置 passed=False。
    """
    from PIL import Image

    with open(path, 'rb') as f:
        data = f.read()
    if not data:
        raise ValueError('JPEG 文件为空')
    names, header_end = check_header(data)

    warn_msgs = []
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        with Image.open(path) as im:
            fmt, mode, size = im.format, im.mode, im.size
            im.load()
            arr = np.asarray(im.convert('L'), dtype=np.uint8)
        # 再 verify 一次，能发现尾部截断 / 标记损坏。
        with Image.open(path) as im2:
            im2.verify()
    for w in caught:
        if issubclass(w.category, Warning):
            warn_msgs.append('%s: %s' % (w.category.__name__, w.message))

    w, h = size
    result = {
        'path': path,
        'bytes': len(data),
        'format': fmt,
        'mode': mode,
        'width': w,
        'height': h,
        'header_bytes': header_end,
        'header_markers': names,
        'entropy_bytes': len(data) - header_end - 2,
        'warnings': warn_msgs,
        'raw_bytes': w * h,
        'ratio': (w * h) / len(data),
        'psnr': None,
        'mse': None,
        'ref': ref_path,
        'threshold': threshold,
        'passed': True,
    }

    # 头部结构自检：SOF0 尺寸、Nf=1、表号 0。
    # （scan_markers 只保证顺序；这里读 SOF0 载荷做数值校验。）
    i = data.find(b'\xff\xc0')
    if i >= 0:
        sof = data[i + 4:i + 4 + 6]
        if len(sof) == 6:
            prec, sh, sw, nf = sof[0], (sof[1] << 8) | sof[2], (sof[3] << 8) | sof[4], sof[5]
            if prec != 8 or nf != 1 or sh != h or sw != w:
                result['passed'] = False
                result['error'] = 'SOF0 不符：precision=%d Nf=%d %dx%d' % (prec, nf, sw, sh)

    if ref_path and os.path.basename(str(ref_path)).lower() not in ('none', 'null', '-'):
        with Image.open(ref_path) as rim:
            ref = np.asarray(rim.convert('L'), dtype=np.uint8)
        if ref.shape != arr.shape:
            raise ValueError('尺寸不一致：JPEG %dx%d，参考 %dx%d' %
                             (arr.shape[1], arr.shape[0], ref.shape[1], ref.shape[0]))
        result['mse'] = float(np.mean((arr.astype(np.float64) -
                                       ref.astype(np.float64)) ** 2))
        result['psnr'] = _psnr(arr, ref)
        if result['psnr'] < threshold:
            result['passed'] = False

    return result


def _fmt_psnr(p):
    return 'inf (逐像素相同)' if p == float('inf') else '%.2f dB' % p


def _cmd_wrap(args) -> int:
    with open(args.entropy, 'rb') as f:
        entropy = f.read()
    jpg = build_jpeg(entropy, width=args.width, height=args.height)
    with open(args.out, 'wb') as f:
        f.write(jpg)
    header = len(jpg) - len(entropy) - 2
    raw = args.width * args.height
    print('熵流      : %s (%d 字节)' % (args.entropy, len(entropy)))
    print('输出      : %s (%d 字节)' % (args.out, len(jpg)))
    print('尺寸      : %dx%d，单分量灰度 baseline' % (args.width, args.height))
    print('头部长度  : %d 字节（SOI+APP0+DQT+SOF0+DHT+2xDHT+SOS）' % header)
    print('压缩率    : 原始 %d 字节 -> %d 字节 = %.2fx（节省 %.2f%%）' %
          (raw, len(jpg), raw / len(jpg) if len(jpg) else 0,
           100.0 * (1 - len(jpg) / raw)))
    return 0


def _cmd_check(args) -> int:
    r = check_jpeg(args.jpeg, args.ref, threshold=args.threshold)
    print('文件      : %s' % r['path'])
    print('文件字节  : %d' % r['bytes'])
    print('尺寸      : %dx%d' % (r['width'], r['height']))
    print('格式/模式 : %s / %s' % (r['format'], r['mode']))
    print('头部      : %d 字节，标记 %s' %
          (r['header_bytes'], ' '.join(r['header_markers'])))
    print('熵数据    : %d 字节' % r['entropy_bytes'])
    print('压缩率    : 原始 %d 字节 -> %d 字节 = %.2fx（节省 %.2f%%）' %
          (r['raw_bytes'], r['bytes'], r['ratio'],
           100.0 * (1 - 1.0 / r['ratio'])))
    if r['warnings']:
        print('警告      : %d 条' % len(r['warnings']))
        for w in r['warnings']:
            print('  - %s' % w)
    else:
        print('警告      : 无')
    if r['psnr'] is not None:
        print('参考图    : %s' % r['ref'])
        print('MSE       : %.4f' % r['mse'])
        print('PSNR      : %s' % _fmt_psnr(r['psnr']))
        print('判定      : %s（阈值 %.1f dB）' %
              ('PASS' if r['passed'] else 'FAIL', args.threshold))
    else:
        print('判定      : %s（仅解码检查，未给参考图）' %
              ('PASS' if r['passed'] else 'FAIL'))
    if 'error' in r:
        print('错误      : %s' % r['error'])
    return 0 if r['passed'] else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description='FPGA JPEG 纯熵流 -> 标准 JPEG 文件包装/校验工具')
    sub = p.add_subparsers(dest='cmd', required=True)

    w = sub.add_parser('wrap', help='把 entropy.bin 包装成 .jpg')
    w.add_argument('entropy', help='FPGA 输出的纯熵编码字节流')
    w.add_argument('out', help='输出 .jpg')
    w.add_argument('--width', type=int, default=1920)
    w.add_argument('--height', type=int, default=1088)
    w.set_defaults(func=_cmd_wrap)

    c = sub.add_parser('check', help='用 PIL 解码并检查 .jpg')
    c.add_argument('jpeg')
    c.add_argument('ref', nargs='?', default=None,
                   help='参考 PNG/PGM；不需要时省略或写 none')
    c.add_argument('--threshold', type=float, default=30.0,
                   help='PSNR 门限，默认 30 dB')
    c.set_defaults(func=_cmd_check)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
