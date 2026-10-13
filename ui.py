# -*- coding: utf-8 -*-
"""PyQt6 参考答案窗口：半透明、置顶、点击不抢焦点、可拖动、透明度可调。

四个 flag 的含义（这是"点它/拖它不让会议软件失焦"的关键）：
- Qt.Tool                       → WS_EX_TOOLWINDOW，不占任务栏 / Alt-Tab
- Qt.WindowStaysOnTopHint       → 置顶
- Qt.WindowDoesNotAcceptFocus   → WS_EX_NOACTIVATE，点击不激活，会议保持焦点
- Qt.FramelessWindowHint        → 无边框，配合鼠标事件手动拖动
"""
from PyQt6.QtCore import Qt, QRectF, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QPalette
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QSlider, QPushButton, QLabel,
    QLineEdit, QApplication, QFileDialog,
)

import preflight
from materials_build import extract_to_materials


class _Panel(QWidget):
    """圆角半透明面板。空白处（标题条、边距）可拖动整窗；QTextEdit 区域不拦截拖动。

    背景不走 QSS（普通 QWidget 不画 background-color），改 paintEvent 手绘圆角，
    透明度由 set_opacity 直接改 alpha 并重绘，滑块才真正生效。
    边缘 6px 内按住 = 缩放（四边 + 四角）；其余空白按住 = 拖动。
    """

    # 边缘位标志
    LEFT = 1
    RIGHT = 2
    TOP = 4
    BOTTOM = 8
    RESIZE_MARGIN = 6
    MIN_W = 320
    MIN_H = 220

    def __init__(self, win):
        super().__init__(win)
        self._win = win
        self._drag = None
        self._resize_edge = 0
        self._resize_start = None
        self._opacity = 232
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)

    def set_opacity(self, v):
        self._opacity = v
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 14, 14)
        p.fillPath(path, QColor(24, 26, 30, self._opacity))
        p.setPen(QPen(QColor(255, 255, 255, 30), 1))
        p.drawPath(path)

    def _edge_at(self, pos):
        e = 0
        if pos.x() <= self.RESIZE_MARGIN:
            e |= self.LEFT
        elif pos.x() >= self.width() - self.RESIZE_MARGIN:
            e |= self.RIGHT
        if pos.y() <= self.RESIZE_MARGIN:
            e |= self.TOP
        elif pos.y() >= self.height() - self.RESIZE_MARGIN:
            e |= self.BOTTOM
        return e

    def _update_cursor(self, pos):
        e = self._edge_at(pos)
        if e in (self.LEFT, self.RIGHT):
            c = Qt.CursorShape.SizeHorCursor
        elif e in (self.TOP, self.BOTTOM):
            c = Qt.CursorShape.SizeVerCursor
        elif e in (self.LEFT | self.TOP, self.RIGHT | self.BOTTOM):
            c = Qt.CursorShape.SizeFDiagCursor
        elif e in (self.LEFT | self.BOTTOM, self.RIGHT | self.TOP):
            c = Qt.CursorShape.SizeBDiagCursor
        else:
            c = Qt.CursorShape.ArrowCursor
        self.setCursor(c)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            edge = self._edge_at(e.position())
            if edge:
                self._resize_edge = edge
                self._resize_start = (e.globalPosition().toPoint(), self._win.geometry())
            else:
                self._drag = e.globalPosition().toPoint() - self._win.frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._resize_edge and self._resize_start:
            gpos = e.globalPosition().toPoint()
            start_gpos, geo = self._resize_start
            dx = gpos.x() - start_gpos.x()
            dy = gpos.y() - start_gpos.y()
            edge = self._resize_edge
            x, y, w, h = geo.x(), geo.y(), geo.width(), geo.height()

            if edge & self.LEFT:
                w -= dx
                x += dx
            elif edge & self.RIGHT:
                w += dx
            if edge & self.TOP:
                h -= dy
                y += dy
            elif edge & self.BOTTOM:
                h += dy

            if w < self.MIN_W:
                if edge & self.LEFT:
                    x = geo.x() + geo.width() - self.MIN_W
                w = self.MIN_W
            if h < self.MIN_H:
                if edge & self.TOP:
                    y = geo.y() + geo.height() - self.MIN_H
                h = self.MIN_H

            self._win.setGeometry(x, y, w, h)
            return

        if self._drag is not None and (e.buttons() & Qt.MouseButton.LeftButton):
            self._win.move(e.globalPosition().toPoint() - self._drag)
        else:
            self._update_cursor(e.position())
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._drag = None
        self._resize_edge = 0
        self._resize_start = None
        super().mouseReleaseEvent(e)


