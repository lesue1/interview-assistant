# -*- coding: utf-8 -*-
"""真实调用 DeepSeek，验证模型是否照 [Q] / [F] / [SKIP] 格式输出。

和其他 test_*.py 不同 —— 这个会真的打 API（要联网、要花钱，一次约几分钱）。
场景全部取自 sessions\\ 里的真实面试记录。

只测"模型听不听话"，测的是 test_gate.py 覆盖不到的那半边：
判定格式、追问与新问题的区分、乱码识别、反问建议。

用法：.venv\\Scripts\\python test_live_prompt.py        # 全跑
      .venv\\Scripts\\python test_live_prompt.py 8      # 只跑第 8 个场景（调 prompt 时省钱）
退出码 0 = 全对；2 = key 有问题（不是模型的锅）。
"""
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from llm import AnswerGenerator
from main import _StreamParser

# temperature=0 求稳定：这是格式合规性测试，答案好不好不是重点
TEMP = 0.0

CASES = [
    {
        "name": "① 寒暄确认（不是问题）",
        "history": (
            "[面试官] 好的,稍等一下啊。\n"
            "[我] 嗯。\n"
            "[面试官] 好的,王小明是吧。"
        ),
        "expect": "skip",
        "why": "打招呼确认姓名，不需要答题",
    },
    {
        "name": "② 新问题（长句带铺垫）",
        "history": (
            "[面试官] 我给你学人力资源的,也不是学计算机相关的,你刚才提到你对AI这块"
            "了解还比较多,也开发了一些工具,这个是在大学的时候自己就研究吗?"
        ),
        "expect": "new",
        "why": "前面全是铺垫，末尾才是真问题",
    },
    {
        "name": "③ 追问同一个点",
        "history": (
            "[面试官] 哦,你是实习在哪儿啊?\n"
            "[我] 第一段是在猎头公司做招聘实习，第二段是在一家互联网公司做甲方招聘实习。\n"
            "[面试官] 这两段都是吗?"
        ),
        "expect": "follow",
        "why": "刚答完实习地点，他在追同一个话题 → 应该是 [F]",
    },
    {
        "name": "④ 换新话题",
        "history": (
            "[面试官] 你实习是在哪儿啊?\n"
            "[我] 两段都在杭州，一段做猎头，一段做甲方招聘。\n"
            "[面试官] 那你现在是打算直接就业还是考研?"
        ),
        "expect": "new",
        "why": "话题从实习换成毕业规划 → 应该是 [Q] 而不是 [F]",
    },
    {
        "name": "⑤ 转写乱码（听不清）",
        "history": (
            "[面试官] 哦,明白。\n"
            "[面试官] 会参考嘛,未然也没有讨论。\n"
            "[面试官] 有没有可言,口宫口边。"
        ),
        "expect": "skip",
        "why": "完全不成句，没有可答的问题",
    },
    {
        "name": "⑥ 带错字但猜得出（应纠正复述）",
        "history": (
            "[面试官] 好的,你这个学校是在山东。\n"
            "[我] 对,在潍坊。\n"
            "[面试官] 哦,你是行事在哪儿啊?"
        ),
        "expect": "new",
        "why": "'行事'是'实习'的转写错误，[Q] 行里应该纠正过来",
    },
    {
        "name": "⑦ 反问环节",
        "history": "[面试官] 行,那我这边主要的问题就差不多问完了,你看你有什么想问我的吗?",
        "expect": "reverse",
        "why": "应该给几个可以直接问出口的问题建议",
    },
    {
        # 只验证"问到城市时能正常识别成问题并作答"，**不绑定具体城市** ——
        # 绑了的话换了素材就会挂。想加素材相关的断言，自己用 "require": ["关键词"] 加一条。
        "name": "⑧ 问实习城市",
        "history": "[面试官] 你这几段实习分别是在哪些城市做的?",
        "expect": "new",
        "why": "应该识别成问题并照着素材作答",
    },
    {
        # 永久性的编造陷阱：素材里从头到尾没有绩点/排名，问到了只能说记不准。
        # 这个事实素材里永远不会有，所以这条用例可以长期留着当哨兵。
        "name": "⑨ 编造陷阱（绩点排名）",
        "history": "[面试官] 你大学期间的绩点和专业排名大概是多少?",
        "expect": "new",
        "forbid": [r"\d\.\d+", r"前\s*\d+\s*%", r"\d+\s*/\s*\d+"],
        "why": "素材没有绩点，答案里不许出现 3.5 / 前20% / 5/120 这类编造",
    },
    {
        # 公司名故意用一个**不存在**的名字 —— 模型不可能真知道它，
        # 所以只要它讲出这家公司做什么，就一定是编的。
        # 填了公司名只该让它知道"要面的是哪家"，用来在答案里提到名字，
        # 不该让它拿训练数据去"回忆"公司业务（小公司会编）。
        "name": "⑩ 填了公司名也不许编公司业务",
        "history": "[面试官] 你对我们公司有什么了解？",
        "company": "青川量子智研科技有限责任公司",
        "expect": "new",
        "forbid": [r"贵公司(是|主要|专注|致力于|从事)", r"贵司(是|主要|专注|致力于|从事)",
                   r"我了解到贵", r"公司主要(做|业务|产品)"],
        "why": "只许知道名字，不许讲公司业务",
    },
]


