# -*- coding: utf-8 -*-
"""双通道音频采集 + VAD 分句。

M1 只做系统 loopback（面试官）通道；M2 再加麦克风（自己）通道。
设计原则：采集线程只负责把原始音频变成"已结束的语音段"，不做任何重活。
"""
import time

import numpy as np
import pyaudiowpatch as paw
from faster_whisper.vad import get_speech_timestamps, VadOptions

SAMPLE_RATE = 16000


def read_raw_block(stream, frames, timeout=0.6):
    """轮询读一块原始音频。超时没数据返回 None。

    **不要直接用 stream.read()**：WASAPI 的 loopback 在设备空闲（没有任何程序在放声音）
    时根本不送数据，阻塞式 read() 会**永久卡住**，整条采集线程就死了 ——
    这正是"面试官通道一条都没录到"的元凶。所以一律先问 get_read_available()。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stream.get_read_available() >= frames:
            return stream.read(frames, exception_on_overflow=False)
        time.sleep(0.01)
    return None


def resample(audio: np.ndarray, src_rate: int, dst_rate: int = SAMPLE_RATE) -> np.ndarray:
    """重采样到 16k。整数倍用块平均（带轻抗混叠），非整数倍用线性插值。"""
    audio = np.asarray(audio, dtype=np.float32)
    if src_rate == dst_rate:
        return audio
    ratio = src_rate / dst_rate
    if abs(ratio - round(ratio)) < 1e-6:
        n = int(round(ratio))
        trimmed = audio[: len(audio) // n * n]
        return trimmed.reshape(-1, n).mean(axis=1).astype(np.float32)
    n_out = int(len(audio) * dst_rate / src_rate)
    x_old = np.linspace(0, 1, len(audio))
    x_new = np.linspace(0, 1, n_out)
    return np.interp(x_new, x_old, audio).astype(np.float32)


class LoopbackCapture:
    """系统声音采集（面试官通道）。打开默认 WASAPI loopback，持续产出 16k mono float32 块。

    默认跟随"当前默认输出设备"。但**只在启动那一刻认一次是不够的** ——
    很多人是先开程序、后戴耳机，Windows 这时才把默认输出切到耳机，
    结果程序一直在录那个已经没人说话的外放。所以额外提供 follow_default()，
    由采集线程定期调用，发现默认设备换了就重开流。
    """

    def __init__(self, device_index=None):
        self.p = paw.PyAudio()
        self._fixed_index = device_index   # 非 None = 手动指定，不跟随默认
        self._open()

    def _open(self):
        if self._fixed_index is None:
            dev = self.p.get_default_wasapi_loopback()
        else:
            dev = self.p.get_device_info_by_index(self._fixed_index)
        self.dev = dev
        self.src_rate = int(dev["defaultSampleRate"])
        self.channels = int(dev["maxInputChannels"])
        self.block_frames = int(self.src_rate * 0.1)  # 100ms 一块
        self.stream = self.p.open(
            format=paw.paFloat32,
            channels=self.channels,
            rate=self.src_rate,
            input=True,
            input_device_index=dev["index"],
            frames_per_buffer=self.block_frames,
        )

    def follow_default(self):
        """默认输出设备换了吗？换了就重开采集。

        返回 "旧设备名 → 新设备名" 字符串；没换、取的失败、或手动指定了设备时返回 None。
        """
        if self._fixed_index is not None:
            return None
        try:
            want = self.p.get_default_wasapi_loopback()
        except Exception:
            return None
        if want["index"] == self.dev["index"]:
            return None
        old = self.dev["name"]
        try:
            self.stream.stop_stream()
            self.stream.close()
        except Exception:
            pass
        try:
            self._open()
        except Exception as e:
            return f"{old} → 切换失败({e})"
        return f"{old} → {self.dev['name']}"

    def read_block(self) -> np.ndarray:
        """读一块音频，降混成 mono 再重采样到 16k。

        设备空闲拿不到数据时返回等长静音（而不是卡死），让上层循环继续转 ——
        面试官还没开口的那几十秒就靠这个撑过去。
        """
        data = read_raw_block(self.stream, self.block_frames)
        if data is None:
            return np.zeros(int(SAMPLE_RATE * 0.1), dtype=np.float32)
        arr = np.frombuffer(data, dtype=np.float32)
        if self.channels > 1:
            arr = arr.reshape(-1, self.channels).mean(axis=1)
        return resample(arr, self.src_rate, SAMPLE_RATE)

    def close(self):
        try:
            self.stream.stop_stream()
            self.stream.close()
        finally:
            self.p.terminate()


def list_loopback_devices(p):
    """列出所有可录的系统声音设备。启动时打一遍，出问题能一眼看出跟错了哪个。"""
    lines = []
    try:
        d = p.get_default_output_device_info()
        lines.append(f"Windows 默认输出设备: {d['name']}")
    except Exception:
        lines.append("Windows 默认输出设备: （取不到）")
    try:
        for dev in p.get_loopback_device_info_generator():
            lines.append(f"  [{dev['index']:>3}] {dev['name']}")
    except Exception as e:
        lines.append(f"  （枚举 loopback 失败: {e}）")
    return lines


class MicCapture:
    """麦克风采集（自己通道）。打开默认输入设备，持续产出 16k mono float32 块。

    面试戴 2.4G 耳机时，把耳机设为默认输入，这里会自动跟到耳机的麦克风。
    """

    def __init__(self, device_index=None):
        self.p = paw.PyAudio()
        if device_index is None:
            dev = self.p.get_default_input_device_info()
        else:
            dev = self.p.get_device_info_by_index(device_index)
        self.dev = dev
        self.src_rate = int(dev["defaultSampleRate"])
        self.channels = int(dev["maxInputChannels"])
        self.block_frames = int(self.src_rate * 0.1)  # 100ms 一块
        self.stream = self.p.open(
            format=paw.paFloat32,
            channels=self.channels,
            rate=self.src_rate,
            input=True,
            input_device_index=dev["index"],
            frames_per_buffer=self.block_frames,
        )

    def read_block(self) -> np.ndarray:
        """读一块音频，降混成 mono 再重采样到 16k。拿不到数据时给静音，不卡线程。"""
        data = read_raw_block(self.stream, self.block_frames)
        if data is None:
            return np.zeros(int(SAMPLE_RATE * 0.1), dtype=np.float32)
        arr = np.frombuffer(data, dtype=np.float32)
        if self.channels > 1:
            arr = arr.reshape(-1, self.channels).mean(axis=1)
        return resample(arr, self.src_rate, SAMPLE_RATE)

    def close(self):
        try:
            self.stream.stop_stream()
            self.stream.close()
        finally:
            self.p.terminate()


class UtteranceSegmenter:
    """维护滚动音频缓冲，用 silero VAD 找出"已结束"的语音段并吐出。

    "已结束" = 段尾之后还有 finalize_ms 连续静音，说明面试官这句话说完了。
    段没结束（还在说）的继续留在缓冲里，等下次再判。
    """

    def __init__(self, min_silence_ms=600, finalize_ms=1200, threshold=0.5, base_time=None):
        self.min_silence_ms = min_silence_ms
        self.min_silence_samples = int(min_silence_ms / 1000 * SAMPLE_RATE)
        self.finalize_samples = int(finalize_ms / 1000 * SAMPLE_RATE)
        self.threshold = threshold
        # 双通道共享同一个 base_time，t_start 用绝对秒，面试官/我的对话才能按同一时间轴排序。
        # 单通道/测试不传时回退到构造时刻，行为不变。
        self.base_time = base_time if base_time is not None else time.time()
        self.buffer = np.zeros(0, dtype=np.float32)
        self.offset = 0          # buffer[0] 对应的绝对采样点
        self.last_emit_end = 0   # 上一段已吐出段(1200ms 确认说完)的绝对结束采样点
        self.last_partial_end = 0  # 上一段已闭合段(600ms)的绝对结束采样点

    def push(self, samples: np.ndarray):
        self.buffer = np.concatenate([self.buffer, samples])
        max_len = SAMPLE_RATE * 30
        if len(self.buffer) > max_len:
            drop = len(self.buffer) - SAMPLE_RATE * 15
            self.buffer = self.buffer[drop:]
            self.offset += drop

    def _segments(self):
        """对当前 buffer 跑一次 VAD，返回语音段列表。"""
        opts = VadOptions(
            threshold=self.threshold,
            min_silence_duration_ms=self.min_silence_ms,
            min_speech_duration_ms=250,
            speech_pad_ms=200,
        )
        return get_speech_timestamps(self.buffer, opts, sampling_rate=SAMPLE_RATE)

    def pop_partial(self):
        """返回 600ms 静音闭合、内容已确定的段（可能后面还有话），用于抢跑转写。

        只要段尾后静音 ≥ min_silence_ms(600ms) 就吐，不必等 finalize_ms(1200ms)。
        靠 last_partial_end 游标保证每段只吐一次。和 pop_finalized 吐出的同一段，
        音频与 t_start 完全一致（asr 层按 (speaker, t_start) 缓存去重，避免重转）。
        """
        if len(self.buffer) < int(SAMPLE_RATE * 0.5):
            return []
        out = []
        for seg in self._segments():
            start_local, end_local = seg["start"], seg["end"]
            # 段尾后静音不足 600ms → 还在说，没闭合
            if len(self.buffer) - end_local < self.min_silence_samples:
                continue
            abs_start = self.offset + start_local
            abs_end = self.offset + end_local
            if abs_end > self.last_partial_end:
                # 用段真实起点（不再 max 到游标）：VAD 参数保证相邻段间隔 ≥200ms 不重叠，
                # 而 max 会让同一段在 partial/final 两处 t_start 不一致 → asr 缓存 miss 重复转写
                out.append((abs_start, abs_end))
        if out:
            self.last_partial_end = out[-1][1]
        return [(self.base_time + s / SAMPLE_RATE, self.buffer[s - self.offset: e - self.offset]) for s, e in out]

    def pop_finalized(self):
        """返回已结束语音段（1200ms 静音确认说完，16k float32 数组列表）。"""
        if len(self.buffer) < int(SAMPLE_RATE * 0.5):
            return []
        out = []
        for seg in self._segments():
            start_local, end_local = seg["start"], seg["end"]
            # 段尾之后还要有 finalize_samples 静音，才算"这句说完了"
            if len(self.buffer) - end_local < self.finalize_samples:
                continue
            abs_start = self.offset + start_local
            abs_end = self.offset + end_local
            # VAD 在滚动 buffer 上重跑，同一段的边界会抖动（end 差几十 ms），
            # 用"end > 游标"判重会漏——抖出更大的 end 就重复 emit，导致同一句被转写
            # 两次、触发两次生成。改判重叠：新段起点落在已 emit 范围内就是同一段。
            # 相邻合法段间隔 ≥600ms(min_silence)，远大于 200ms 前导 pad，不会误合并。
            if abs_start < self.last_emit_end:
                continue
            out.append((abs_start, abs_end))
        if out:
            self.last_emit_end = out[-1][1]
        # 返回 (绝对起始秒, 音频段)。两通道共享 base_time，绝对秒可直接排序对齐。
        return [(self.base_time + s / SAMPLE_RATE, self.buffer[s - self.offset: e - self.offset]) for s, e in out]
