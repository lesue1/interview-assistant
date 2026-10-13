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


def extract_to_materials(src):
    """把简历 PDF 提取成 materials\\resume.txt。

    返回 (成功?, 给人看的一句话)。设置窗的「导入简历」按钮直接调这个函数，
    所以这里不 print，把结果交回调用方去显示。
    """
    if not src or not os.path.isfile(src):
        return False, f"找不到文件：{src}"

    try:
        import pymupdf
    except ImportError:
        return False, "缺 pymupdf —— 先跑一次 setup.bat 装依赖"

    try:
        doc = pymupdf.open(src)
        text = "\n".join(page.get_text() for page in doc).strip()
        pages = len(doc)
    except Exception as e:
        return False, f"打不开这个 PDF：{e}"

    if not text:
        return False, "这个 PDF 里没提取到文字 —— 可能是扫描件，得先 OCR 成文字版"

    os.makedirs(PROCESSED, exist_ok=True)
    if os.path.isfile(DST):
        bak = os.path.join(PROCESSED, f"resume_旧_{time.strftime('%Y%m%d_%H%M%S')}.txt")
        try:
            shutil.copyfile(DST, bak)
        except OSError:
            pass

    with open(DST, "w", encoding="utf-8") as f:
        f.write(text)
    return True, f"简历已导入（{pages} 页 / {len(text)} 字）"


def main():
    src = pick_source()
    if not src:
        print("用法：python materials_build.py <你的简历.pdf>")
        print("  或者把简历 PDF 放进 materials\\ 目录（只放一个），再不带参数运行。")
        return 1
    ok, msg = extract_to_materials(src)
    print(f"[素材] {msg}")
    if ok:
        print(f"[素材] 来源：{src}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
