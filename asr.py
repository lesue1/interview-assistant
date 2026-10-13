# -*- coding: utf-8 -*-
"""faster-whisper 转写。单实例，独立线程串行消费队列（面试一问一答，几乎不重叠）。"""
import os
import sys
import threading
import queue

# 保持 os.add_dll_directory() 返回的句柄存活。这些句柄一旦被垃圾回收，
# 它添加的 DLL 搜索目录就会立即失效（Python 3.8+ 的行为）。
_DLL_DIRS = []

if sys.platform == "win32":
    # ctranslate2 走 CUDA 需要 cuBLAS/cuDNN/cuRT 等 DLL，但它们不随 ctranslate2 打包。
    # nvidia 官方 pip 包（nvidia-cublas-cu12 / nvidia-cudnn-cu12 / nvidia-cuda-runtime-cu12 …）
    # 把 DLL 装在 site-packages\nvidia\<包>\bin 下。这里遍历所有 nvidia 子包的 bin 目录，
    # 全部加进 DLL 搜索路径（依赖链如 cublas→cudart 才能找到）。
    for _p in sys.path:
        if not _p:
            continue
        _nv = os.path.join(_p, "nvidia")
        if not os.path.isdir(_nv):
            continue
        for _sub in os.listdir(_nv):
            _bin = os.path.join(_nv, _sub, "bin")
            if os.path.isdir(_bin):
                try:
                    _DLL_DIRS.append(os.add_dll_directory(_bin))
                except OSError:
                    pass

SAMPLE_RATE = 16000

# Whisper 在静音段会"脑补"训练数据里的高频片尾字幕（"请点赞订阅""字幕志愿者"等）。
# 命中这些词就判幻觉丢弃，避免污染对话记录。见 faster-whisper #465 等已知 issue。
_HALLUCINATION = (
    "请订阅", "点赞", "订阅", "转发", "打赏",
    "感谢观看", "谢谢观看", "字幕志愿者", "字幕组",
    # 2026-08-27 真实面试新冒出的静音幻觉（视频片尾/水印语，两个通道都出现）
    "优优独播剧场", "YoYo Television Series Exclusive",
    "谢谢大家", "多谢观看", "我们下期再见", "下期再见",
)


class ASRWorker:
    def __init__(self, model_size="large-v3-turbo", device="cuda",
                 compute_type="int8_float16", on_text=None, download_root=None):
        from faster_whisper import WhisperModel
        self.q = queue.Queue()
        self.on_text = on_text
        self._stop = threading.Event()
        # 转写缓存：(speaker, 采样点) -> 文本。同一段会先以 partial、再以 final 各 submit
        # 一次（音频完全相同），这里去重，避免 partial/final 重复转写浪费 GPU。
        self._cache = {}
        try:
            self.model = WhisperModel(
                model_size, device=device, compute_type=compute_type,
                download_root=download_root,
            )
            self.device_used = device
        except Exception as e:
            print(f"[asr] CUDA 初始化失败({e})，回退 CPU int8", flush=True)
            self.model = WhisperModel(
                model_size, device="cpu", compute_type="int8",
                download_root=download_root,
            )
            self.device_used = "cpu"
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self.thread.start()

    def submit(self, speaker, audio, t_start, kind="final"):
        self.q.put((speaker, audio, t_start, kind))

    def _loop(self):
        while not self._stop.is_set():
            try:
                speaker, audio, t_start, kind = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            # 10ms 容差：partial/final 对同一段 VAD 起点可能有 1-2 采样点抖动，
            # 精确到采样点会导致缓存 miss、重复转写。段间隔 ≥600ms，10ms 不会误合并。
            key = (speaker, round(t_start * 100))
            if key in self._cache:
                text = self._cache[key]
            else:
                try:
                    text = self._transcribe(audio)
                except Exception as e:
                    # 单段转写失败（CUDA 被占/显存不足/输入异常）跳过该段，别让 ASR 线程死掉
                    print(f"[asr] 转写失败({e})，跳过该段", flush=True)
                    text = ""
                if self._is_hallucination(text):
                    text = ""   # 幻觉段丢弃，存空串让 partial/final 都不回调
                self._cache[key] = text
            if text and self.on_text:
                self.on_text(speaker, text, t_start, kind)

    def _transcribe(self, audio):
        segments, info = self.model.transcribe(
            audio,
            language="zh",
            beam_size=5,
            vad_filter=False,  # 已自己做过 VAD 分句，这里关掉避免二次切
            initial_prompt="以下是普通话的面试对话。",
            condition_on_previous_text=False,  # 不把上一段输出当上文，打断静音复读机
            repetition_penalty=1.15,           # 惩罚重复 token（需 beam_size>1，=5 已满足）
        )
        parts = [s.text.strip() for s in segments]
        return "".join(parts).strip()

    @staticmethod
    def _is_hallucination(text):
        """静音幻觉兜底：命中片尾字幕黑名单就判幻觉，整段丢弃。"""
        return any(p in text for p in _HALLUCINATION)

    def stop(self):
        self._stop.set()
