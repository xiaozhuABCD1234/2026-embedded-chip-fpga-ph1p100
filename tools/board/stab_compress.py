#!/usr/bin/env python3
"""压缩支路长稳压测：按固定间隔采样 jb_done / 0x250 / 0x264，结束时打印汇总。

用法（板端，默认 3600 s）：
  python3 tools/board/stab_compress.py
  python3 tools/board/stab_compress.py --duration 120 --interval 5   # 自定义时长
结束会恢复 ALGO_CTRL(0x240)=0。判据：jb_done 单调、帧率稳定、无长时间停滞。
本脚本独立部署（只需 /root/pci_bar），刻意不 import jpeg_cap.py。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import time

PCI_BAR = '/root/pci_bar'
ALGO_CTRL = 0x240
ALGO_COMPRESS = 0x10
REG_STREAM_LEN = 0x250
REG_JB_STATUS = 0x258
REG_RD_CW_STATE = 0x264

_BAR_READ_RE = re.compile(r'Read data:\s*(0x[0-9a-fA-F]+)')


def bar(args: list[str]) -> str:
    """调用 pci_bar 并返回 stdout（写失败沿用原脚本的静默行为）。"""
    return subprocess.run([PCI_BAR, '0', *args],
                          capture_output=True, text=True).stdout


def rd(off: int) -> int:
    """读 32 bit 寄存器；解析失败返回 -1，长稳期间不中断采样。"""
    m = _BAR_READ_RE.search(bar([hex(off)]))
    return int(m.group(1), 0) if m else -1


def main() -> int:
    ap = argparse.ArgumentParser(
        description='压缩支路长稳：采样 jb_done / 0x250 / 0x264')
    ap.add_argument('--duration', type=float, default=3600.0,
                    help='总时长秒（默认 3600）')
    ap.add_argument('--interval', type=float, default=5.0,
                    help='采样间隔秒（默认 5）')
    ap.add_argument('--log', default='/tmp/stab_compress.log',
                    help='采样日志（默认 /tmp/stab_compress.log）')
    args = ap.parse_args()

    bar([hex(ALGO_CTRL), hex(ALGO_COMPRESS)])
    time.sleep(1)
    jb0 = rd(REG_JB_STATUS) >> 16
    t0 = time.time()
    last, bad, n, lens = jb0, 0, 0, []
    try:
        with open(args.log, 'w') as f:
            f.write(f'start jb0={jb0} duration={args.duration:.0f}s\n')
            f.flush()
            while time.time() - t0 < args.duration:
                jb = rd(REG_JB_STATUS) >> 16
                ln = rd(REG_STREAM_LEN)
                st = rd(REG_RD_CW_STATE)
                if jb < last:
                    bad += 1
                if ln > 0:
                    lens.append(ln)
                last = jb
                if n % 24 == 0:
                    f.write(f'{time.time()-t0:7.1f}s jb={jb} (+{jb-jb0}) '
                            f'len={ln} st=0x{st:04x}\n')
                    f.flush()
                n += 1
                time.sleep(args.interval)
    finally:
        # Ctrl-C / 异常也要把压缩使能收回去，避免板子留在 0x240=0x10。
        bar([hex(ALGO_CTRL), '0x0'])

    dt = time.time() - t0
    frames = last - jb0
    summary = (f'DONE jb0={jb0} last={last} frames={frames} dt={dt:.1f}s '
               f'fps={frames/dt:.2f} non_monotonic={bad} '
               f'len_min={min(lens) if lens else 0} '
               f'len_max={max(lens) if lens else 0}')
    with open(args.log, 'a') as f:
        f.write(summary + '\n')
    print(summary)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
