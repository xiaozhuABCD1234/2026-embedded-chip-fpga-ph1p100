#!/usr/bin/env python3
"""selftest.py —— tools/ 的纯本地自检：不 ssh、不碰板子、不写 /dev。

覆盖：pci_bar 输出解析与错误映射、两段式取流的编排（fake BAR/frame_wait/
mmap）、JPEG 328 字节头 golden hash、jpeg_ref -> jpeg_file -> Pillow 回环、
fpga_jpeg_npu 的 letterbox/YOLO 后处理契约。

用法：python3 tools/selftest.py
缺 numpy/Pillow 时相关项打印 SKIP（其余仍会跑）。
"""
from __future__ import annotations

import hashlib
import io
import os
import struct
import subprocess
import sys
import tempfile
import traceback
from contextlib import contextmanager
from unittest import mock

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, 'board'))

import jpeg_cap  # noqa: E402  （纯标准库，先导入用于 fake 测试）

# build_header(1920, 1088) 的历史 328 字节常量，重构时逐字节不变。
GOLDEN_HEADER_SHA256 = '36bd2f434d88e1dbad34c4e9e739e18b93603256f04f017b1701c4ab4c183bd5'

FAILS = []
SKIPS = []


class Skip(Exception):
    pass


def need(*modules):
    for name in modules:
        try:
            __import__(name)
        except ImportError as e:
            raise Skip(f'缺少 {name}') from e


@contextmanager
def raises(exc):
    try:
        yield
    except exc:
        pass
    else:
        raise AssertionError(f'没有抛出 {exc.__name__}')


def check(name, fn):
    try:
        fn()
    except Skip as e:
        SKIPS.append(name)
        print(f'SKIP {name}（{e}）')
    except Exception as e:  # noqa: BLE001 - 自检要把所有失败收成一行
        FAILS.append(name)
        print(f'FAIL {name}: {e!r}')
        traceback.print_exc(limit=3)
    else:
        print(f'PASS {name}')


def _proc(stdout='', stderr='', returncode=0):
    p = mock.Mock()
    p.stdout, p.stderr, p.returncode = stdout, stderr, returncode
    return p


class FakeBoard:
    """用内存寄存器模拟 pci_bar + 压缩完成 + writer 尾拍。"""

    def __init__(self, stream_len=93, payload=b'\xa5' * 93,
                 compress_completes=True):
        self.stream_len = stream_len
        self.payload = payload
        self.compress_completes = compress_completes
        self.jb_done = 0
        self.write_progress = 7
        self.writes = []

    def read(self, off):
        if off == jpeg_cap.REG_STREAM_LEN:
            return self.stream_len
        if off == jpeg_cap.REG_JB_STATUS:
            return self.jb_done << 16
        if off == jpeg_cap.REG_WRITE_PROGRESS:
            return self.write_progress
        return 0

    def write(self, off, val):
        self.writes.append((off, val))
        if off == jpeg_cap.ALGO_CTRL and val == jpeg_cap.ALGO_COMPRESS:
            self.write_progress += 1      # writer 帧号总会推进
            if self.compress_completes:
                self.jb_done += 1


class FakeMmap:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def __getitem__(self, key):
        return self.data[key]


@contextmanager
def fake_board(board, frame_seq=(5, 1, 0, 0), frame_new=(6, 2, 1, 3),
               arm_rc=0, arm_err=''):
    """替换 jpeg_cap 的板端原语；mmap 窗口里在 +128 处放 payload。"""
    calls = []

    def fw(_fd, last_seq, timeout_ms):
        calls.append((last_seq, timeout_ms))
        return frame_seq if last_seq == 0 else frame_new

    def arm(_action):
        return arm_rc, arm_err

    def mmap_ctor(_fd, size, prot=0, flags=0):
        buf = bytearray(size)
        payload = board.payload
        buf[jpeg_cap.HDR_OFF:jpeg_cap.HDR_OFF + len(payload)] = payload
        return FakeMmap(bytes(buf))

    with mock.patch.object(jpeg_cap, 'bar_read', board.read), \
            mock.patch.object(jpeg_cap, 'bar_write', board.write), \
            mock.patch.object(jpeg_cap, 'link_ctl', arm), \
            mock.patch.object(jpeg_cap, 'frame_wait', fw), \
            mock.patch.object(jpeg_cap.mmap, 'mmap', mmap_ctor):
        yield calls


