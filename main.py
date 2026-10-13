# -*- coding: utf-8 -*-
"""入口：配置 + 启动采集/ASR/LLM/热键线程 + Qt 窗口。

线程模型（单进程多线程，UI 只在主线程碰）：
  Thread-A  系统 loopback（面试官）→ VAD 分句 → asr.submit
  Thread-B  麦克风（自己）      → VAD 分句 → asr.submit
  Thread-C  ASR 串行消费（faster-whisper 单实例）
  Thread-D  LLM 生成（DeepSeek 流式）→ Bridge 信号 → 主线程 UI
  Thread-E  全局热键（pynput）→ 只发信号 / spawn 生成
  主线程    Qt 事件循环
"""
import json
import os
import re
import sys
import threading
import time

import numpy as np

# 模型下载走国内镜像（模型已缓存时无关，仅兜底）；禁用 HF 新版 xet 传输
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from capture import LoopbackCapture, MicCapture, UtteranceSegmenter, list_loopback_devices
from asr import ASRWorker
from transcript import TranscriptStore
from llm import AnswerGenerator
from ui import AnswerWindow, SetupWindow

BASE = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(BASE, "models")
SESSIONS_DIR = os.path.join(BASE, "sessions")

# 抓错设备时手改这里（None = 自动跟随系统默认输入/输出）
LOOPBACK_DEVICE_INDEX = None
MIC_DEVICE_INDEX = None

# VAD 与触发参数
VAD_THRESHOLD = 0.5
MIN_SILENCE_MS = 600       # 短停顿不切句，语气词并入本句
FINALIZE_MS = 1200         # 段尾后静音这么久才判"这句说完了"（触发的主闸门）
TRIGGER_MIN_CHARS = 5      # 转写少于这个字数不触发（过滤"嗯/那个"）

# "我正在说话"的判定：麦克风块能量超过阈值，之后 MIC_SPEAKING_HOLD 秒内都算我在说。
# 我说话时面试官那边的动静一律不打扰（见 _on_text），免得他一声"嗯"就闪状态、花调用。
# 阈值按 16k float32 单声道估的，换麦克风/改增益后如果判断不准，调这个数。
MIC_VOICE_RMS = 0.008
MIC_SPEAKING_HOLD = 1.5

# 面试官通道（系统声音）的健康检查。
# 最常见的翻车方式：先启动程序、后戴耳机 —— 程序认下了当时默认的"电脑外放"，
# 随后 Windows 把默认输出切到耳机，面试声音全走耳机，程序对着空气录完整场。
DEVICE_CHECK_S = 3.0       # 多久看一次默认输出设备换没换
LOOP_VOICE_RMS = 0.003     # loopback 块能量超过这个就算"听到系统声音了"
SILENT_WARN_S = 30.0       # 开场这么久还没听到系统声音、而你自己已经在说话 → 报警

# 我在说话时，只有"看着像问句"的面试官发言才放行（否则很可能是他在应和我）。
# 可能是他插话打断我问了真问题，所以给个长度兜底：长句子也放行，别把问题漏掉。
_QUESTION_HINTS = (
    "什么", "怎么", "为什么", "为啥", "哪", "吗", "呢", "几", "多少", "如何",
    "是不是", "有没有", "能不能", "可不可以", "介绍", "讲讲", "说说", "谈谈",
    "?", "？",
)
_SILENT_MIN_CHARS = 14     # 我在说话时，超过这个长度的面试官发言仍按真问题处理

# 本地粗筛：明显不是问题的（纯应答/语气词）在本地就拦掉，不浪费一次 LLM 调用。
# 刻意保守 —— 只砍 100% 不可能是问题的。"为什么""那后来呢"这类短追问必须放行。
# 漏判（放给 LLM 判）只是多花一次调用；误砍真问题才是事故。
_PUNCT = "，。、？！,.?!~～…—－-\"'“”‘’()（）《》 \t\n\r"
_FILLER_CHARS = frozenset("嗯哦噢啊呃额唉哎哟呀啦呐")
_FILLER_WORDS = frozenset({
    "好的", "好", "ok", "okay", "对", "对的", "是", "是的", "行", "可以",
    "明白", "明白了", "知道", "了解", "收到", "我明白", "我知道", "好嘞",
    "好的好的", "好好好", "没事", "没有", "嗯嗯", "哦哦", "这样啊", "是吧",
})


