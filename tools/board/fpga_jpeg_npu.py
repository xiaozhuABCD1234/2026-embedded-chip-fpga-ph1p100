#!/usr/bin/env python3
"""fpga_jpeg_npu.py —— FPGA JPEG -> PCIe -> RK3588 解码 -> RKNN YOLO11n 全链路演示。

板端运行（Orange Pi 5 Max / RK3588）：
  python3 tools/board/fpga_jpeg_npu.py --frames 3 --show-detections

链路分段：
  1. FPGA 硬压缩：ALGO_CTRL=0x10，轮询 0x258[31:16]（jb_done）等压缩完成；
  2. PCIe 取流：ALGO_CTRL=0x40，ARM 一帧，等 FRAME_WAIT，读 0x250 长度，
     从槽 0 的 +128 处拷出纯熵流（与 /root/jpeg_cap.py 的做法一致）；
  3. 补 328 字节标准 JFIF 头 + EOI，用 PIL 解码成灰度图；
  4. 灰度复制成 RGB，按 display/src/detect/yolo.rs 的 letterbox/解码/NMS 契约
     做 RKNN 推理（默认模型与 display 相同）；
  5. PIL 画框，存图，打印各阶段耗时与检测结果。

注意：FPGA 输出的是 **纯熵编码比特流**，不含 SOI/DQT/SOF0/DHT/SOS/EOI，
所以必须用下面的常量头包装；头由 tools/jpeg_file.py 的
build_header(1920, 1088)（q=50 亮度表 + 标准 Annex K 哈夫曼表）生成。
"""
from __future__ import annotations

import argparse
import fcntl
import io
import mmap
import os
import re
import struct
import subprocess
import sys
import time
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ---- 板端设备与寄存器约定（与 /root/jpeg_cap.py 相同）----
FRAME_DEV = '/dev/ANLOGIC-PCI0_frame'
PCI_BAR = '/root/pci_bar'
LINK_CTL = '/root/link_ctl'
# _IOWR('x', 11, struct anlogic_ioc_frame_wait)，6 个 u32
IOC_FRAME_WAIT = 0xC018780B
ALGO_CTRL = 0x240
ALGO_COMPRESS = 0x10
ALGO_PUSH_COMP = 0x40
REG_STREAM_LEN = 0x250
REG_JB_STATUS = 0x258
HDR_OFF = 128                 # 槽 0 内数据起点
FRAME_W = 1920
FRAME_H = 1088
PAD_VALUE = 114               # letterbox 灰边，与 yolo.rs 一致
NUM_CLASSES = 80
MAX_DETECTIONS = 100

# build_header(1920, 1088)：SOI + APP0/JFIF + DQT(q50) + SOF0 + DHT(DC) +
# DHT(AC) + SOS，共 328 字节。不要手改，尺寸必须与 FPGA 几何一致。
JPEG_HEADER = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300100b0c0e0c0a100e0d0e1211101318281a181616183123"
    "251d283a333d3c3933383740485c4e404457453738506d51575f626768673e4d71797064785c656763ffc0000b080440"
    "078001011100ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b51000020103"
    "03020403050504040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f024336272"
    "82090a161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a737475"
    "767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9"
    "cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00"
)

COCO_NAMES = (
    'person', 'bicycle', 'car', 'motorcycle', 'airplane', 'bus', 'train',
    'truck', 'boat', 'traffic light', 'fire hydrant', 'stop sign',
    'parking meter', 'bench', 'bird', 'cat', 'dog', 'horse', 'sheep', 'cow',
    'elephant', 'bear', 'zebra', 'giraffe', 'backpack', 'umbrella', 'handbag',
    'tie', 'suitcase', 'frisbee', 'skis', 'snowboard', 'sports ball', 'kite',
    'baseball bat', 'baseball glove', 'skateboard', 'surfboard',
    'tennis racket', 'bottle', 'wine glass', 'cup', 'fork', 'knife', 'spoon',
    'bowl', 'banana', 'apple', 'sandwich', 'orange', 'broccoli', 'carrot',
    'hot dog', 'pizza', 'donut', 'cake', 'chair', 'couch', 'potted plant',
    'bed', 'dining table', 'toilet', 'tv', 'laptop', 'mouse', 'remote',
    'keyboard', 'cell phone', 'microwave', 'oven', 'toaster', 'sink',
    'refrigerator', 'book', 'clock', 'vase', 'scissors', 'teddy bear',
    'hair drier', 'toothbrush',
)