def test_parse_bar():
    assert jpeg_cap.parse_bar_read('Read data: 0x1234abcd\n') == 0x1234ABCD
    assert jpeg_cap.parse_bar_read('Read data: 0x00000000\n') == 0
    with raises(jpeg_cap.BoardError):
        jpeg_cap.parse_bar_read('no such field')


def test_bar_cli():
    calls = []

    def fake(cmd, **_kwargs):
        calls.append(cmd)
        return _proc(stdout='Read data: 0x0000002a\n')

    with mock.patch.object(jpeg_cap.subprocess, 'run', fake):
        assert jpeg_cap.bar_read(0x250) == 0x2A
        jpeg_cap.bar_write(0x240, 0x10)
    assert calls == [['/root/pci_bar', '0', '0x250'],
                     ['/root/pci_bar', '0', '0x240', '0x10']]

    with mock.patch.object(jpeg_cap.subprocess, 'run',
                           side_effect=FileNotFoundError):
        with raises(jpeg_cap.BoardError):
            jpeg_cap.bar_read(0)
    with mock.patch.object(
            jpeg_cap.subprocess, 'run',
            side_effect=subprocess.CalledProcessError(7, 'pci_bar', stderr='EIO')):
        with raises(jpeg_cap.BoardError):
            jpeg_cap.bar_read(0)


def test_frame_wait():
    raw = struct.pack('<6I', 11, 500, 7, 3, 1, 2)
    with mock.patch.object(jpeg_cap.fcntl, 'ioctl', return_value=raw):
        assert jpeg_cap.frame_wait(9, 6, 100) == (7, 3, 1, 2)


def test_capture_happy():
    board = FakeBoard()
    msgs = []
    with fake_board(board) as calls:
        cap = jpeg_cap.capture_entropy(
            42, trigger=True, force=True, push=True, timeout_ms=1234,
            say=msgs.append)
    assert cap.data == board.payload
    assert (cap.stream_len, cap.seq, cap.count, cap.slot) == (93, 6, 2, 3)
    assert (jpeg_cap.ALGO_CTRL, jpeg_cap.ALGO_COMPRESS) in board.writes
    assert (jpeg_cap.ALGO_CTRL, jpeg_cap.ALGO_PUSH_COMP) in board.writes
    assert calls[0] == (0, 0) and calls[-1] == (5, 1234)
    assert any('开压缩' in m for m in msgs)
    assert any('切推帧源' in m for m in msgs)
    assert any('新帧' in m for m in msgs)
    assert cap.compress_ms >= 0.0 and cap.pcie_ms >= 0.0


def test_capture_reuse_existing():
    board = FakeBoard(stream_len=93)
    with fake_board(board):
        cap = jpeg_cap.capture_entropy(
            42, trigger=True, force=False, push=False)
    assert (jpeg_cap.ALGO_CTRL, jpeg_cap.ALGO_COMPRESS) not in board.writes
    assert (jpeg_cap.ALGO_CTRL, jpeg_cap.ALGO_PUSH_COMP) not in board.writes
    assert cap.compress_ms == 0.0 and cap.data == board.payload


def test_capture_no_stream():
    board = FakeBoard(stream_len=0)
    with fake_board(board):
        with raises(jpeg_cap.NoStream):
            jpeg_cap.capture_entropy(42, trigger=False)


def test_capture_compress_timeout():
    board = FakeBoard(compress_completes=False)
    with fake_board(board):
        with raises(jpeg_cap.CompressTimeout):
            jpeg_cap.capture_entropy(
                42, trigger=True, force=True, compress_timeout=0.01)


def test_capture_compress_timeout_tolerant():
    board = FakeBoard(stream_len=0, compress_completes=False)
    seen = []
    with fake_board(board):
        with raises(jpeg_cap.NoStream):
            jpeg_cap.capture_entropy(
                42, trigger=True, force=True, compress_timeout=0.01,
                strict_timeout=False, on_stream_ready=seen.append)
    assert seen == [0]


def test_frame_timeout_attrs():
    board = FakeBoard()
    try:
        with fake_board(board, frame_new=(6, 1, 0, 0)):
            jpeg_cap.capture_entropy(42, trigger=False, push=False)
    except jpeg_cap.FrameTimeout as e:
        assert (e.seq, e.count) == (6, 1)
        assert '等新帧超时' in str(e)
    else:
        raise AssertionError('没有抛 FrameTimeout')


