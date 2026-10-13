# -*- coding: utf-8 -*-
"""自动验证：判定闸门 + 结构化输出解析 + 答案窗状态机。

和 test_m1~m4 不同 —— 那几个要你肉眼看窗口，这个是自动断言，退出码 0 = 全通过。
覆盖五处、都不依赖真实音频、不调用 DeepSeek：
  1. is_non_question   本地粗筛：哪些句子根本不用发 LLM
  2. _StreamParser     解析 [Q] / [F] / [SKIP] 的流式输出
  3. _looks_like_question + _on_text   我在说话时的静默规则
  4. AnswerWindow      新问题刷新 / 追问追加 / 判 SKIP 时窗口不动 / 翻页
  5. Bridge            跨线程信号到窗口的全链路

用法：.venv\\Scripts\\python test_gate.py
"""
import contextlib
import io
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 无头跑，不弹窗

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from PyQt6.QtWidgets import QApplication

from main import Bridge, Controller, _StreamParser, is_non_question
from ui import AnswerWindow

_fail = []
_n = 0


def ck(name, got, want):
    global _n
    _n += 1
    if got != want:
        _fail.append(name)
        print(f"  FAIL {name}")
        print(f"       得到 {got!r}")
        print(f"       期望 {want!r}")


def parse(deltas):
    """喂一串流式分片，返回 (识别到的问题列表, 答案, 是否跳过)。"""
    qs, ans = [], []
    p = _StreamParser(lambda t, f: qs.append((t, f)), ans.append)
    for d in deltas:
        p.feed(d)
    p.finish()
    return qs, "".join(ans), p.skipped


# —— 1. 本地粗筛 ——

def test_coarse_filter():
    print("[1] 本地粗筛 is_non_question")
    # True = 直接不发 LLM。真实面试记录里的原句优先
    for text, want in [
        ("嗯", True), ("OK", True), ("好的", True), ("哦，明白。", True),
        ("明白了", True), ("嗯嗯嗯", True), ("好嘞", True), ("这样啊", True),
        # 这些是短追问，必须放行给 LLM —— 误砍真问题才是事故
        ("为什么", False), ("那后来呢", False), ("这两端都是吗", False),
        ("哦,你是行事在哪儿啊?", False), ("好的,王小明是吧。", False),
        ("你帮我先介绍一下自己吧。", False),
    ]:
        ck(f"粗筛 {text!r}", is_non_question(text), want)


# —— 2. 结构化输出解析 ——

def test_parser():
    print("[2] 流式输出解析 _StreamParser")
    # [Q] 新问题：刷新一页
    ck("Q 单块", parse(["[Q] 你实习是在哪儿\n\n我在字节做招聘实习。"]),
       ([("你实习是在哪儿", False)], "我在字节做招聘实习。", False))
    # 标记和空行都可能被拆成独立分片
    ck("Q 逐字流式", parse(["[", "Q", "] 你实习是在哪儿", "\n", "\n", "我在字节", "。"]),
       ([("你实习是在哪儿", False)], "我在字节。", False))
    # [F] 追问：接在下面，不刷新
    ck("F 单块", parse(["[F] 这两段都是吗\n\n对，两段都是招聘。"]),
       ([("这两段都是吗", True)], "对，两段都是招聘。", False))
    ck("F 逐字", parse(["[", "F", "] 那后来呢", "\n", "\n", "后来改好了。"]),
       ([("那后来呢", True)], "后来改好了。", False))
    # 标记容忍：模型偶尔写全称
    ck("FOLLOWUP 全称", parse(["[FOLLOWUP] 还有吗\n\n还有一个。"])[0], [("还有吗", True)])
    ck("NEW 同义词", parse(["[NEW] 你家在哪儿\n\n浙江温州。"])[0], [("你家在哪儿", False)])
    # SKIP：窗口必须一动不动
    ck("SKIP", parse(["[SKIP]"]), ([], "", True))
    ck("SKIP 带垃圾", parse(["[SKIP] 好的，我明白了"]), ([], "", True))
    ck("SKIP 逐字", parse(["[S", "K", "I", "P", "]"]), ([], "", True))
    # 兜底：宁可格式丑，不能吞内容、不能把标记显示给用户
    ck("未知标记当答案", parse(["[FOO] 这是正文"]), ([], "[FOO] 这是正文", False))
    ck("完全没格式", parse(["好的", "，我", "明白了"]), ([], "好的，我明白了", False))
    ck("中文方括号正文", parse(["[注：这里强调一下]"]), ([], "[注：这里强调一下]", False))
    ck("裸 [Q] 不刷窗", parse(["[Q]"]), ([], "", False))
    ck("Q 无换行到结尾", parse(["[Q] 你有什么想问我的吗"]),
       ([("你有什么想问我的吗", False)], "", False))
    ck("答案里带换行", parse(["[Q] 问题\n\n第一段\n第二段"]),
       ([("问题", False)], "第一段\n第二段", False))
    ck("markdown 包裹", parse(["**[F] 那后来呢**\n\n后来改好了。"]),
       ([("那后来呢", True)], "后来改好了。", False))


