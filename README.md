# PH1P100 PCIe EP —— 相机采集（总仓库）

安路 PH1P100 做 PCIe 端点，把 MIPI 相机整帧用 MWr 写进 RK3588 内存；主机侧驱动收帧、
用户态取帧 / 推流。

当前三个子仓库都已对齐到各自 `origin/main`（2026-10-27 合并），主线架构是
**T07/RKA1 单引擎 one-shot**：一帧一个 ARM 额度、推完进 held、主机核对后 ACK 才放行下一帧，
帧格式是 1080p RAW10 的 R10F 非压缩容器。

## 子仓库（submodule）

| 目录 | 仓库 | 职责 |
|---|---|---|
| `fpga/` | `ph1p100-pcie-ep-fpga` | PH1P100 的 EP 逻辑：MIPI CSI-2 收 4K、R10F 写者落 DDR3、one-shot 推帧引擎、BAR0 寄存器面（含 RKA1 安全扩展） |
| `driver/` | `ph1p100-pcie-ep-driver` | RK3588 侧 Linux 驱动：一致性收帧缓冲、字符节点、链路/推帧/RKA1 ioctl 与 Rust 工具 |
| `display/` | `ph1p100-pcie-ep-display` | 用户态显示：MJPEG / 快照 HTTP 服务、Bayer 去马赛克与后处理 |

## 数据通路（当前）

```
IMX415 ×2 ──MIPI CSI-2（4 lane）──▶ checked CSI 包层（CRC/ECC 校验）
   → 偶数 ROI 裁剪 1920×1080（roi 960,540）→ packed RAW10（2400 B/行）
   → R10F 容器（128B 头 + 2592000B 载荷 + 4KiB 对齐尾零 = 2592768 B）
   → DDR3 双缓冲（A/B 各一对，写口按突发仲裁）
   → one-shot 推帧引擎（20256 笔 128B MWr + 一笔 4B 门铃）
   ──PCIe Gen2 ×2──▶ RK3588 一致性内存（槽 + mailbox 页）
   → /dev/ANLOGIC-PCI0_frame → 取帧 / 推流
```

`CTRL[8]` 选推帧取哪一路（0 = A、1 = B），ARM 那一拍锁存，所以**改选择必须在 ARM 之前**。

## 2026-10-27 板上实测（本仓当前代码）

| 项 | 结果 |
|---|---|
| 位流 | `ph1p_pcie_ep_top.bit` 5224023 B，SHA256 `af8a7499…1fb16df2`；SWNS +0.108 ns、HWNS +0.020 ns、0 条违例 |
| 链路 | `PCIe Gen.2 x2 link up`，`[1edb:abcd]`，BAR0 1 MB |
| RKA1 扩展 | `0x118 = 0x31414B52`（"RKA1"）、`0x11C = 0x00010000`（v1.0） |
| one-shot 推帧 | ARM → 20256 笔 MWr → `0xC0 pushed=1` → mailbox `seq=1` → ACK 释放 |
| 帧校验 | 2592768 B，`r10f_strict.py check` → **R10F_STRICT_OK**（几何/三层长度/三段 CRC/尾零全过） |
| 帧 SHA256 | `a7063149c4bbb953ce7fdeacfb280fa677097f03c69862cfae9b1e07437e4edb` |

细节与复跑命令见 [fpga/docs/BOARD_TEST_20261027.md](fpga/docs/BOARD_TEST_20261027.md)。

## 已知问题（当前）

- **A 路 R10F 写者从不发布**：`0xA0 DDR_FRAME_SEQ` 恒 0、`0xA8` 每帧记错（err_cnt 逐帧 +1，
  valid=0/busy=1/err=1），写者诊断只数到 1144 行（B 路 2192 行）。上表那一帧走的是 B 路
  （`CTRL[8]=1`）。待修：A 路的行/组裁剪计数与真实 2192 行 × 4830 B/行 对齐。
- one-shot 语义意味着**每帧要一次 ARM + 一次 ACK**；驱动 probe 里会自动 ARM 一次，
  用户态 ACK 时 `ack_session` 传 0 表示用驱动自己那份会话号。
- 位流是 SRAM 配置：**掉电/重烧后必须重启 RK3588** 才会重新枚举出 Gen2 ×2。
- 每路只有一个槽且推帧目标不轮换：连续推时取帧可能撞上覆盖，重取一次即可。

## 历史：双路 4K 直通（2026-10-17 实测，已不在主线）

本地历史里的 `537a14c4` 一线跑的是另一套：两路 3840×2160 灰度各自推帧、两个节点
`/dev/ANLOGIC-PCI0_frame` 与 `…_frame_b` 同时可用，当时实测：

| 项 | 数值 |
|---|---|
| 帧率（FPGA→主机，每路） | 7.85 fps（A 7.90 / B 7.80） |
| 每帧 | 3840×2160 灰度 = 8294400 B，64800 笔 MWr |
| PCIe | Gen2 ×2，两路合计 130 MB/s ≈ 13% |
| DDR3 | 用户口 3.2 GB/s，实际 260 MB/s ≈ 8% |
| MJPEG 推流 | 每路 3.0 / 3.2 fps；`display-stream --scale 2`（1080p）约 14 fps |

结论当时是「链路与 DDR 都剩 85% 以上余量，瓶颈在主机侧 JPEG 编码」。这套位流/驱动
在 `fpga`、`driver` 的本地历史（合并提交的第一父）里仍可检出复现。

## 传感器能力与余量

IMX415：12-bit 最高 60.3 fps、10-bit 最高 90.9 fps（3840×2160）。当前 7.85 fps/路是
MCLK 12.5MHz + INCKSEL 的配置限制，不是通路限制；放到 60 fps 会顶满 Gen2 ×2（需 x4/Gen3）。

## 快速开始（板子侧）

```sh
# 1) 下载位流（TD，SRAM，易失）
/opt/anlogic/TD_Release_2026.2_NL/bin/bw.sh download_sram.tcl
# 2) 重启 RK3588（必须，否则枚举不到 Gen2 ×2）
ssh orangepi@<板子IP> 'sudo reboot'
# 3) 驱动（默认 1920×1080 / 1 槽；probe 内会 CONFIGURE + ARM 一次）
sudo insmod anlogic_pci.ko frame_width=1920 frame_height=1080 frame_slots=1
ls /dev/ANLOGIC-PCI0_{user,control,frame}
# 4) 选相机 B（A 路写者待修）并取一帧
sudo /root/tools/pci_bar 0 0x44 0x161
sudo /home/orangepi/newdrv/rka1_smoke --arm --baseline 0 --timeout 4000
# 5) 严格校验
python3 driver/tools/pcie_integration/r10f_strict.py check /tmp/rka1_frame.dma --width 1920 --height 1080
```

## 硬件与工具链

- FPGA：安路 **PH1P100SBG484**，TD `Release_2026.2_NL`（Linux 无头：`td.sh build.tcl`）
- 主机：**Orange Pi 5 Max / RK3588**，内核 `7.2.0-dirty`（aarch64，NFS root）
- 相机：**FEP-VID006-MIPI** 子卡（IMX415 ×2）
- 交叉编译驱动：`make -C <rk3588 内核树> M=$PWD ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- modules`
