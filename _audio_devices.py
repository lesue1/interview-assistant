# -*- coding: utf-8 -*-
"""音频通道体检：找出"面试官的声音到底进没进电脑、走的哪个设备"。

背景：面试时转写记录里一条"面试官"都没有 —— 麦克风（你自己）正常，但对方的声音
一个字都没录到。这个脚本用来定位是设备选错了、还是声音压根没进这台电脑。

两种用法：

  # ① 自检（推荐，不用你配合）：脚本自己往默认输出设备放测试音，
  #    再逐个 loopback 录，看谁能听到。一步就能判定通道通不通。
  .venv\\Scripts\\python _audio_devices.py test

  # ② 实时表：边放视频/音乐边看哪个设备在动（Ctrl-C 结束）
  .venv\\Scripts\\python _audio_devices.py
  .venv\\Scripts\\python _audio_devices.py 60      # 只跑 60 秒

看到哪个设备听到了，把索引填进 main.py 的 LOOPBACK_DEVICE_INDEX。
"""
import os
import sys
import tempfile
import time

import numpy as np
import pyaudiowpatch as paw

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SAMPLE_S = 0.4          # 实时表里每个设备每次听多久
SELFTEST_S = 1.5        # 自检里每个设备听多久（测试音是循环的，够长就行）
BAR = 28                # 电平条宽度


def bar(rms):
    n = min(BAR, int(rms * 90))
    return "█" * n + "·" * (BAR - n)


def probe(p, index, rate, channels, seconds=SAMPLE_S):
    """打开设备听一小段，返回 RMS。没拿到任何数据返回 -1。

    必须轮询 get_read_available() 再读 —— 空闲或静音的 WASAPI 设备上
    直接 stream.read() 会无限阻塞，整个脚本会卡死在第一个设备上。
    设备串行打开、用完即关（并发 open 容易崩）。
    """
    try:
        st = p.open(format=paw.paFloat32, channels=channels, rate=rate, input=True,
                    input_device_index=index, frames_per_buffer=int(rate * 0.1))
    except Exception:
        return -1.0
    try:
        n = int(rate * 0.1)
        vals = []
        t0 = time.time()
        while time.time() - t0 < seconds:
            if st.get_read_available() < n:
                time.sleep(0.02)
                continue
            raw = st.read(n, exception_on_overflow=False)
            a = np.frombuffer(raw, dtype=np.float32)
            if channels > 1:
                a = a.reshape(-1, channels).mean(axis=1)
            if a.size:
                vals.append(float(np.sqrt(np.mean(a ** 2))))
        return max(vals) if vals else -1.0
    except Exception:
        return -1.0
    finally:
        try:
            st.stop_stream()
            st.close()
        except Exception:
            pass


def collect(p):
    """列出设备，返回 (loopbacks, default_loop, mic)。"""
    try:
        dr = p.get_default_output_device_info()
        print(f"  ★ Windows 默认输出设备：[{dr['index']}] {dr['name']}")
    except Exception as e:
        print(f"  （取默认输出设备失败：{e}）")
        dr = None

    loopbacks = []
    try:
        loopbacks = list(p.get_loopback_device_info_generator())
    except Exception as e:
        print(f"  （枚举 loopback 失败：{e}）")

    default_loop = None
    try:
        default_loop = p.get_default_wasapi_loopback()
        print(f"  ★ 程序当前会用的 loopback：[{default_loop['index']}] {default_loop['name']}")
    except Exception as e:
        print(f"  （取默认 loopback 失败：{e}）")

    print()
    print(f"  一共 {len(loopbacks)} 个可录系统声音的 loopback：")
    for d in loopbacks:
        mark = "  ← 程序现在用这个" if default_loop and d["index"] == default_loop["index"] else ""
        print(f"    [{d['index']:>3}] {d['name']}{mark}")

    mic = None
    try:
        m = p.get_default_input_device_info()
        mic = {"index": m["index"], "name": m["name"],
               "rate": int(m["defaultSampleRate"]), "ch": int(m["maxInputChannels"])}
        print()
        print(f"  麦克风（录你自己）：[{m['index']}] {m['name']}")
    except Exception as e:
        print(f"  （取麦克风失败：{e}）")
    return loopbacks, default_loop, mic


def _make_tone_wav(path, seconds=2.0, freq=440.0, rate=44100):
    """生成一段 440Hz 正弦的 WAV，用来当测试音。"""
    import wave
    n = int(rate * seconds)
    t = np.arange(n) / rate
    data = (np.sin(2 * np.pi * freq * t) * 0.35 * 32767).astype(np.int16)
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())


