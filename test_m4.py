# -*- coding: utf-8 -*-
"""M4 验证：启动窗口，模拟流式追加，验证置顶 / 拖动 / 透明度 / 不抢焦点。

用法：.venv\\Scripts\\python test_m4.py
窗口启动后：
  1. 拖动顶部标题条 = 移动窗口
  2. 底部滑块 = 调透明度
  3. 点 ✕ = 隐藏
  4. 开腾讯会议测：点窗口 / 拖窗口 / 拉透明度，会议焦点始终不丢；任务栏无图标
"""
import sys

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from ui import AnswerWindow

FAKE = (
    "这是模拟的参考答案。真实答案在 M5 接入后由 DeepSeek 流式吐出。\n\n"
    "面试官问完约 3 秒，这里会开始逐字滚动出完整答案，你能照着说。"
)


def main():
    app = QApplication(sys.argv)
    win = AnswerWindow()
    win.show()

    # 模拟流式：每 60ms 追加一小段，验证 append_chunk 的滚动效果
    chunks = [FAKE[i:i + 6] for i in range(0, len(FAKE), 6)]
    idx = 0

    def tick():
        nonlocal idx
        if idx < len(chunks):
            win.append_chunk(chunks[idx])
            idx += 1
        else:
            timer.stop()

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(60)

    print("[M4] 窗口已启动。", flush=True)
    print("[M4] 拖动标题条 = 移动；底部滑块 = 透明度；✕ = 隐藏。", flush=True)
    print("[M4] 开腾讯会议实测：点/拖/拉，会议焦点不丢，任务栏无图标。", flush=True)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
