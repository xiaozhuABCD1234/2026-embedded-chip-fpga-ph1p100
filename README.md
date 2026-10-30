# PH1P100 × RK3588 —— 4K 采集 / 硬件 JPEG / PCIe / NPU 全链路

安路 PH1P100SBG484 做 PCIe Gen2 ×2 端点：一条 4K30 全帧 RAW10 直传通路，
一条 1080p ROI 灰度的**硬件 JPEG 压缩**通路（典型压缩率 95.5%），压缩流经 PCIe
回传后由 RK3588 解码、RKNN 检测并叠加。RK3588 驱动提供字符节点供取帧/推流。

| 目录 | 仓库 | 职责 |
|---|---|---|
| `fpga/` | `ph1p100-pcie-ep-fpga` | MIPI CSI-2、R10F 写者、DDR3、JPEG、one-shot 推帧、BAR0/RKA1 |
| `driver/` | `ph1p100-pcie-ep-driver` | RK3588 内核模块、一致性收帧缓冲、ioctl 与 Rust 工具 |
| `display/` | `ph1p100-pcie-ep-display` | MJPEG/快照 HTTP、R10F 解析、Bayer/后处理、RKNN 叠加 |

## 交付状态（对照赛题六项）

| # | 要求 | 状态 | 关键数据 |
|---|---|---|---|
| 1 | MIPI CSI，≥1080p60 / 鼓励 4K30 | 4K30 达成 | 3840×2160@29.9 fps，丢组 0 |
| 2 | ≥3 种预处理，AXI-Stream 流水线 | 达成（类 AXI-Stream） | 灰度/高斯/HE，1920×1088@30 fps |
| 3 | 硬件 JPEG，压缩率 ≥30% | 达成，非实时 | 95.5%（分母为 8 bit 灰度），65.7 ms/帧，7.5~8.5 fps |
| 4 | PCIe 传压缩帧，端到端 ≤50 ms | 传输段达成，端到端未闭合 | 传输 <1 ms；SOF→主机 ≈118 ms |
| 5 | RKNN 推理 + 回显/叠加 | 达成 | YOLO11n INT8，40~45 ms/帧 |
| 6 | 完整量化报告 | 五项有数，功耗未测 | 见 [测试报告](docs/TEST_REPORT.md) |

严格口径见 [TEST_REPORT](docs/TEST_REPORT.md) §2，缺口与下一步见
[REQUIREMENTS](docs/REQUIREMENTS.md) §5。
本项目不把“传输段 <50 ms”写成“端到端 <50 ms”，也不把 4K30 写成 1080p60。

## 数据通路

~~~mermaid
flowchart LR
  CAM["IMX415 4K30"] -->|"MIPI CSI-2 4-lane"| CSI["checked CSI"]
  CSI --> W["cam_r10f_writer"]
  CSI --> A["灰度/高斯/HE + ROI"] --> J["硬件 JPEG"] --> CB[("压缩块 0x0500_0000")]
  W --> DDR[("DDR3 双缓冲")]
  DDR --> P["ddr_frame_pusher"]
  CB --> P
  P -->|"PCIe Gen2 ×2 Posted MWr"| MEM["主机一致性缓冲"]
  MEM --> DEV["/dev/ANLOGIC-PCI0_frame"]
  DEV --> GRAB["frame_grab / r10f_check"]
  DEV --> DISP["display-stream → HTTP"]
  DEV --> NPU["解码 + RKNN 叠加"]
~~~

寄存器、帧契约与同步细节见 [ARCHITECTURE](docs/ARCHITECTURE.md)；JPEG 码流见
[CODEC](docs/CODEC.md)。

## 帧契约

| 量 | 4K 采集位流 | 1920×1088 位流（ROI/JPEG） |
|---|---:|---:|
| 图像 | 3840×2160 packed RAW10 | 1920×1088 packed RAW10（算法侧解包成 8 bit 灰度） |
| stride | 4,800 B | 2,400 B |
| payload | 10,368,000 B | 2,611,200 B（解包后 8 bit 为 2,088,960 B） |
| 整帧 | **10,371,072 B**（2,532×4 KiB） | **2,613,248 B**（638×4 KiB） |
| DDR 双缓冲 | A0/A1=0x0100_0000/0x0200_0000 | 同左 |

早期 1080p ROI 位流（1920×1080、slot 2,592,768 B）的数字只出现在
[evidence/pair.md](docs/evidence/pair.md)、[evidence/fps.md](docs/evidence/fps.md)，不要与上表混用。
位流是 SRAM 配置：重下/掉电后必须重启 RK3588 才会重新枚举 Gen2 ×2。