# —— 3. 我在说话时的静默规则 ——

class _Sig:
    def __init__(self, out):
        self.out = out

    def emit(self, v):
        self.out.append(v)


class _FakeBridge:
    def __init__(self):
        self.status = []
        self.warns = []

    @property
    def sig_status(self):
        return _Sig(self.status)

    @property
    def sig_warn(self):
        return _Sig(self.warns)


class _FakeStore:
    def __init__(self):
        self.entries = []

    def add(self, *a):
        self.entries.append(a)


def _fake_ctrl(speaking):
    """绕过 __init__（那会去读 .env / 建 PlayAudio），只装 _on_text 需要的零件。"""
    c = object.__new__(Controller)
    c._seen_texts = set()
    c.store = _FakeStore()
    c.bridge = _FakeBridge()
    c._last_mic_voice = time.time() if speaking else 0.0
    c.fired = []
    c._trigger_generate = lambda *a, **k: c.fired.append(a)
    return c


def _drive(speaking, text):
    c = _fake_ctrl(speaking)
    with contextlib.redirect_stdout(io.StringIO()):   # 挡掉 _on_text 的 [面试官] 打印
        c._on_text("面试官", text, 1.0)
    return bool(c.fired), (c.bridge.status[0] if c.bridge.status else None)


def test_speaking_gate():
    print("[3] 我在说话时的静默规则")
    ck("问句过滤 应和我", Controller._looks_like_question("好，我明白了，你继续"), False)
    ck("问句过滤 语气词", Controller._looks_like_question("OK"), False)
    ck("问句过滤 短追问", Controller._looks_like_question("然后呢"), True)
    ck("问句过滤 疑问词", Controller._looks_like_question("那你为什么选这个方向"), True)

    # 我没在说话：正常判断，语气词连 LLM 都不发
    ck("没说话·语气词", _drive(False, "嗯。"), (False, "这句不用答"))
    ck("没说话·短追问", _drive(False, "这两端都是吗?"), (True, None))
    ck("没说话·长句问题", _drive(False, "你帮我先介绍一下自己吧。"), (True, None))
    # 含人名的寒暄本地抓不住，交给 LLM 判 SKIP —— 触发是对的
    ck("没说话·寒暄", _drive(False, "好的,王小明是吧。"), (True, None))

    # 我正在说话：应和声完全静默（连状态都不闪）
    ck("我说·应和", _drive(True, "好，我明白了，你继续"), (False, None))
    ck("我说·语气词", _drive(True, "嗯嗯。"), (False, None))
    ck("我说·随口一句", _drive(True, "你说得挺好的"), (False, None))
    # 但他插话问真问题必须放行，不能漏
    ck("我说·他插话追问", _drive(True, "那你具体是怎么做的"), (True, None))


# —— 4. 面试官通道静音告警 ——

def _warn_ctrl(mic_heard, loop_heard, elapsed, warned=False):
    c = object.__new__(Controller)
    c._mic_heard = mic_heard
    c._loop_heard = loop_heard
    c._warned_silent = warned
    c._t_start = time.time() - elapsed
    c.bridge = _FakeBridge()
    return c


