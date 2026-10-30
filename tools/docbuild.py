#!/usr/bin/env python3
"""docbuild.py —— 把仓库里的 Markdown 文档合成一份可直接提交/打印的 HTML。

只依赖 python-markdown（宿主 python3 若没有，用 venv）：
  python3 -m venv /tmp/docgen && /tmp/docgen/bin/pip install markdown
  /tmp/docgen/bin/python tools/docbuild.py

产物：docs/交付文档.html（单文件，图片内嵌 base64，A4 打印样式）。
浏览器打开后 Ctrl+P 可另存 PDF，Word 也能直接打开。
"""
from __future__ import annotations

import base64
import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "交付文档.html"

# (文件, 章标题, 是否新起一页)
SECTIONS = [
    ("README.md", "一、作品概览", True),
    ("docs/REQUIREMENTS.md", "二、赛题要求对照与缺口", True),
    ("docs/TEST_REPORT.md", "三、测试报告（量化数据）", True),
    ("docs/ARCHITECTURE.md", "四、架构与寄存器", True),
    ("docs/CODEC.md", "五、硬件 JPEG 压缩", True),
    ("docs/RKNN.md", "六、RKNN 部署", True),
]

# 文档里少量 LaTeX：按已知形态转成 HTML，避免引入 KaTeX 依赖
LATEX_BLOCKS = [
    (
        r"""T_{\mathrm{E2E}} = 52.6 \sim 55.2\ \mathrm{ms}
= \underbrace{32.4\ \mathrm{ms}}_{\text{2192 行} \times 14.8\,\mu\mathrm{s}}
+ \underbrace{\approx 20\ \mathrm{ms}}_{\text{打包+DDR+PCIe}}""",
        "T<sub>E2E</sub> = 52.6 ~ 55.2 ms = "
        "<span class=\"ub\">32.4 ms<small>2192 行 × 14.8 µs</small></span> + "
        "<span class=\"ub\">≈ 20 ms<small>打包 + DDR + PCIe</small></span>",
    ),
]

CSS = """
@page { size: A4; margin: 18mm 16mm 16mm 16mm; }
body { font-family: "Noto Sans CJK SC","Source Han Sans SC","Microsoft YaHei",sans-serif;
       font-size: 10.5pt; line-height: 1.55; color: #1a1a1a; max-width: 100%; }
h1 { font-size: 19pt; border-bottom: 2px solid #333; padding-bottom: 6px; margin-top: 0; }
h2 { font-size: 14pt; margin-top: 22px; border-left: 4px solid #4472c4; padding-left: 8px; }
h3 { font-size: 12pt; margin-top: 16px; }
table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 9.5pt; }
th, td { border: 1px solid #bbb; padding: 4px 7px; vertical-align: top; }
th { background: #eef2f8; font-weight: 600; }
tr:nth-child(even) td { background: #fafafa; }
code { background: #f2f2f2; padding: 1px 4px; border-radius: 3px;
       font-family: "JetBrains Mono",Consolas,monospace; font-size: 9pt; }
pre { background: #f7f7f7; border: 1px solid #e0e0e0; border-radius: 4px;
      padding: 8px 10px; overflow-x: auto; font-size: 8.8pt; line-height: 1.4; }
pre code { background: none; padding: 0; }
img { max-width: 100%; height: auto; display: block; margin: 10px auto;
      border: 1px solid #ddd; }
blockquote { border-left: 3px solid #ddd; margin-left: 0; padding-left: 10px; color: #555; }
.cover { text-align: center; padding-top: 60mm; page-break-after: always; }
.cover h1 { border: none; font-size: 26pt; margin-bottom: 8px; }
.cover .sub { font-size: 13pt; color: #444; margin: 4px 0; }
.cover .meta { margin-top: 40mm; font-size: 10pt; color: #666; line-height: 2; }
.section { page-break-before: always; }
.math { text-align: center; margin: 12px 0; font-size: 11pt; }
.ub { display: inline-block; text-align: center; border-bottom: 1px solid #888;
      padding: 0 6px; margin: 0 3px; }
.ub small { display: block; font-size: 7.5pt; color: #555; border-top: 0;
            font-family: "Noto Sans CJK SC",sans-serif; }
.toc a { color: #1a1a1a; text-decoration: none; }
.toc li { margin: 3px 0; }
@media print { .section { page-break-before: always; } }
"""


