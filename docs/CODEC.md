# 硬件 JPEG 压缩

交付项 3 的实现说明。量化数据见 [TEST_REPORT](TEST_REPORT.md) §5。

## 1. 选型

白天实拍 1920×1088 灰度帧（2,088,960 B）上，无损方案都到不了 30%：

| 方案 | 压缩率 | 判定 |
|---|---:|---|
| 差分 + 零游程 RLE | -8.45% | 膨胀，否决 |
| 差分 + RLE（bit7 格式） | -0.23% | 无收益，否决 |
| 差分 + LZ77 | 3.54% | 远低于 30% |
| 3×3 高斯 + 差分 LZ77 | 17.26% | 仍低于 30% |

DCT 量化直接丢弃人眼不敏感的高频，压缩率由量化表控制，因此改为**灰度基线 JPEG**：
只需单分量，不需要色度采样、色彩转换和重启间隔。

## 2. 码流契约

- 输入：DDR 里是 1920×1088 packed RAW10（2,611,200 B），`cstream_reader` 5→4 解包成
  8 bit 灰度（2,088,960 B），行优先；8×8 分块、像素减 128。
- DCT：Q12 定点、行列分离。
- 量化：标准亮度表，q=50；$z = \operatorname{sign}(c)\cdot((|c|\cdot R + 2^{15}) \gg 16)$，$R = \operatorname{round}(2^{16}/q)$。
- 之字形后编码：DC 差分（每帧首块预测器清 0）、AC 的 `(run<<4)|category`、
  `ZRL` 与 `EOB`、负数幅度按 T.81 用 `v-1`。
- 熵编码：T.81 附录 K 亮度 DC/AC 规范哈夫曼表；`0xFF` 后补 `0x00`。
- 输出：**只有熵编码字节**，不含 SOI/APP0/DQT/SOF0/DHT/SOS/EOI。
- 封装：主机 `tools/jpeg_file.py` 补常量头 + EOI；FPGA 把熵流写到
  `0x0500_0000`，补零到 2,613,248 B，真实长度在 `0x250`。

## 3. RTL 模块

| 模块 | 职责 |
|---|---|
| `cstream_reader` | DDR 回读；64 拍突发 AR + 2 KB FIFO（R 通道不能反压），5→4 拆包 |
| `line2block` | 双缓冲 8 行组：写 bank 与编码 bank 并行 |
| `dct1d8` / `dct2d8` | 8 点 1D DCT（Q12），行→转置→列 |
| `jpeg_quant_zz` | q=50 亮度表量化 + 之字形 |
| `jpeg_huff_tab/enc` | 规范哈夫曼表；DC 差分、AC 游程、打包 |
| `jpeg_enc` / `jpeg_frame` | 单块三级流水 / 整帧顶层 |
| `cstream_writer` | 定长 2,613,248 B 帧写 DDR，记录每帧熵流长度 |

主链：

~~~text
cstream_reader → line2block → dct2d8 → jpeg_quant_zz → jpeg_huff_enc → cstream_writer
~~~

## 4. 寄存器与主机流程

| 偏移 | 含义 |
|---|---|
| `0x240` | ALGO_CTRL：bit4=压缩使能，bit6=推压缩块，bits[2:1] 算法选路 |
| `0x250` | 最近完成帧的熵流字节数（每帧刷新） |
| `0x258` | `{jb_done[31:16], jb_v[15:0]}` |
| `0x264` | `{reader state, writer state, busy}` |
| `0x26C` | 压缩帧序号 |
| `0x270/0x274` | rd_start→首字节 / →`jb_done` 周期数 |

DDR 读口与推帧分时，主机两段式：

1. `0x240=0x10` 开压缩，轮询 `0x258[31:16]` 等帧完成；
2. 再等 writer 帧号 `0x26C` 变化（最后一个 beat 落 DDR）；
3. `0x240=0x40` 切推帧源到压缩块，`link_ctl arm`；
4. 从槽 0 的 `+128` 读 `0x250` 字节。

**约束**：`0x240` bit4 只能在帧边界切换。字节在 `jb_done` 后立刻切会丢 writer
尾拍，主机解码报 `broken data stream`（已复现并修复）。

## 5. 工具与验证

| 工具 | 用途 |
|---|---|
| `tools/jpeg_ref.py` | 主机黄金编码器，与 Pillow 交叉验证 |
| `tools/jpeg_file.py` | 熵流包 JFIF、检查头与 PSNR |
| `tools/board/jpeg_cap.py` | 板端两段式压缩/推帧/取流 |
| `tools/board/fpga_jpeg_npu.py` | 压缩→PCIe→解码→RKNN→画框全链路 |
| `tools/board/stab_compress.py` | 压缩长稳压测 |

- 参考编码器与 Pillow 的码流大小/PSNR 偏差 ≤1.5%；
- `cd fpga && sh sim/run_codec_tb.sh` 自动生成确定性输入，跑两帧 JPEG/突发回读/写出器三个 TB；
- 两帧码流的 `m_done` 可能比最后一个 pad 字节早一拍，回归允许 1 字节切分。

## 6. 结果与边界

- 典型压缩率 95.5%（93,443 B / 2,088,960 B，分母为解包后 8 bit 灰度），长稳范围 94.7%~98.3%；
- 编码器 65.7 ms/帧，亮场 7.5~8.5 fps，低光可降到 ~4 fps；
- 1 h 长稳 26,965 帧，`jb_done` 严格单调；
- 静态区域 PSNR 34~40 dB，整帧 34.9 dB。

板端压缩、PCIe 回传后由主机包 JFIF 解码的实拍图（1920×1088，93,443 B）：

![FPGA JPEG 板端解码](figures/jpeg_board_capture.png)

**源块释放修复**：原先 `rd_done` 一次释放两块，写者会覆盖已发布的另一块，
回读开头约 128 行读到新旧混合，标注图出现横线、周期 ~250 ms。改成只释放本次读的
bank 后横线消失，发布率 4.5→8.5 fps。

**长稳死锁修复**：`reader` 的 `start` 在 `S_IDLE` 多打一拍、`base` 晚一拍锁存，
若在 `rd_start` 当拍采样会把 bank 记反，约 12 s 后两块锁死；改成同样晚一拍后通过 1 h 长稳。

边界：仅灰度 1920×1088；q 固定 50；压缩位流与 4K 采集位流独立；
帧级 `codec_ready` 握手，上一帧没结束不启动下一帧；熵流补零必须按 `0x250` 截取。