def test_silent_warning():
    print("[4] 面试官通道静音告警")
    # 正常情况：面试官通道有声音 → 永远不报警
    c = _warn_ctrl(mic_heard=True, loop_heard=True, elapsed=99)
    c._maybe_warn_silent()
    ck("系统有声音时不报警", c.bridge.warns, [])
    # 你自己还没开过口 → 判断不了，别误报
    c = _warn_ctrl(mic_heard=False, loop_heard=False, elapsed=99)
    c._maybe_warn_silent()
    ck("我没说话时不报警", c.bridge.warns, [])
    # 时间还没到 → 等一等
    c = _warn_ctrl(mic_heard=True, loop_heard=False, elapsed=5)
    c._maybe_warn_silent()
    ck("开场 5 秒不报警", c.bridge.warns, [])
    # 真正的翻车场景：我说过话了，30 秒过去系统声音一直空 → 必须报
    c = _warn_ctrl(mic_heard=True, loop_heard=False, elapsed=40)
    c._maybe_warn_silent()
    ck("该报警时报了", len(c.bridge.warns), 1)
    ck("报警里说了设备跟错", "设备" in c.bridge.warns[0], True)
    # 只报一次，别刷屏
    c._maybe_warn_silent()
    ck("只报一次", len(c.bridge.warns), 1)


# —— 5 & 6. 窗口状态机 ——

def test_window():
    print("[5] AnswerWindow 状态机")
    w = AnswerWindow()
    w.show()
    w.set_answer("等待面试官提问…")

    w.set_question("你实习是在哪儿", False)
    w.append_chunk("第1段：在字节做招聘实习。")
    ck("新问题写入", w.question.text(), "面试官问的是：你实习是在哪儿")
    ck("一题时 nav 收起", w.nav_prev.isVisible(), False)

    # 追问：不清空，下面接一段
    w.set_question("这两段都是吗", True)
    ck("追问分隔已插入", w.answer.toPlainText(),
       "第1段：在字节做招聘实习。\n\n——— 追问：这两段都是吗 ———\n\n")
    ck("追问不换问题行", w.question.text(), "面试官问的是：你实习是在哪儿")
    w.append_chunk("第2段：对，两段都是招聘。")
    ck("补充接在原答案后", w.answer.toPlainText().endswith("第2段：对，两段都是招聘。"), True)
    ck("追问仍算同一题", w.nav_prev.isVisible(), False)

    # 新问题：刷新 + 上一题（含追问）整体归档
    w.set_question("为什么选人力资源", False)
    ck("新问题刷新窗口", w.answer.toPlainText(), "")
    ck("nav 出现 2/2", w.nav_pos.text(), "2/2")
    ck("归档含追问内容", "对，两段都是招聘。" in w._history[0][1], True)
    w._nav_prev()
    ck("翻回第1题", w.question.text(), "面试官问的是：你实习是在哪儿")
    ck("翻回内容完整", w.answer.toPlainText(), w._history[0][1])
    w._nav_next()
    ck("翻回最新", w.question.text(), "面试官问的是：为什么选人力资源")

    # 判 SKIP 时：窗口一动不动，只动状态行
    before = (w.question.text(), w.answer.toPlainText())
    w.set_status("这句不用答")
    ck("SKIP 不动窗", (w.question.text(), w.answer.toPlainText()), before)

    # 边界：还没有主问题时来追问 → 当新问题处理，别把内容接到空答案上
    w2 = AnswerWindow()
    w2.show()
    w2.set_question("这算新问题", True)
    ck("无主问题时追问=新问题", w2.question.text(), "面试官问的是：这算新问题")

    # 通道告警：还没出过题 → 写进答案区（显眼）；已出过题 → 只动状态行，不冲掉答案
    w3 = AnswerWindow()
    w3.show()
    w3.set_answer("等待面试官提问…")
    w3.show_warning("面试官通道没声音")
    ck("出题前告警写进答案区", w3.answer.toPlainText().startswith("⚠ 面试官通道没声音"), True)
    ck("同状态行也标记了", "异常" in w3.status.text(), True)
    w3.set_question("你实习是在哪儿", False)
    w3.append_chunk("答案正文")
    w3.show_warning("面试官通道没声音")
    ck("出题后告警不冲答案", w3.answer.toPlainText(), "答案正文")
    ck("出题后告警只改状态行", "异常" in w3.status.text(), True)

    print("[6] Bridge 跨线程信号 → 窗口 全链路")
    # 信号参数写错（比如少个 bool）在 Qt 里是静默失败，必须实测
    b = Bridge()
    b.sig_chunk.connect(w2.append_chunk)
    b.sig_question.connect(w2.set_question)
    b.sig_status.connect(w2.set_status)
    b.sig_question.emit("你实习是在哪儿", False)
    b.sig_chunk.emit("我在字节做招聘实习。")
    ck("信号·新问题", w2.question.text(), "面试官问的是：你实习是在哪儿")
    ck("信号·答案", w2.answer.toPlainText(), "我在字节做招聘实习。")
    b.sig_question.emit("这两段都是吗", True)
    b.sig_chunk.emit("对，两段都是。")
    ck("信号·追问保留原文", "我在字节做招聘实习。" in w2.answer.toPlainText(), True)
    # 到这里 w2 上有 2 题（"这算新问题" / "你实习是在哪儿"，追问不算新页）
    b.sig_question.emit("为什么选人力资源", False)
    ck("信号·第3题 nav 3/3", w2.nav_pos.text(), "3/3")
    w2._nav_prev()
    ck("信号·翻回第2题", w2.question.text(), "面试官问的是：你实习是在哪儿")
    ck("信号·翻回含追问", "对，两段都是。" in w2.answer.toPlainText(), True)