def run_case(gen, case):
    """跑一个场景，返回 (解析结果, 模型原始输出)。"""
    qs, ans = [], []
    p = _StreamParser(lambda t, f: qs.append((t, f)), ans.append)
    raw = []
    try:
        gen.generate(case["history"], lambda d: (raw.append(d), p.feed(d)), temperature=TEMP)
        p.finish()
    except Exception as e:
        return {"err": str(e)}, "".join(raw)
    return {
        "skip": p.skipped,
        "q": qs[0][0] if qs else None,
        "follow": qs[0][1] if qs else None,
        "ans": "".join(ans),
    }, "".join(raw)


def check(case, got):
    """返回 (是否通过, 说明)。"""
    want = case["expect"]
    if "err" in got:
        return False, f"调用失败: {got['err']}"
    # 编造检查：答案里出现了素材里没有的事实，直接判失败（正则，好避开公司名这类合法出现）
    for pat in case.get("forbid", ()):
        m = re.search(pat, got.get("ans", ""))
        if m:
            return False, f"答案里出现了素材没有的「{m.group(0)}」—— 编造"
    # 用了素材没给的事实，同样判失败
    for pat in case.get("require", ()):
        if not re.search(pat, got.get("ans", "")):
            return False, f"答案里没出现该有的「{pat}」"
    if want == "skip":
        return got["skip"], "判 [SKIP]" if got["skip"] else f"没判 SKIP，出了答案（{len(got['ans'])} 字）"
    if want == "new":
        if got["skip"] or not got["q"]:
            return False, "没识别出问题"
        return (not got["follow"]), f"{'[F] 追问' if got['follow'] else '[Q] 新问题'}：{got['q']}"
    if want == "follow":
        if got["skip"] or not got["q"]:
            return False, "没识别出问题（应该判为追问）"
        return got["follow"], f"{'[F] 追问' if got['follow'] else '[Q] 新问题'}：{got['q']}"
    if want == "reverse":
        if got["skip"] or not got["q"]:
            return False, "把反问当成了不用答"
        bullets = got["ans"].count("•")
        return bullets >= 3, f"识别为问题，给了 {bullets} 条提问建议"
    return False, "未知期望"


def main():
    try:
        gen = AnswerGenerator(None)
    except RuntimeError as e:
        print(f"[key] {e}", flush=True)
        sys.exit(2)

    # 可选：命令行给序号就只跑那一个（如 `python test_live_prompt.py 8`）
    cases = CASES
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        idx = int(sys.argv[1])
        if not 1 <= idx <= len(CASES):
            print(f"[参数] 序号要在 1~{len(CASES)} 之间", flush=True)
            sys.exit(2)
        cases = [CASES[idx - 1]]

    print(f"prompt {len(gen.system_prompt)} 字 | temperature={TEMP} | "
          f"{len(cases)}/{len(CASES)} 个场景", flush=True)
    print(flush=True)

    bad = []
    for case in cases:
        print("=" * 70, flush=True)
        print(f"{case['name']}   期望: {case['why']}", flush=True)
        # 用例可以自带 company，用来验"填了公司名也不许编公司业务"
        g = AnswerGenerator(None, case["company"]) if case.get("company") else gen
        got, raw = run_case(g, case)
        ok, note = check(case, got)
        print(f"  模型原话: {raw[:120].replace(chr(10), ' / ')}", flush=True)
        print(f"  解析结果: {note}", flush=True)
        print(f"  {'✅ 通过' if ok else '❌ 不符合预期'}", flush=True)
        print(flush=True)
        if not ok:
            bad.append(case["name"])

    print("=" * 70, flush=True)
    if bad:
        print(f"结果: {len(cases) - len(bad)}/{len(cases)} 符合预期", flush=True)
        for n in bad:
            print(f"  - {n}", flush=True)
        sys.exit(1)
    print(f"结果: {len(cases)}/{len(cases)} 全部符合预期", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
