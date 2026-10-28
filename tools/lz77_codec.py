#!/usr/bin/env python3
"""LZ77 压缩格式的参考实现（编码 + 解码），用于验证 FPGA 编码器。

格式（与 fpga/rtl/ep_side/codec/lz77_enc.v 对齐）：

  token 首字节 bit7 = 0：字面量段，长度 = 首字节 + 1（1..128），后跟该长度的字节
  token 首字节 bit7 = 1：匹配，长度 = (首字节 >> 3) & 0x1F + 3（3..34），
                        距离高 3 位 = 首字节 & 0x07，后跟 1 字节距离低 8 位
                        距离 = 1..2047

预处理：d[n] = (y[n] - y[n-1]) & 0xFF，帧首的 y[-1] 取 128。

用法：
  python3 lz77_codec.py enc in.pgm out.bin     # 压缩并打印压缩率
  python3 lz77_codec.py dec in.bin out.pgm W H # 解压（W/H 从原图读也行）
  python3 lz77_codec.py selftest in.pgm        # 往返一致性 + 压缩率
"""
import sys

WIN = 2047   # 距离字段只有 11 位（1..2047），窗口按 2047 算
MAXLEN = 18


def delta_encode(data):
    out = bytearray(len(data))
    prev = 128
    for i, y in enumerate(data):
        out[i] = (y - prev) & 0xFF
        prev = y
    return bytes(out)


def delta_decode(data):
    out = bytearray(len(data))
    prev = 128
    for i, d in enumerate(data):
        v = (prev + d) & 0xFF
        out[i] = v
        prev = v
    return bytes(out)


def lz77_encode(data, win=WIN, maxlen=MAXLEN, minlen=3):
    """双字节 key + 单候选贪心匹配（与硬件实现一致：哈希表存同 key 的最近位置）。"""
    out = bytearray()
    i, n = 0, len(data)
    tab = {}
    lit = bytearray()

    def flush():
        nonlocal lit
        while lit:
            chunk = lit[:128]
            out.append(len(chunk) - 1)
            out.extend(chunk)
            lit = lit[len(chunk):]

    while i < n:
        best_len, best_dist = 0, 0
        if i + 2 <= n:
            cand = tab.get(data[i:i + 2])
            if cand is not None and 0 < i - cand <= win:
                l = 0
                while l < maxlen and i + l < n and data[cand + l] == data[i + l]:
                    l += 1
                if l >= minlen:
                    best_len, best_dist = l, i - cand
        if best_len:
            flush()
            out.append(0x80 | ((best_len - 3) << 3) | ((best_dist >> 8) & 0x07))
            out.append(best_dist & 0xFF)
            for k in range(best_len):
                if i + k + 1 < n:
                    tab[data[i + k:i + k + 2]] = i + k
            i += best_len
        else:
            lit.append(data[i])
            if i + 1 < n:
                tab[data[i:i + 2]] = i
            i += 1
    flush()
    return bytes(out)


def lz77_decode(enc, n_out, win=WIN):
    out = bytearray()
    i = 0
    while i < len(enc):
        t = enc[i]
        i += 1
        if t & 0x80 == 0:
            ln = t + 1
            out.extend(enc[i:i + ln])
            i += ln
        else:
            ln = ((t >> 3) & 0x0F) + 3
            dist = ((t & 0x07) << 8) | enc[i]
            i += 1
            start = len(out) - dist
            for k in range(ln):
                out.append(out[start + k])
        if len(out) >= n_out:
            break
    return bytes(out[:n_out])


def read_pgm(path):
    data = open(path, 'rb').read()
    parts = data.split(b'\n', 3)
    if parts[0] != b'P5':
        raise SystemExit('not P5')
    dims = parts[1].split()
    return int(dims[0]), int(dims[1]), parts[3]


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    cmd = sys.argv[1]
    if cmd in ('enc', 'selftest'):
        w, h, pix = read_pgm(sys.argv[2])
        d = delta_encode(pix)
        enc = lz77_encode(d)
        print('图像 %dx%d  %d 字节' % (w, h, len(pix)))
        print('差分后 %d 字节，压到 %d 字节，压缩率 %.2f%%' % (len(d), len(enc), (1 - len(enc) / len(d)) * 100))
        if cmd == 'selftest':
            back = delta_decode(lz77_decode(enc, len(d)))
            print("往返一致:", "OK" if back == pix else "FAIL")
            if len(sys.argv) > 3:
                open(sys.argv[3], 'wb').write(enc)
                print('压缩流写入', sys.argv[3])
    elif cmd == 'dec':
        enc = open(sys.argv[2], 'rb').read()
        w, h = int(sys.argv[4]), int(sys.argv[5])
        d = lz77_decode(enc, w * h)
        pix = delta_decode(d)
        with open(sys.argv[3], 'wb') as f:
            f.write(b'P5\n%d %d\n255\n' % (w, h))
            f.write(pix)
        print('解压出 %dx%d，%d 字节' % (w, h, len(pix)))
    else:
        raise SystemExit('未知命令')


if __name__ == '__main__':
    main()

