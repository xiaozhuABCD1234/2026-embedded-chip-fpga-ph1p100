# 架构与寄存器

PH1P100 作为 PCIe 端点，负责 MIPI 采集、预处理、JPEG 压缩与推帧；
RK3588 负责取流、显示、NPU 推理与网络。本文只写链路、帧契约和寄存器，
实测数据见 [TEST_REPORT](TEST_REPORT.md)。

## 1. 拓扑

~~~mermaid
flowchart LR
  subgraph FPGA["PH1P100SBG484（PCIe EP）"]
    CAM["2× IMX415 4K30"] -->|"MIPI CSI-2 4-lane"| CSI["checked CSI"]
    CSI --> GATE["sync_frame_gate"]
    GATE --> W["cam_r10f_writer"]
    GATE --> ROI["ROI 适配器"] --> ALG["灰度 / 高斯 / HE"] --> CB[("压缩源 1920×1088")]
    W --> DDR[("DDR3 双缓冲")]
    CB --> JPEG["硬件 JPEG"] --> CS[("压缩块 0x0500_0000")]
    DDR --> P["ddr_frame_pusher"]
    CS --> P
    P --> TL["pcie_ep_engine Posted MWr"]
    BAR["BAR0 1 MiB"] -.-> P
  end
  TL -->|"PCIe Gen2 ×2"| RC["RK3588 RC"]
  RC --> MEM["一致性缓冲：槽 0/1 + mailbox"]
  MEM --> DEV["/dev/ANLOGIC-PCI0_frame"]
  DEV --> GRAB["frame_grab / r10f_check"]
  DEV --> DISP["display-stream：解包→去马赛克→NPU→HTTP"]
~~~

A/B 各一片 writer，双路时 `ddr_rd_arb2` 串行化 DDR 读、`utx_arb2` 做帧级授权。
压缩支路与实时 CSI 不直接相连：从 DDR 回读已发布的 1920×1088 packed RAW10 帧
（cstream_reader 5→4 解包成 8 bit 灰度），避开 CSI FIFO 反压。

## 2. 帧契约

| 量 | 4K 采集位流 | 1920×1088 位流（ROI/JPEG） |
|---|---:|---:|
| 图像 | 3840×2160 packed RAW10 | 1920×1088 packed RAW10（算法侧解包成 8 bit 灰度） |
| stride | 4,800 B | 2,400 B |
| payload | 10,368,000 B | 2,611,200 B（解包后 8 bit 为 2,088,960 B） |
| R10F 头 + 尾零 | 128 B + 2,944 B | 128 B + 1,920 B |
| 整帧 | **10,371,072 B**（2,532×4 KiB） | **2,613,248 B**（638×4 KiB） |
| 每帧 MWr | 81,024×128 B + 门铃 | 20,416×128 B + 门铃 |
| DDR 双缓冲 | A0=0x0100_0000、A1=0x0200_0000 | 同左；B 路 0x0300/0x0400_0000 |

槽长 = align4k(128 + payload)，由驱动按几何推导，4 KiB 整数倍。
历史上的 1080p ROI 位流（1920×1080）槽长是 2,592,768 B（633×4 KiB），
只出现在 [evidence/fps.md](evidence/fps.md)、[evidence/pair.md](evidence/pair.md)。
压缩块固定 2,613,248 B/帧，真实熵流长度在 `0x250`，其余补零；主机必须按长度截取；
压缩率分母是解包后的 8 bit 灰度 2,088,960 B，不是线上 10 bit 载荷。

## 3. 帧路径

1. `sync_offline_top` 经 I2C 写 IMX415 寄存器表：4K30 全阵列（`SYS_MODE=0x08`、
   HMAX=1100、VMAX=2250、27 MHz / 891 Mbps ×4 lane）。
2. checked CSI 做 ECC/VC/DT/WC/CRC/行数/FS-FE 裁决；`sync_frame_gate` 只在整帧校验通过后放行。
3. `cam_r10f_writer` 取 3840×2160（`X_OFF=12`、`SKIP_ROWS=16`，原点 (12,16)），
   打包 RAW10、加 128 B 头与 4 KiB 尾零，写 DDR 双缓冲。
4. `ddr_frame_pusher` 把已发布块打成 4DW 头 + 128 B 载荷的 MWr；推完进 HELD，ACK 后才释放源块。
5. 主机槽 0 收帧；A 门铃在 mailbox +32，B 路 +36。驱动补 16 B 槽头与 `mailbox.seq/count`。

1080p ROI 时代用 `SKIP_ROWS=37`；4K 全帧改用 `X_OFF=12`、`SKIP_ROWS=16`，
避免吃掉左右 OB 列与顶部哑行。ROI 适配器取中心 1920×1088，原点 (972,552)。

压缩数据流：

~~~text
DDR packed RAW10 帧 → cstream_reader（5→4 拆包）→ line2block → dct2d8
           → jpeg_quant_zz → jpeg_huff_enc → cstream_writer（定长帧 0x0500_0000）
~~~

## 4. 寄存器（BAR0）