def is_non_question(text):
    """本地判断"这句几乎不可能是问题"。True = 不用触发 LLM。"""
    core = "".join(ch for ch in text.strip() if ch not in _PUNCT).lower()
    if len(core) <= 2:                       # "嗯" "OK" "好的" "对"
        return True
    if core in _FILLER_WORDS:                # "明白了" "好的好的"
        return True
    # 剥掉纯语气字再看剩下什么："哦，明白。"→"明白"、"啊好的"→"好的"、"嗯嗯嗯"→""
    stripped = "".join(ch for ch in core if ch not in _FILLER_CHARS)
    if not stripped or stripped in _FILLER_WORDS:
        return True
    return False


# 自定义热键：手改 config.json 后重启生效
CONFIG_PATH = os.path.join(BASE, "config.json")

DEFAULT_CONFIG = {
    "_说明": (
        "自定义热键：改 hotkeys 里的值，改完重启程序生效。"
        "regenerate=换一版答案，angle=换个角度，hide=隐藏/恢复窗口，"
        "end=结束本场并保存记录，quit=退出程序。"
        "键名格式：F1 写成 <f1>，Ctrl+Alt+G 写成 <ctrl>+<alt>+g，Shift+F5 写成 <shift>+<f5>。"
    ),
    "hotkeys": {
        "regenerate": "<f1>",
        "angle": "<f2>",
        "hide": "<f3>",
        "end": "<f5>",
        "quit": "<f4>",
    },
}


def load_config():
    """读 config.json，没有就生成一份默认的；解析失败回退默认。返回 hotkeys dict。"""
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        print(f"[config] 已生成默认配置文件: {CONFIG_PATH}", flush=True)
        return dict(DEFAULT_CONFIG["hotkeys"])
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[config] 配置文件读失败({e})，用默认热键", flush=True)
        return dict(DEFAULT_CONFIG["hotkeys"])
    hotkeys = dict(DEFAULT_CONFIG["hotkeys"])
    hotkeys.update(cfg.get("hotkeys", {}))
    return hotkeys


def fmt_key(s):
    """'<f1>' -> 'F1'，'<ctrl>+<alt>+g' -> 'Ctrl+Alt+g'（仅用于窗口/控制台显示）。"""
    return (s.replace("<ctrl>", "Ctrl").replace("<alt>", "Alt")
             .replace("<shift>", "Shift").replace("<f", "F").replace(">", ""))


class _Cancelled(Exception):
    """旧生成被新生成顶替时，用它中断流式循环。"""


class Bridge(QObject):
    """worker 线程 → 主线程 UI 的信号桥。任何对控件的操作都经它转，避免跨线程碰 UI。"""
    sig_chunk = pyqtSignal(str)          # 流式答案片段
    sig_question = pyqtSignal(str, bool)  # (问题文本, 是否追问)；此刻才动答案区
    sig_status = pyqtSignal(str)     # 标题栏状态提示
    sig_warn = pyqtSignal(str)       # 通道异常告警（比状态提示显眼，要让人看见）
    sig_toggle = pyqtSignal()        # 隐藏/恢复窗口
    sig_end = pyqtSignal()           # 结束本场（F5 热键 → main 层生成记录）
    sig_quit = pyqtSignal()          # 退出程序


