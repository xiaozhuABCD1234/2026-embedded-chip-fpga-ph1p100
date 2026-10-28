# RKNN YOLO11n 检测与叠加

代码在 display 子仓库 `src/detect/`。

## 1. 结论

- RK3588 NPU 上跑 YOLO11n 640 INT8；双路各一个 context，A=core0、B=core1。
- 检测在 postprocessed RGB 上做，letterbox 到 640×640 NHWC，结果按归一化坐标叠加到当前输出尺寸，所以 `--scale` 改尺寸不影响框位置。
- 检测 worker 与 JPEG 编码解耦，mailbox 只保留最新任务；结果超过 500 ms 不画。
- 板测（960×544 双路）：21.9 pair/s，单路推理约 40 ms，A 6 框 / B 7 框。

## 2. 模型契约

| 项 | 值 |
|---|---|
| 模型 | `models/yolo11n-640-int8.rknn` |
| 输入 | `images [1,3,640,640]` NCHW float32，mean=0/std=255，RGB |
| 输出 | `boxes [1,4,8400]` + `scores [1,80,8400]` |
| 量化 | w8a8，双输出分开量化 |
| 类别 | COCO 80 |

## 3. 为什么必须拆输出

Ultralytics 默认 ONNX 输出是一个 `[1,84,8400]` 张量，前 4 通道是 0..640 的框坐标，后 80 通道是 0..1 的类别分数。
RKNN 对整张量做一层 int8 量化时 scale 由框坐标主导（约 5.3/bit），0..1 的分数全部量化到 0。
实测原模型 classes max 正好 0.0，框坐标正常。

在 ONNX 最后一层 Concat 前把 boxes 和 scores 拆成两个 graph output，RKNN 会分别算量化参数；
实测 scores max 恢复到 0.87，框与类别都正常。转换脚本 `display/scripts/convert_yolo11n.sh`。

FP16 单输出模型也能用（scores max 0.87），但推理约 86 ms；INT8 双输出约 40 ms。

## 4. 线程与数据流

~~~mermaid
flowchart TB
  CAP["pair-capture：ARM_PAIR → 两门铃 → 两槽 R10F"] --> EA["encode 线程 A"]
  CAP --> EB["encode 线程 B"]
  EA --> P1["解包 / CFA binning / 黑电平 / AWB / AE / gamma"]
  EB --> P2["同上"]
  P1 --> SA["detector.submit：letterbox 640×640 NHWC"]
  P2 --> SB["detector.submit"]
  SA --> MA["mailbox A（只留最新）"]
  SB --> MB["mailbox B（只留最新）"]
  MA --> WA["detect worker A（NPU core0）"]
  MB --> WB["detect worker B（NPU core1）"]
  WA --> OA["overlay：取最新结果，按归一化坐标画框"]
  WB --> OB["overlay"]
  OA --> J["VEPU121 / 软件 JPEG → FrameStore"]
  OB --> J
~~~

提交按 `--detect-fps` 限频（默认 15）。worker 忙时新任务覆盖旧任务并记 dropped，不排队，因此延迟有界。

## 5. FFI 与部署

不链接 librknnrt：用 `libc::dlopen` 运行时加载 `librknnrt.so`，x86 开发机不需要 RKNN SDK 也能编译和跑纯算法测试。
结构体按 rknn-toolkit2 2.3.2 的 `rknn_api.h` 镜像（`rknn_input`/`rknn_output`/`rknn_tensor_attr`）。

板上依赖：

- `/usr/lib/librknnrt.so` 2.3.2
- rknpu 0.9.8，`/dev/dri/renderD129`
- `/root/models/yolo11n-640-int8.rknn`

~~~sh
/root/tools/display-detect --source device-pair --fps 30 --scale 2 \
  --detect --rknn /root/models/yolo11n-640-int8.rknn --port 8091
~~~

## 6. 命令行

| 选项 | 默认 | 说明 |
|---|---|---|
| `--detect` | 关 | 打开检测；不带则零开销 |
| `--rknn` | `/root/models/yolo11n-640-int8.rknn` | 模型 |
| `--conf` | 0.25 | 置信度阈值 |
| `--nms` | 0.45 | NMS IoU |
| `--detect-side` | pair=both，单路=a | `both`/`a`/`b` |
| `--detect-fps` | 15 | 每路提交上限 |
| `--no-overlay` | 关 | 只推理/日志，不画框 |

## 7. 失败模式

- `librknnrt.so` 缺失或 `rknn_init` 返回负值：启动失败并打印 RKNN 错误码与 rknpu 排查提示。
- 输出不是单 `[1,84,N]` 或双 `[1,4,N]`+`[1,80,N]`：启动失败并打印实际 dims。
- 运行中连续 5 次推理失败：worker 置 disabled，视频继续，不再叠框。
- 检测慢：mailbox 覆盖旧任务并记丢弃；超过 500 ms 的结果不画。
- `--bayer gray` 与 `--detect` 互斥；单路 `--detect-side b` 报错。

## 8. 证据

`log/detect/RESULTS.md`，图 `log/detect/live_a.jpg`、`live_b.jpg`、`offline_int8.jpg`。

复跑（离线，不影响设备占用）：

~~~sh
scp display/target/aarch64-unknown-linux-gnu/release/display-stream root@<BOARD>:/root/tools/display-detect
scp display/models/yolo11n-640-int8.rknn root@<BOARD>:/root/models/
ssh root@<BOARD> '/root/tools/display-detect --source r10f --r10f /tmp/fa.dma \
    --auto --scale 2 --detect --port 8094'
~~~