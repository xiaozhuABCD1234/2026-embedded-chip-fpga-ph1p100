# tools 脚本

FPGA JPEG 压缩链路的全部工具：主机侧负责参考编码/封装/校验，板端负责取流、
推理与长稳。主机侧需要 numpy + Pillow；板端另需 `/root/{pci_bar,link_ctl,frame_grab}`
和已加载的 `anlogic_pci`。

| 文件 | 跑在哪 | 作用 |
|---|---|---|
| `jpeg_ref.py` | 主机 | 灰度基线 JPEG 黄金编码器：`encode_entropy()` 出纯熵流，`run()` 出字节数/PSNR |
| `jpeg_file.py` | 主机/板端 | `build_header()`/`build_jpeg()` 补标准 JFIF 头（默认 1920×1088，可传尺寸）；CLI `wrap`/`check` |
| `board/jpeg_cap.py` | 板端 | 两段式取流库 + CLI：`bar_read/bar_write/link_ctl/frame_wait`、`capture_entropy(fd)`、`capture_stream(path)` |
| `board/fpga_jpeg_npu.py` | 板端 | import 上面两个模块：压缩 -> PCIe -> 解码 -> RKNN YOLO11n -> 画框 |
| `board/stab_compress.py` | 板端 | 压缩支路长稳采样；独立脚本，只依赖 `/root/pci_bar` |
| `board/demo.sh` | 主机 | 探测板端、scp 依赖到 `/tmp`、执行 env/cap/npu/stab/all |
| `selftest.py` | 主机 | 纯本地自检：fake BAR/mmap、JPEG 头 golden hash、编解码回环 |

## 本地自检

```sh
python3 -m py_compile tools/jpeg_ref.py tools/jpeg_file.py tools/selftest.py \
    tools/board/jpeg_cap.py tools/board/fpga_jpeg_npu.py tools/board/stab_compress.py
python3 tools/selftest.py
```

本机 numpy/Pillow 只装在系统 python3.12 时：

```sh
PYTHONPATH=/usr/lib/python3/dist-packages python3.12 tools/selftest.py
```

缺依赖的项打印 SKIP，其余仍会跑；有失败时返回非零。

## 板端

```sh
tools/board/demo.sh env     # PCIe/驱动/温度/设备节点
tools/board/demo.sh cap     # 只读取一帧到 /tmp/demo_cap.pgm
tools/board/demo.sh npu     # FPGA JPEG -> RKNN，标注图 /tmp/demo_npu.jpg
tools/board/demo.sh stab    # 压缩长稳 20 s，日志 /tmp/stab_compress.log
tools/board/demo.sh all
BOARD=192.168.1.102 tools/board/demo.sh env   # 覆盖板端 IP
```

`npu` 要求 `/tmp` 下同时有 `fpga_jpeg_npu.py`、`jpeg_cap.py`、`jpeg_file.py`、
`jpeg_ref.py`（demo.sh 负责同步）以及 `/root/models/yolo11n-640-int8.rknn`。
`npu`/`stab` 会写 `0x240`；检测到长稳在跑时默认拒绝，`DEMO_FORCE=1` 可覆盖。
`cap` 只读既有帧缓冲，不 ARM、不写 `0x240`，可与长稳并存。