class _StreamParser:
    """解析 LLM 的结构化输出，决定"刷新窗口 / 追加追问 / 什么都不做"。

    模型要输出三种格式之一：
        [Q] <面试官问的新问题>\\n\\n<参考答案>
        [F] <面试官在追问什么>\\n\\n<只写补上的那部分>
        [SKIP]

    实际输出常带 markdown 装饰（**、#）或把标记写成 [FOLLOWUP]/[NEW]，这里做宽容解析；
    万一模型完全没守格式，就把原文当答案是输出，绝不把标记本身显示给用户。
    """

    _BUFFER, _SKIP, _QUESTION, _ANSWER = range(4)
    _DECOR = "*# \t\n\r-—>"
    # 标记容忍表：模型偶尔会写全称或同义词，别让它变成答案正文
    _TAG_SKIP = frozenset({"SKIP", "NONE", "NO", "PASS"})
    _TAG_NEW = frozenset({"Q", "NEW", "QUESTION", "ASK"})
    _TAG_FOLLOW = frozenset({"F", "FOLLOWUP", "FOLLOW", "FOLLOWUPQ", "FU"})

    def __init__(self, on_question, on_chunk):
        self._on_question = on_question   # on_question(问题文本, 是否追问)
        self._on_chunk = on_chunk
        self._state = self._BUFFER
        self._buf = ""            # BUFFER 态累积（判断开头的标记用）
        self._qbuf = ""           # QUESTION 态累积（问题行还没等到换行）
        self._q_follow = False    # 本行标记是 [F] 吗
        self._eat_blank = False   # 刚切到 ANSWER：先吃掉问题行与答案之间的空行

    @property
    def skipped(self):
        """模型判定"这句不是问题"。"""
        return self._state == self._SKIP

    def feed(self, delta):
        if self._state == self._SKIP:
            return                                    # 跳过：后面全是废话，丢掉
        if self._state == self._ANSWER:
            if self._eat_blank:
                # 分隔用的空行可能被拆成独立分片流过来，这里逐片吃掉，别让它进答案
                delta = delta.lstrip("\n")
                if not delta:
                    return
                self._eat_blank = False
            self._on_chunk(delta)
            return
        if self._state == self._QUESTION:
            self._qbuf += delta
            self._flush_question()
            return
        self._buf += delta
        self._parse_head()

    def finish(self):
        """流结束：问题行还没等到换行也要吐出去，别把识别结果丢了。"""
        if self._state == self._QUESTION:
            self._flush_question(force=True)

    def _parse_head(self):
        s = self._buf.lstrip(self._DECOR)
        if not s:
            return                                    # 全是装饰字符，继续等
        m = re.match(r"^\[([A-Za-z_]{1,12})\]", s)
        if m:
            tag = m.group(1).upper()
            rest = s[m.end():]
            if tag in self._TAG_SKIP:
                self._state = self._SKIP
                return
            if tag in self._TAG_NEW or tag in self._TAG_FOLLOW:
                self._state = self._QUESTION
                self._q_follow = tag in self._TAG_FOLLOW
                self._qbuf = rest
                self._flush_question()
                return
            # 认不出的标记 → 连同内容当答案吐出去，宁可格式丑也不吞内容
            self._state = self._ANSWER
            self._on_chunk(self._buf)
            return
        # 开头的标记还没凑完整（"["、"[S"、"[FO"…）→ 继续等，别急着当答案吐出去
        if re.match(r"^\[[A-Za-z_]{0,12}$", s):
            return
        # 模型没按格式输出：把已收到的原文当答案是输出
        self._state = self._ANSWER
        self._on_chunk(self._buf)

    def _flush_question(self, force=False):
        if "\n" in self._qbuf:
            line, _, rest = self._qbuf.partition("\n")
        elif force or len(self._qbuf) > 120:
            # 模型忘了换行，或问题长到不可能还是问题行 → 就地断开，别把答案也吞进问题行
            line, rest = self._qbuf, ""
        else:
            return
        question = line.strip().strip("*#").strip()
        if not question:
            # 标记后面空着：不刷新窗口，交给"没生成出内容"提示兜底，别把用户看的东西清掉
            return
        self._on_question(question, self._q_follow)
        self._state = self._ANSWER
        self._eat_blank = True
        self._qbuf = ""
        if rest:
            self.feed(rest)   # 复用 ANSWER 分支，空行剥离只写一处


