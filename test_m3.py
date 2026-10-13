# -*- coding: utf-8 -*-
"""M3 验证：不接音频，手动构造对话 → DeepSeek 生成参考答案，验证引用真实素材 + 追问接得住。

用法：.venv\\Scripts\\python test_m3.py
前提：.env 里已填 DEEPSEEK_API_KEY（本脚本不碰 key，由 llm.py 读）。
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from llm import AnswerGenerator

# 第一问：直接问知识库里有明确答案的（两阶段筛选 + 阈值）
Q1 = "你简历上写的两阶段筛选，第二阶段阈值怎么定的？"
# 追问：接上一轮，考"凭什么这么定"，验证能接住上下文
Q2 = "那这个'命中率低'的线是拍脑袋定的，还是有依据？"


def main():
    gen = AnswerGenerator()
    print(f"[M3] system prompt 长度：{len(gen.system_prompt)} 字符", flush=True)
    print(f"[M3] 素材是否已加载：见下方答案是否引用真实内容。\n", flush=True)

    def on_chunk(delta):
        print(delta, end="", flush=True)

    # —— 第一问 ——
    print(f"========== 第一问 ==========", flush=True)
    print(f"[面试官] {Q1}", flush=True)
    print(f"[参考] ", end="", flush=True)
    history1 = f"[面试官] {Q1}"
    ans1 = gen.generate(history1, on_chunk)
    print("\n", flush=True)

    # —— 追问（接住上一轮：把答案 A 作为 [我] 的回话） ——
    print(f"\n========== 追问 ==========", flush=True)
    print(f"[面试官] {Q2}", flush=True)
    print(f"[参考] ", end="", flush=True)
    history2 = f"{history1}\n[我] {ans1}\n[面试官] {Q2}"
    gen.generate(history2, on_chunk)
    print("\n", flush=True)


if __name__ == "__main__":
    main()