# —— 7. 启动自检 + 设置窗 ——

def test_setup_and_preflight():
    print("[7] 启动自检 + 设置窗")
    import os
    import tempfile

    import preflight
    from ui import SetupWindow

    # 自检返回的结构要能直接喂给界面
    items = preflight.check()
    ck("自检有返回", isinstance(items, list), True)
    ck("自检至少给一条", len(items) >= 1, True)
    for it in items:
        ck("自检项字段完整", set(it) >= {"level", "title", "fix"}, True)
        ck("level 合法", it["level"] in ("error", "warn", "info"), True)

    # 用临时的不存在路径验证"缺 .env / 缺素材"能被识别出来
    orig_env, orig_mat = preflight.ENV, preflight.MATERIALS
    try:
        preflight.ENV = os.path.join(tempfile.gettempdir(), "_preflight_no_such_env")
        r = preflight._check_key()
        ck("缺 .env 判为 error", r and r[0]["level"], "error")
        ck("缺 .env 给了怎么补", bool(r and r[0].get("fix")), True)
        preflight.MATERIALS = os.path.join(tempfile.gettempdir(), "_preflight_no_such_materials")
        r = preflight._check_materials()
        ck("缺素材判为 warn", r and r[0]["level"], "warn")
    finally:
        preflight.ENV, preflight.MATERIALS = orig_env, orig_mat

    # 设置窗三个输入框都在，点开始要把三个值都传出去
    sw = SetupWindow()
    for attr in ("job_input", "company_input", "jd_input", "import_btn", "start_btn"):
        ck(f"设置窗有 {attr}", hasattr(sw, attr), True)
    got = []
    sw.start_requested.connect(lambda *a: got.append(a))
    sw.job_input.setText("测试岗")
    sw.company_input.setText("测试公司")
    sw.jd_input.setPlainText("测试JD全文")
    sw._on_start()
    ck("点开始传出 (岗位名, JD, 公司名)", got[0] if got else None,
       ("测试岗", "测试JD全文", "测试公司"))

    # 岗位名留空要兜底，不能把空串当岗位名
    got.clear()
    sw.job_input.setText("   ")
    sw._on_start()
    ck("岗位名留空有兜底", got[0][0] if got else None, "模拟面试")

    # 导入简历：文件不存在要返回失败 + 一句人话，而不是抛异常
    from materials_build import extract_to_materials
    ok, msg = extract_to_materials("C:/definitely/not/a/real/path.pdf")
    ck("导入不存在的 PDF → 失败", ok, False)
    ck("且给了说明文字", bool(msg), True)


def main():
    test_coarse_filter()
    test_parser()
    test_speaking_gate()
    test_silent_warning()
    app = QApplication(sys.argv)   # 建窗口前必须先有 QApplication
    test_window()
    test_setup_and_preflight()

    print()
    if _fail:
        print(f"结果: {_n - len(_fail)}/{_n} 通过，{len(_fail)} 个失败")
        for name in _fail:
            print(f"  - {name}")
        sys.exit(1)
    print(f"结果: {_n}/{_n} 全部通过")
    sys.exit(0)


if __name__ == "__main__":
    main()