class Controller:
    def __init__(self, bridge, win, job_name, jd, company=""):
        self.bridge = bridge
        self.win = win
        self.job_name = job_name
        self.jd = jd
        self.company = company
        self.store = TranscriptStore(SESSIONS_DIR, job_name=job_name, jd=jd)
        self.hotkeys = load_config()
        self._gen_lock = threading.Lock()
        self._gen_cancel = threading.Event()
        self._gen_thread = None
        self._pending = False        # 生成期间被 guard 挡下的新问题，等当前生成完补上
        self._stop = threading.Event()
        self._seen_texts = set()     # 已处理的 (speaker, 采样点)，避免 partial/final 重复 add
        self._last_mic_voice = 0.0   # 最后一次在麦克风里听到我说话的时刻（见 _im_speaking）
        self._t_start = time.time()  # 本场开始时刻，用于"面试官通道静音太久"的计时
        self._mic_heard = False      # 麦克风里听到过我说话
        self._loop_heard = False     # 面试官通道里听到过系统声音
        self._warned_silent = False  # 静音警告只报一次

    # —— 采集线程 ——

    @staticmethod
    def _new_segmenter(base_time):
        return UtteranceSegmenter(
            min_silence_ms=MIN_SILENCE_MS, finalize_ms=FINALIZE_MS,
            threshold=VAD_THRESHOLD, base_time=base_time)

    def _loopback_loop(self, cap, base_time):
        seg = self._new_segmenter(base_time)
        last_check = 0.0
        while not self._stop.is_set():
            try:
                block = cap.read_block()
            except Exception as e:
                print(f"[capture] loopback 读失败: {e}", flush=True)
                break

            # 定期看默认输出设备换没换 —— 先开程序后戴耳机的人全靠这一步救
            now = time.time()
            if now - last_check > DEVICE_CHECK_S:
                last_check = now
                changed = cap.follow_default()
                if changed:
                    print(f"[capture] 默认输出设备变了，已切换：{changed}", flush=True)
                    seg = self._new_segmenter(base_time)   # 丢掉旧设备的残留音频，别和新设备混在一起
                    self._loop_heard = False
                    self._t_start = now
                    self.bridge.sig_status.emit("已跟随到新的音频设备")

            if float(np.sqrt(np.mean(np.square(block)))) > LOOP_VOICE_RMS:
                self._loop_heard = True

            seg.push(block)
            for t_start, audio in seg.pop_finalized():
                # 面试官一句刚说完就先给个状态：转写+判定还要 2-3 秒，不然窗口像卡住
                self.bridge.sig_status.emit("面试官刚说完，识别中…")
                self.asr.submit("面试官", audio, t_start, kind="final")

            self._maybe_warn_silent()
        cap.close()

    def _maybe_warn_silent(self):
        """面试官通道一直没声音、但麦克风里有动静 → 多半是音频设备跟错了，赶紧说。

        只在自己确实说过话之后才报 —— 否则面试官还没开口就被误报。
        """
        if self._warned_silent or self._loop_heard or not self._mic_heard:
            return
        if time.time() - self._t_start < SILENT_WARN_S:
            return
        self._warned_silent = True
        print(f"[capture] 面试官通道 {SILENT_WARN_S:.0f} 秒内没听到任何系统声音，"
              f"但麦克风有动静 —— 可能跟错了音频设备", flush=True)
        self.bridge.sig_warn.emit(
            "面试官通道一直没听到声音，但你这边有说话。\n\n"
            "多半是音频设备跟错了 —— 声音从耳机出，程序却在录电脑外放。\n"
            "把耳机连好、确认它是 Windows 的默认播放设备，然后重开一次面试。")

    def _mic_loop(self, cap, base_time):
        seg = self._new_segmenter(base_time)
        while not self._stop.is_set():
            try:
                block = cap.read_block()
            except Exception as e:
                print(f"[capture] mic 读失败: {e}", flush=True)
                break
            seg.push(block)
            # 用块能量判断"我正在说话"。比跑一遍 VAD 便宜得多，每 100ms 一次开销可忽略。
            if float(np.sqrt(np.mean(np.square(block)))) > MIC_VOICE_RMS:
                self._last_mic_voice = time.time()
                self._mic_heard = True
            for t_start, audio in seg.pop_finalized():
                self.asr.submit("我", audio, t_start)
        cap.close()

    def _im_speaking(self):
        """我刚才（MIC_SPEAKING_HOLD 秒内）是不是在说话。"""
        return time.time() - self._last_mic_voice < MIC_SPEAKING_HOLD

    @staticmethod
    def _looks_like_question(text):
        """像不像真问句。只在我说话期间用来过滤面试官的应和声，标准放宽无所谓。"""
        return len(text) >= _SILENT_MIN_CHARS or any(h in text for h in _QUESTION_HINTS)

    # —— ASR 回调（ASR 线程） ——

    def _on_text(self, speaker, text, t_start, kind="final"):
        # 10ms 容差（和 asr 缓存 key 一致）：容忍 partial/final 对同一段的 VAD 起点抖动
        key = (speaker, round(t_start * 100))
        if key not in self._seen_texts:
            self._seen_texts.add(key)
            self.store.add(speaker, text, t_start)
            print(f"[{speaker}] {text}", flush=True)
        if speaker != "面试官":
            return
        speaking = self._im_speaking()
        if len(text) < TRIGGER_MIN_CHARS or is_non_question(text):
            # 寒暄/应答/语气词：本地就断定不是问题，一次 LLM 都不发，窗口完全不动。
            # 我正在说话时连状态都不显示 —— 你说你的，屏幕上别闪任何东西。
            if not speaking:
                self.bridge.sig_status.emit("这句不用答")
            return
        if speaking and not self._looks_like_question(text):
            # 我还在说话，他这句又不像问句 → 多半是在应和我，完全静默，不花钱不打扰
            return
        self._trigger_generate()

    # —— LLM 生成 ——

    def _spawn_gen(self, extra=""):
        """真正起一个生成线程：清 pending、取消旧的、用最新 transcript 生成。"""
        self._pending = False
        self._gen_cancel.set()
        cancel = threading.Event()
        self._gen_cancel = cancel
        self._gen_thread = threading.Thread(
            target=self._run_generate, args=(extra, cancel), daemon=True)
        self._gen_thread.start()

    def _trigger_generate(self, extra="", force=False):
        # 自动触发（extra 空且非 force）且正在生成 → 不打断，记 pending，等当前生成完补上；
        # 热键/修正（extra 非空或 force）允许打断重来
        if not extra and not force and self._gen_thread and self._gen_thread.is_alive():
            self._pending = True
            return
        if extra:
            self.bridge.sig_status.emit("重新生成…")
        self._spawn_gen(extra)

    def _run_generate(self, extra, cancel):
        try:
            with self._gen_lock:
                history = self.store.formatted()
                answer_parts = []

                def on_question(text, followup):
                    # 确认是问题 → 此刻才动窗口：新问题刷新，追问在下面接补充。
                    # 判 [SKIP] 时走不到这里，用户正在看的答案不会被抢走。
                    self.bridge.sig_status.emit("")
                    self.bridge.sig_question.emit(text, followup)

                def on_chunk(delta):
                    if cancel.is_set():
                        raise _Cancelled()
                    answer_parts.append(delta)
                    self.bridge.sig_chunk.emit(delta)

                parser = _StreamParser(on_question, on_chunk)
                try:
                    def on_delta(delta):
                        if cancel.is_set():
                            raise _Cancelled()
                        parser.feed(delta)
                    self.gen.generate(history, on_delta, extra)
                    parser.finish()
                except _Cancelled:
                    return
                except Exception as e:
                    # API 报错/超时/断网：不把生成线程打死，给窗口一个可见提示
                    print(f"[llm] 生成失败: {e}", flush=True)
                    self.bridge.sig_chunk.emit(f"\n（生成失败，可按 F1 重试：{e}）")
                    return

                if parser.skipped:
                    # 面试官这句不是问题（陈述/寒暄/玩笑）：不存答案，窗口保持原样
                    self.bridge.sig_status.emit("这句不用答")
                    return
                answer = "".join(answer_parts)
                if not answer.strip():
                    self.bridge.sig_status.emit("没生成出内容，按 F1 重试")
                    return
                self.store.add_answer(answer)
        finally:
            # 生成期间若有新问题被 guard 挡下（_pending=True），现在补生成，别丢
            if not self._stop.is_set() and self._gen_cancel is cancel and self._pending:
                self._spawn_gen()

    # —— 热键线程 ——

    def _hotkey_loop(self):
        from pynput import keyboard

        # 热键是"已经拍板这题要答案"的信号：不再让模型判断要不要回答，
        # 这样即使模型之前判错了 [SKIP]，按 F1 也能把答案抢回来。
        FORCE = ("已确认这句是问题，不要再判断要不要回答，直接输出 [Q] 或 [F] 加答案。")

        def on_regenerate():
            self._trigger_generate(f"换一版，别和上一版重复。{FORCE}")

        def on_angle():
            self._trigger_generate(f"换个角度再答一版，和上一版明显不同。{FORCE}")

        def on_hide():
            self.bridge.sig_toggle.emit()

        def on_end():
            self.bridge.sig_end.emit()

        def on_quit():
            self.bridge.sig_quit.emit()

        hk = self.hotkeys
        print(f"[hotkey] {fmt_key(hk['regenerate'])}换一版 / {fmt_key(hk['angle'])}换角度 / "
              f"{fmt_key(hk['hide'])}隐藏 / {fmt_key(hk['end'])}结束 / "
              f"{fmt_key(hk['quit'])}退出", flush=True)
        self._hk_listener = None
        try:
            with keyboard.GlobalHotKeys({
                hk["regenerate"]: on_regenerate,
                hk["angle"]: on_angle,
                hk["hide"]: on_hide,
                hk["end"]: on_end,
                hk["quit"]: on_quit,
            }) as h:
                self._hk_listener = h
                h.join()
        except RuntimeError:
            # 结束面试时 stop() 已停掉 listener，with 的 __exit__ 再 stop 会抛
            # RuntimeError（stop 重复调用），属正常退出，静默。
            pass
        except Exception as e:
            print(f"[hotkey] 热键注册失败: {e}", flush=True)

    # —— 启动 / 停止 ——

    def start(self):
        print("[asr] 加载 faster-whisper 模型...", flush=True)
        self.asr = ASRWorker(on_text=self._on_text, download_root=MODELS_DIR)
        print(f"[asr] 模型就绪（{self.asr.device_used}）", flush=True)
        self.asr.start()

        self.gen = AnswerGenerator(self.jd, self.company)
        print("[llm] 素材与 DeepSeek client 就绪", flush=True)

        # 两个 PyAudio 设备必须串行创建（并发 open 会段错误），放主线程像 M2 一样
        self._loop_cap = LoopbackCapture(LOOPBACK_DEVICE_INDEX)
        print(f"[capture] 面试官通道: {self._loop_cap.dev['name']}", flush=True)
        # 把所有可录的系统声音设备打出来 —— 万一跟错了，控制台里能直接看出是哪个
        for line in list_loopback_devices(self._loop_cap.p):
            print(f"[audio] {line}", flush=True)
        self._mic_cap = MicCapture(MIC_DEVICE_INDEX)
        print(f"[capture] 麦克风通道: {self._mic_cap.dev['name']}", flush=True)
        print("[capture] 提示：面试官的声音从哪个耳机/音箱出，程序就跟哪个设备。"
              "中途换耳机它会自己跟上（每 3 秒查一次）。", flush=True)

        # 两通道共享同一时间基准，t_start 用绝对秒对齐，消除各自从 0 计时导致的对话乱序
        t0 = time.time()
        self._t_start = t0
        threading.Thread(target=self._loopback_loop, args=(self._loop_cap, t0), daemon=True).start()
        threading.Thread(target=self._mic_loop, args=(self._mic_cap, t0), daemon=True).start()
        threading.Thread(target=self._hotkey_loop, daemon=True).start()

    def finish(self):
        """结束本场面试：写完整报告，停掉采集/ASR/热键。返回报告路径。"""
        path = self.store.write_report()
        self.stop()
        return path

    def stop(self):
        self._stop.set()
        self._gen_cancel.set()
        if getattr(self, "asr", None):
            self.asr.stop()
        if getattr(self, "_hk_listener", None):
            try:
                self._hk_listener.stop()
            except Exception:
                pass