def embed_images(text: str, base: Path) -> str:
    def repl(m: re.Match) -> str:
        alt, src = m.group(1), m.group(2)
        if src.startswith(("http://", "https://", "data:")):
            return m.group(0)
        p = (base / src).resolve()
        if not p.is_file():
            return m.group(0)
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif"}.get(p.suffix.lower(), "application/octet-stream")
        b64 = base64.b64encode(p.read_bytes()).decode()
        return f"![{alt}](data:{mime};base64,{b64})"
    return re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", repl, text)


def _math_to_html(tex: str) -> str:
    """已知公式给手写 HTML；其余原样放进等宽块，避免 LaTeX 被当 Markdown 吃掉。"""
    norm = re.sub(r"\s+", " ", tex.strip())
    for tex_src, html_snip in LATEX_BLOCKS:
        if re.sub(r"\s+", " ", tex_src.strip()) == norm:
            return f'<div class="math">{html_snip}</div>'
    return f'<div class="math"><code>{html.escape(tex.strip())}</code></div>'


def render_md(rel: str, md) -> str:
    path = ROOT / rel
    text = path.read_text(encoding="utf-8")
    # 去掉各文件自己的一级标题（章标题由 SECTIONS 给）
    text = re.sub(r"\A\s*#[^\n]*\n", "", text)
    text = embed_images(text, path.parent)
    # 公式块必须先摘出来：否则 markdown 会把 _ ^ 当强调语法拆坏
    blocks: list[str] = []

    def stash(m: re.Match) -> str:
        blocks.append(_math_to_html(m.group(1)))
        return f"\n\nMATHPLACEHOLDER{len(blocks) - 1}\n\n"

    text = re.sub(r"\$\$(.+?)\$\$", stash, text, flags=re.S)
    body = md.convert(text)
    for i, snip in enumerate(blocks):
        body = re.sub(rf"<p>\s*MATHPLACEHOLDER{i}\s*</p>", snip, body)
    # 站内 .md 链接改成不带链接的文本（合并成单文件后无意义）
    body = re.sub(r'<a href="[^"]*\.md[^"]*">([^<]*)</a>', r"\1", body)
    return body


def main() -> int:
    try:
        import markdown
    except ImportError:
        print("缺少 python-markdown：python3 -m venv /tmp/docgen && "
              "/tmp/docgen/bin/pip install markdown", file=sys.stderr)
        return 2

    md = markdown.Markdown(extensions=["tables", "fenced_code", "toc", "sane_lists"])

    parts, toc = [], []
    for rel, title, _ in SECTIONS:
        parts.append(f'<div class="section"><h1>{title}</h1>'
                     f'{render_md(rel, md)}</div>')
        md.reset()
        toc.append(f'<li>{title}</li>')

    css = CSS.replace("</style>", "")
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>PH1P100 × RK3588 异构图像加速平台 —— 交付文档</title>
<style>{css}</style></head><body>
<div class="cover">
  <h1>PH1P100 × RK3588<br>异构图像加速平台</h1>
  <p class="sub">MIPI 采集 · 四路硬件预处理 · 硬件 JPEG · PCIe 传帧 · NPU 推理</p>
  <p class="sub">2026 嵌入式芯片与系统设计竞赛 —— 平台三</p>
  <div class="meta">
    器件：安路 PH1P100SBG484 + 瑞芯微 RK3588<br>
    工具链：Anlogic TD Release_2026.2_NL / RKNN-Toolkit2 2.3.2<br>
    文档版本：2026-10-30
  </div>
</div>
<div class="section"><h1>目录</h1><ol class="toc">{''.join(toc)}</ol></div>
{''.join(parts)}
</body></html>
"""
    OUT.write_text(doc, encoding="utf-8")
    print(f"写出 {OUT.relative_to(ROOT)}（{OUT.stat().st_size / 1024:.0f} KiB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