class AnswerWindow(QWidget):
    finish_requested = pyqtSignal()  # 点「结束」→ main 层停止采集并生成记录

    def __init__(self, width=500, height=340):
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(width, height)

        self._opacity = 232  # 面板背景 alpha，80~255
        # 问答历史：新问题来了就把上一题归档，靠顶部 ‹ n/m › 往回翻。
        # _view = None 表示正在看最新一题；否则是 _history 的下标。
        self._history = []   # [(问题, 答案)]，只存真正识别出问题的那些
        self._cur_q = ""     # 当前题的问题（"" = 还没识别出问题）
        self._cur_a = ""     # 当前题的答案全文
        self._view = None
        self._stick_bottom = False   # 追问补充接在下面长，滚动条得跟着走
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        panel = _Panel(self)
        panel.setObjectName("panel")
        self._panel = panel
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        # 顶部：标题 + 状态 + 关闭（点关闭 = 隐藏，热键再唤出）
        top = QHBoxLayout()
        title = QLabel("参考答案")
        title.setObjectName("title")
        self.status = QLabel("")
        self.status.setObjectName("status")
        end = QPushButton("结束")
        end.setObjectName("end")
        end.setCursor(Qt.CursorShape.PointingHandCursor)
        end.clicked.connect(self.finish_requested.emit)
        close = QPushButton("✕")
        close.setObjectName("close")
        close.setFixedSize(26, 26)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.hide)

        # 往回翻上一题：只有攒够 2 题才显示
        self.nav_prev = QPushButton("‹")
        self.nav_prev.setObjectName("nav")
        self.nav_prev.setFixedSize(20, 20)
        self.nav_prev.setCursor(Qt.CursorShape.PointingHandCursor)
        self.nav_prev.clicked.connect(self._nav_prev)
        self.nav_pos = QLabel("")
        self.nav_pos.setObjectName("nav")
        self.nav_next = QPushButton("›")
        self.nav_next.setObjectName("nav")
        self.nav_next.setFixedSize(20, 20)
        self.nav_next.setCursor(Qt.CursorShape.PointingHandCursor)
        self.nav_next.clicked.connect(self._nav_next)

        top.addWidget(title)
        top.addWidget(self.status)
        top.addStretch()
        top.addWidget(self.nav_prev)
        top.addWidget(self.nav_pos)
        top.addWidget(self.nav_next)
        top.addWidget(end)
        top.addWidget(close)
        layout.addLayout(top)

        # 识别出的核心问题：面试官一大段话里真正在问的那句，独立于答案显示，
        # 让用户一眼确认"系统抓对没有"。没有识别到问题时整行隐藏。
        self.question = QLabel("")
        self.question.setObjectName("question")
        self.question.setWordWrap(True)
        self.question.setMaximumHeight(80)   # 最多约 4 行，别把答案区挤没
        self.question.hide()
        layout.addWidget(self.question)

        # 答案区
        self.answer = QTextEdit()
        self.answer.setObjectName("answer")
        self.answer.setReadOnly(True)
        layout.addWidget(self.answer, 1)

        # 底部：透明度滑块
        bottom = QHBoxLayout()
        hint = QLabel("透明度")
        hint.setObjectName("hint")
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(80, 255)
        self.slider.setValue(self._opacity)
        self.slider.valueChanged.connect(self._on_opacity)
        bottom.addWidget(hint)
        bottom.addWidget(self.slider, 1)
        layout.addLayout(bottom)

        root.addWidget(panel)
        self._apply_style()
        self._update_nav()   # 开局面还没题，先收起 ‹ n/m ›

    def _on_opacity(self, v):
        self._opacity = v
        self._panel.set_opacity(v)

    def _apply_style(self):
        self.setStyleSheet(f"""
            #title {{ color: rgba(255,255,255,0.55); font-size: 12px; }}
            #status {{ color: rgba(255,255,255,0.4); font-size: 11px; padding-left: 8px; }}
            #question {{
                color: rgba(150,205,255,0.95); font-size: 13px; line-height: 18px;
                border-left: 2px solid rgba(120,190,255,0.5); padding-left: 8px;
            }}
            #hint {{ color: rgba(255,255,255,0.55); font-size: 11px; }}
            #end {{
                color: rgba(255,255,255,0.7); background: rgba(255,255,255,0.08);
                border: 1px solid rgba(255,255,255,0.15); border-radius: 6px;
                font-size: 12px; padding: 3px 10px;
            }}
            #end:hover {{ color: white; background: rgba(255,255,255,0.15); }}
            #close {{
                color: rgba(255,255,255,0.5); background: transparent;
                border: none; font-size: 14px;
            }}
            #close:hover {{ color: white; }}
            #nav {{
                color: rgba(255,255,255,0.5); background: transparent;
                border: none; font-size: 14px; padding: 0;
            }}
            #nav:hover {{ color: white; }}
            #nav:disabled {{ color: rgba(255,255,255,0.15); }}
            QLabel#nav {{ font-size: 11px; padding: 0 2px; }}
            QTextEdit#answer {{
                color: white; background: transparent; border: none; font-size: 14px;
            }}
            QSlider::groove:horizontal {{
                height: 3px; background: rgba(255,255,255,0.2); border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                width: 12px; height: 12px; margin: -5px 0;
                background: rgba(255,255,255,0.8); border-radius: 6px;
            }}
        """)

    # —— 翻页 ——

    def _total(self):
        """总页数 = 已归档的题 + 当前这题（还没识别出问题就不算一页）。"""
        return len(self._history) + (1 if self._cur_q else 0)

    def _show(self, idx):
        """idx=None 看最新，否则看 _history[idx]。"""
        self._view = idx
        if idx is None:
            q, a = self._cur_q, self._cur_a
        else:
            q, a = self._history[idx]
        self.question.setText(f"面试官问的是：{q}" if q else "")
        self.question.setVisible(bool(q))
        self.answer.setPlainText(a)
        self.answer.verticalScrollBar().setValue(0)
        self._update_nav()

    def _update_nav(self):
        total = self._total()
        show = total > 1          # 只有一题时没有可翻的，整组收起
        pos = total if self._view is None else self._view + 1
        self.nav_prev.setVisible(show)
        self.nav_pos.setVisible(show)
        self.nav_next.setVisible(show)
        self.nav_pos.setText(f"{pos}/{total}" if show else "")
        self.nav_prev.setEnabled(show and pos > 1)
        self.nav_next.setEnabled(show and self._view is not None)

    def _nav_prev(self):
        if self._view is None:
            self._show(len(self._history) - 1)      # 从最新翻回上一题
        elif self._view > 0:
            self._show(self._view - 1)

    def _nav_next(self):
        if self._view is not None:
            nxt = self._view + 1
            self._show(nxt if nxt < len(self._history) else None)

    # —— 供 main 层调用的接口 ——

    def append_chunk(self, text):
        """流式追加一段文本。新答案保持滚动条不动（用户自己滑），补充则跟着滚。"""
        first = not self._cur_a
        self._cur_a += text
        if first and self._view is not None:
            self._show(None)      # 新答案的第一片到了 → 把正在翻旧的用户拉回最新
            return
        if self._view is not None:
            return                # 用户正在翻旧题，新答案继续在后台攒着，不打扰
        if self._stick_bottom:
            self.answer.moveCursor(self.answer.textCursor().MoveOperation.End)
            self.answer.insertPlainText(text)
            self._scroll_bottom()
            return
        sb = self.answer.verticalScrollBar()
        pos = sb.value()
        self.answer.moveCursor(self.answer.textCursor().MoveOperation.End)
        self.answer.insertPlainText(text)
        sb.setValue(pos)

    def _scroll_bottom(self):
        sb = self.answer.verticalScrollBar()
        sb.setValue(sb.maximum())

    def set_answer(self, text):
        """一次性覆盖整段答案。仅用于开场提示这类非问答内容，不进历史。"""
        self._cur_q, self._cur_a = "", text
        self.answer.setPlainText(text)
        self.question.hide()
        self._view = None
        self._stick_bottom = False
        self._update_nav()

    def set_question(self, text, followup=False):
        """确认识别到了问题：新问题刷新一页，追问则在当前答案下面接一段。

        整个程序里只有这里会让答案换篇 —— 面试官的寒暄/陈述/玩笑判成 [SKIP] 时
        根本走不到这里，用户正在看的答案不会被抢走。
        """
        if followup and self._cur_q:
            self._add_followup(text)
            return
        if self._cur_q:
            self._history.append((self._cur_q, self._cur_a))
        self._cur_q, self._cur_a = text, ""
        self._stick_bottom = False
        self._show(None)

    def _add_followup(self, question):
        """追问同一个话题：不清空，原答案留着，下面接一段分隔 + 补充内容。"""
        block = f"\n\n——— 追问：{question} ———\n\n"
        self._cur_a += block
        self._stick_bottom = True
        if self._view is None:
            self.answer.moveCursor(self.answer.textCursor().MoveOperation.End)
            self.answer.insertPlainText(block)
            self._scroll_bottom()

    def set_status(self, text):
        """标题栏右侧的一行淡色状态提示（识别中… / 这句不用答）。"""
        self.status.setText(text)

    def show_warning(self, text):
        """通道异常这类告警要让人看见，不能只丢在角落。

        还没出过题 → 直接写在答案区（那里最显眼，反正是空的）；
        已经在答题了 → 只更新状态行，别把人家正在看的答案冲掉。
        """
        self.set_status("⚠ 音频通道异常")
        if not self._cur_q:
            self.set_answer("⚠ " + text)