def selftest(p, loopbacks, default_loop, mic):
    """脚本自己往默认输出设备放测试音，同时逐个 loopback 录 —— 不需要人工配合。"""
    import winsound

    tone = os.path.join(tempfile.gettempdir(), "_audio_probe_tone.wav")
    _make_tone_wav(tone)

    print()
    print("=" * 74)
    print("自检：脚本会自己往【默认输出设备】循环播放一段测试音，同时逐个 loopback 录。")
    print("请先把耳机音量调到能听见的程度，然后等着 —— 不需要你做任何事。")
    print("=" * 74)
    print()

    winsound.PlaySound(tone, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)
    time.sleep(0.5)          # 等播放起来
    try:
        got = {}
        for d in loopbacks:
            rms = probe(p, d["index"], int(d["defaultSampleRate"]),
                        int(d["maxInputChannels"]), seconds=SELFTEST_S)
            got[d["index"]] = rms
            if rms < 0:
                print(f"  [{d['index']:>3}] {d['name'][:38]:<38} 无数据", flush=True)
            else:
                print(f"  [{d['index']:>3}] {d['name'][:38]:<38} {rms:.4f}  {bar(rms)}"
                      f"{'  ← 听到了' if rms > 0.02 else ''}", flush=True)
        if mic:
            rms = probe(p, mic["index"], mic["rate"], mic["ch"], seconds=SELFTEST_S)
            print(f"  [麦克风] {mic['name'][:36]:<36} "
                  f"{'无数据' if rms < 0 else f'{rms:.4f}  ' + bar(rms)}", flush=True)
    finally:
        winsound.PlaySound(None, winsound.SND_PURGE)

    heard = [d for d in loopbacks if got.get(d["index"], -1) > 0.02]
    print()
    print("=" * 74)
    print("结论")
    print("=" * 74)
    if not heard:
        print("  ❌ 没有任何 loopback 听到测试音。")
        print("     测试音是往【默认输出设备】放的，所以：")
        print("     · 如果你**完全没听到**那段嘟嘟声 → 声音没出到这台电脑（检查耳机开关/音量/接的是不是这个设备）")
        print("     · 如果你**听到了**但这里全静音 → 这个声卡的 loopback 被驱动禁了，得绕路")
        print("       （你装了 VB-Cable / VoiceMeeter，可以用它们把声音转一路出来录）")
    else:
        for d in heard:
            tag = "（就是程序在用的）" if default_loop and d["index"] == default_loop["index"] else ""
            print(f"  ✅ [{d['index']}] {d['name']} 听到了测试音 {tag}")
        if default_loop and all(d["index"] != default_loop["index"] for d in heard):
            print()
            print("  ⚠️ 能听到的**不是**程序默认会用那个 → 这就是面试收不到声音的原因。")
            print(f"     修法：main.py 里 LOOPBACK_DEVICE_INDEX 改成 {heard[0]['index']}")
    return heard


def livemeter(p, loopbacks, mic, seconds):
    print()
    print("=" * 74)
    print("实时电平表 —— 现在放个视频或音乐！" +
          (f"（跑 {seconds} 秒）" if seconds else "（一直跑，按 Ctrl-C 结束）"))
    print("=" * 74)
    print("  面试官的声音走的那个设备，会有根柱子在动。全都不动 = 声音没进这台电脑。")
    print()

    t0 = time.time()
    peak = {}
    while seconds <= 0 or time.time() - t0 < seconds:
        print(f"--- 已跑 {int(time.time() - t0):>3}s ---", flush=True)
        for d in loopbacks:
            rms = probe(p, d["index"], int(d["defaultSampleRate"]),
                        int(d["maxInputChannels"]))
            if rms < 0:
                print(f"  [{d['index']:>3}] {d['name'][:34]:<34} 无数据", flush=True)
                continue
            peak[d["index"]] = max(peak.get(d["index"], 0.0), rms)
            print(f"  [{d['index']:>3}] {d['name'][:34]:<34} {rms:.4f}  {bar(rms)}"
                  f"{'  ← 有声音' if rms > 0.005 else ''}", flush=True)
        if mic:
            rms = probe(p, mic["index"], mic["rate"], mic["ch"])
            print(f"  [麦克风] {mic['name'][:30]:<30} "
                  f"{'无数据' if rms < 0 else f'{rms:.4f}  ' + bar(rms)}", flush=True)
        print(flush=True)

    print("=" * 74)
    print("整场峰值（这个才是判断依据 —— 单次采样可能正好卡在停顿上）")
    print("=" * 74)
    for d in sorted(loopbacks, key=lambda x: -peak.get(x["index"], 0.0)):
        v = peak.get(d["index"], -1)
        s = "  无数据" if v < 0 else f"峰值 {v:.4f}"
        print(f"  [{d['index']:>3}] {d['name'][:40]:<40} {s}"
              f"{'  ★ 听到了' if v > 0.005 else ''}")


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    p = paw.PyAudio()
    print("=" * 74)
    print("音频设备")
    print("=" * 74)
    loopbacks, default_loop, mic = collect(p)
    try:
        if arg == "test":
            selftest(p, loopbacks, default_loop, mic)
        else:
            livemeter(p, loopbacks, mic, int(arg) if arg.isdigit() else 0)
    except KeyboardInterrupt:
        print("\n（已停止）")
    finally:
        p.terminate()


if __name__ == "__main__":
    main()
