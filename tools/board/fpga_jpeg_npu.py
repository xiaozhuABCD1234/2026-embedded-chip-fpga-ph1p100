#!/usr/bin/env python3
"""fpga_jpeg_npu.py —— FPGA JPEG -> PCIe -> RK3588 解码 -> RKNN YOLO11n 全链路演示。

板端运行（Orange Pi 5 Max / RK3588；需与 jpeg_cap.py / jpeg_file.py /
jpeg_ref.py 放同一目录，demo.sh 负责一起 scp 到 /tmp）：
  python3 /tmp/fpga_jpeg_npu.py --frames 3 --show-detections

链路：jpeg_cap.capture_stream() 两段式取流 -> jpeg_file.build_jpeg() 补标准
JFIF 头 -> PIL 解码 -> letterbox/RKNN/NMS（对齐 display/src/detect/yolo.rs）
-> 画框存图。FPGA 只输出纯熵流，不含 SOI/DQT/SOF0/DHT/SOS/EOI。
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import time
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# 仓库内：jpeg_cap 在 tools/board/，jpeg_file/jpeg_ref 在 tools/；
# 板端：四个脚本都在 /tmp。两种布局都把所在目录塞进 sys.path。
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
for _p in (_HERE, _PARENT):
    if _p in sys.path:
        continue
    # 只把确实含依赖脚本的目录放进 sys.path（板端 /tmp 的父目录是 /，不能乱加）。
    if any(os.path.exists(os.path.join(_p, name))
           for name in ('jpeg_cap.py', 'jpeg_file.py')):
        sys.path.insert(0, _p)

from jpeg_cap import (  # noqa: E402
    ALGO_CTRL,
    bar_write,
    capture_stream,
    link_ctl,
)
from jpeg_file import build_jpeg  # noqa: E402

FRAME_W = 1920
FRAME_H = 1088
PAD_VALUE = 114               # letterbox 灰边，与 yolo.rs 一致
NUM_CLASSES = 80
MAX_DETECTIONS = 100

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

    det = None
    try:
        det = Detector(args.model)
        print(f'模型：{args.model}')
        bar_write(ALGO_CTRL, 0)
        link_ctl('ack')

        recs = []
        for fi in range(args.frames):
            t_frame = time.perf_counter()
            cap = capture_stream(
                force_compress=True,
                cleanup=True,
                timeout_ms=args.timeout_ms,
                compress_timeout=args.compress_timeout,
                arm_retry_delay=0.1,      # 与原 capture_frame 的 100 ms 重试一致
                on_arm_fail=lambda attempt, _detail: print(
                    f'  ARM 第 {attempt} 次失败（推帧在飞）', file=sys.stderr),
            )
            entropy, cms, pms = cap.data, cap.compress_ms, cap.pcie_ms
            t_dec = time.perf_counter()
            jpeg = build_jpeg(entropy, width=FRAME_W, height=FRAME_H)
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


if __name__ == '__main__':
    raise SystemExit(main())