def test_arm_retry_uses_landed_frame():
    board = FakeBoard()
    failed = []

    def on_fail(attempt, detail):
        failed.append((attempt, detail))

    with fake_board(board, arm_rc=1, arm_err='EIO'):
        cap = jpeg_cap.capture_entropy(
            42, trigger=False, push=False, on_arm_fail=on_fail)
    assert failed == [(1, 'EIO')]
    assert (cap.seq, cap.count, cap.slot) == (6, 2, 3)


def test_capture_stream_wrapper():
    sentinel = object()
    captured = {}

    def fake_capture(fd, **kwargs):
        captured['fd'] = fd
        captured['kwargs'] = kwargs
        return sentinel

    with mock.patch.object(jpeg_cap.os, 'open', return_value=77), \
            mock.patch.object(jpeg_cap.os, 'close') as close, \
            mock.patch.object(jpeg_cap, 'capture_entropy', fake_capture), \
            mock.patch.object(jpeg_cap, 'bar_write') as bw, \
            mock.patch.object(jpeg_cap, 'link_ctl', return_value=(0, '')) as lc:
        got = jpeg_cap.capture_stream(
            '/dev/fake', force_compress=True, cleanup=True, timeout_ms=99)
    assert got is sentinel
    assert captured['fd'] == 77
    assert captured['kwargs'] == {'force': True, 'timeout_ms': 99}
    close.assert_called_once_with(77)
    bw.assert_called_once_with(jpeg_cap.ALGO_CTRL, 0)
    lc.assert_called_once_with('ack')


def test_cap_cli():
    import contextlib

    board = FakeBoard()
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, 'frame.bin')
        argv = ['jpeg_cap.py', '--mb-off', '4096', '--out', out,
                '--force-compress', '--restore']
        text = io.StringIO()
        with fake_board(board), \
                mock.patch.object(jpeg_cap.os, 'open', return_value=5), \
                mock.patch.object(jpeg_cap.os, 'close'), \
                mock.patch.object(jpeg_cap.sys, 'argv', argv), \
                contextlib.redirect_stdout(text):
            rc = jpeg_cap.main()
        assert rc == 0
        with open(out, 'rb') as f:
            assert f.read() == board.payload
        log = text.getvalue()
        assert '开压缩' in log and '切推帧源' in log
        assert '写出 93 字节 -> ' in log
        assert 'mailbox 偏移' not in log
        assert (jpeg_cap.ALGO_CTRL, 0) in board.writes   # --restore


def test_jpeg_header_golden():
    need('numpy')
    from jpeg_file import build_header, build_jpeg, check_header

    header = build_header(1920, 1088)
    assert len(header) == 328
    assert hashlib.sha256(header).hexdigest() == GOLDEN_HEADER_SHA256
    names, end = check_header(header)
    assert names == ['APP0', 'DQT', 'SOF0', 'DHT', 'DHT', 'SOS']
    assert end == 328

    # 尺寸参数可复用：任意尺寸仍是 328 字节头，SOF0 里是 16 bit 大端尺寸。
    small = build_header(640, 480)
    assert len(small) == 328 and small != header
    i = small.find(b'\xff\xc0')
    sof = small[i + 4:i + 10]
    assert ((sof[3] << 8) | sof[4], (sof[1] << 8) | sof[2]) == (640, 480)
    assert sof[0] == 8 and sof[5] == 1

    wrapped = build_jpeg(b'\x00\x01', 1920, 1088)
    assert wrapped[:-2] == header + b'\x00\x01'
    assert wrapped[-2:] == b'\xff\xd9'

    with raises(ValueError):
        build_header(0, 1088)


def test_jpeg_roundtrip():
    need('numpy', 'PIL')
    import numpy as np
    from PIL import Image
    from jpeg_file import build_jpeg, check_jpeg
    from jpeg_ref import encode_entropy

    img = (np.arange(64 * 48).reshape(48, 64) % 251).astype(np.float64)
    entropy = encode_entropy(img, quality=50)
    jpg = build_jpeg(entropy, 64, 48)
    with Image.open(io.BytesIO(jpg)) as im:
        im.load()
        assert (im.format, im.mode, im.size) == ('JPEG', 'L', (64, 48))
        arr = np.asarray(im.convert('L'), dtype=np.float64)
    mse = float(np.mean((arr - img) ** 2))
    assert mse < 120.0, f'回环 MSE 过大：{mse}'

    with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as f:
        f.write(jpg)
        path = f.name
    try:
        r = check_jpeg(path)
        assert r['passed'] and r['header_bytes'] == 328
        assert r['entropy_bytes'] == len(entropy)
        assert (r['width'], r['height']) == (64, 48)
        same = check_jpeg(path, path, threshold=99.0)
        assert same['psnr'] == float('inf') and same['passed']
    finally:
        os.unlink(path)


