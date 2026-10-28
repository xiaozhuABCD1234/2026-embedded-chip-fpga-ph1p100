# 数据链路

单路 IMX415 全像素 4K30 → MIPI CSI-2 → R10F → DDR3 双缓冲 → PCIe one-shot 推帧 → RK3588。

## 1. 拓扑

~~~mermaid
flowchart LR
  subgraph FPGA["PH1P100SBG484（EP）"]
    CAM["IMX415 4K30 全像素"] -->|MIPI CSI-2 4-lane 27MHz/891Mbps| CSI["checked CSI"]
    CSI --> GATE["sync_frame_gate"]
    GATE --> W["cam_r10f_writer"]
    W --> DDR[("DDR3 双缓冲 A0/A1")]
    DDR --> P["ddr_frame_pusher"]
    BAR["ep_bar_win BAR0 1MiB"] -.->|"控制/状态"| P
    P -->|"AXI 读"| ARB["ddr_rd_arb2"]
    P --> TL["pcie_ep_engine Posted MWr"]
  end
  TL -->|"PCIe Gen2 x2"| RC["RK3588 RC"]
  RC --> MEM["一致性收帧缓冲：槽 0 / 槽 1 / mailbox"]
  MEM --> DEV["/dev/ANLOGIC-PCI0_frame"]
  DEV --> GRAB["frame_grab / r10f_check"]
  DEV --> DISP["display-stream"]
  DISP --> ENC["解包 + 去马赛克 + 后处理 + VEPU121 JPEG"]
  ENC --> WEB["HTTP /stream /snapshot"]
~~~

双路时 A/B 各一片 writer + pusher，`ddr_rd_arb2` 串行化 DDR 读、`utx_arb2` 做帧级 TL 授权（A/B 包不交错）。

## 2. 帧契约

| 量 | 值 |
|---|---:|
| 源图 | 3840×2160（IMX415 全像素，4K30） |
| packed 行宽（stride） | 4800 B |
| payload | 10,368,000 B（= 4800 × 2160） |
| R10F 头 | 128 B |
| 尾零 | 2,944 B（填充到 4 KiB 边界） |
| 整帧 dma_bytes | **10,371,072 B**（= 2532 × 4 KiB） |
| 每帧 MWr | 81,024 笔 128 B + 1 笔 4 B 门铃 |
| host 槽步长 | PAGE_ALIGN(128 + 10,371,072) = 10,375,168 B |
| host 槽数据起点 | 槽基址 + 128 B（16 B 槽头 + 112 B pad） |
| DDR 双缓冲 | A0=0x0100_0000、A1=0x0200_0000；B0=0x0300_0000、B1=0x0400_0000 |
| 板载 DDR3 | 256 MB（0x1000_0000） |

R10F 头里的 `sof_timestamp`/`timestamp_hz` 仍为 0；`bayer_phase` 字段是 RTL 占位推导，不是测量值。

## 3. 帧路径

1. `sync_offline_top` 经 I2C 写完整寄存器表：4K30 全像素（`SYS_MODE=0x08`、HMAX=1100、VMAX=2250、27 MHz/891 Mbps×4 lane）。
2. checked CSI 做 ECC/VC/DT/WC/CRC/行数/FS-FE 裁决；`sync_frame_gate` 只在同步锁定且整帧校验通过后放行帧边界。
3. `cam_r10f_writer` 取整帧（X0=Y0=0，不做 ROI），打包 packed RAW10，加 128 B 头与 4 KiB 尾零，写 DDR3 双缓冲。
4. `ddr_frame_pusher` 把已发布块打成 4DW 头 + 128 B 载荷的 MWr；推完进 HELD，匹配 ACK 才释放源块。
5. 主机槽 0 收帧，A 门铃在 mailbox +32（B 路 +36）。驱动补 16 B 槽头与 `mailbox.seq/count`。

`CAMDDR_SKIP_ROWS=37` 是 1080p ROI 时代的板测标定值，4K 全帧尚未复测。

## 4. 寄存器