| 偏移 | 名称 | 说明 |
|---|---|---|
| `0x040` | MAGIC | 恒 `0x50434946` |
| `0x044` | CTRL | bit0 使能、bit2 reinit、bit5 DDR_EN、bit6 PUSH_EN、bit8 CAM_B |
| `0x06C` | SCRATCH | arm 掩码：1=A、2=B、3=A+B |
| `0x0A0..0x0AC` | A DDR 握手 | seq/base/status/len |
| `0x0B0..0x0BC` | A 目标/门铃 | B 路 `0x220..0x230` |
| `0x0C0..0x0C8` | A done/status/err | `0xC8` 错误码见 §7 |
| `0x100` | DMA_CAP_BYTES | ARM 前写容量 |
| `0x104` | SESSION_ID | 活动期写被拒 |
| `0x108` | TX_CMD | W1：1=ARM、2=ACK、4=CANCEL、8=ARM_PAIR、16=ACK_PAIR、32=CANCEL_PAIR |
| `0x10C/0x110` | ACK_SEQ / ACK_SESSION | ACK 时写 |
| `0x114` | TX_STATUS | armed/busy/held/reject/draining |
| `0x118/0x11C` | EXT_MAGIC / EXT_VERSION | RKA1 与版本 |
| `0x120..0x12C` | CSI_DIAG_A/B | good/bad/CRC/行数 |
| `0x170/0x174` | 写者 FSM | `[6]=hdr_ready`、`[5:3]=s_state`、`[2:0]=a_st` |
| `0x1A0..0x1DC` | 帧诊断 | 组数/丢组/行数/拍数/判坏原因 |
| `0x1E0` | SYNC_CMD | W1：1=START、2=ABORT |
| `0x1E4..0x20C` | SYNC 状态 | 见 §6 |
| `0x234` | PAIR_STATUS | bit0/1=A/B held；错误码 `[9:2]/[17:10]` |
| `0x238/0x23C` | TS_NOW | 64 位自由计数器（clk_ddr，实测 99.92 MHz） |
| `0x240` | ALGO_CTRL | `[2:1]` 0=灰度、1=高斯、2=HE、3=Sobel 边缘；bit4=压缩；bit6=推压缩块 |
| `0x244/0x248/0x24C` | ALGO_ACC/REJ/OVF | ROI 适配器收下/拒绝/溢出帧数 |
| `0x250` | 压缩流长度 | 最近完成帧的熵流字节数 |
| `0x254/0x260` | 回读像素/启动次数 | 诊断 |
| `0x258` | JPEG 状态 | `{jb_done[31:16], jb_v[15:0]}` |
| `0x264` | 编解码状态 | reader/writer 状态、busy |
| `0x268` | 读口握手 | DDR 读口 |
| `0x26C` | 压缩帧序号 | 帧边界判据 |
| `0x270/0x274` | 首字节/整帧周期 | rd_start→首字节 / →`jb_done` |
| `0x280` | CAM_GAIN_CTL | 写 `{24'd0, gain[7:0]}`：锁存并触发一次 I2C 写 0x3090 |
| `0x284` | CAM_GAIN_STAT | `{18'd0, done_b, done_a, ae_st, 2'd0, applied_gain}` |

时间戳：writer 在 SOF 锁存计数器写进 R10F 头 16/17 字，18 字写频率。
4K 采集位流实测有效（延迟测量即用它）；JPEG 位流未单独复核。

**错误码**：0 正常、2 TL 超时、3 AXI 读错、5 未 ARM、6 ARM 被拒、7 取消、
8 HELD 新帧、9 源不一致、10 配置被拒、11 ACK 不匹配。

## 5. 主机缓冲布局

~~~text
phys + 0        : 槽 0（16 B 槽头 + 112 B pad + 数据 @ +128）
phys + stride   : 槽 1（B 路，布局同上）
phys + 2*stride : mailbox（门铃 A +32、B +36；frame_slots=1 时为 phys + stride）
~~~

`frame_slots` 取 1 或 2（默认 2）；驱动按几何推导槽长，只接受 4 KiB 整数倍。

## 6. 双路与硬件同步

IMX415 子卡上 A/B 的 XHS/XVS 是共享网络；FPGA 不发同步脉冲，只做角色配置、
SOF 数字配对和帧边界门控。

| 传感器 | XMASTER(0x3003) | XMSTA(0x3002) | XVS_DRV(0x30C1) |
|---|---|---|---|
| A（主） | 0 | 1 待启动 → 0 | 0x00（输出） |
| B（从） | 1 | **0** | 0x0F（Hi-Z） |

- 稳态 `0x1E4 = 0x8000D`：ready/locked/busy、warmup=8、error=0；
- `0x1E8` 是 SOF/包层数字配对差（tick），**不是曝光相位差**；
- 判据必须同时满足：A/B 寄存器读回 + 共享波形 + 共同时基多帧偏置。
  同帧号或同 `capture_seq` 不算同步证据；
- 1080p ROI 时代实测 skew 2~3 tick（62.5 MHz，约 32~48 ns），miss/overrun=0；
- **4K 下 B 切 slave 后拿不到可用 SOF**（`sync start` 在 WARM 超时，`miss_b` 逐帧 +1），
  已排除 B 的 I2C 与 PHY，指向板级 XVS/XHS 飞线，需示波器确认。

双路 pair 取流：`ARM_PAIR` 同时授权两片，`ACK_PAIR` 同时释放；门铃会早于
`g_st→HELD` 几拍，工具在 ACK 后补一拍 `CANCEL_PAIR`。

## 7. 边界

- **4K30 全帧单路已上板**（29.9 fps、丢组 0）；双路 4K 不可行：单路打包已占 78%，
  两路 156% 必溢出。
- 4K + 硬件同步未组合验证；4K 长稳 >60 s 未测。
- 压缩位流写者几何固定 1920×1088；4K 与 JPEG 是两个独立位流。
- 位流是 SRAM 配置：重下/掉电后必须重启 RK3588 才能重新枚举 Gen2 ×2。
- PCIe 上 A/B 串行发送，不是同时占用链路；`sync_pairs` 未打进 R10F 头。