def _placeholder(widget):
    """深色背景下 placeholder 默认是黑字，改成半透明白才看得清。"""
    pal = widget.palette()
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(255, 255, 255, 90))
    widget.setPalette(pal)


class SetupWindow(QWidget):
    """面试前的设置窗：岗位名 / 公司名（选填）/ JD，加一个「导入简历」按钮。

    目标：第一次用的人**不用读文档**就能跑起来 ——
    启动先自检（缺什么、怎么补都写在界面上），缺简历就当场导一份。
    """

    start_requested = pyqtSignal(str, str, str)  # (岗位名, jd, 公司名)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("面试助手")
        self.resize(520, 660)
        self._build_ui()
        self.run_preflight()

    def _label(self, text):
        lbl = QLabel(text)
        lbl.setObjectName("fieldLbl")
        return lbl

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(8)

        title = QLabel("面试助手 · 新建一场")
        title.setObjectName("setupTitle")
        layout.addWidget(title)

        # 启动自检：缺什么、怎么补
        self.preflight_box = QLabel("")
        self.preflight_box.setObjectName("preflight")
        self.preflight_box.setWordWrap(True)
        layout.addWidget(self.preflight_box)

        layout.addWidget(self._label("岗位名称"))
        self.job_input = QLineEdit()
        self.job_input.setObjectName("inp")
        self.job_input.setPlaceholderText("如：腾讯AI-HR / AI产品经理")
        _placeholder(self.job_input)
        layout.addWidget(self.job_input)

        layout.addWidget(self._label("公司名称（选填，只用来让答案提到对的名字）"))
        self.company_input = QLineEdit()
        self.company_input.setObjectName("inp")
        self.company_input.setPlaceholderText("如：腾讯")
        _placeholder(self.company_input)
        layout.addWidget(self.company_input)

        layout.addWidget(self._label("岗位 JD（粘贴全文；留空则用 jd.txt）"))
        self.jd_input = QTextEdit()
        self.jd_input.setObjectName("jdInput")
        self.jd_input.setPlaceholderText("粘贴 JD 全文…")
        _placeholder(self.jd_input)
        layout.addWidget(self.jd_input, 1)

        # 素材：这是这工具的价值来源，所以把入口直接放在界面上
        row = QHBoxLayout()
        self.import_btn = QPushButton("从简历 PDF 导入素材")
        self.import_btn.setObjectName("ghostBtn")
        self.import_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.import_btn.clicked.connect(self._on_import)
        row.addWidget(self.import_btn)
        self.import_tip = QLabel("")
        self.import_tip.setObjectName("importTip")
        self.import_tip.setWordWrap(True)
        row.addWidget(self.import_tip, 1)
        layout.addLayout(row)

        self.start_btn = QPushButton("开始面试")
        self.start_btn.setObjectName("startBtn")
        self.start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_btn.clicked.connect(self._on_start)
        layout.addWidget(self.start_btn)

        self._apply_style()

    # —— 启动自检 ——

    def run_preflight(self):
        """把缺的东西一次性列清楚。有 error 就不让点开始（点了也是白崩）。"""
        items = preflight.check()
        icons = {"error": "❌", "warn": "⚠️", "info": "🎧"}
        lines, blocked = [], False
        for it in items:
            lines.append(f"{icons.get(it['level'], '•')} {it['title']}")
            if it.get("fix"):
                lines.append(f"     {it['fix']}")
            if it["level"] == "error":
                blocked = True
        self.preflight_box.setText("\n".join(lines))
        self.preflight_box.setVisible(bool(lines))
        self.start_btn.setEnabled(not blocked)
        self.start_btn.setToolTip("先解决上面标 ❌ 的问题" if blocked else "")

    # —— 导入简历 ——

    def _on_import(self):
        path, _ = QFileDialog.getOpenFileName(self, "选你的简历 PDF", "", "PDF 文件 (*.pdf)")
        if not path:
            return
        ok, msg = extract_to_materials(path)
        self.import_tip.setText(("✅ " if ok else "❌ ") + msg)
        self.import_tip.setStyleSheet(
            "color: %s; font-size: 12px;" % ("#6ee7a0" if ok else "#ff8a8a"))
        if ok:
            self.run_preflight()   # 素材那条警告应该消失了

    def _on_start(self):
        job = self.job_input.text().strip() or "模拟面试"
        company = self.company_input.text().strip()
        jd = self.jd_input.toPlainText().strip()
        self.start_requested.emit(job, jd, company)

    def _apply_style(self):
        self.setStyleSheet("""
            QWidget { background: #17181c; }
            #setupTitle { color: white; font-size: 16px; font-weight: 600; }
            #fieldLbl { color: rgba(255,255,255,0.6); font-size: 12px; }
            #preflight {
                color: rgba(255,255,255,0.8); font-size: 12px;
                background: rgba(255,255,255,0.05); border-radius: 8px;
                padding: 10px 12px; line-height: 17px;
            }
            #importTip { color: rgba(255,255,255,0.6); font-size: 12px; }
            QLineEdit#inp {
                color: white; background: #22242a;
                border: 1px solid rgba(255,255,255,0.12); border-radius: 8px;
                padding: 8px 12px; font-size: 14px;
            }
            QLineEdit#inp:focus { border-color: rgba(255,255,255,0.35); }
            QTextEdit#jdInput {
                color: white; background: #22242a;
                border: 1px solid rgba(255,255,255,0.12); border-radius: 8px;
                padding: 8px 12px; font-size: 13px;
            }
            QTextEdit#jdInput:focus { border-color: rgba(255,255,255,0.35); }
            #ghostBtn {
                color: rgba(255,255,255,0.85); background: rgba(255,255,255,0.08);
                border: 1px solid rgba(255,255,255,0.15); border-radius: 8px;
                padding: 8px 14px; font-size: 13px;
            }
            #ghostBtn:hover { background: rgba(255,255,255,0.15); }
            #startBtn {
                color: white; background: #0a84ff; border: none;
                border-radius: 8px; padding: 10px 0; font-size: 15px; font-weight: 600;
            }
            #startBtn:hover { background: #2b94ff; }
            #startBtn:pressed { background: #0a74e0; }
            #startBtn:disabled { background: #33363d; color: rgba(255,255,255,0.35); }
        """)

    def closeEvent(self, e):
        """设置窗是程序主窗口，点 ✕ 关闭 = 退出整个程序。"""
        QApplication.quit()
        super().closeEvent(e)
