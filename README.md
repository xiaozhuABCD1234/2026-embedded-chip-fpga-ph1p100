# PH1P100 PCIe EP —— 双路 4K 相机采集（总仓库）

两颗 IMX415 各出 4K → FPGA 整帧直通 DDR3 双缓冲 → EP 主动用 MWr 写进 RK3588 内存 →
主机两路各自取帧 / 推 MJPEG 流。**两路独立、同时**。

## 子仓库（submodule）

| 目录 | 仓库 | 职责 |
|---|---|---|
| `fpga/` | `ph1p100-pcie-ep-fpga` | 安路 PH1P100 的 EP 逻辑：MIPI CSI-2 收 4K、直通 DDR3 双缓冲、两路推帧引擎、BAR0 寄存器面 |
| `driver/` | `ph1p100-pcie-ep-driver` | RK3588 侧 Linux 驱动：两路一致性缓冲、两个字符节点、链路/推帧 ioctl 与用户态工具（Rust） |
| `display/` | `ph1p100-pcie-ep-display` | 用户态显示：MJPEG / 快照 HTTP 服务、Bayer 去马赛克与后处理 |

各子仓库的 README 有本组件的细节；本文件只讲整体与实测性能。

## 数据通路

```
IMX415 ×2 ──MIPI CSI-2（4 lane × 1.5Gbps）──▶ FPGA
   裁剪/组字（RAW10>>2，4px/字）→ DDR3 双缓冲（A 1/9MB、B 17/25MB，写口按突发仲裁）
   → 两路推帧引擎（AXI 读口按突发仲裁、TL 发送口按 TLP 仲裁）
   ──PCIe Gen2 ×2，MWr──▶ RK3588 一致性内存（每路 8.29MB 槽 + mailbox）
   → /dev/…_frame（A）、/dev/…_frame_b（B）→ frame_grab / display-stream
```

## 当前性能（2026-10-17 板上实测）

| 项 | 数值 |
|---|---|
| 帧率（FPGA→主机，每路） | **7.85 fps**（A 测 7.90、B 测 7.80） |
| MJPEG 推流（每路） | 3.0 / 3.2 fps（受 RK3588 单核 JPEG 编码限制）；`display-stream --scale 2` 输出 1080p 可到 **约 14 fps** |
| 每帧 | 3840×2160 灰度 = 8294400 B；每路 64800 笔 MWr |
| 帧错误 | 两路 `err=0`，门铃/时长均正常 |
| PCIe | Gen2 ×2（理论 1 GB/s），实际两路合计 **130 MB/s ≈ 13%** |
| DDR3 | 用户口 3.2 GB/s，实际 **260 MB/s**（写 130 + 推帧读 130）≈ 8% |
| MIPI | 每路峰值 750 MB/s，实际像素载荷 65 MB/s ≈ 9% |
| 主机内存 | 驱动缓冲 16.6 MB（2 路 × 8.3MB）；两推流进程 RSS 89MB+62MB |
| CPU | 两个 display-stream 各占满 1 核（RK3588 共 8 核 ⇒ ≈25%），load ≈2 |
| FPGA 资源 | ERAM 141/300（47%）、LUT 18.3%、寄存器 10.7%、slice 20.1%；**时序 0 违规** |

结论：**链路与 DDR 都还剩 85% 以上余量，当前瓶颈是主机侧的 JPEG 编码**。

## 传感器能力与余量

IMX415 本身：**12-bit 输出最高 60.3 fps，10-bit 输出最高 90.9 fps**（3840×2160）。

本设计现在跑 7.85 fps/路，是**传感器时钟配置**（MCLK 12.5MHz + INCKSEL）限制的，不是通路限制。
若放到 60 fps/路：

- 每路 497 MB/s；两路写 + 推帧读 DDR3 ≈ **2 GB/s**（用户口 3.2 GB/s 的 62%，仍有余量）
- PCIe 两路合计 ≈ **1 GB/s** ⇒ 会顶满 Gen2 ×2（需升 x4 或 Gen3）
- 主机侧 4K JPEG 编码：单核 4K 只有 3 fps；用 `display-stream --scale 2`（CFA binning，输出 1080p）
  已能到约 14 fps，够覆盖传感器 7.85 fps

## 快速开始（板子侧）

```sh
# 1) 驱动（默认就是 4K、两路）
insmod anlogic_pci.ko frame_width=3840 frame_height=2160 frame_slots=1
# 2) 下载 bit（TD 的 download_sram.tcl；热重配后需重启 RK3588 才能恢复 Gen2 ×2）
bw.sh download_sram.tcl
# 3) 两路节点应都在
ls /dev/ANLOGIC-PCI0_frame /dev/ANLOGIC-PCI0_frame_b
# 4) 起两路推流（A→8098、B→8099）
sudo /home/orangepi/live_dual.sh
```

看视频：浏览器开 `http://<板子IP>:8098/`（A）、`http://<板子IP>:8099/`（B），
单帧 `…/snapshot.jpg`，播放器 `ffplay http://<板子IP>:8098/stream.mjpg`，
原始帧 `frame_grab -o a.pgm` / `frame_grab -d /dev/ANLOGIC-PCI0_frame_b -o b.pgm`。

## 已知限制

- 每路只有**一个槽**且推帧目标不轮换：连续推时取帧可能撞上覆盖，`frame_grab` 会报
  「一致性不通过」（重取一次即可）；要根治需推帧引擎支持目标地址轮换 + 多槽。
- **FPGA 热重配会把 PCIe 链路降到 Gen1 ×1**，此后 MWr 会被 RC 静默丢 ⇒ 必须重启 RK3588。
- 只取 RAW10 的高 8 位（1 字节 1 像素）；要 16bit 通路得带宽翻倍。
- `display-stream` 单线程编码，4K 彩色只有 3 fps；`--scale 2`（CFA binning → 1080p）约 14 fps。

## 硬件与工具链

- FPGA：安路 **PH1P100SBG484**，TD `Release_2026.2`
- 主机：**Orange Pi 5 Max / RK3588**，内核 `7.2.0-dirty`
- 相机：**FEP-VID006-MIPI** 子卡（IMX415 ×2）
- 时钟：`tl_clk_user` 62.5MHz、传感器 MCLK 12.5MHz（÷5）、相机字节时钟 187.5MHz、DDR3 用户时钟 100MHz
