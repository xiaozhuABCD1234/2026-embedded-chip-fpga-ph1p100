#!/bin/sh
# demo.sh —— PH1P100 + RK3588 板端一键演示
# 本机执行；自动把依赖脚本 scp 到板端 /tmp，再通过 ssh 运行。
set -eu

BOARD=${BOARD:-192.168.1.101}
case "$BOARD" in
    *@*) BOARD_TARGET=$BOARD ;;
    *)   BOARD_TARGET=root@$BOARD ;;
esac
SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=10"

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
TOOLS_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
NPU_SCRIPT=$SCRIPT_DIR/fpga_jpeg_npu.py
STAB_SCRIPT=$SCRIPT_DIR/stab_compress.py
# npu 链路在板端 import 这些模块，必须一起同步到 /tmp
NPU_DEPS="$SCRIPT_DIR/jpeg_cap.py $TOOLS_DIR/jpeg_file.py $TOOLS_DIR/jpeg_ref.py"

die() {
    printf '[demo] 错误: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
用法: tools/board/demo.sh <env|cap|npu|stab|all>

  env   位流 SHA256（/root 有 .bit 时）、PCIe LnkSta、modinfo、hwmon 温度、设备节点
  cap   取一帧到 /tmp/demo_cap.pgm，打印尺寸/均值（只读帧缓冲）
  npu   FPGA JPEG -> RKNN YOLO11n 跑 3 帧，标注图拷到 /tmp/demo_npu.jpg
  stab  压缩支路 20 s 稳定性采样
  all   env + cap + npu

板端 IP 可用 BOARD 覆盖，默认 192.168.1.101，例如：
  BOARD=192.168.1.102 tools/board/demo.sh env
EOF
}

ssh_board() {
    LC_ALL=C ssh $SSH_OPTS "$BOARD_TARGET" "$@"
}

scp_to_board() {
    LC_ALL=C scp -q $SSH_OPTS "$1" "$BOARD_TARGET:/tmp/" \
        || die "scp 到板端失败: $1"
}

check_board() {
    ssh_board true >/dev/null 2>&1 \
        || die "无法 SSH 到 $BOARD_TARGET（检查 IP、网络和 root 免密登录）"
}

sync_dep() {
    [ -f "$1" ] || die "缺少依赖脚本: $1"
    scp_to_board "$1"
}

board_busy() {
    ssh_board "pgrep -f '[s]tab_compress.py' >/dev/null 2>&1"
}

guard_algo() {
    if [ "${DEMO_FORCE:-0}" != 1 ] && board_busy; then
        die "板端有 stab_compress.py 在跑，$1 会写 0x240 并干扰长稳；已拒绝（确认可打断后设 DEMO_FORCE=1）"
    fi
}

cmd_env() {
    check_board
    ssh_board sh -s <<'REMOTE'
set -eu
echo "== 位流 SHA256 =="
found=0
for f in /root/*.bit; do
    [ -f "$f" ] || continue
    found=1
    command -v sha256sum >/dev/null 2>&1 || { echo "  错误: 板端没有 sha256sum" >&2; exit 5; }
    printf '  %s  %s\n' "$(sha256sum "$f" | cut -d' ' -f1)" "$f"
done
if [ "$found" -eq 0 ]; then
    echo "  跳过: /root 下没有 .bit（当前为 SRAM 位流）"
fi

echo
echo "== PCIe 链路 =="
bdf=$(lspci -Dnn 2>/dev/null | grep -m1 '1edb:abcd' | cut -d' ' -f1 || true)
if [ -z "$bdf" ]; then
    echo "  错误: lspci 未找到 1edb:abcd（FPGA 未枚举）" >&2
    exit 3
fi
echo "  BDF: $bdf"
lspci -vv -s "$bdf" 2>/dev/null | sed -n -e '/LnkCap:/p' -e '/LnkSta:/p' \
    | sed 's/^[[:space:]]*/  /'

echo
echo "== 驱动 =="
if ! modinfo anlogic_pci >/dev/null 2>&1; then
    echo "  错误: anlogic_pci 模块未加载（先 insmod /root/anlogic_pci.ko）" >&2
    exit 4
fi
modinfo anlogic_pci | grep -E '^(filename|vermagic|description):' | sed 's/^/  /'
lsmod | awk '$1 == "anlogic_pci" {printf "  已加载: 大小 %s B, 引用 %s\n", $2, $3}'

echo
echo "== hwmon 温度 =="
for h in /sys/class/hwmon/hwmon*; do
    [ -r "$h/name" ] && [ -r "$h/temp1_input" ] || continue
    name=$(cat "$h/name")
    t=$(cat "$h/temp1_input")
    printf '  %-18s %d.%d C\n' "$name" $((t / 1000)) $((t % 1000 / 100))
done

echo
echo "== 设备节点 =="
missing=0
for d in /dev/ANLOGIC-PCI0_control /dev/ANLOGIC-PCI0_user /dev/ANLOGIC-PCI0_frame; do
    if [ -c "$d" ]; then
        ls -l "$d" | sed 's/^/  /'
    else
        echo "  缺失: $d"
        missing=1
    fi
done
[ "$missing" -eq 0 ] || { echo "  错误: 驱动设备节点不完整" >&2; exit 6; }
REMOTE
}