def test_npu_postprocess():
    need('numpy', 'PIL')
    import numpy as np
    import fpga_jpeg_npu as npu
    from jpeg_file import build_jpeg

    assert not hasattr(npu, 'JPEG_HEADER')      # 头常量已收敛到 jpeg_file
    assert npu.build_jpeg is build_jpeg

    out, lb = npu.letterbox_rgb(np.zeros((1088, 1920, 3), np.uint8), 640, 640)
    assert out.shape == (640, 640, 3) and out.dtype == np.uint8
    assert lb.scale < 1.0
    assert (out[0, 0] == npu.PAD_VALUE).all()

    n = 5
    a = np.zeros((1, 4 + npu.NUM_CLASSES, n), np.float32)
    a[0, 4 + 3, 2] = 0.9                     # 类别 3 高分
    a[0, 0, 2], a[0, 1, 2] = 320.0, 320.0
    a[0, 2, 2], a[0, 3, 2] = 100.0, 100.0
    dets, top = npu.decode_yolo(
        [a], npu.Letterbox(640, 640, 640, 640), conf=0.25, nms=0.45)
    assert len(dets) == 1 and dets[0].class_id == 3
    assert abs(dets[0].score - 0.9) < 1e-6
    assert top and abs(top[0][0] - 0.9) < 1e-6

    with raises(RuntimeError):
        npu._as_2d(np.zeros(3), 'bad')


def test_stab_compress_parse():
    import stab_compress as stab

    with mock.patch.object(stab.subprocess, 'run',
                           return_value=_proc(stdout='Read data: 0x0000002a\n')):
        assert stab.rd(0x250) == 0x2A
    with mock.patch.object(stab.subprocess, 'run',
                           return_value=_proc(stdout='garbage')):
        assert stab.rd(0x250) == -1


def test_py_compile():
    import py_compile

    for name in ('jpeg_ref.py', 'jpeg_file.py', 'board/jpeg_cap.py',
                 'board/fpga_jpeg_npu.py', 'board/stab_compress.py'):
        py_compile.compile(os.path.join(_ROOT, name), doraise=True)


def main() -> int:
    tests = [
        ('pci_bar 输出解析', test_parse_bar),
        ('pci_bar 读写命令/错误映射', test_bar_cli),
        ('FRAME_WAIT ioctl 解包', test_frame_wait),
        ('两段式取流（正常帧）', test_capture_happy),
        ('复用已有 0x250 码流（不压缩/不推流）', test_capture_reuse_existing),
        ('0x250=0 -> NoStream', test_capture_no_stream),
        ('jb_done 不跳 -> CompressTimeout', test_capture_compress_timeout),
        ('CLI 超时宽容模式仍读 0x250', test_capture_compress_timeout_tolerant),
        ('等帧超时 -> FrameTimeout', test_frame_timeout_attrs),
        ('ARM 失败但帧已落地', test_arm_retry_uses_landed_frame),
        ('capture_stream(path) 包装', test_capture_stream_wrapper),
        ('jpeg_cap CLI（fake 板端 -> 文件）', test_cap_cli),
        ('JPEG 328 字节头 golden hash', test_jpeg_header_golden),
        ('jpeg_ref->jpeg_file->Pillow 回环', test_jpeg_roundtrip),
        ('npu letterbox/decode_yolo', test_npu_postprocess),
        ('stab_compress 寄存器解析', test_stab_compress_parse),
        ('py_compile 全脚本', test_py_compile),
    ]
    for name, fn in tests:
        check(name, fn)
    passed = len(tests) - len(FAILS) - len(SKIPS)
    print(f'\n结果：{passed} 通过 / {len(SKIPS)} 跳过 / {len(FAILS)} 失败'
          f'（共 {len(tests)} 项）')
    return 1 if FAILS else 0


if __name__ == '__main__':
    raise SystemExit(main())