| 偏移 | 名称 | 说明 |
|---|---|---|
| `0x040` | MAGIC | 恒 `0x50434946` |
| `0x044` | CTRL | bit0 使能、bit2 reinit toggle、bit5 DDR_EN、bit6 PUSH_EN、bit8 CAM_B |
| `0x06C` | SCRATCH | arm 掩码：1=A、2=B、3=A+B（运行时生效） |
| `0x0A0..0x0AC` | A DDR 握手 | seq/base/status/len |
| `0x0B0..0x0BC` | A 目标/门铃 | 单路 B 在 pair_en=0 时复用 |
| `0x0C0..0x0C8` | A done/status/err | 成功序号、状态、错误码 |
| `0x0E0..0x0EC` | B DDR 握手 | 同上 |
| `0x100` | DMA_CAP_BYTES | ARM 前写容量 |
| `0x104` | SESSION_ID | 活动期写会被拒 |
| `0x108` | TX_CMD | W1：1=ARM、2=ACK、4=CANCEL、8=ARM_PAIR、16=ACK_PAIR、32=CANCEL_PAIR |
| `0x10C/0x110` | ACK_SEQ / ACK_SESSION | ACK 时写 |
| `0x114` | TX_STATUS | armed/busy/held/reject/draining |
| `0x118/0x11C` | EXT_MAGIC / EXT_VERSION | `RKA1` 与版本 |
| `0x120..0x12C` | CSI_DIAG_A/B | 每路 good/bad/CRC/行数 |
| `0x170/0x174` | 写者 FSM | `[7:0]`，见 §6 |
| `0x1A0..0x1DC` | 帧诊断 | 见 §6 |
| `0x1E0` | SYNC_CMD | W1：1=START、2=ABORT |
| `0x1E4..0x20C` | SYNC 状态 | 见 [HARDWARE_SYNC](HARDWARE_SYNC.md) |
| `0x210..0x21C` | B done/status/err/tlp | 双路 |
| `0x220..0x230` | B ACK/DST/DB | 双路 |
| `0x234` | PAIR_STATUS | bit0=A held、bit1=B held；A err=[9:2]、B err=[17:10] |
| `0x238/0x23C` | TS_NOW | 自由运行 64 位计数器（clk_ddr，实测 99.92 MHz），延迟测量用 |
| `0x240` | ALGO_CTRL | R/W：[2:1] 选路 0=灰度、1=高斯、2=直方图均衡；bit0 保留 |
| `0x244/0x248/0x24C` | ALGO_ACC/REJ/OVF | ROI 适配器收下/拒绝/溢出的帧数 |

时间戳链路（延迟测量）：

- writer 在 SOF 到达时锁存计数器，写进 R10F 头 16/17 字，18 字写频率（`100000000`）；
- 主机在读帧前读 `0x238/0x23C`，两者相减即「SOF → 本机拿到帧」；
- 这个值包含整帧读出时间（4K 约 32.4 ms），传输段要用它减去读出时间，或等后续加 EOF 时间戳直接测。

错误码：0 正常、2 TL 等待超时、3 AXI 读错、5 未 ARM、6 ARM 被拒、7 取消、8 HELD 新帧、9 源不一致、10 配置被拒、11 ACK 不匹配。

## 5. 主机缓冲布局

~~~text
phys + 0        : 槽 0（16B 槽头 + 112B pad + R10F @ +128）
phys + stride   : 槽 1（B 路，同上）
phys + 2*stride : mailbox（A 门铃 +32，B 门铃 +36；frame_slots=1 时是 phys + stride）
~~~

`frame_slots` 取 1 或 2（默认 2）。驱动按几何推导槽长，只接受 4 KiB 整数倍；`frame_slot_bytes=0` 表示自动推导。

## 6. 诊断位域

写者 FSM（`0x170`/`0x174`）：`[6]=hdr_ready`、`[5:3]=s_state`、`[2:0]=a_st`。
`s_state`：0 S_SYNC、1 S_HDR、2 S_PAY、3 S_TAIL、4 S_DRAIN、5 S_WAIT、6 S_SKIP。

每帧锁存（`0x1A0..0x1BC` / `0x1C0..0x1DC`）：

| 偏移 | 内容 |
|---|---|
| +0 | 本帧入队像素组数（3840×2160 满帧应为 2,073,600） |
| +4 | 因 FIFO 满丢掉的组数 |
| +8 | `{frame_lines, y_cnt}` |
| +0x0C | `{16'd0, max_grp_per_line}` |
| +0x10 | 因 FIFO 满丢掉的标记数（SOF/EOF） |
| +0x14 | 本帧打包占用 clk_ddr 拍数 |
| +0x18 | 收口时刻的 gcnt |
| +0x1C | `{tmo_seen, p_bad, p_eof_seen, eof_bad}` |

> 组数是按当前几何算的：满帧 3840×2160 每 5 B 一组 = 2,073,600 组（1080p 时代为 518,400）。该值 4K 未上板核对。

## 7. 边界

- **4K 全帧未上板验证**：RTL 已按 3840×2160 全帧与上述 DDR 基址改好，但没有任何 4K 板测数字。
- 4K30 单路带宽算术值 10,371,072 × 30 ≈ 311 MB/s，占 Gen2 x2 可用带宽约 30%，未实测。
- 位流是 SRAM 配置：重下/掉电后必须重启 RK3588 才能重新枚举 Gen2 x2。
- `sync_pairs` 没有打进 R10F 头；严格「同对」靠硬件同步门控 + 同一次 ARM。
- PCIe 上 A/B 串行发送，不是同时占用链路。