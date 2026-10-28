# 硬件同步（A 主 / B 从）

IMX415 子卡上 A/B 两路的 XHS/XVS 是共享网络（原理图 R147/R148，100 Ω）；模组内部连接尚未做实物连续性确认。
FPGA 不发同步脉冲，只做角色配置、SOF 数字配对和帧边界门控。

## 1. 传感器角色

| 传感器 | XMASTER(0x3003) | XMSTA(0x3002) | XVS_DRV/XHS_DRV(0x30C1) |
|---|---|---|---|
| A（主） | 0 | 1 待启动 → 0 启动 | 0x00（输出） |
| B（从） | 1 | **0** | 0x0F（Hi-Z） |

B 的 XMSTA 必须为 0。写成 1 时 B 只出 1 个 SOF、A 出约 24 个，配对超时；改 0 后 9 对锁定（2026-10-28 板测）。
主机侧没有可写 I2C，角色配置只能由同步控制器的 14 笔写完成。

## 2. FPGA 做的三件事

1. 用各自 I2C 写完整 201 项寄存器表，再按 A/B 角色写同步寄存器；
2. 在 SOF 上做数字配对，统计 skew/miss/relock；
3. 只有 `sync_ready`（warmup 8 帧 + pair 锁定）才把整帧放给 R10F 写者。

代码位置：`fpga/rtl/ep_side/cam/sync_offline/`（启动序列、SOF 邮箱、配对、清理）、
`sync_i2c_config.v`（同步控制器独占 I2C）、`sync_frame_gate.v`（帧边界放行/撤销）、
`ph1p_pcie_ep_top.v`（`SYNC_ENABLE` 与实例化）。

## 3. 寄存器

| 偏移 | 名称 | 说明 |
|---|---|---|
| `0x1E0` | SYNC_CMD | W1：1=START、2=ABORT（读恒 0） |
| `0x1E4` | SYNC_STATUS | bit0 ready、bit2 locked、bit3 busy、bit4 stopped、bit5 pair_valid（单拍）、[11:8] error |
| `0x1E8` | SYNC_SKEW | SOF/包层数字配对差（signed tick） |
| `0x1EC` | SYNC_PAIR_COUNT | 累计配对帧数 |
| `0x1F0` | SYNC_WARMUP | warmup 配置/进度 |
| `0x1F4` | SYNC_TIMESTAMP | 同步时间戳（当前未用于 R10F） |
| `0x1F8/0x1FC` | SYNC_MISS_A/B | 两路配对缺失计数 |
| `0x200` | SYNC_OVERRUN | `{B,A}` |
| `0x204` | SYNC_RELOCK | 重新锁定计数 |
| `0x208/0x20C` | SYNC_PAIR_SEQ_A/B | 两路最近配对序号 |

`0x1E8` 是 **SOF/包层数字配对差，不是曝光相位差**，不能拿它闭环曝光。

## 4. 主机命令与判读

~~~sh
link_ctl sync status
link_ctl sync start -t 5000
link_ctl sync abort -t 5000
~~~

- 正常稳态 `0x1E4 = 0x8000D`：ready=1、locked=1、busy=1、warmup=8、error=0。
- 失锁后 RTL 不会自动把相机打入待机（避免自锁），主机必须重新 `sync start`。
- 判据必须同时满足：A/B 寄存器读回正确 + 共享波形 + 共同时基下多帧偏置。**同帧号或同 `capture_seq` 不算**同步证据。

## 5. 实测（2026-10-28，1080p ROI 双路）

| 项 | 结果 |
|---|---|
| 首次锁定 | 9 对（warmup 8 + 1） |
| 30.39 s 采样 | A/B 各 +911 帧、pair_count +912 → 29.98 / 30.01 fps |
| skew | `0x1E8` 稳定 2~3 tick（tl_clk_user 62.5 MHz，约 32~48 ns） |
| miss / overrun | A/B miss=0/0，overrun=0 |
| abort / restart | abort 后 stopped=1；重新 start 后 9 对再锁，error 清零 |

证据：`log/sync/RESULTS.md`、`log/sync/pair_contact.jpg`。

## 6. 双路 pair 取流（已实现，4K 单路模式下不使用）

两片完全独立的推帧引擎各服务一台相机，共享 DDR 读口和 TL 用户发送口：

~~~mermaid
flowchart LR
  WA["R10F writer A"] --> PA["ddr_frame_pusher A"]
  WB["R10F writer B"] --> PB["ddr_frame_pusher B"]
  PA --> RA["ddr_rd_arb2"]
  PB --> RA
  RA --> DDR[("DDR3")]
  PA --> TA["utx_arb2 帧级授权"]
  PB --> TA
  TA --> PCIe["PCIe Posted MWr"]
~~~

- `ARM_PAIR`（`0x108=8`）同时授权两片，`ACK_PAIR`（`=16`）同时释放，`CANCEL_PAIR`（`=32`）兜底；
- B 路状态在 `0x210..0x230`，`0x234` 给两片 HELD 与错误码；
- `ACK_PAIR` 后必须读 `0x234 & 0x3 == 0`，否则仍有一片 HELD；
- 门铃会早于 `g_st→HELD` 几拍，工具在 ACK 后补一拍 `CANCEL_PAIR`。

实测：600 次连续双路取帧 600 成功 / 0 失败（180 s），20 次逐帧核对 A/B `capture_seq` 全相同。
证据：`log/pair/RESULTS.md`。

## 7. 边界

- 子卡 XHS/XVS 内部连接未逐网确认；当前是「文档候选 + 板测」。
- `sync_pairs` 没有打进 R10F 头，丢帧后无法从图像回溯所属 pair。
- 4K 全帧 + 硬件同步没有组合验证过（同步实测全部在 1080p ROI 口径）。
- 未做故意丢帧/断链故障注入。