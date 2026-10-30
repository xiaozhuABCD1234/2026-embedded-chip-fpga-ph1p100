#!/bin/sh
# 板端：四种预处理算法对照取样。写 0x240 的 [2:1]，各取一帧并算统计量。
set -u
PCI=/root/pci_bar
for v in 0x0 0x2 0x4 0x6; do
    $PCI 0 0x240 "$v" >/dev/null 2>&1
    sleep 0.3
    /root/link_ctl ack >/dev/null 2>&1
    /root/link_ctl arm >/dev/null 2>&1
    sleep 0.9
    /root/frame_grab -t 5000 -o "/tmp/algo_$v.pgm" >/dev/null 2>&1
done
python3 - <<'PY'
import numpy as np
from PIL import Image, ImageFilter

for v in ('0x0', '0x2', '0x4', '0x6'):
    try:
        im = Image.open(f'/tmp/algo_{v}.pgm').convert('L')
    except OSError:
        print(f'  {v}: 取帧失败')
        continue
    a = np.asarray(im, dtype=np.float64)
    lap = np.asarray(im.filter(ImageFilter.Kernel(
        (3, 3), (0, 1, 0, 1, -4, 1, 0, 1, 0), scale=1, offset=0)), dtype=np.float64)
    print('  %s  mean=%6.2f  sd=%6.2f  lap=%6.2f  p99=%3.0f'
          % (v, a.mean(), a.std(), np.abs(lap).mean(), np.percentile(a, 99)))
    im.save(f'/tmp/algo_{v}.png')
PY