cmd_cap() {
    check_board
    ssh_board sh -s <<'REMOTE'
set -eu
if pgrep -f '[s]tab_compress.py' >/dev/null 2>&1; then
    echo "注意: 检测到压缩稳定性测试在跑；cap 只读既有帧缓冲，不 ARM、不写 0x240。"
fi
[ -c /dev/ANLOGIC-PCI0_frame ] || { echo "  错误: /dev/ANLOGIC-PCI0_frame 不存在" >&2; exit 3; }
[ -x /root/frame_grab ] || { echo "  错误: 板端没有 /root/frame_grab" >&2; exit 4; }
rm -f /tmp/demo_cap.pgm
if ! /root/frame_grab -t 3000 -o /tmp/demo_cap.pgm; then
    echo "  错误: frame_grab 取帧失败（无可用帧或驱动异常）" >&2
    exit 5
fi
[ -s /tmp/demo_cap.pgm ] || { echo "  错误: frame_grab 未生成 /tmp/demo_cap.pgm" >&2; exit 6; }
python3 - /tmp/demo_cap.pgm <<'PY'
import sys
import numpy as np

path = sys.argv[1]
with open(path, 'rb') as f:
    data = f.read()

tokens, pos = [], 0
while len(tokens) < 4:
    while pos < len(data) and data[pos:pos + 1].isspace():
        pos += 1
    if data[pos:pos + 1] == b'#':
        while pos < len(data) and data[pos:pos + 1] not in (b'\n', b'\r'):
            pos += 1
        continue
    start = pos
    while pos < len(data) and not data[pos:pos + 1].isspace():
        pos += 1
    tokens.append(data[start:pos])
pos += 1

magic = tokens[0].decode()
w, h, maxv = int(tokens[1]), int(tokens[2]), int(tokens[3])
raw = data[pos:]
if magic == 'P5':
    a = np.frombuffer(raw[:w * h], dtype=np.uint8) if maxv < 256 \
        else np.frombuffer(raw[:w * h * 2], dtype='>u2')
elif magic == 'P2':
    a = np.array(raw.split(), dtype=np.int32)[:w * h]
else:
    raise SystemExit(f'不支持的 PGM 魔数: {magic}')
if a.size != w * h:
    raise SystemExit(f'PGM 像素数不符: {a.size} != {w * h}')
mean = float(a.mean())
print(f'  尺寸: {w} x {h}，像素 {a.size}，文件 {len(data)} B')
print(f'  均值: {mean:.2f} / {maxv} ({100.0 * mean / maxv:.2f}%)')
PY
REMOTE
    LC_ALL=C scp -q $SSH_OPTS "$BOARD_TARGET:/tmp/demo_cap.pgm" /tmp/demo_cap.pgm \
        || die "把 /tmp/demo_cap.pgm 拷回本机失败"
    echo "  本机副本: /tmp/demo_cap.pgm"
}

cmd_npu() {
    check_board
    guard_algo npu
    sync_dep "$NPU_SCRIPT"
    for dep in $NPU_DEPS; do
        [ -f "$dep" ] && sync_dep "$dep"
    done
    ssh_board sh -s <<'REMOTE'
set -eu
[ -c /dev/ANLOGIC-PCI0_frame ] || { echo "  错误: /dev/ANLOGIC-PCI0_frame 不存在" >&2; exit 3; }
[ -x /root/pci_bar ] && [ -x /root/link_ctl ] || { echo "  错误: 板端缺少 /root/pci_bar 或 /root/link_ctl" >&2; exit 3; }
[ -f /root/models/yolo11n-640-int8.rknn ] || { echo "  错误: 缺少 /root/models/yolo11n-640-int8.rknn" >&2; exit 4; }
[ -f /tmp/fpga_jpeg_npu.py ] || { echo "  错误: /tmp/fpga_jpeg_npu.py 未同步" >&2; exit 4; }
python3 /tmp/fpga_jpeg_npu.py --frames 3 --show-detections --out /tmp/fpga_npu.jpg
[ -s /tmp/fpga_npu.jpg ] || { echo "  错误: npu 未生成 /tmp/fpga_npu.jpg" >&2; exit 5; }
REMOTE
    LC_ALL=C scp -q $SSH_OPTS "$BOARD_TARGET:/tmp/fpga_npu.jpg" /tmp/demo_npu.jpg \
        || die "把 /tmp/fpga_npu.jpg 拷回本机失败"
    echo "  本机副本: /tmp/demo_npu.jpg"
}

cmd_stab() {
    check_board
    guard_algo stab
    sync_dep "$STAB_SCRIPT"
    ssh_board "python3 /tmp/stab_compress.py --duration 20"
    echo "  板端日志: /tmp/stab_compress.log"
}

cmd_all() {
    cmd_env
    cmd_cap
    cmd_npu
}

[ $# -eq 1 ] || { usage >&2; exit 2; }
case "$1" in
    env)  cmd_env ;;
    cap)  cmd_cap ;;
    npu)  cmd_npu ;;
    stab) cmd_stab ;;
    all)  cmd_all ;;
    -h|--help|help) usage ;;
    *)    usage >&2; exit 2 ;;
esac
