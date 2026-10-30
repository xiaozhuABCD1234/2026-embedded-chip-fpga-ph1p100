#!/usr/bin/env python3
"""jpeg_cap.py —— 板端 JPEG 两段式取流库 + 命令行工具。

阶段 1（压缩）：ALGO_CTRL=0x10 触发一帧，轮询 jb_done(0x258[31:16])，再等
                writer 尾拍落盘(0x26C)，从 0x250 读真实熵流长度；
阶段 2（取流）：ALGO_CTRL=0x40 把推帧源切到压缩块，link_ctl arm 一帧，
                等 mailbox 序号变化，从槽 0 的 +128 处 mmap 拷出 stream_len 字节。
fpga_jpeg_npu.py 直接 import 本模块的 capture_entropy()/capture_stream()，
不再复制这套流程。

用法（板端，需 /root/pci_bar、/root/link_ctl 与 anlogic_pci 已加载）：
  python3 jpeg_cap.py --out /tmp/fpga_jpeg.bin   # 压缩 + 推 + 取
  python3 jpeg_cap.py --ack                      # 只释放 HELD
  python3 jpeg_cap.py --out x.bin --no-compress  # 只推已有的压缩块
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
from dataclasses import dataclass

# ---- 板端路径 / 设备 / 寄存器 ----
PCI_BAR = '/root/pci_bar'
LINK_CTL = '/root/link_ctl'
FRAME_DEV = '/dev/ANLOGIC-PCI0_frame'
# _IOWR('x', 11, struct anlogic_ioc_frame_wait)，6 个 u32
IOC_FRAME_WAIT = 0xC018780B
ALGO_CTRL = 0x240
ALGO_COMPRESS = 0x10           # bit4：压缩使能
ALGO_PUSH_COMP = 0x40          # bit6：推压缩块（与 bit4 互斥）
REG_STREAM_LEN = 0x250         # 最近完成帧的熵流字节数
REG_JB_STATUS = 0x258          # {jb_done[31:16], jb_v[15:0]}
REG_WRITE_PROGRESS = 0x26C     # writer 压缩帧号
HDR_OFF = 128                  # 槽 0 内数据起点
SLOT_BYTES = 4096              # mmap 页粒度

# writer 尾拍等待：jb_done 后立刻切 0x40 会复位 writer（JPEG broken data stream）
TAIL_TIMEOUT = 0.5
TAIL_POLL = 0.002
TAIL_SETTLE = 0.005

_BAR_READ_RE = re.compile(r'Read data:\s*(0x[0-9a-fA-F]+)')


class BoardError(RuntimeError):
    """板端工具缺失、pci_bar 读写失败或取流不完整。"""


class BoardTimeout(BoardError):
    """等压缩 / 等新帧超时。"""


class CompressTimeout(BoardTimeout):
    """jb_done 在 compress_timeout 内没有跳变。"""


class FrameTimeout(BoardTimeout):
    """等 mailbox 新帧超时；保留 seq/count 供调用方打印。"""

    def __init__(self, seq: int, count: int):
        super().__init__(f'等新帧超时：seq={seq} count={count}')
        self.seq = seq
        self.count = count


class NoStream(BoardError):
    """0x250 没有有效码流长度。"""


@dataclass
class Capture:
    """一次两段式取流的结果。"""

    data: bytes          # 纯熵流（stream_len 字节）
    stream_len: int      # 0x250 的值
    compress_ms: float   # 阶段 1 耗时；未触发压缩时为 0
    pcie_ms: float       # 0x250 读 + mmap 拷贝耗时
    seq: int
    count: int
    slot: int


def _noop(*_args) -> None:
    """默认诊断回调：静默。"""


def _pci_bar(args: list[str]) -> str:
    """执行 pci_bar 0 <args...>，把常见失败统一成 BoardError。"""
    try:
        return subprocess.run([PCI_BAR, '0', *args], check=True,
                              capture_output=True, text=True).stdout
    except FileNotFoundError as e:
        raise BoardError(f'找不到 {PCI_BAR}（板端工具未部署）') from e
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or '').strip() or f'退出码 {e.returncode}'
        raise BoardError(f'pci_bar 访问失败：{detail}') from e


def parse_bar_read(out: str) -> int:
    """解析 pci_bar 的 'Read data: 0x...' 输出。"""
    m = _BAR_READ_RE.search(out)
    if not m:
        raise BoardError(f'pci_bar 输出无法解析：{out!r}')
    return int(m.group(1), 0)


def bar_read(off: int) -> int:
    """读 BAR 偏移 off（32 bit）。"""
    return parse_bar_read(_pci_bar([hex(off)]))


def bar_write(off: int, val: int) -> None:
    """写 BAR 偏移 off。"""
    _pci_bar([hex(off), hex(val)])


def link_ctl(action: str) -> tuple[int, str]:
    """执行 /root/link_ctl <action>，返回 (退出码, stderr)；是否致命由调用方决定。"""
    try:
        p = subprocess.run([LINK_CTL, action], capture_output=True, text=True)
    except FileNotFoundError as e:
        raise BoardError(f'找不到 {LINK_CTL}（板端工具未部署）') from e
    return p.returncode, (p.stderr or '').strip()


def frame_wait(fd: int, last_seq: int, timeout_ms: int) -> tuple[int, int, int, int]:
    """等一帧 DMA，返回 (seq, count, ready, slot)。"""
    buf = struct.pack('<6I', last_seq, timeout_ms, 0, 0, 0, 0)
    raw = fcntl.ioctl(fd, IOC_FRAME_WAIT, buf)
    _ls, _to, seq, count, ready, slot = struct.unpack('<6I', raw)
    return seq, count, ready, slot


def dump_debug() -> int:
    """打印压缩相关寄存器，返回 0x250 的码流长度。"""
    n = bar_read(REG_STREAM_LEN)
    jb = bar_read(REG_JB_STATUS)
    print(f'  {"码流长度":>10} @0x250 = {n}')
    print(f'  {"jb_done":>10} @0x258 = {jb >> 16}    jb_v = {jb & 0xFFFF}')
    for off, name in ((0x254, '回读字节'), (0x260, 'start 次数'),
                      (0x264, 'rd/cw 状态'), (0x268, '读口握手'),
                      (0x26C, '压缩帧号')):
        print(f'  {name:>10} @0x{off:03x} = 0x{bar_read(off):08x}')
    return n


def capture_entropy(
    fd: int,
    *,
    timeout_ms: int = 8000,
    compress_timeout: float = 6.0,
    hdr_off: int = HDR_OFF,
    trigger: bool = True,
    force: bool = False,
    push: bool = True,
    slot_bytes: int | None = None,
    arm_retries: int = 3,
    arm_retry_delay: float = 0.3,
    strict_timeout: bool = True,
    on_stream_ready=None,
    say=None,
    on_arm_fail=None,
) -> Capture:
    """跑完两段式取流，返回熵流与耗时；硬失败抛 BoardError/BoardTimeout。

    trigger/force 控制阶段 1（是否重新压缩）：force=True 时无条件压一帧，
    否则只在 0x250 为 0 时压。push 控制阶段 2 是否写 0x40 切推帧源。
    slot_bytes 为 mmap 窗口下限，默认按 hdr_off+stream_len 页对齐。
    strict_timeout=True 时压缩超时直接抛 CompressTimeout；CLI 传 False 以保留
    “超时后仍按既有 0x250 继续”的原行为。
    on_stream_ready(n) 在阶段 1 结束、阶段 2 之前调用（如打印调试寄存器），
    返回非 None 时用作新的 stream_len；say/on_arm_fail 是 CLI 诊断回调。
    """
    say = say or _noop
    on_arm_fail = on_arm_fail or _noop

    t = time.perf_counter()
    n = bar_read(REG_STREAM_LEN)
    read_ms = (time.perf_counter() - t) * 1000.0

    compress_ms = 0.0
    if trigger and (force or n == 0):
        done0 = (bar_read(REG_JB_STATUS) >> 16) & 0xFFFF
        say(f'开压缩（ALGO_CTRL=0x{ALGO_COMPRESS:02x}），基线 jb_done={done0}，'
            f'最多等 {compress_timeout}s')
        cw0 = bar_read(REG_WRITE_PROGRESS)
        t0 = time.perf_counter()
        bar_write(ALGO_CTRL, ALGO_COMPRESS)
        timed_out = True
        while time.perf_counter() - t0 < compress_timeout:
            if ((bar_read(REG_JB_STATUS) >> 16) & 0xFFFF) != done0:
                timed_out = False
                break
            time.sleep(0.05)
        if timed_out and strict_timeout:
            raise CompressTimeout(
                f'等压缩超时（{compress_timeout}s，jb_done 未变化）')
        # jb_done 只表示熵流结束；writer 还要把最后一个 beat / 补齐字节写完。
        t1 = time.perf_counter()
        while (time.perf_counter() - t1 < TAIL_TIMEOUT and
               bar_read(REG_WRITE_PROGRESS) == cw0):
            time.sleep(TAIL_POLL)
        time.sleep(TAIL_SETTLE)
        compress_ms = (time.perf_counter() - t0) * 1000.0
        t = time.perf_counter()
        n = bar_read(REG_STREAM_LEN)
        read_ms = (time.perf_counter() - t) * 1000.0

    if on_stream_ready is not None:
        updated = on_stream_ready(n)
        if updated is not None:
            n = int(updated)
    if n <= 0:
        raise NoStream(f'码流长度非法：0x250={n}（压缩阶段没有产出）')

    if push:
        say(f'切推帧源到压缩块（ALGO_CTRL=0x{ALGO_PUSH_COMP:02x}）')
        bar_write(ALGO_CTRL, ALGO_PUSH_COMP)

    seq0, _, _, _ = frame_wait(fd, 0, 0)
    say(f'基线序号 seq={seq0}')

    landed = None
    armed = False
    for attempt in range(1, arm_retries + 1):
        rc, detail = link_ctl('arm')
        if rc == 0:
            armed = True
            break
        on_arm_fail(attempt, detail)
        # 推帧已在飞时 ARM 常返回 EIO：先看这一帧是否已落地，落地就不等。
        got = frame_wait(fd, seq0, 0)
        if got[2]:
            landed = got
            break
        if attempt < arm_retries:
            time.sleep(arm_retry_delay)
    if not armed and landed is None:
        say('ARM 未成功；若推帧已在飞，仍继续等序号变化')

    seq, count, ready, slot = (landed if landed is not None
                               else frame_wait(fd, seq0, timeout_ms))
    if not ready:
        raise FrameTimeout(seq, count)
    say(f'新帧 seq={seq} count={count} slot={slot}')

    need = hdr_off + n
    size = max(slot_bytes or 0,
               (need + SLOT_BYTES - 1) // SLOT_BYTES * SLOT_BYTES)
    t = time.perf_counter()
    with mmap.mmap(fd, size, prot=mmap.PROT_READ, flags=mmap.MAP_SHARED) as m:
        data = bytes(m[hdr_off:hdr_off + n])
    copy_ms = (time.perf_counter() - t) * 1000.0
    if len(data) != n:
        raise BoardError(
            f'取流不完整：{len(data)}/{n} 字节（mmap 窗口 {size}B）')
    return Capture(data, n, compress_ms, read_ms + copy_ms, seq, count, slot)


def capture_stream(
    path: str = FRAME_DEV,
    *,
    force_compress: bool = False,
    cleanup: bool = False,
    **kwargs,
) -> Capture:
    """打开帧设备跑一次两段式取流，返回 Capture；供其他板端脚本 import。

    path 默认 /dev/ANLOGIC-PCI0_frame；其余关键字参数原样透传
    capture_entropy()（timeout_ms / compress_timeout / say / on_arm_fail 等）。
    cleanup=True 时退出前把 ALGO_CTRL 清 0 并 ack HELD，等价于演示脚本的
    单帧收尾；CLI 默认不清，保持原行为。
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        return capture_entropy(fd, force=force_compress, **kwargs)
    finally:
        os.close(fd)
        if cleanup:
            bar_write(ALGO_CTRL, 0)
            link_ctl('ack')