def _round_half_up(v: float) -> int:
    """Rust f32::round() 对正数等价于 floor(v + 0.5)。"""
    return int(np.floor(v + 0.5))


def bar_read(off: int) -> int:
    out = subprocess.run([PCI_BAR, '0', hex(off)], check=True,
                         capture_output=True, text=True).stdout
    m = re.search(r'Read data:\s*(0x[0-9a-fA-F]+)', out)
    if not m:
        raise RuntimeError(f'pci_bar 输出无法解析：{out!r}')
    return int(m.group(1), 0)


def bar_write(off: int, val: int) -> None:
    subprocess.run([PCI_BAR, '0', hex(off), hex(val)], check=True,
                   capture_output=True, text=True)


def link_ctl(action: str) -> int:
    """返回 link_ctl 退出码；ack/arm 失败由调用方决定是否致命。"""
    return subprocess.run([LINK_CTL, action], capture_output=True,
                          text=True).returncode


def frame_wait(fd: int, last_seq: int, timeout_ms: int):
    """等一帧 DMA；返回 (seq, count, ready, slot)。"""
    buf = struct.pack('<6I', last_seq, timeout_ms, 0, 0, 0, 0)
    raw = fcntl.ioctl(fd, IOC_FRAME_WAIT, buf)
    _ls, _to, seq, count, ready, slot = struct.unpack('<6I', raw)
    return seq, count, ready, slot


# ---- letterbox / 后处理：逐项对齐 display/src/detect/yolo.rs ----
class Letterbox:
    def __init__(self, src_w: int, src_h: int, dst_w: int, dst_h: int):
        self.src_w, self.src_h = src_w, src_h
        scale = min(dst_w / max(src_w, 1), dst_h / max(src_h, 1))
        self.scale = scale
        self.resized_w = min(max(_round_half_up(src_w * scale), 1), dst_w)
        self.resized_h = min(max(_round_half_up(src_h * scale), 1), dst_h)
        self.pad_x = (dst_w - self.resized_w) / 2.0
        self.pad_y = (dst_h - self.resized_h) / 2.0

    def to_norm(self, x: float, y: float):
        return ((x - self.pad_x) / self.scale / self.src_w,
                (y - self.pad_y) / self.scale / self.src_h)


def letterbox_rgb(rgb: np.ndarray, dst_w: int = 640, dst_h: int = 640):
    """RGB8 HWC -> 灰边 114 的 NHWC；双线性采样点与 yolo.rs 完全相同。"""
    src_h, src_w = rgb.shape[:2]
    lb = Letterbox(src_w, src_h, dst_w, dst_h)
    out = np.full((dst_h, dst_w, 3), PAD_VALUE, dtype=np.uint8)

    dy = np.arange(lb.resized_h, dtype=np.float64)
    sy = np.maximum((dy + 0.5) / lb.scale - 0.5, 0.0)
    y0 = np.minimum(np.floor(sy).astype(np.int32), src_h - 1)
    y1 = np.minimum(y0 + 1, src_h - 1)
    fy = sy - y0
    dx = np.arange(lb.resized_w, dtype=np.float64)
    sx = np.maximum((dx + 0.5) / lb.scale - 0.5, 0.0)
    x0 = np.minimum(np.floor(sx).astype(np.int32), src_w - 1)
    x1 = np.minimum(x0 + 1, src_w - 1)
    fx = sx - x0

    r = rgb.astype(np.float64)
    top = r[y0[:, None], x0[None, :], :] * (1.0 - fx)[None, :, None] + \
        r[y0[:, None], x1[None, :], :] * fx[None, :, None]
    bot = r[y1[:, None], x0[None, :], :] * (1.0 - fx)[None, :, None] + \
        r[y1[:, None], x1[None, :], :] * fx[None, :, None]
    vals = np.floor(top * (1.0 - fy)[:, None, None] +
                    bot * fy[:, None, None] + 0.5).astype(np.uint8)
    oy, ox = int(lb.pad_y), int(lb.pad_x)   # Rust 里 pad 转 usize 是截断
    out[oy:oy + lb.resized_h, ox:ox + lb.resized_w, :] = vals
    return out, lb


