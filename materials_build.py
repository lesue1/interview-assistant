# -*- coding: utf-8 -*-
"""刷新 materials\\resume.txt —— 从你的简历 PDF 提取文本。

只管一件事：把简历 PDF 转成纯文本喂给模型。其他素材（01-*.md 那些）是手写的，
不经这里，直接编辑文件即可。

用法：
    .venv\\Scripts\\python materials_build.py "路径\\到\\你的简历.pdf"
    .venv\\Scripts\\python materials_build.py        # 简历 PDF 已放在 materials\\ 里时

旧版 resume.txt 会自动备份到 materials\\processed\\。
"""
import os
import shutil
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
MATERIALS = os.path.join(BASE, "materials")
PROCESSED = os.path.join(MATERIALS, "processed")
DST = os.path.join(MATERIALS, "resume.txt")


def pick_source():
    """找简历 PDF：命令行参数优先；没给就去 materials\\ 里找（只能有一个）。"""
    if len(sys.argv) > 1:
        return sys.argv[1]
    if os.path.isdir(MATERIALS):
        pdfs = [os.path.join(MATERIALS, n) for n in sorted(os.listdir(MATERIALS))
                if n.lower().endswith(".pdf")]
        if len(pdfs) == 1:
            return pdfs[0]
        if len(pdfs) > 1:
            print("[素材] materials\\ 下有多个 PDF，请指定用哪个：")
            for p in pdfs:
                print(f"    python materials_build.py \"{p}\"")
            return None
    return None


def main():
    src = pick_source()
    if not src:
        print("用法：python materials_build.py <你的简历.pdf>")
        print("  或者把简历 PDF 放进 materials\\ 目录（只放一个），再不带参数运行。")
        return 1
    if not os.path.isfile(src):
        print(f"[素材] 找不到简历文件：{src}")
        return 1

    os.makedirs(PROCESSED, exist_ok=True)
    if os.path.isfile(DST):
        bak = os.path.join(PROCESSED, f"resume_旧_{time.strftime('%Y%m%d_%H%M%S')}.txt")
        shutil.copyfile(DST, bak)
        print(f"[素材] 旧简历已备份 → {bak}")

    try:
        import pymupdf
    except ImportError:
        print("[素材] 缺 pymupdf。先跑：.venv\\Scripts\\pip install pymupdf")
        return 1

    doc = pymupdf.open(src)
    text = "\n".join(page.get_text() for page in doc).strip()
    if not text:
        print(f"[素材] {src} 里没提取到文字 —— 可能是扫描件，需要 OCR。")
        return 1

    with open(DST, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"[素材] 简历已刷新 → {DST}（{len(text)} 字，{len(doc)} 页）")
    print(f"[素材] 来源：{src}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
