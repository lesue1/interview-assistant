# -*- coding: utf-8 -*-
"""对话记录。双通道转写按时间戳合并，供追问上下文与 sessions 存档使用。"""
import os
import threading
import time


class TranscriptStore:
    """维护一份按时间排序的对话条目，并把会话存档到 sessions\\*.txt。

    线程安全：采集/ASR 线程调 add，UI/LLM 线程调 recent/formatted。
    存档每次 add 后按时间戳整份重写，保证文件里的顺序就是对话顺序（会话短，开销可忽略）。
    """

    def __init__(self, sessions_dir=None, job_name=None, jd=None):
        self.entries = []          # [{speaker, text, t_start}]，t_start 单位秒（绝对）
        self.answers = []          # AI 生成的答案（按生成顺序），报告写最后一版
        self.job_name = job_name
        self.jd = jd
        self._t0 = None            # 第一条 entry 的绝对秒，显示时减掉变成相对 mm:ss
        self._lock = threading.Lock()
        self._path = None
        self._report_path = None
        if sessions_dir:
            os.makedirs(sessions_dir, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            prefix = self._sanitize(job_name) if job_name else "session"
            self._path = os.path.join(sessions_dir, f"{prefix}_{ts}.txt")
            self._report_path = os.path.join(sessions_dir, f"{prefix}_{ts}.md")

    def add(self, speaker, text, t_start):
        with self._lock:
            if self._t0 is None:
                self._t0 = t_start
            self.entries.append({"speaker": speaker, "text": text, "t_start": t_start})
            self.entries.sort(key=lambda e: e["t_start"])
            if self._path:
                self._rewrite()

    def _rewrite(self):
        with open(self._path, "w", encoding="utf-8") as f:
            for e in self.entries:
                f.write(f"[{self._fmt(e['t_start'])}] {e['speaker']}: {e['text']}\n")

    def recent(self, n=None):
        """返回最近 n 条（或全部）条目副本。"""
        with self._lock:
            es = self.entries if n is None else self.entries[-n:]
            return list(es)

    def formatted(self, n=None):
        """拼成带说话人标签的对话文本，供 system/user prompt 使用。"""
        with self._lock:
            es = self.entries if n is None else self.entries[-n:]
            return "\n".join(f"[{e['speaker']}] {e['text']}" for e in es)

    def _fmt(self, sec):
        # t_start 是绝对秒（Unix 时间戳），显示成相对第一条的 mm:ss
        base = self._t0 if self._t0 is not None else sec
        m, s = divmod(int(sec - base), 60)
        return f"{m:02d}:{s:02d}"

    def add_answer(self, text):
        """记录一版 AI 答案（热键换一版会追加，报告取最后一版）。"""
        with self._lock:
            self.answers.append(text)

    def write_report(self):
        """结束面试时写一份完整报告：岗位名 + JD + 双通道转写 + 最后一版答案。

        返回报告路径；未指定 sessions_dir（或已写过）时返回 None。
        """
        if not self._report_path:
            return None
        with self._lock:
            lines = [f"# {self.job_name or '模拟面试'}", ""]
            lines.append(f"开始时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
            lines.append("")
            if self.jd:
                lines += ["## 岗位 JD", "", self.jd, ""]
            lines += ["## 面试转写", ""]
            for e in self.entries:
                lines.append(f"[{self._fmt(e['t_start'])}] {e['speaker']}: {e['text']}")
            lines.append("")
            lines += ["## AI 参考答案", ""]
            lines.append(self.answers[-1] if self.answers else "（无）")
            lines.append("")
            with open(self._report_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        return self._report_path

    @staticmethod
    def _sanitize(name):
        """岗位名可能含文件名非法字符（空格/斜杠等），清洗成安全前缀。"""
        for ch in '\\/:*?"<>|':
            name = name.replace(ch, "")
        name = name.strip().replace(" ", "_")
        return name or "session"

    def close(self):
        """存档是每次 add 即时写的，这里无事可做，保留接口兼容。"""
        pass
