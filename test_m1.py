# -*- coding: utf-8 -*-
"""M1 验证：loopback → VAD → ASR → 控制台打印 [面试官] …"""
import os
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import time
from capture import LoopbackCapture, UtteranceSegmenter, SAMPLE_RATE
from asr import ASRWorker

BASE = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(BASE, "models")


def main():
    cap = LoopbackCapture()
    seg = UtteranceSegmenter()
    print(f"[M1] loopback 设备: {cap.dev['name']} (idx {cap.dev['index']}, "
          f"{cap.src_rate}Hz, {cap.channels}ch)", flush=True)

    def on_text(speaker, text, t_start, kind="final"):
        print(f"[{speaker}] {text}", flush=True)

    asr = ASRWorker(on_text=on_text, download_root=MODEL_DIR)
    asr.start()
    print(f"[M1] ASR 设备: {asr.device_used}，开始听。", flush=True)
    print("[M1] 请播放一段中文真人语音（或让朋友说话）。Ctrl+C 退出。", flush=True)

    try:
        while True:
            block = cap.read_block()
            seg.push(block)
            for t_start, utt in seg.pop_finalized():
                if len(utt) < int(SAMPLE_RATE * 0.3):  # <300ms 丢弃（语气词/噪声）
                    continue
                asr.submit("面试官", utt, t_start)
            time.sleep(0.005)
    except KeyboardInterrupt:
        pass
    finally:
        asr.stop()
        cap.close()


if __name__ == "__main__":
    main()
