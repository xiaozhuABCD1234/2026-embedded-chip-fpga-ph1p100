#!/usr/bin/env python3
"""压缩支路长稳压测：按固定间隔采样 jb_done / 0x250 / 0x264，结束时打印汇总。

用法（板端）：
  python3 tools/board/stab_compress.py --duration 7200
结束会恢复 ALGO_CTRL(0x240)=0。判据：jb_done 单调、帧率稳定、无长时间停滞。
"""
import argparse
import re
import subprocess
import time

PCI_BAR = '/root/pci_bar'


def rd(off: int) -> int:
    out = subprocess.run([PCI_BAR, '0', hex(off)], capture_output=True, text=True).stdout
    m = re.search(r'Read data:\s*(0x[0-9a-fA-F]+)', out)
    return int(m.group(1), 0) if m else -1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--duration', type=float, default=3600.0, help='秒')
    ap.add_argument('--interval', type=float, default=5.0, help='采样间隔秒')
    ap.add_argument('--log', default='/tmp/stab_compress.log')
    args = ap.parse_args()

    subprocess.run([PCI_BAR, '0', '0x240', '0x10'], capture_output=True)
    time.sleep(1)
    jb0 = rd(0x258) >> 16
    t0 = time.time()
    last, bad, n, lens = jb0, 0, 0, []
    with open(args.log, 'w') as f:
        f.write(f'start jb0={jb0} duration={args.duration:.0f}s\n')
        f.flush()
        while time.time() - t0 < args.duration:
            jb = rd(0x258) >> 16
            ln = rd(0x250)
            st = rd(0x264)
            if jb < last:
                bad += 1
            if ln > 0:
                lens.append(ln)
            last = jb
            if n % 24 == 0:
                f.write(f'{time.time()-t0:7.1f}s jb={jb} (+{jb-jb0}) len={ln} st=0x{st:04x}\n')
                f.flush()
            n += 1
            time.sleep(args.interval)

    subprocess.run([PCI_BAR, '0', '0x240', '0x0'], capture_output=True)
    dt = time.time() - t0
    frames = last - jb0
    summary = (f'DONE jb0={jb0} last={last} frames={frames} dt={dt:.1f}s '
               f'fps={frames/dt:.2f} non_monotonic={bad} '
               f'len_min={min(lens) if lens else 0} len_max={max(lens) if lens else 0}')
    with open(args.log, 'a') as f:
        f.write(summary + '\n')
    print(summary)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
