# RKNN YOLO11n 检测/叠加板测（2026-10-28）

板卡：Orange Pi 5 Max / RK3588，rknpu 0.9.8，librknnrt 2.3.2。
模型：`yolo11n-640-int8.rknn`（双输出，SHA-256 `d105e9f4…ae00b5`）。

## 1. 离线 R10F

~~~sh
display-detect --source r10f --r10f /tmp/fa.dma --auto --scale 2 \
  --detect --rknn /root/models/yolo11n-640-int8.rknn --port 8094
~~~

- 模型契约：640×640，双输出 `boxes [1,4,8400]` + `scores [1,80,8400]`，NPU core 0；
- 推理约 50.8 ms（含双输出反量化），7 框；叠加图 `detect_offline.jpg`（显示器/椅子/瓶子框与目标对齐）。

## 2. 双路实时（960×544）

| 项 | A | B |
|---|---:|---:|
| 推理次数 | 53 | 53 |
| 丢弃 | 2 | 2 |
| 错误 | 0 | 0 |
| 最近推理 | 39.8 ms | 40.0 ms |
| 框数 | 6 | 7 |

- `/stats`：`a_fps=21.9`、`b_fps=21.9`、`pair=true`、960×544；平均编码 44.81 ms/对；
- 双路检测没有把推流拖到 15 fps 以下；图 `detect_live_a.jpg`、`detect_live_b.jpg`。

## 3. 量化对照

- 单输出 `[1,84,8400]` int8：classes max = 0.0（框坐标尺度压掉类别分数），不可用；
- FP16 单输出：classes max = 0.8745，推理约 86 ms（原图未归档）；
- 双输出 INT8：scores max = 0.8712，推理约 40~51 ms，正式使用。

## 4. 注意

双路实时测试会独占设备节点，必须停掉其它 display/frame_grab 进程；测完恢复原来的 display-pair。