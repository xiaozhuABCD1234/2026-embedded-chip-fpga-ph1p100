#!/bin/sh
# 板端：连续 ARM/ACK 取帧，统计 SOF→主机的端到端延迟分布。
set -u
N=${1:-20}
i=0
while [ "$i" -lt "$N" ]; do
    /root/link_ctl ack >/dev/null 2>&1
    /root/link_ctl arm >/dev/null 2>&1
    /root/frame_grab -t 3000 -o /tmp/lat.pgm 2>&1 | sed -n 's/.*端到端延迟: \([0-9.]*\) ms.*/\1/p'
    i=$((i+1))
done