def main():
    app = QApplication(sys.argv)
    setup = SetupWindow()

    state = {"ctrl": None, "win": None}

    def on_quit():
        ctrl = state.get("ctrl")
        if ctrl:
            ctrl.finish()  # 退出前也存一份记录，不丢数据
        app.quit()

    def on_finish():
        ctrl = state.get("ctrl")
        win = state.get("win")
        if not ctrl:
            return
        path = ctrl.finish()
        print(f"[record] 记录已保存: {path}", flush=True)
        if win:
            win.hide()
        state["ctrl"] = None
        state["win"] = None
        setup.show()

    def on_start(job, jd, company):
        win = AnswerWindow()
        bridge = Bridge()
        ctrl = Controller(bridge, win, job, jd, company)

        bridge.sig_chunk.connect(win.append_chunk)
        bridge.sig_question.connect(win.set_question)
        bridge.sig_status.connect(win.set_status)
        bridge.sig_warn.connect(win.show_warning)
        bridge.sig_toggle.connect(lambda: win.setVisible(not win.isVisible()))
        bridge.sig_end.connect(on_finish)
        bridge.sig_quit.connect(on_quit)
        win.finish_requested.connect(on_finish)

        win.show()
        hk = ctrl.hotkeys
        win.set_answer(
            f"等待面试官提问…\n\n"
            f"{fmt_key(hk['regenerate'])} 换一版 · "
            f"{fmt_key(hk['angle'])} 换角度 · "
            f"{fmt_key(hk['end'])} 结束 · "
            f"{fmt_key(hk['quit'])} 退出")

        ctrl.start()
        state["ctrl"] = ctrl
        state["win"] = win
        setup.hide()

    setup.start_requested.connect(on_start)
    setup.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
