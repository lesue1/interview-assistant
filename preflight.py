# -*- coding: utf-8 -*-
"""启动自检：把"缺什么、怎么补"一次性用大白话列出来。

给第一次用的人看的 —— 不要让朋友对着一堆报错发懵。
"""
import os

BASE = os.path.dirname(os.path.abspath(__file__))
MATERIALS = os.path.join(BASE, "materials")
ENV = os.path.join(BASE, ".env")

# 这些只是给人看的提示，不影响程序本身
LEVEL_ERROR = "error"   # 跑不起来
LEVEL_WARN = "warn"     # 能跑，但效果会打折
LEVEL_INFO = "info"     # 只是告诉你一声


def check():
    """返回 [{"level", "title", "fix"}]。列表顺序就是显示顺序。"""
    out = []
    out.extend(_check_key())
    out.extend(_check_materials())
    out.extend(_check_audio())
    return out


def _check_key():
    if not os.path.isfile(ENV):
        return [{"level": LEVEL_ERROR,
                 "title": "还没建 .env 文件",
                 "fix": "把项目里的 .env.example 复制一份、改名成 .env，"
                        "再把里面的 DEEPSEEK_API_KEY 换成你自己的 key"}]

    try:
        from dotenv import load_dotenv
        load_dotenv(ENV, override=True)
    except Exception:
        pass

    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not key:
        return [{"level": LEVEL_ERROR,
                 "title": ".env 里没有 DEEPSEEK_API_KEY",
                 "fix": "打开 .env，加一行：DEEPSEEK_API_KEY=sk-你的key"}]
    if key == "your_deepseek_api_key_here" or key.startswith("sk-你的"):
        return [{"level": LEVEL_ERROR,
                 "title": "API Key 还是占位符",
                 "fix": "打开 .env，把 DEEPSEEK_API_KEY 换成真 key"
                        "（去 platform.deepseek.com 拿）"}]
    return []


def _check_materials():
    """素材是这工具的价值来源。没有素材不是跑不起来，但答案会退化成通用模板。"""
    has_resume, n_md = False, 0
    if os.path.isdir(MATERIALS):
        for name in os.listdir(MATERIALS):
            p = os.path.join(MATERIALS, name)
            if not os.path.isfile(p):
                continue
            if name.lower().endswith(".md"):
                n_md += 1
            if name == "resume.txt" and os.path.getsize(p) > 200:
                has_resume = True

    if not has_resume and n_md == 0:
        return [{"level": LEVEL_WARN,
                 "title": "还没有任何素材",
                 "fix": "点下面的「从简历 PDF 导入」选一份简历就能开始用了。"
                        "想答得更深，再往 materials\\ 里补几段经历细节"
                        "（照着 materials.example\\ 的样子写）"}]
    if not has_resume:
        return [{"level": LEVEL_WARN,
                 "title": "没有简历素材",
                 "fix": "建议点「从简历 PDF 导入」补一份 —— "
                        "光有经历文件也够用，但简历能补上时间线和基本面"}]
    return []


def _check_audio():
    """面试官的声音从哪个设备出来、程序跟的是哪个 —— 这是最容易翻车的一环。"""
    try:
        import pyaudiowpatch as paw
    except ImportError:
        return [{"level": LEVEL_ERROR,
                 "title": "缺 PyAudioWPatch",
                 "fix": "先跑一次 setup.bat 装依赖"}]

    p = None
    try:
        p = paw.PyAudio()
        dev = p.get_default_wasapi_loopback()
        return [{"level": LEVEL_INFO,
                 "title": f"面试官的声音会从「{dev['name']}」抓",
                 "fix": "如果面试用的不是这个设备，先把耳机设成 Windows 默认播放设备，"
                        "再点开始（程序中途也会自己跟上）"}]
    except Exception as e:
        # 用 warn 不用 error：自检这一次探不到不代表真跑不起来，
        # 不该因此把「开始面试」按钮锁死。真正的失败由采集线程自己报。
        return [{"level": LEVEL_WARN,
                 "title": "没探到系统声音设备",
                 "fix": f"{e}\n建议先跑一次：.venv\\Scripts\\python _audio_devices.py test"}]
    finally:
        try:
            if p:
                p.terminate()
        except Exception:
            pass