@dataclass
class Detection:
    x1: float
    y1: float
    x2: float
    y2: float
    class_id: int
    score: float


def _as_2d(a, name: str) -> np.ndarray:
    a = np.asarray(a)
    if a.ndim == 3 and a.shape[0] == 1:
        a = a[0]
    a = np.squeeze(a)
    if a.ndim != 2:
        raise RuntimeError(f'{name} 输出不是二维：shape={np.asarray(a).shape}')
    return a


def _iou(a: Detection, b: Detection) -> float:
    x1, y1 = max(a.x1, b.x1), max(a.y1, b.y1)
    x2, y2 = min(a.x2, b.x2), min(a.y2, b.y2)
    iw, ih = max(x2 - x1, 0.0), max(y2 - y1, 0.0)
    inter = iw * ih
    ua = (max(a.x2 - a.x1, 0.0) * max(a.y2 - a.y1, 0.0) +
          max(b.x2 - b.x1, 0.0) * max(b.y2 - b.y1, 0.0) - inter)
    return inter / ua if ua > 0.0 else 0.0


def decode_yolo(outs, lb: Letterbox, conf: float, nms: float):
    """支持单输出 [1,84,N] 与双输出 boxes [1,4,N]/scores [1,80,N]。

    RKNNLite 的 get_outputs 已按 want_float 反量化，这里拿到的是 float32；
    若换到直接返回 int8 的运行时，必须先按 attr.zp/scale 反量化再进本函数。
    """
    if len(outs) == 1:
        a = _as_2d(outs[0], 'output0')
        if a.shape[0] == 4 + NUM_CLASSES:
            ch_first = True
        elif a.shape[1] == 4 + NUM_CLASSES:
            ch_first = False
        else:
            raise RuntimeError(f'单输出 shape 不接受：{a.shape}，期望 [84,N]/[N,84]')
        boxes = a[:4, :] if ch_first else a[:, :4]
        scores = a[4:, :] if ch_first else a[:, 4:]
    elif len(outs) == 2:
        o0, o1 = _as_2d(outs[0], 'output0'), _as_2d(outs[1], 'output1')
        # 按通道数认 boxes / scores，不依赖输出顺序。
        def has_c(a2, c):
            return a2.shape[0] == c or a2.shape[1] == c
        if has_c(o0, 4) and has_c(o1, NUM_CLASSES):
            boxes, scores = o0, o1
        elif has_c(o1, 4) and has_c(o0, NUM_CLASSES):
            boxes, scores = o1, o0
        else:
            raise RuntimeError(
                f'双输出 shape 不接受：{o0.shape}, {o1.shape}，期望 4 通道 + 80 通道')
    else:
        raise RuntimeError(f'模型输出数 {len(outs)}，只支持 1 或 2')

    b_first = boxes.shape[0] == 4
    s_first = scores.shape[0] == NUM_CLASSES
    n = boxes.shape[1] if b_first else boxes.shape[0]
    sn = scores.shape[1] if s_first else scores.shape[0]
    if n != sn:
        raise RuntimeError(f'boxes N={n} 与 scores N={sn} 不一致')

    if b_first:
        cx, cy, bw, bh = boxes[0], boxes[1], boxes[2], boxes[3]
    else:
        cx, cy, bw, bh = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    sc = scores if s_first else scores.T          # (80, N)
    sc = np.where(np.isfinite(sc), sc, -np.inf)
    cls = np.argmax(sc, axis=0)
    best = sc[cls, np.arange(n)]

    # 不管阈值，先给出原始最高分，便于判断暗场景是否只是分数低。
    order = np.argsort(-best)[:5]
    top = [(float(best[i]), int(cls[i]), float(cx[i]), float(cy[i]))
           for i in order if np.isfinite(best[i])]

    ok = (np.isfinite(best) & (best >= conf) &
          np.isfinite(cx) & np.isfinite(cy) & np.isfinite(bw) & np.isfinite(bh) &
          (bw > 0) & (bh > 0))
    raw = []
    for i in np.nonzero(ok)[0]:
        x1, y1 = lb.to_norm(float(cx[i] - bw[i] * 0.5), float(cy[i] - bh[i] * 0.5))
        x2, y2 = lb.to_norm(float(cx[i] + bw[i] * 0.5), float(cy[i] + bh[i] * 0.5))
        x1, y1 = min(max(x1, 0.0), 1.0), min(max(y1, 0.0), 1.0)
        x2, y2 = min(max(x2, 0.0), 1.0), min(max(y2, 0.0), 1.0)
        if x2 <= x1 or y2 <= y1:
            continue
        raw.append(Detection(x1, y1, x2, y2, int(cls[i]), float(best[i])))

    raw.sort(key=lambda d: -d.score)
    keep = []
    for d in raw:
        if len(keep) >= MAX_DETECTIONS:
            break
        if not any(k.class_id == d.class_id and _iou(k, d) > nms for k in keep):
            keep.append(d)
    return keep, top


