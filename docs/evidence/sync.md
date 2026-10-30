# 硬件同步板测（2026-10-28 20:08，1080p ROI 双路）

| 项 | 结果 |
|---|---|
| 位流 | `ph1p_pcie_ep_top.bit` 5,224,023 B，SHA256 `5b058997…41321`（20:06 构建） |
| 链路 | Gen2 ×2，BAR0 1 MB |
| 同步模式 | A 主 / B 从（XHS/XVS 共享网络） |
| 首次锁定 | 9 对（warmup 8 + 1） |
| 稳态 | `0x1E4=0x8000D`：ready=1、locked=1、busy=1、warmup=8、error=0 |
| 30.39 s 采样 | A/B 各 +911 帧，pair_count +912 → 29.98 / 30.01 fps |
| skew | `0x1E8` 稳定 2~3 tick（62.5 MHz，约 32~48 ns） |
| miss/overrun | A/B miss=0/0，overrun=0 |
| abort/restart | abort 后 stopped=1；重新 start 后 9 对再锁，error 清零 |

## 关键修复

首版 B 的 `XMSTA(0x3002)` 沿用了 1，B 只出 1 个 SOF、A 出约 24 个，配对超时。改成 0 后 9 对锁定。
主机侧没有可写 I2C，角色写只能由同步控制器完成。

## 图像

两帧是**先后 one-shot 抓取**（当时驱动单槽/单 pusher），不是同一 pair：A seq=5062、B seq=5542，各自 R10F strict 通过。

| 文件 | SHA256 |
|---|---|
| A.pgm | `f6a0934bc1d6732f2da19bd2b27741076d16fe71f26e3fcec098b278413ac1af` |
| B.pgm | `9c6d2d9b700e821a239b8f695a5b5d4c7f3e94c3f38874ba2fd2c331ee36326f` |
| sync_contact.jpg | `c973d1f8a49a2f8e2624af96d1961ee62566073a67b34a28f678fca3fb6d46ac` |

![A|B](sync_contact.jpg)

## 未完成

- 同一 pair 双槽同时取流（见 [pair.md](pair.md) 已补齐）；
- A/B R10F 时间戳字段仍为 0；
- 60 s 以上长稳、丢一路/坏帧注入；
- 同步判据仍需主从寄存器读回 + 共享 XHS/XVS 波形 + 共同时基多帧偏置；本文件 skew 只是 SOF 数字配对差。