#!/usr/bin/env python3
"""jpeg_cap.py —— 触发 FPGA 压缩、取回码流（板端运行）。

流程（两段式，和 RTL 的 DDR 读口分时一致）：
  1. ALGO_CTRL=0x10：开压缩。回读引擎读相机帧 → JPEG 编码 → 写 0x0500_0000，
     真实码流长度在 0x250。
  2. 等 0x250 非 0 后 ALGO_CTRL=0x40：关压缩、把推帧源切到压缩块，再 ARM 一帧，
     等 mailbox 序号变化，从槽 0 的 +128 处取 stream_len 字节。

用法（板端）：
  ./jpeg_cap.py --out /tmp/fpga_jpeg.bin          # 自动压缩 + 推 + 取
  ./jpeg_cap.py --ack                             # 释放 HELD
  ./jpeg_cap.py --out x.bin --no-compress          # 只推已有的压缩块
"""
from __future__ import annotations

import argparse
import fcntl
import mmap
import os
import re
import struct
import subprocess
import sys
import time

CTRL_DEV = '/dev/ANLOGIC-PCI0_control'
# _IOWR('x', 11, struct anlogic_ioc_frame_wait)，结构是 6 个 u32
IOC_FRAME_WAIT = 0xC018780B  # _IOWR=3（读写），size=24，type='x'，nr=11
# ALGO_CTRL（0x240）：bit4 = 压缩模式，bit6 = 推压缩块（两者互斥）
ALGO_COMPRESS = 0x10
ALGO_PUSH_COMP = 0x40


def frame_wait(fd: int, last_seq: int, timeout_ms: int) -> tuple[int, int, int, int]:
    """返回 (seq, count, ready, slot)。"""
    buf = struct.pack('<6I', last_seq, timeout_ms, 0, 0, 0, 0)
    raw = fcntl.ioctl(fd, IOC_FRAME_WAIT, buf)
    _ls, _to, seq, count, ready, slot = struct.unpack('<6I', raw)
    return seq, count, ready, slot


def bar_read(off: int) -> int:
    out = subprocess.run(['/root/pci_bar', '0', hex(off)], check=True,
                         capture_output=True, text=True).stdout
    m = re.search(r'Read data:\s*(0x[0-9a-fA-F]+)', out)
    if not m:
        raise RuntimeError(f'pci_bar 输出无法解析：{out!r}')
    return int(m.group(1), 0)


def bar_write(off: int, val: int) -> None:
    subprocess.run(['/root/pci_bar', '0', hex(off), hex(val)], check=True,
                   capture_output=True, text=True)


def dump_debug() -> int:
    n = bar_read(0x250)
    jb = bar_read(0x258)
    print(f'  {"码流长度":>10} @0x250 = {n}')
    print(f'  {"jb_done":>10} @0x258 = {jb >> 16}    jb_v = {jb & 0xFFFF}')
    for off, name in ((0x254, '回读字节'), (0x260, 'start 次数'),
                      (0x264, 'rd/cw 状态'), (0x268, '读口握手'),
                      (0x26C, '压缩帧号')):
        print(f'  {name:>10} @0x{off:03x} = 0x{bar_read(off):08x}')
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dev', default='/dev/ANLOGIC-PCI0_frame')
    ap.add_argument('--out', default='/tmp/fpga_jpeg.bin')
    ap.add_argument('--mb-off', type=int, default=0,
                    help='mailbox 偏移；0 = 从 frame_grab --info 自动解析')
    ap.add_argument('--hdr', type=int, default=128, help='槽内数据起点')
    ap.add_argument('--timeout', type=int, default=8000, help='等推帧毫秒')
    ap.add_argument('--compress-timeout', type=float, default=6.0,
                    help='等一帧压缩完成的秒数')
    ap.add_argument('--no-compress', action='store_true', help='跳过压缩，直接推已有块')
    ap.add_argument('--force-compress', action='store_true', help='即使 0x250 非 0 也重新压一帧')
    ap.add_argument('--ack', action='store_true', help='只做 link_ctl ack 后退出')
    ap.add_argument('--restore', action='store_true', help='取完后 ALGO_CTRL 归 0')
    args = ap.parse_args()

    if args.ack:
        subprocess.run(['/root/link_ctl', 'ack'])
        return 0

    if not args.mb_off:
        out = subprocess.run(['/root/frame_grab', '--info'], check=True,
                             capture_output=True, text=True).stdout
        m = re.search(r'缓冲内偏移\s+(\d+)', out)
        if not m:
            print('无法从 frame_grab --info 解析 mailbox 偏移', file=sys.stderr)
            return 4
        args.mb_off = int(m.group(1))
        print(f'mailbox 偏移 = {args.mb_off}（来自 frame_grab --info）')

    # ---- 阶段 1：压缩 ----
    n = bar_read(0x250)
    if not args.no_compress and (n == 0 or args.force_compress):
        done0 = (bar_read(0x258) >> 16) & 0xFFFF
        print(f'开压缩（ALGO_CTRL=0x{ALGO_COMPRESS:02x}），基线 jb_done={done0}，'
              f'最多等 {args.compress_timeout}s')
        bar_write(0x240, ALGO_COMPRESS)
        t0 = time.time()
        while time.time() - t0 < args.compress_timeout:
            if ((bar_read(0x258) >> 16) & 0xFFFF) != done0:
                break
            time.sleep(0.05)
        # writer 还要把尾部补零写完（整帧 2.6MB，约 0.8ms），留足余量
        time.sleep(0.05)
    n = dump_debug()
    if n <= 0:
        print('压缩没有产出：看上面的 rd/cw 状态', file=sys.stderr)
        return 3

    # ---- 阶段 2：推压缩块 ----
    if not args.no_compress:
        print('切推帧源到压缩块（ALGO_CTRL=0x40）')
        bar_write(0x240, ALGO_PUSH_COMP)
    fd = os.open(args.dev, os.O_RDONLY)
    try:
        seq0, _, _, _ = frame_wait(fd, 0, 0)
        print(f'基线序号 seq={seq0}')
        armed = False
        for attempt in range(3):
            try:
                subprocess.run(['/root/link_ctl', 'arm'], check=True,
                               capture_output=True, text=True)
                armed = True
                break
            except subprocess.CalledProcessError as e:
                print(f'ARM 第 {attempt + 1} 次失败（可能正在推帧）：{(e.stderr or "").strip()}')
                time.sleep(0.3)
        if not armed:
            print('ARM 未成功；若推帧已在飞，仍继续等序号变化')
        seq, count, ready, slot = frame_wait(fd, seq0, args.timeout)
        if not ready:
            print(f'超时：没有新帧（seq={seq} count={count}）', file=sys.stderr)
            return 2
        print(f'新帧 seq={seq} count={count} slot={slot}')
        size = args.mb_off + 4096
        m = mmap.mmap(fd, size, prot=mmap.PROT_READ, flags=mmap.MAP_SHARED)
        data = m[args.hdr:args.hdr + n]
        with open(args.out, 'wb') as f:
            f.write(data)
        print(f'写出 {len(data)} 字节 -> {args.out}')
    finally:
        os.close(fd)

    if args.restore:
        bar_write(0x240, 0)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())