class Detector:
    def __init__(self, model: str):
        from rknnlite.api import RKNNLite    # 延迟导入，--help 不需要板端库
        self.rknn = RKNNLite()
        if self.rknn.load_rknn(model) != 0:
            raise RuntimeError(f'加载 RKNN 模型失败：{model}')
        if self.rknn.init_runtime(core_mask=RKNNLite.NPU_CORE_AUTO) != 0:
            raise RuntimeError('RKNN init_runtime 失败')

        # 用 tensor attr 校验模型契约，并记下输出 zp/scale 作反量化兜底。
        # 正常情况下 rknnlite 的 get_outputs 已按 want_float 返回 float32。
        rt = self.rknn.rknn_runtime
        ia = rt.get_tensor_attr(0, False)
        dims = tuple(ia.dims[:ia.n_dims])
        if len(dims) != 4:
            raise RuntimeError(f'模型输入不是 4 维：{dims}')
        h, w = (dims[1], dims[2]) if ia.fmt == 1 else (dims[2], dims[3])
        if (w, h) != (640, 640):
            raise RuntimeError(f'模型输入 {w}x{h}，本脚本按 640x640 契约实现')
        self.out_q = []
        for i in range(rt.get_in_out_num()[1]):
            a = rt.get_tensor_attr(i, True)
            zp = int(a.zp)
            if zp >= 1 << 31:                 # C attr 里 zp 是无符号 u32
                zp -= 1 << 32
            self.out_q.append((zp, float(a.scale)))

    def close(self):
        self.rknn.release()

    def _dequant(self, outs):
        fixed = []
        for o, (zp, scale) in zip(outs, self.out_q):
            a = np.asarray(o)
            if a.dtype.kind in 'iu':          # 仅当运行时没做 want_float 时
                a = (a.astype(np.float32) - np.float32(zp)) * np.float32(scale)
            fixed.append(a)
        return fixed

    def run(self, rgb: np.ndarray, conf: float, nms: float):
        t0 = time.perf_counter()
        inp, lb = letterbox_rgb(rgb, 640, 640)
        t1 = time.perf_counter()
        outs = self.rknn.inference(inputs=[inp[None, ...]], data_format='nhwc')
        t2 = time.perf_counter()
        if not outs:
            raise RuntimeError('RKNN inference 返回 None（看上面 RKNN 日志）')
        dets, top = decode_yolo(self._dequant(outs), lb, conf, nms)
        t3 = time.perf_counter()
        return dets, top, (t1 - t0) * 1000.0, (t2 - t1) * 1000.0, (t3 - t2) * 1000.0