## 快速开始（板子）

~~~sh
# 1) 下载 SRAM 位流（本机 TD）
cd fpga
/opt/anlogic/TD_Release_2026.2_NL/bin/bw.sh download_sram.tcl
ssh root@<BOARD> 'sync; reboot'

# 2) 驱动：按位流几何加载（当前 JPEG 位流 1920×1088）
ssh root@<BOARD> 'insmod /root/anlogic_pci.ko frame_width=1920 frame_height=1088 frame_slots=1'
ssh root@<BOARD> 'ls /dev/ANLOGIC-PCI0_{user,control,frame}'

# 3) 一键自检（本机执行）
tools/board/demo.sh env     # 链路/驱动/温度/设备节点
tools/board/demo.sh cap     # 取一帧
tools/board/demo.sh npu     # 压缩→PCIe→解码→RKNN→画框
tools/board/demo.sh stab    # 20 s 压缩稳定性

# 4) 曝光：写 0x280 调传感器模拟增益（每 LSB 0.3 dB）
ssh root@<BOARD> '/root/pci_bar 0 0x280 0x10'   # 0x10=4.8 dB 亮场，0x40=19.2 dB 暗场
ssh root@<BOARD> '/root/pci_bar 0 0x284'        # 回读 {done_b, done_a, ae_st, gain}

# 5) 实时页
ssh root@<BOARD> '/root/display-pair --source device --scale 2 --port 8091'
# 浏览器打开 http://<BOARD>:8091/
~~~

双路（1080p ROI）：先 `link_ctl sync start -t 5000`，再 `frame_grab --pair` 或
`display-pair --source device-pair`。驱动与工具构建见 [driver/README](driver/README.md)；
显示/NPU 见 [display/README](display/README.md)。

## 实测（2026-10-28~30）

| 项 | 结果 |
|---|---|
| 4K30 单路采集 | 3840×2160@29.9 fps，丢组 0 |
| 双路 1080p 自由采集 | A/B 各 29.997 fps，丢组 0 |
| 双路 pair 取帧 | 600/600 成功（180 s），A/B `capture_seq` 全相同 |
| 帧传输段带宽 | 4K RAW ≈519 MB/s（≈20 ms/帧） |
| 硬件 JPEG | 95.5%，65.7 ms/帧，7.5~8.5 fps（亮场）；1 h 26,965 帧 |
| RKNN YOLO11n | 40~45 ms/帧；双路 960×544 21.9 pair/s |
| 压缩→NPU 全链路 | 464~486 ms/帧 |
| 资源（JPEG 位流） | LUT6 28,025（39.1%），WNS +0.026 ns |

证据与判据见 [TEST_REPORT](docs/TEST_REPORT.md)，历史专项记录在
[docs/evidence](docs/evidence/)（fps/pair/sync/color/detect）。

## 已知边界

- 压缩支路 7.5~8.5 fps（亮场）、暗场 ~4 fps，不是实时；只处理 1920×1088 灰度（线上 packed RAW10）。
- 4K 与 JPEG 是两个独立位流；4K+JPEG、双路 4K、4K+硬件同步未做。
- 端到端 ≤50 ms 未闭合：压缩帧从 SOF 到主机约 118 ms，瓶颈是 65.7 ms 编码。
- 整机功耗未测（板上无电流传感器），需 USB-C PD 表或台式电源。
- 双路 1080p 有硬件同步记录；4K 下 B 路 slave 收不到可用 SOF，需查板级 XVS/XHS。
- 位流 SRAM 配置；重下/掉电后必须重启 RK3588。

## 文档

- [ARCHITECTURE](docs/ARCHITECTURE.md)：拓扑、帧契约、寄存器、双路同步
- [TEST_REPORT](docs/TEST_REPORT.md)：六项交付状态、实测数据、缺口
- [REQUIREMENTS](docs/REQUIREMENTS.md)：赛题逐条对照、评分点与优先级
- [CODEC](docs/CODEC.md)：JPEG 码流契约、RTL 模块、主机流程
- [RKNN](docs/RKNN.md)：模型契约、双输出量化、线程模型
- [docs/evidence](docs/evidence/)：专项板测记录与图

## 硬件与工具链

- FPGA：PH1P100SBG484，TD `Release_2026.2_NL`（无头：`td.sh build.tcl`）
- 主机：Orange Pi 5 Max / RK3588，内核 7.2.0（aarch64，NFS root）
- 相机：FEP-VID006-MIPI 子卡（IMX415 ×2），27 MHz / 891 Mbps ×4 lane