def main() -> int:
    ap = argparse.ArgumentParser(
        description='板端 JPEG 两段式取流：压缩 -> 推帧 -> 从槽 0 取熵流')
    ap.add_argument('--dev', default=FRAME_DEV)
    ap.add_argument('--out', default='/tmp/fpga_jpeg.bin')
    ap.add_argument('--mb-off', type=int, default=0,
                    help='mailbox 偏移；0 = 从 frame_grab --info 自动解析')
    ap.add_argument('--hdr', type=int, default=HDR_OFF, help='槽内数据起点')
    ap.add_argument('--timeout', type=int, default=8000, help='等推帧毫秒')
    ap.add_argument('--compress-timeout', type=float, default=6.0,
                    help='等一帧压缩完成的秒数')
    ap.add_argument('--no-compress', action='store_true',
                    help='跳过压缩，直接推已有块')
    ap.add_argument('--force-compress', action='store_true',
                    help='即使 0x250 非 0 也重新压一帧')
    ap.add_argument('--ack', action='store_true', help='只做 link_ctl ack 后退出')
    ap.add_argument('--restore', action='store_true', help='取完后 ALGO_CTRL 归 0')
    args = ap.parse_args()

    if args.ack:
        # 保持与原 CLI 一致：ack 的 stdout/stderr 直接透传，退出码不参与判断。
        subprocess.run([LINK_CTL, 'ack'])
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

    fd = os.open(args.dev, os.O_RDONLY)
    try:
        try:
            cap = capture_entropy(
                fd,
                timeout_ms=args.timeout,
                compress_timeout=args.compress_timeout,
                hdr_off=args.hdr,
                trigger=not args.no_compress,
                force=args.force_compress,
                push=not args.no_compress,
                slot_bytes=args.mb_off + SLOT_BYTES,
                strict_timeout=False,       # 保留 CLI 超时后继续的原行为
                on_stream_ready=lambda _n: dump_debug(),
                say=print,
                on_arm_fail=lambda attempt, detail: print(
                    f'ARM 第 {attempt} 次失败（可能正在推帧）：{detail}'),
            )
        except NoStream:
            print('压缩没有产出：看上面的 rd/cw 状态', file=sys.stderr)
            return 3
        except FrameTimeout as e:
            print(f'超时：没有新帧（seq={e.seq} count={e.count}）',
                  file=sys.stderr)
            return 2

        with open(args.out, 'wb') as f:
            f.write(cap.data)
        print(f'写出 {len(cap.data)} 字节 -> {args.out}')
    finally:
        os.close(fd)

    if args.restore:
        bar_write(ALGO_CTRL, 0)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