def capture_frame(fd: int, timeout_ms: int, compress_timeout: float):
    """压缩一帧并取回纯熵流；返回 (entropy, compress_wait_ms, pcie_read_ms)。"""
    bar_write(ALGO_CTRL, 0)
    done0 = (bar_read(REG_JB_STATUS) >> 16) & 0xFFFF
    t0 = time.perf_counter()
    bar_write(ALGO_CTRL, ALGO_COMPRESS)
    while True:
        if ((bar_read(REG_JB_STATUS) >> 16) & 0xFFFF) != done0:
            break
        if time.perf_counter() - t0 > compress_timeout:
            raise TimeoutError(f'等压缩超时（{compress_timeout}s，jb_done 未变化）')
        time.sleep(0.03)
    # writer 还要把尾部补齐字节写完，和 jpeg_cap.py 一样留 50ms 余量。
    time.sleep(0.05)
    compress_ms = (time.perf_counter() - t0) * 1000.0

    # 0x250 必须在切到推流（0x40）之前读：切过去后该寄存器可能被清 0，
    # jpeg_cap.py 也是先快照长度、再 ARM。这里把这次 BAR 读计入 PCIe 阶段。
    t_len = time.perf_counter()
    n = bar_read(REG_STREAM_LEN)
    bar_ms = (time.perf_counter() - t_len) * 1000.0
    if n <= 0:
        raise RuntimeError(f'码流长度非法：0x250={n}（压缩阶段没有产出）')

    bar_write(ALGO_CTRL, ALGO_PUSH_COMP)
    try:
        seq0, _, _, _ = frame_wait(fd, 0, 0)
        frame = None
        for attempt in range(3):
            rc = link_ctl('arm')
            if rc == 0:
                break
            # 推帧已在飞时 ARM 常返回 EIO；先看这一帧是否已落地，落地就不等。
            print(f'  ARM 第 {attempt + 1} 次失败（推帧在飞）', file=sys.stderr)
            got = frame_wait(fd, seq0, 0)
            if got[2]:
                frame = got
                break
            time.sleep(0.1)
        seq, count, ready, slot = frame if frame else frame_wait(fd, seq0, timeout_ms)
        if not ready:
            raise TimeoutError(f'等新帧超时：seq={seq} count={count}')
        t_copy = time.perf_counter()
        size = ((HDR_OFF + n + 4095) // 4096) * 4096
        with mmap.mmap(fd, size, prot=mmap.PROT_READ,
                       flags=mmap.MAP_SHARED) as m:
            data = bytes(m[HDR_OFF:HDR_OFF + n])
        pcie_ms = bar_ms + (time.perf_counter() - t_copy) * 1000.0
    finally:
        # 每帧收尾：压缩/推流源归零并释放 HELD，保证下一轮从干净状态开始。
        bar_write(ALGO_CTRL, 0)
        link_ctl('ack')
    return data, compress_ms, pcie_ms


def draw_and_save(gray: np.ndarray, dets, out: str) -> None:
    img = Image.fromarray(gray, mode='L').convert('RGB')
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=20)
    except TypeError:
        font = ImageFont.load_default()
    palette = ((230, 40, 40), (40, 200, 60), (40, 120, 255),
               (240, 170, 30), (200, 50, 220), (30, 210, 210))
    for det in dets:
        x1, y1 = int(det.x1 * FRAME_W), int(det.y1 * FRAME_H)
        x2, y2 = int(det.x2 * FRAME_W), int(det.y2 * FRAME_H)
        color = palette[det.class_id % len(palette)]
        d.rectangle((x1, y1, x2, y2), outline=color, width=3)
        name = COCO_NAMES[det.class_id] if det.class_id < len(COCO_NAMES) else str(det.class_id)
        text = f'{name} {det.score:.2f}'
        tb = d.textbbox((0, 0), text, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        ty = max(y1 - th - 6, 0)
        d.rectangle((x1, ty, x1 + tw + 6, ty + th + 6), fill=color)
        d.text((x1 + 3, ty + 2), text, fill=(255, 255, 255), font=font)
    img.save(out, quality=92)


def main() -> int:
    ap = argparse.ArgumentParser(
        description='FPGA JPEG -> PCIe -> RK3588 解码 -> RKNN YOLO11n 演示')
    ap.add_argument('--model', default='/root/models/yolo11n-640-int8.rknn',
                    help='RKNN 模型（默认与 display 一致）')
    ap.add_argument('--conf', type=float, default=0.25, help='置信度阈值')
    ap.add_argument('--iou', type=float, default=0.45, help='NMS IoU 阈值')
    ap.add_argument('--frames', type=int, default=3, help='采集/推理帧数')
    ap.add_argument('--out', default='/tmp/fpga_npu.jpg', help='标注图输出')
    ap.add_argument('--show-detections', action='store_true',
                    help='逐框打印 class/score/box')
    ap.add_argument('--timeout-ms', type=int, default=8000, help='等帧毫秒')
    ap.add_argument('--compress-timeout', type=float, default=6.0,
                    help='等压缩完成秒数')
    args = ap.parse_args()
    if args.frames < 1:
        print('--frames 必须 >= 1', file=sys.stderr)
        return 2
    if not (0.0 <= args.conf <= 1.0 and 0.0 <= args.iou <= 1.0):
        print('--conf/--iou 必须在 0..1', file=sys.stderr)
        return 2

    if len(JPEG_HEADER) != 328:
        print(f'内置 JPEG 头长度 {len(JPEG_HEADER)} != 328，脚本损坏', file=sys.stderr)
        return 2

    fd = None
    det = None
    try:
        det = Detector(args.model)
        print(f'模型：{args.model}')
        fd = os.open(FRAME_DEV, os.O_RDONLY)
        bar_write(ALGO_CTRL, 0)
        link_ctl('ack')

        recs = []
        for fi in range(args.frames):
            t_frame = time.perf_counter()
            entropy, cms, pms = capture_frame(
                fd, args.timeout_ms, args.compress_timeout)
            t_dec = time.perf_counter()
            jpeg = JPEG_HEADER + entropy + b'\xff\xd9'
            try:
                with Image.open(io.BytesIO(jpeg)) as im:
                    im.load()
                    gray = np.asarray(im.convert('L'), dtype=np.uint8)
            except Exception as e:
                raise RuntimeError(
                    f'第 {fi + 1} 帧 JPEG 解码失败（熵流 {len(entropy)} 字节）：'
                    f'{e}；检查几何/头/FPGA 码流，脚本不重试') from e
            if gray.shape != (FRAME_H, FRAME_W):
                raise RuntimeError(
                    f'解码尺寸 {gray.shape[1]}x{gray.shape[0]}，'
                    f'期望 {FRAME_W}x{FRAME_H}')
            jms = (time.perf_counter() - t_dec) * 1000.0
            rgb = np.repeat(gray[:, :, None], 3, axis=2)
            dets, top, pre_ms, inf_ms, post_ms = det.run(rgb, args.conf, args.iou)
            draw_and_save(gray, dets, args.out)
            total_ms = (time.perf_counter() - t_frame) * 1000.0
            recs.append((cms, pms, jms, inf_ms, total_ms))

            print(f'[帧 {fi + 1}/{args.frames}] '
                  f'compress_wait_ms={cms:.1f} pcie_read_ms={pms:.1f} '
                  f'jpeg_decode_ms={jms:.1f} rknn_infer_ms={inf_ms:.1f} '
                  f'total_ms={total_ms:.1f}')
            print(f'  JPEG {FRAME_W}x{FRAME_H}，熵流 {len(entropy)} B；'
                  f'letterbox {pre_ms:.1f} ms，后处理 {post_ms:.1f} ms；'
                  f'检测 {len(dets)} 个')
            if top:
                ts, tc, tx, ty = top[0]
                nm = COCO_NAMES[tc] if tc < len(COCO_NAMES) else str(tc)
                print(f'  原始最高分 {ts:.4f}（{nm}）@ 模型坐标 '
                      f'({tx:.1f},{ty:.1f})')
            if args.show_detections:
                for d in dets:
                    x1, y1 = int(d.x1 * FRAME_W), int(d.y1 * FRAME_H)
                    x2, y2 = int(d.x2 * FRAME_W), int(d.y2 * FRAME_H)
                    nm = COCO_NAMES[d.class_id] if d.class_id < len(COCO_NAMES) else str(d.class_id)
                    print(f'    {nm:<14} score={d.score:.4f} '
                          f'box=({x1},{y1},{x2},{y2})')
        print(f'输出：{args.out}')
        n = len(recs)
        if n:
            avg = [sum(r[i] for r in recs) / n for i in range(5)]
            print(f'平均（{n} 帧）compress_wait_ms={avg[0]:.1f} '
                  f'pcie_read_ms={avg[1]:.1f} jpeg_decode_ms={avg[2]:.1f} '
                  f'rknn_infer_ms={avg[3]:.1f} total_ms={avg[4]:.1f}')
        return 0
    except Exception as e:
        print(f'错误：{e}', file=sys.stderr)
        return 2
    finally:
        # 退出前一定把 ALGO_CTRL 归零并释放 HELD 帧。
        try:
            bar_write(ALGO_CTRL, 0)
        except Exception:
            pass
        try:
            link_ctl('ack')
        except Exception:
            pass
        if det is not None:
            det.close()
        if fd is not None:
            os.close(fd)


if __name__ == '__main__':
    raise SystemExit(main())