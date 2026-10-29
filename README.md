# PH1P100 PCIe EP —— IMX415 4K30 全帧采集

安路 PH1P100SBG484 做 PCIe Gen2 ×2 端点，把 IMX415 的整帧 RAW10 写进 RK3588 内存；
RK3588 侧驱动提供字符节点，用户态取帧、显示/推流。

| 目录 | 仓库 | 职责 |
|---|---|---|
| `fpga/` | `ph1p100-pcie-ep-fpga` | MIPI CSI-2 接收、R10F 写者、DDR3、one-shot 推帧、BAR0/RKA1 |
| `driver/` | `ph1p100-pcie-ep-driver` | RK3588 内核模块、一致性收帧缓冲、ioctl 与 Rust 工具 |
| `display/` | `ph1p100-pcie-ep-display` | MJPEG/快照 HTTP、R10F 解析、Bayer/后处理、RKNN 叠加 |

## 数据通路

~~~mermaid
flowchart LR
  CAM["IMX415 4K30"] -->|"MIPI CSI-2 4-lane"| CSI["checked CSI"]
  CSI --> W["cam_r10f_writer"]
  W --> DDR[("DDR3 双缓冲")]
  DDR --> P["ddr_frame_pusher"]
  P -->|"PCIe Gen2 ×2 Posted MWr"| MEM["主机一致性缓冲"]
  MEM --> DEV["/dev/ANLOGIC-PCI0_frame"]
  DEV --> GRAB["frame_grab / r10f_check"]
  DEV --> DISP["display-stream → HTTP"]
~~~

细节（寄存器、缓冲布局、诊断位域）见 [docs/DATAFLOW.md](docs/DATAFLOW.md)。

## 帧契约

| 量 | 值 |
|---|---:|
| 图像 | 3840×2160 packed RAW10，stride 4800 B |
| payload | 10,368,000 B |
| 整帧 | 128 B 头 + payload + 2,944 B 尾零 = **10,371,072 B**（2532 × 4 KiB） |
| 每帧 MWr | 81,024 笔 128 B + 1 笔门铃 |
| DDR 双缓冲 | A0=0x0100_0000、A1=0x0200_0000（B0/B1=0x0300_0000/0x0400_0000） |

## 快速开始（板子）

~~~sh
# 1) 下载 SRAM 位流（本地 TD；板卡随后必须重启才能重新枚举 Gen2 ×2）
cd fpga
/opt/anlogic/TD_Release_2026.2_NL/bin/bw.sh download_sram.tcl
ssh root@<BOARD> 'sync; reboot'

# 2) 驱动：当前 JPEG 位流是 1920×1088；4K 采集位流改 frame_width=3840 frame_height=2160
ssh root@<BOARD> 'insmod /root/anlogic_pci.ko frame_width=1920 frame_height=1088 frame_slots=1'
ssh root@<BOARD> 'ls /dev/ANLOGIC-PCI0_{user,control,frame}'

# 3) 取一帧（离线 PGM）
ssh root@<BOARD> '/root/frame_grab -t 3000 -o /dev/shm/a.pgm'

# 4) 实时页
ssh root@<BOARD> '/root/display-pair --source device --scale 2 --port 8091'
# 浏览器打开 http://<BOARD>:8091/

# 5) 压缩→PCIe→RKNN 全链路（约 8 fps，输出 /tmp/fpga_npu.jpg）
scp tools/board/fpga_jpeg_npu.py root@<BOARD>:/tmp/
ssh root@<BOARD> 'python3 /tmp/fpga_jpeg_npu.py --frames 3 --show-detections'
~~~

双路（1080p ROI 时代的能力，仍可用）：先 `link_ctl sync start -t 5000`，再 `frame_grab --pair` 或
`display-pair --source device-pair`；见 [docs/HARDWARE_SYNC.md](docs/HARDWARE_SYNC.md)。

## 实测（2026-10-28/29）

| 项 | 结果 |
|---|---|
| 4K30 单路采集 | 3840×2160@29.9 fps，丢组 0（2026-10-29） |
| 双路自由采集 | A/B 各 29.993 fps，丢组 0（1080p ROI） |
| 纯取帧 | 30.4 pair/s（60 帧/s，PCIe ~155 MB/s） |
| 双路 pair 取帧 | 600/600 成功（180 s），A/B `capture_seq` 全相同 |
| display 1080p / 540p | 11 pair/s / 23 pair/s |
| RKNN YOLO11n（960×544） | 21.9 pair/s，单路推理约 40 ms |
| 硬件同步 | A 主/B 从，`0x1E4=0x8000D`，skew 2~3 tick（约 32~48 ns），miss=0 |
| 硬件 JPEG 压缩 | 1920×1088 灰度，95.5%，7.5~8.5 fps（180 s 长稳） |
| 压缩→RKNN 全链路 | 解码 18 ms + 推理 45 ms，4~10 个目标（随场景），标注图入库 |

完整表格与证据见 [docs/BOARD_TEST.md](docs/BOARD_TEST.md)；六项交付状态见 [docs/TEST_REPORT.md](docs/TEST_REPORT.md)。
文档入口：[DATAFLOW](docs/DATAFLOW.md)（链路/寄存器）、[CODEC](docs/CODEC.md)（JPEG 压缩）、
[RKNN](docs/RKNN.md)（NPU 部署）、[HARDWARE_SYNC](docs/HARDWARE_SYNC.md)（双路同步）。

## 已知边界

- 4K30 全帧在 4K 采集位流上验证（`fpga` 提交 `27bcd9ff`）；当前 JPEG 位流走 1080p ROI。
- 双路 4K、4K+同步、MSI 中断、故障注入都没有板测数据。
- 压缩支路 7.5~8.5 fps，非实时；编码器 65.7 ms/帧，剩余 ~50 ms 是等下一帧发布。
- 压缩→NPU 全链路已打通（`tools/board/fpga_jpeg_npu.py`），但受压缩帧率限制，~2 fps 端到端。
- R10F 的 `sof_timestamp/timestamp_hz` 仍为 0；`bayer_phase` 是 RTL 占位推导，不是测量值。
- `0x1E8` 是 SOF/包层数字配对差，不是曝光相位差。
- 位流是 SRAM 配置：重下/掉电后必须重启 RK3588。
- 根 `log/` 是本地证据（gitignore），只放每专题一份 RESULTS 与少量图。

## 硬件与工具链

- FPGA：PH1P100SBG484，TD `Release_2026.2_NL`（无头：`td.sh build.tcl`）
- 主机：Orange Pi 5 Max / RK3588，内核 `7.2.0-dirty`（aarch64，NFS root）
- 相机：FEP-VID006-MIPI 子卡（IMX415 ×2，CAM005 模组），27 MHz / 891 Mbps ×4 lane