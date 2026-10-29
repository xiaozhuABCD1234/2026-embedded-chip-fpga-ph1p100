#!/bin/sh
# 板端：紧邻取参考帧 -> 压缩一帧 -> 再取参考帧，用于同场景 PSNR。
set -x
/root/pci_bar 0 0x240 0x0 >/dev/null
/root/link_ctl ack >/dev/null 2>&1
/root/link_ctl arm >/dev/null 2>&1
sleep 0.4
/root/frame_grab -o /tmp/ref_a.pgm >/dev/null 2>&1
/root/jpeg_cap.py --force-compress --out /tmp/cap.bin >/tmp/cap.log 2>&1
tail -8 /tmp/cap.log
/root/pci_bar 0 0x240 0x0 >/dev/null
/root/link_ctl ack >/dev/null 2>&1
/root/link_ctl arm >/dev/null 2>&1
sleep 0.4
/root/frame_grab -o /tmp/ref_b.pgm >/dev/null 2>&1
ls -l /tmp/ref_a.pgm /tmp/ref_b.pgm /tmp/cap.bin
