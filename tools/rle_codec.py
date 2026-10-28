#!/usr/bin/env python3
"""RLE（水平差分 + 零游程）参考实现，与 fpga/rtl/ep_side/codec/rle_enc.v 对齐。

差分：d[n] = (y[n] - y[n-1]) & 0xFF，帧首 y[-1] = 128
编码：d == 0 的游程 -> 0x00, N（N=1..255）
      d != 0        -> 该字节本身
"""
import sys

WIN = 2048


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


def rle_encode(delta):
    out = bytearray()
    i, n = 0, len(delta)
    while i < n:
        if delta[i] == 0:
            j = i
            while j < n and delta[j] == 0 and j - i < 255:
                j += 1
            out.append(0)
            out.append(j - i)
            i = j
        else:
            out.append(delta[i])
            i += 1
    return bytes(out)


def rle_decode(enc, n_out):
    out = bytearray()
    i = 0
    while i < len(enc) and len(out) < n_out:
        b = enc[i]
        i += 1
        if b != 0:
            out.append(b)
        else:
            run = enc[i]
            i += 1
            out.extend(bytes(run))
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
    if cmd == 'selftest':
        w, h, pix = read_pgm(sys.argv[2])
        d = delta_encode(pix)
        enc = rle_encode(d)
        back = delta_decode(rle_decode(enc, len(d)))
        print('%dx%d  %d 字节' % (w, h, len(pix)))
        print('压缩后 %d 字节，压缩率 %.2f%%' % (len(enc), (1 - len(enc) / len(pix)) * 100))
        print('往返一致:', 'OK' if back == pix else 'FAIL')
        if len(sys.argv) > 3:
            open(sys.argv[3], 'wb').write(enc)
    elif cmd == 'dec':
        enc = open(sys.argv[2], 'rb').read()
        w, h = int(sys.argv[4]), int(sys.argv[5])
        pix = delta_decode(rle_decode(enc, w * h))
        with open(sys.argv[3], 'wb') as f:
            f.write(b'P5\n%d %d\n255\n' % (w, h))
            f.write(pix)
        print('解压 %dx%d' % (w, h))
    else:
        raise SystemExit('未知命令')


if __name__ == '__main__':
    main()

