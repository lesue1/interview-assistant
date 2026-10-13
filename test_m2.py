# -*- coding: utf-8 -*-
"""M2 验证：双通道（loopback 面试官 + 麦克风我）→ ASR → 打印 + sessions 记录。"""
import os
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import threading
import time
from capture import LoopbackCapture, MicCapture, UtteranceSegmenter, SAMPLE_RATE
from asr import ASRWorker
from transcript import TranscriptStore

BASE = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(BASE, "models")
SESSIONS_DIR = os.path.join(BASE, "sessions")

SPEAKER_LOOPBACK = "面试官"
SPEAKER_MIC = "我"


def channel_loop(cap, seg, speaker, asr, stop):
    """单通道循环：采集 → VAD 分句 → 提交 ASR。"""
    while not stop.is_set():
        block = cap.read_block()
        seg.push(block)
        for t_start, utt in seg.pop_finalized():
            if len(utt) < int(SAMPLE_RATE * 0.3):  # <300ms 丢弃
                continue
            asr.submit(speaker, utt, t_start)
        time.sleep(0.005)


def main():
    loop_cap = LoopbackCapture()
    mic_cap = MicCapture()
    print(f"[M2] loopback: {loop_cap.dev['name']} (idx {loop_cap.dev['index']})", flush=True)
    print(f"[M2] 麦克风: {mic_cap.dev['name']} (idx {mic_cap.dev['index']})", flush=True)

    store = TranscriptStore(sessions_dir=SESSIONS_DIR)

    def on_text(speaker, text, t_start, kind="final"):
        store.add(speaker, text, t_start)
        print(f"[{speaker}] {text}", flush=True)

    asr = ASRWorker(on_text=on_text, download_root=MODEL_DIR)
    asr.start()
    print(f"[M2] ASR 设备: {asr.device_used}，双通道开始听。", flush=True)
    print("[M2] 对麦说话 = [我]，电脑放录音 = [面试官]。Ctrl+C 退出。", flush=True)

    stop = threading.Event()
    t1 = threading.Thread(target=channel_loop,
                          args=(loop_cap, UtteranceSegmenter(), SPEAKER_LOOPBACK, asr, stop), daemon=True)
    t2 = threading.Thread(target=channel_loop,
                          args=(mic_cap, UtteranceSegmenter(), SPEAKER_MIC, asr, stop), daemon=True)
    t1.start()
    t2.start()

    try:
        while not stop.is_set():
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        asr.stop()
        store.close()


if __name__ == "__main__":
    main()
