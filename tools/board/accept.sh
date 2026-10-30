#!/bin/sh
# 板端验收：增益扫描 -> 取帧 -> 硬件压缩 -> NPU 全链路。只读/受控写寄存器。
# 用法（板端）：sh /tmp/accept.sh [gain_hex ...]
set -u
PCI=/root/pci_bar
say() { printf '\n== %s ==\n' "$*"; }
grab_mean() {
    /root/link_ctl ack >/dev/null 2>&1
    /root/link_ctl arm >/dev/null 2>&1
    sleep 0.8
    /root/frame_grab -t 5000 -o /tmp/acc.pgm >/dev/null 2>&1 || { echo "  grab 失败"; return 1; }
    python3 - "$1" <<'PY'
import sys
from PIL import Image
im = Image.open('/tmp/acc.pgm').convert('L')
h = im.histogram(); n = sum(h)
mean = sum(i * c for i, c in enumerate(h)) / n
hi = max(i for i, c in enumerate(h) if c)
print('  gain=%s mean=%.1f max=%d' % (sys.argv[1], mean, hi))
PY
}

say "寄存器基线"
for off in 0x040 0x240 0x284; do
    printf '  %s = %s\n' "$off" "$($PCI 0 $off 2>/dev/null | grep -o '0x[0-9a-f]*' | tail -1)"
done

say "增益扫描（写 0x280，读回 0x284 的 applied gain）"
for g in "$@"; do
    $PCI 0 0x280 "$g" >/dev/null 2>&1
    sleep 1.2
    printf '  wrote 0x%s -> stat %s\n' "$g" "$($PCI 0 0x284 2>/dev/null | grep -o '0x[0-9a-f]*' | tail -1)"
    grab_mean "$g"
done

say "硬件 JPEG 压缩"
/root/link_ctl ack >/dev/null 2>&1
python3 /tmp/jpeg_cap.py --out /tmp/acc.bin 2>&1 | tail -3
ls -l /tmp/acc.bin 2>/dev/null || echo "  无输出"

say "压缩 -> PCIe -> RKNN"
python3 /tmp/fpga_jpeg_npu.py --frames 2 --show-detections 2>&1 | grep -E '帧 |平均|检测 ' | tail -6
ls -l /tmp/fpga_npu.jpg 2>/dev/null

$PCI 0 0x240 0x0 >/dev/null 2>&1
say "完成"
