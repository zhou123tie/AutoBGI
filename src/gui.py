# -*- coding: utf-8 -*-
"""
图形界面层。

界面分四个页：
  基础设置  —— BetterGI 路径、一条龙配置、每日时间、时间窗
  自动运行  —— 安装/卸载计划任务、唤醒需登录、开机自启
  唤醒测试  —— 设 N 分钟后自动睡眠并唤醒、查看唤醒来源
  运行日志  —— 实时输出 + 打开日志目录

风格：左侧深色导航栏 + 右侧浅色卡片，扁平无边框、圆角强调色。
"""

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

import core
from core import APP_NAME, APP_VERSION, Config

# ---------------------------------------------------------------------------
# 配色（浅色 + 紫粉可爱风）
# ---------------------------------------------------------------------------
BG = "#F6F3FC"          # 页面底：极淡薰衣草
CARD = "#FFFFFF"
SIDEBAR_TOP = "#8B6FD4"   # 侧栏渐变上：柔紫
SIDEBAR_BOT = "#E88FB8"   # 侧栏渐变下：粉
SIDEBAR = "#8B6FD4"
SIDEBAR_HOVER = "#7A5FC4"
SIDEBAR_ACTIVE = "#FFFFFF"
TEXT = "#3B3355"
TEXT_DIM = "#8B84A3"
BORDER = "#EBE4F7"
ACCENT = "#9B7BE0"      # 紫
ACCENT_DARK = "#7C5CC7"
ACCENT_SOFT = "#F0E9FE"  # 淡紫底
PINK = "#F287B8"
PINK_SOFT = "#FDEBF3"
SUCCESS = "#48C9A9"
SUCCESS_SOFT = "#E6F9F3"
WARN = "#F5B544"
WARN_SOFT = "#FEF4E3"
DANGER = "#F07777"
DANGER_SOFT = "#FDEDED"
DARK_PANEL = "#3A3350"   # 日志终端底

FONT = "Microsoft YaHei UI"
FONT_FALLBACK = ("Microsoft YaHei UI", "微软雅黑", "Segoe UI", "Tahoma")


def pick_family():
    """挑一个系统里存在的中文字体。

    必须在 Tk 根窗口创建之后调用（tkfont.families 依赖 default root）。
    """
    try:
        import tkinter.font as tkfont
        fams = set(tkfont.families())
        for f in FONT_FALLBACK:
            if f in fams:
                return f
    except Exception:
        pass
    return "TkDefaultFont"


UI_FONT = "Microsoft YaHei UI"

# 侧栏导航图标（用字符以免依赖图片资源）
_NAV_GLYPH = {
    "base": "⚙",    # 基础设置
    "auto": "⏰",   # 自动运行
    "wake": "☾",   # 唤醒测试
    "log": "☷",    # 运行日志
}

_ICON_CACHE = {}


def load_icon_bytes(name="app.png"):
    """读取内嵌图标字节。

    打包成 onefile 后 PyInstaller 会把 datas 释放到 sys._MEIPASS，
    开发时则从源码目录读。
    """
    if name in _ICON_CACHE:
        return _ICON_CACHE[name]
    data = None
    base = getattr(sys, "_MEIPASS", None)
    cands = []
    if base:
        cands.append(os.path.join(base, name))
    cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), name))
    cands.append(os.path.join(core.app_dir(), name))
    for p in cands:
        try:
            if os.path.isfile(p):
                with open(p, "rb") as fh:
                    data = fh.read()
                break
        except OSError:
            continue
    _ICON_CACHE[name] = data
    return data


def load_photo(name="app.png", size=44):
    """把图标读成 PhotoImage（已缩放到 size）。没有资源时返回 None。"""
    key = (name, size)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    raw = load_icon_bytes(name)
    img = None
    if raw:
        try:
            import io
            from PIL import Image, ImageTk
            im = Image.open(io.BytesIO(raw)).convert("RGBA")
            im = im.resize((size, size), Image.LANCZOS)
            img = ImageTk.PhotoImage(im)
        except Exception:
            img = None
    if img is None:
        try:
            img = tk.PhotoImage(data=raw) if raw else None
        except Exception:
            img = None
    _ICON_CACHE[key] = img
    return img


# ---------------------------------------------------------------------------
# 基础控件
# ---------------------------------------------------------------------------

def make_font(size=10, weight="normal"):
    return (UI_FONT, size, weight)


def _shade(hex_color, delta):
    """把 #RRGGBB 变亮/变暗 delta。"""
    try:
        h = hex_color.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        r = max(0, min(255, r + delta))
        g = max(0, min(255, g + delta))
        b = max(0, min(255, b + delta))
        return "#%02X%02X%02X" % (r, g, b)
    except Exception:
        return hex_color


def _short_path(p, keep=26):
    """把长路径缩成侧栏放得下的形式（单行）。

    优先把 %LOCALAPPDATA% / %APPDATA% / %USERPROFILE% 换成变量名
    （既短又好认），仍超长才省略中间层 —— 省略时**必须保留变量前缀**，
    否则 `\\…\\AutoBGI\\x.ini` 根本看不出在哪。
    """
    if not p:
        return ""
    p = str(p).replace("/", "\\")
    var = ""
    for name in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        base = os.environ.get(name, "")
        if base and p.lower().startswith(base.lower()):
            p = "%" + name + "%" + p[len(base):]
            var = "%" + name + "%"
            break
    if len(p) <= keep:
        return p
    rest = p[len(var):] if var else p
    drive, rest2 = os.path.splitdrive(rest)
    segs = [s for s in rest2.split("\\") if s]
    head = var or drive
    if len(segs) >= 2:
        out = "%s\\…\\%s\\%s" % (head, segs[-2], segs[-1])
    else:
        out = "%s\\…\\%s" % (head, segs[-1] if segs else rest)
    return out.lstrip("\\") if out.startswith("\\") else out


def _short_path_lines(p):
    """拆成 1~2 行短文本：目录一行、文件名一行（侧栏窄，一行放不下）。

    返回 [目录行] 或 [目录行, 文件名]。
    """
    if not p:
        return [""]
    p = str(p).replace("/", "\\")
    var = ""
    for name in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        base = os.environ.get(name, "")
        if base and p.lower().startswith(base.lower()):
            var = "%" + name + "%"
            p = var + p[len(base):]
            break

    head, tail = os.path.split(p)
    if not head:
        return [p]
    if len(head) <= 26:
        return [head + "\\", tail]

    # 目录太长：保留变量/盘符 + 末两段
    if var:
        segs = [s for s in head[len(var):].split("\\") if s]
        pre = var
    else:
        drive, rest = os.path.splitdrive(head)
        segs = [s for s in rest.split("\\") if s]
        pre = drive
    if len(segs) >= 2:
        return ["%s\\…\\%s\\" % (pre, segs[-1]), tail]
    if segs:
        return ["%s\\%s\\" % (pre, segs[-1]), tail]
    return [head + "\\", tail]


def _mix(c1, c2, t):
    """在两个 #RRGGBB 之间插值，t=0 取 c1，t=1 取 c2。"""
    try:
        a, b = c1.lstrip("#"), c2.lstrip("#")
        out = ""
        for i in (0, 2, 4):
            v0, v1 = int(a[i:i + 2], 16), int(b[i:i + 2], 16)
            out += "%02X" % max(0, min(255, int(round(v0 + (v1 - v0) * t))))
        return "#" + out
    except Exception:
        return c1


class Rounded(tk.Canvas):
    """圆角容器（tkinter 无原生圆角，用 Canvas 自绘）。

    直接继承 Canvas —— 这样控件自身的请求高度就是圆角矩形的高度，
    父级 pack/grid 拿到的 reqheight 正确，不会塌成 1px。
    内容放进 `self.content`（Canvas 上的一个窗口项）。
    """

    def __init__(self, master, bg=CARD, radius=14, border=None,
                 padx=0, pady=0, width=None, height=None, **kw):
        tk.Canvas.__init__(self, master, highlightthickness=0, bd=0,
                           bg=master.cget("bg"),
                           width=width or 1, height=height or 40, **kw)
        self._radius = radius
        self._fill = bg
        self._border = border
        self._padx = padx
        self._pady = pady
        self._req_w = width
        self._req_h = height

        self.content = tk.Frame(self, bg=bg)
        # Canvas 上的窗口项：anchor=nw 且不指定 width/height 时，
        # Frame 会按自身请求尺寸显示；我们在 _redraw 里同步尺寸。
        self._win = self.create_window(2, 2, window=self.content, anchor="nw")
        self.content.bind("<Configure>", self._on_resize)
        self.bind("<Configure>", self._on_resize)
        self._ticks = 0
        self._stable = 0
        self._poll()

    def _poll(self):
        """内容分层添加，Configure 可能早于子控件就位 —— 反复对齐直到稳定。"""
        if not self.winfo_exists():
            return
        self._ticks += 1
        prev = (self.winfo_reqwidth(), self.winfo_reqheight())
        self._on_resize()
        now = (self.winfo_reqwidth(), self.winfo_reqheight())
        # 连续 3 次尺寸不变就认为稳定（但至少跑 8 次，覆盖首次布局）
        if self._ticks > 8 and now == prev:
            self._stable += 1
            if self._stable >= 3:
                return
        else:
            self._stable = 0
        if self._ticks > 200:
            return
        try:
            self.after(35, self._poll)
        except Exception:
            pass

    def _on_resize(self, _e=None):
        ch = max(1, self.content.winfo_reqheight() + self._pady * 2)
        want_h = self._req_h if self._req_h else (ch + 4)
        if abs(self.winfo_reqheight() - want_h) > 0:
            self.configure(height=want_h)
        # 宽度：外层已分配 > 内容请求 > 显式要求 > 兜底
        w = self.winfo_width()
        if w <= 1:
            w = self.content.winfo_reqwidth()
        if w <= 1 and self._req_w:
            w = self._req_w
        if not w or w <= 1:
            w = 40
        self._redraw(w, want_h, ch)

    def _redraw(self, w, h, ch):
        if w <= 1 or h <= 1:
            return
        # 只删背景图形（tag="bg"）。注意：delete("all") 会把 create_window
        # 创建的内容项一起删掉，导致内容整个消失 —— 必须用 tag 精确删除。
        self.delete("bg")
        r = max(0, min(self._radius, h // 2, w // 2))
        if r == 0:
            self.create_rectangle(0, 0, w, h, fill=self._fill, outline="",
                                  tags="bg")
        else:
            d = r * 2
            self.create_oval(0, 0, d, d, fill=self._fill, outline="", tags="bg")
            self.create_oval(w - d, 0, w, d, fill=self._fill, outline="",
                             tags="bg")
            self.create_oval(0, h - d, d, h, fill=self._fill, outline="",
                             tags="bg")
            self.create_oval(w - d, h - d, w, h, fill=self._fill, outline="",
                             tags="bg")
            self.create_rectangle(r, 0, w - r, h, fill=self._fill, outline="",
                                  tags="bg")
            self.create_rectangle(0, r, w, h - r, fill=self._fill, outline="",
                                  tags="bg")
            if self._border:
                for box in ((0, 0, d, d), (w - d, 0, w, d),
                            (0, h - d, d, h), (w - d, h - d, w, h)):
                    self.create_oval(box[0], box[1], box[2], box[3],
                                     outline=self._border, width=1, tags="bg")
                self.create_line(r, 0, w - r, 0, fill=self._border, width=1,
                                 tags="bg")
                self.create_line(r, h - 1, w - r, h - 1, fill=self._border,
                                 width=1, tags="bg")
                self.create_line(0, r, 0, h - r, fill=self._border, width=1,
                                 tags="bg")
                self.create_line(w - 1, r, w - 1, h - r, fill=self._border,
                                 width=1, tags="bg")
        self.itemconfigure(self._win, width=max(1, w - 4), height=max(1, ch))
        self.coords(self._win, 2, 2)
        # 背景压到最底层，内容在上
        self.tag_lower("bg")

    def set_fill(self, color):
        self._fill = color
        self.content.config(bg=color)
        self._on_resize()


class Card(Rounded):
    """白色圆角卡片。宽度由父级 pack(fill="x") 决定。"""

    def __init__(self, master, **kw):
        kw.setdefault("bg", CARD)
        kw.setdefault("border", BORDER)
        kw.setdefault("radius", 14)
        kw.setdefault("width", 560)
        Rounded.__init__(self, master, **kw)


class SoftButton(Rounded):
    """圆角胶囊按钮。

    交互反馈（用户反馈"点了不确定有没有触发"）：
    - 悬停变色、**按下变深**（即时触感）
    - `begin_busy()` 进入"处理中…"动画态并禁止重复点击
    - `flash_ok()` / `flash_err()` 短暂变色告知结果
    """

    def __init__(self, master, text, command=None, bg=ACCENT, fg="#FFFFFF",
                 hover=None, padx=18, pady=8, size=10, bold=False,
                 radius=None, cursor="hand2", width=None, **kw):
        self._bg = bg
        self._hover = hover or _shade(bg, -22)
        self._press = _shade(bg, -38)
        self._fg = fg
        self._cmd = command
        self._size = size
        self._bold = bold
        self._enabled = True
        self._text = text
        self._busy = False
        self._bphase = 0

        # Canvas 不像 Frame 那样自动按内容撑开，宽度得自己算
        if width is None:
            fnt = tkfont.Font(family=UI_FONT, size=size,
                              weight="bold" if bold else "normal")
            width = fnt.measure(text) + padx * 2 + 8
        want_h = tkfont.Font(family=UI_FONT, size=size).metrics("linespace") \
            + pady * 2 + 6

        Rounded.__init__(self, master, bg=bg,
                         radius=radius if radius is not None else 999,
                         width=width, height=want_h, **kw)
        self.lbl = tk.Label(self.content, text=text, bg=bg, fg=fg,
                            font=make_font(size, "bold" if bold else "normal"),
                            cursor=cursor)
        self.lbl.pack(padx=padx, pady=pady)
        self.configure(cursor=cursor)
        for w in (self, self.content, self.lbl):
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)
            w.bind("<ButtonPress-1>", self._on_press)
            w.bind("<ButtonRelease-1>", self._on_release)

    def _on_enter(self, _e=None):
        if self._enabled and not self._busy:
            self.set_fill(self._hover)

    def _on_leave(self, _e=None):
        if self._enabled and not self._busy:
            self.set_fill(self._bg)

    def _on_press(self, _e=None):
        if self._enabled and not self._busy:
            self.set_fill(self._press)

    def _on_release(self, _e=None):
        if not self._enabled or self._busy:
            return
        self.set_fill(self._hover)
        if self._cmd:
            self._cmd()

    def set_text(self, text):
        self._text = text
        if not self._busy:
            self.lbl.config(text=text)

    # -- 状态反馈 ---------------------------------------------------------
    def begin_busy(self, text="处理中"):
        """进入处理中状态：禁止点击 + 动态省略号。"""
        self._busy = True
        self._enabled = False
        self._busy_text = text
        self._bphase = 0
        self.configure(cursor="watch")
        self.lbl.config(cursor="watch")
        self.set_fill(_mix(self._bg, "#FFFFFF", 0.45))
        self.lbl.config(fg=self._fg)
        self._anim()

    def end_busy(self):
        """退出处理中状态，恢复原样。"""
        self._busy = False
        self._enabled = True
        self.configure(cursor="hand2")
        self.lbl.config(cursor="hand2")
        self.set_fill(self._bg)
        self.lbl.config(fg=self._fg)
        self.lbl.config(text=self._text)

    def _anim(self):
        if not self._busy:
            return
        dots = "." * (self._bphase % 4)
        self.lbl.config(text="%s%s" % (self._busy_text, dots))
        self._bphase += 1
        try:
            self.after(280, self._anim)
        except Exception:
            pass

    def flash(self, kind="ok", ms=1400):
        """短暂变色提示结果（ok=绿 / err=红）。"""
        color = "#3FAF6B" if kind == "ok" else "#D95757"
        self.set_fill(color)
        self.lbl.config(fg="#FFFFFF")
        if kind == "ok" and not self._busy:
            self.lbl.config(text="✓ " + self._text)

        def restore():
            if self._busy:
                return
            self.set_fill(self._bg)
            self.lbl.config(fg=self._fg)
            self.lbl.config(text=self._text)
        try:
            self.after(ms, restore)
        except Exception:
            pass

    def set_enabled(self, flag):
        if self._busy:
            return
        self._enabled = flag
        c = "hand2" if flag else "arrow"
        self.configure(cursor=c)
        self.lbl.config(cursor=c)
        if flag:
            self.set_fill(self._bg)
            self.lbl.config(fg=self._fg)
        else:
            self.set_fill(_mix(self._bg, BG, 0.6))
            self.lbl.config(fg=_mix(self._fg, "#FFFFFF", 0.45))


# 兼容旧调用名
FlatButton = SoftButton


class Field(tk.Frame):
    """一行：标签 + 圆角输入框（可选浏览按钮）。"""

    def __init__(self, master, label, var, hint="", width=52, browse=None,
                 browse_text="浏览..."):
        tk.Frame.__init__(self, master, bg=CARD)
        tk.Label(self, text=label, bg=CARD, fg=TEXT, font=make_font(10),
                 anchor="w").grid(row=0, column=0, sticky="w", pady=(0, 7))
        holder = tk.Frame(self, bg=CARD)
        holder.grid(row=1, column=0, sticky="ew")
        holder.grid_columnconfigure(0, weight=1)

        box = Rounded(holder, bg="#FCFBFF", radius=10, border=BORDER,
                      width=460)
        box.grid(row=0, column=0, sticky="ew")
        self.entry = tk.Entry(box.content, textvariable=var,
                              font=make_font(10),
                              relief="flat", bd=0, highlightthickness=0,
                              bg="#FCFBFF", fg=TEXT, width=width,
                              justify="left")
        self.entry.pack(padx=12, pady=9, fill="both", expand=True)
        self._box = box

        if browse:
            SoftButton(holder, browse_text, command=browse,
                       bg=ACCENT_SOFT, fg=ACCENT_DARK, hover="#E2D6FB",
                       padx=13, pady=8, size=9).grid(row=0, column=1,
                                                       padx=(9, 0))
        if hint:
            tk.Label(self, text=hint, bg=CARD, fg=TEXT_DIM,
                     font=make_font(9), anchor="w").grid(row=2, column=0,
                                                          sticky="w", pady=(6, 0))


class StatusDot(tk.Frame):
    """状态指示灯（带柔和光晕）+ 说明文字。"""

    def __init__(self, master, text="", color=SUCCESS):
        tk.Frame.__init__(self, master, bg=CARD)
        self.dot = tk.Canvas(self, width=18, height=18, bg=CARD,
                             highlightthickness=0)
        self._color = color
        self.label = tk.Label(self, text=text, bg=CARD, fg=TEXT_DIM,
                              font=make_font(10), anchor="w")
        self.label.pack(side="left", padx=(3, 0))
        self._draw()

    def _draw(self):
        self.dot.delete("all")
        self.dot.create_oval(1, 1, 17, 17,
                             fill=_mix(self._color, "#FFFFFF", 0.78),
                             outline="")
        self.dot.create_oval(6, 6, 12, 12, fill=self._color, outline="")

    def set(self, text, color=SUCCESS):
        self.label.config(text=text)
        self._color = color
        self._draw()


class _NavItem(tk.Frame):
    """侧栏导航项：已废弃。侧栏改为整块 Canvas 自绘（见 App._paint_sidebar）。"""

    def __init__(self, master, text, glyph, command):
        tk.Frame.__init__(self, master, bg=SIDEBAR_TOP, height=42)
        self._cmd = command
        self._active = False
        self.canvas = self
        self.glyph = None
        self.text = None
        self.pack_forget()

    def _layout(self):
        return

    def _paint(self, hover=False):
        return

    def set_active(self, flag):
        self._active = flag


class _SlimScrollbar(tk.Canvas):
    """细圆角滚动条（自绘，不用 ttk 主题，深色底上也能配）。"""

    WIDTH = 10

    def __init__(self, master, bg, command):
        tk.Canvas.__init__(self, master, width=self.WIDTH, bg=bg,
                           highlightthickness=0, bd=0)
        self._command = command
        self._first = 0.0
        self._last = 1.0
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)

    def set(self, first, last):
        self._first, self._last = float(first), float(last)
        self._draw()

    def _draw(self):
        self.delete("all")
        h = self.winfo_height()
        if h <= 1:
            return
        span = max(1e-6, self._last - self._first)
        track = h - 8
        y = 4 + track * self._first
        lh = max(28, track * span)
        self._thumb_y = y
        self._thumb_h = lh
        r = self.WIDTH // 2
        self.create_oval(0, y, self.WIDTH, y + lh,
                         fill=self._thumb(), outline="")

    def _thumb(self):
        return "#CFC0EA"

    def _on_press(self, e):
        h = self.winfo_height()
        if h <= 1:
            return
        track = h - 8
        y = e.y - 4
        # 点在滑块上 -> 记住抓取偏移；点在轨道上 -> 居中跳过去
        ty = getattr(self, "_thumb_y", 0)
        th = getattr(self, "_thumb_h", 28)
        if ty <= e.y <= ty + th:
            self._grab = e.y - ty
        else:
            self._grab = th / 2.0
            self._scroll_to(y - self._grab)

    def _scroll_to(self, y):
        h = self.winfo_height()
        track = max(1, h - 8)
        frac = (y + getattr(self, "_thumb_h", 28) / 2.0) / track
        frac = min(1.0, max(0.0, frac))
        # yview 语义：moveto 传的是"顶部比例"，换算到 canvas 坐标
        last = max(1e-6, self._last)
        self._command("moveto", min(1.0, max(0.0, frac * last)))

    def _on_drag(self, e):
        h = self.winfo_height()
        if h <= 1:
            return
        y = e.y - 4 - getattr(self, "_grab", 0)
        self._scroll_to(y)


class SoftCheck(tk.Frame):
    """圆角勾选项：左侧是自绘的圆角方框（选中变紫 + 打勾）。"""

    def __init__(self, master, text, var, bg=CARD, size=10):
        tk.Frame.__init__(self, master, bg=bg, cursor="hand2")
        self._var = var
        self.box = tk.Canvas(self, width=20, height=20, bg=bg,
                             highlightthickness=0, cursor="hand2")
        self.box.pack(side="left", pady=1)
        self.lbl = tk.Label(self, text=text, bg=bg, fg=TEXT,
                            font=make_font(size), anchor="w", cursor="hand2")
        self.lbl.pack(side="left", padx=(10, 0))
        for w in (self, self.box, self.lbl):
            w.bind("<Button-1>", self._toggle)
        self._draw()
        self.bind("<Configure>", lambda e: self._draw())
        # ★ 必须监听变量变化。
        #   之前只在「点击」和「尺寸变化」时重画 —— 于是**由代码**改
        #   `var.set(True)`（比如按配置初始化、或从别的控件同步）时，
        #   方框外观不会更新，看起来"明明启用了却是个空框"
        #   （2026-10-05 在「游戏与顺序」卡片上实测踩到）。
        try:
            var.trace_add("write", lambda *a: self._draw())
        except Exception:  # noqa: BLE001
            pass

    def _toggle(self, _e=None):
        self._var.set(not self._var.get())
        self._draw()

    def _draw(self):
        c = self.box
        c.delete("all")
        on = bool(self._var.get())
        c.create_oval(1, 1, 19, 19,
                      fill=ACCENT if on else "#FFFFFF",
                      outline=ACCENT if on else BORDER,
                      width=1)
        if on:
            c.create_line(5, 10, 9, 14, fill="#FFFFFF", width=2)
            c.create_line(9, 14, 15, 6, fill="#FFFFFF", width=2)


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        global UI_FONT
        tk.Tk.__init__(self)
        UI_FONT = pick_family()
        self.title("%s  v%s" % (APP_NAME, APP_VERSION))
        self.configure(bg=BG)
        self.geometry("1000x680")
        self.minsize(920, 640)

        self.cfg = Config()
        self.msg_q = queue.Queue()
        self.ui_q = queue.Queue()
        self.busy = False
        self._win_dpi()

        self.vars = {
            "BgiDir": tk.StringVar(value=self.cfg.get("BgiDir")),
            "OneDragonConfig": tk.StringVar(value=self.cfg.get("OneDragonConfig")),
            "TaskName": tk.StringVar(value=self.cfg.get("TaskName")),
            "TaskTime": tk.StringVar(value=self.cfg.get("TaskTime")),
            "WindowStart": tk.StringVar(value=self.cfg.get("WindowStart")),
            "WindowEnd": tk.StringVar(value=self.cfg.get("WindowEnd")),
            "WaitWindowSec": tk.StringVar(value=self.cfg.get("WaitWindowSec")),
            "VerifySec": tk.StringVar(value=self.cfg.get("VerifySec")),
            "RandomMinutes": tk.StringVar(
                value=self.cfg.get("RandomMinutes") or "30"),
            # 接力鸣潮
            "OkwwDir": tk.StringVar(value=self.cfg.get("OkwwDir")),
            "OkwwTask": tk.StringVar(
                value=self.cfg.get("OkwwTask") or "DailyTask"),
            "WuwaExe": tk.StringVar(value=self.cfg.get("WuwaExe")),
            "GenshinExe": tk.StringVar(value=self.cfg.get("GenshinExe")),
            "OneDragonTimeoutMin": tk.StringVar(
                value=self.cfg.get("OneDragonTimeoutMin") or "300"),
            "wake_minutes": tk.StringVar(value="2"),
        }
        # 两个开关用布尔变量
        self.run_okww_var = tk.BooleanVar(value=self.cfg.run_okww)
        self.kill_genshin_var = tk.BooleanVar(value=self.cfg.kill_genshin)

        self._build()
        self.after(120, self._drain)
        self.after(60, self._ensure_visible)
        self.after(300, self._refresh_stages)
        self.after(400, self._refresh_status)
        self.after(700, self._rebind_all_wheels)
        self.after(150, self._anim_tick)
        self.after(900, self._startup_checks)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _ensure_visible(self):
        """确保窗口真的出现在屏幕可见范围内。

        打包后从**某些启动方式**（快捷方式、父进程带着 SW_HIDE /
        SW_SHOWMINIMIZED、或 Windows 记住了"上次是最小化"）启动时，
        Tk 主窗口可能继承到隐藏/最小化的显示状态 ——
        用户双击图标后屏幕上什么也没有，会以为"程序打不开/没内容"。
        这里强制 deiconify + 拉回主屏，并兜住"窗口跑到屏幕外"的情况。
        """
        try:
            self.deiconify()
            self.update_idletasks()
            self.lift()
        except Exception:
            pass
        try:
            x, y = self.winfo_x(), self.winfo_y()
            w, h = self.winfo_width(), self.winfo_height()
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
            if w < 200 or h < 200:
                w, h = 1000, 680
            off = (x < -100 or y < -100 or x > sw - 150 or y > sh - 100
                   or x + w < 100 or y + h < 100)
            if off:
                nx = max(0, (sw - w) // 2)
                ny = max(0, (sh - h) // 3)
                self.geometry("%dx%d+%d+%d" % (w, h, nx, ny))
        except Exception:
            pass

    def _startup_checks(self):
        """启动自检：配置能不能写进去。

        这一项极其重要 —— 配置写不进去时，"安装任务"永远装的是旧设置，
        用户只看到"点了没反应"。必须在**一打开**就告诉他，
        而不是等他折腾半天（2026-10-04 事故就是这么来的）。
        """
        try:
            ok, msg = self.cfg.save()
            if ok:
                self.log("配置可写：%s" % msg, "ok")
            else:
                self.log("=" * 46, "err")
                self.log("✗ 严重问题：配置文件无法写入！", "err")
                self.log("  %s" % msg, "err")
                self.log("  后果：你在界面上改的任何设置都不会生效，"
                         "安装任务也会用旧设置。", "err")
                self.log("  原因：程序所在目录（如 C:\\ProgramData）不允许"
                         "普通用户修改已存在的文件。", "err")
                self.log("  解决：把本程序连同文件夹复制到「文档」或"
                         "「桌面」等普通目录，从那里运行。", "err")
                self.log("=" * 46, "err")
                self._status_set("⚠ 配置无法保存，改动不会生效！", False, None)
                self.stat_text.config(fg="#C94F4F")
        except Exception as exc:  # noqa: BLE001
            self.log("启动自检异常：%s" % exc, "warn")

    # -- 外观 ---------------------------------------------------------------
    def _win_dpi(self):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        try:
            self.iconbitmap(default="")
        except Exception:
            pass

    def _build(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._sidebar()
        self._content()

    def _sidebar(self):
        """侧栏：整块渐变画布，标题与导航项全部直接画在 Canvas 上。

        不用 Frame 叠加（tkinter 的 Frame 不支持真透明，压在渐变上会露底色），
        自绘还能精确控制圆角药丸与间距。
        """
        bar = tk.Frame(self, bg=SIDEBAR_TOP, width=200)
        bar.grid(row=0, column=0, sticky="nsew")
        bar.grid_propagate(False)

        c = tk.Canvas(bar, width=200, highlightthickness=0, bd=0,
                      bg=SIDEBAR_TOP, cursor="hand2")
        c.pack(fill="both", expand=True)
        self._nav = c
        self._hover_key = None
        self._current = "base"
        self._nav_bounds = {}
        self._pages = [("base", "基础设置"), ("auto", "自动运行"),
                       ("wake", "唤醒测试"), ("log", "运行日志")]
        self._logo = load_photo("app.png", 46)

        c.bind("<Configure>", lambda e: self._paint_sidebar(e.width, e.height))
        c.bind("<Button-1>", self._sidebar_click)
        c.bind("<Motion>", self._sidebar_motion)
        c.bind("<Leave>", self._sidebar_leave)
        self._paint_sidebar(200, 680)

    def _sidebar_motion(self, e):
        key = self._nav_hit(e.x, e.y)
        if key != self._hover_key:
            self._hover_key = key
            self._paint_sidebar(self._nav.winfo_width(),
                                self._nav.winfo_height())

    def _sidebar_leave(self, _e=None):
        if self._hover_key:
            self._hover_key = None
            self._paint_sidebar(self._nav.winfo_width(),
                                self._nav.winfo_height())

    def _nav_hit(self, x, y):
        for key, (x0, y0, x1, y1) in self._nav_bounds.items():
            if x0 <= x <= x1 and y0 <= y <= y1:
                return key
        return None

    def _sidebar_click(self, e):
        key = self._nav_hit(e.x, e.y)
        if key:
            self.show_page(key)

    def _paint_sidebar(self, w, h):
        if w <= 1 or h <= 1:
            return
        c = self._nav
        c.delete("all")

        band = 3
        for i in range(0, h, band):
            t = i / float(max(1, h - 1))
            c.create_rectangle(0, i, w, min(h, i + band),
                               fill=_mix(SIDEBAR_TOP, SIDEBAR_BOT, t),
                               outline="")
        c.create_oval(-70, -130, w + 70, 40,
                      fill=_mix(SIDEBAR_TOP, "#FFFFFF", 0.14), outline="")
        c.create_oval(-70, h - 50, w + 70, h + 140,
                      fill=_mix(SIDEBAR_BOT, "#FFFFFF", 0.12), outline="")

        if self._logo is not None:
            c.create_image(28, 38, image=self._logo, anchor="nw")
        c.create_text(84, 50, text=APP_NAME, anchor="w",
                      fill="#FFFFFF", font=make_font(12, "bold"))
        c.create_text(84, 72, text="v%s" % APP_VERSION, anchor="w",
                      fill="#F6E7FC", font=make_font(9))

        self._nav_bounds = {}
        y = 110
        r = 21
        for key, text in self._pages:
            top, hgt = y, 42
            active = (key == self._current)
            if active:
                fill = "#FFFFFF"
            elif self._hover_key == key:
                fill = _mix(SIDEBAR_TOP, "#FFFFFF", 0.22)
            else:
                fill = None
            if fill:
                c.create_oval(12, top, 12 + 2 * r, top + 2 * r,
                              fill=fill, outline="")
                c.create_oval(w - 12 - 2 * r, top, w - 12, top + 2 * r,
                              fill=fill, outline="")
                c.create_rectangle(12 + r, top, w - 12 - r, top + 2 * r,
                                   fill=fill, outline="")
            c.create_text(31, top + hgt // 2,
                          text=_NAV_GLYPH.get(key, "*"), anchor="center",
                          fill=ACCENT_DARK if active else "#F2E2FB",
                          font=make_font(12))
            c.create_text(50, top + hgt // 2, text=text, anchor="w",
                          fill=SIDEBAR_TOP if active else "#FBF1FE",
                          font=make_font(10, "bold" if active else "normal"))
            self._nav_bounds[key] = (12, top, w - 12, top + hgt)
            y += hgt + 4

        # 底部：配置 / 日志位置（用户才知道去哪找、改哪个文件）。
        # 注意 1：侧栏是 Canvas 自绘的，这里画的才真正可见 ——
        #        加在 bar 里的 Frame 会被 Canvas（expand=True）挤成 0 高度。
        # 注意 2：必须**自下而上**排，否则行数一多就画到画布外面看不见。
        cfg_lines = _short_path_lines(core.config_path())
        log_lines = _short_path_lines(core.log_dir())
        row = [("配置文件", True)]
        row += [(ln, False) for ln in cfg_lines]
        row += [("", False)]
        row += [("日志", True)]
        row += [(ln, False) for ln in log_lines]
        y = h - 12
        for text, bold in reversed(row):
            if text:
                c.create_text(18, y, anchor="sw", text=text,
                              fill="#EFDCFA" if bold else "#F7E9FD",
                              font=make_font(8 if bold else 7,
                                             "bold" if bold else "normal"))
            y -= 13

    def _paint_grad_dead(self, w, h):
        return
        for key, text, glyph in (("base", "基础设置", "\u2691"),
                                 ("auto", "自动运行", "\u23F0"),
                                 ("wake", "唤醒测试", "\u263E"),
                                 ("log", "运行日志", "\u2637")):
            item = _NavItem(nav, text, glyph,
                            lambda k=key: self.show_page(k))
            item.pack(fill="x", pady=2)
            self.nav_items.append((key, item))

        # 底部小字：显示**完整路径**，用户才知道该改哪个文件
        foot = tk.Frame(bar, bg="")
        foot.pack(side="bottom", fill="x", padx=18, pady=18)
        tk.Label(foot, text="配置文件：\n%s" % core.config_path(),
                 bg="", fg="#F0E4F6", font=make_font(8),
                 anchor="w", justify="left", wraplength=170).pack(anchor="w")
        tk.Label(foot, text="日志：\n%s" % core.log_dir(),
                 bg="", fg="#D9C6E6", font=make_font(8),
                 anchor="w", justify="left", wraplength=170).pack(anchor="w",
                                                                 pady=(6, 0))

    def _paint_grad(self, w, h):
        return

    def _page_titles(self):
        return {"base": "基础设置", "auto": "自动运行",
                "wake": "唤醒测试", "log": "运行日志"}

    # -- 右侧内容 -----------------------------------------------------------
    def _content(self):
        right = tk.Frame(self, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=1)
        right.grid_rowconfigure(2, weight=0)

        self.pages = {}
        for key in ("base", "auto", "wake", "log"):
            holder = tk.Frame(right, bg=BG)
            holder.grid(row=0, column=0, sticky="nsew")
            self.pages[key] = holder

        # 分隔线 + 底部状态条：任何后台操作都在这里显示进度
        tk.Frame(right, bg="#EAE3F2", height=1).grid(
            row=1, column=0, sticky="ew")
        self._status_strip(right)

        self._page_base()
        self._page_auto()
        self._page_wake()
        self._page_log()
        self.show_page("base")

    def _status_strip(self, parent):
        """底部状态条：文字 + 不确定进度条（忙碌时来回跑）。

        用户反馈"点了按钮不知道有没有反应、也不知道进行到哪"，
        这里给一个**始终可见**的统一反馈区。
        """
        bar = tk.Frame(parent, bg="#F7F4FC", height=38)
        bar.grid(row=2, column=0, sticky="ew")
        bar.grid_propagate(False)
        bar.grid_columnconfigure(1, weight=1)

        self._stat_dot = tk.Canvas(bar, width=14, height=14, bg="#F7F4FC",
                                   highlightthickness=0)
        self._stat_dot.grid(row=0, column=0, padx=(18, 8), pady=12)
        self._dot_item = self._stat_dot.create_oval(
            2, 2, 12, 12, fill="#B9A9CC", outline="")

        self.stat_text = tk.Label(bar, text="就绪", bg="#F7F4FC",
                                  fg="#6B5B7B", font=make_font(9),
                                  anchor="w")
        self.stat_text.grid(row=0, column=1, sticky="ew")

        # 不确定进度条：一条轨道 + 一段来回滑动的色块
        self._pg = tk.Canvas(bar, width=170, height=6, bg="#F7F4FC",
                             highlightthickness=0)
        self._pg.grid(row=0, column=2, padx=(10, 6))
        self._pg.create_rectangle(0, 0, 170, 6, fill="#E7DFF2", outline="")
        self._pg_bar = self._pg.create_rectangle(
            -60, 0, 0, 6, fill=ACCENT, outline="")
        self._pg.grid_remove()          # 空闲时隐藏

        self._stat_phase = 0
        self._stat_busy = False
        self._stat_progress = None      # (cur, total) 或 None

    def _status_set(self, text, busy=None, progress=None):
        """更新底部状态条（必须在主线程调用）。"""
        try:
            self.stat_text.config(text=text)
            if busy is not None:
                self._stat_busy = bool(busy)
                self._stat_dot.itemconfig(
                    self._dot_item,
                    fill="#6BBF8A" if busy else "#B9A9CC")
            self._stat_progress = progress
            if self._stat_busy:
                if not self._pg.winfo_ismapped():
                    self._pg.grid()
            else:
                if self._pg.winfo_ismapped():
                    self._pg.grid_remove()
        except Exception:
            pass

    def _anim_tick(self):
        """统一动画驱动：进度条来回跑 + 状态点呼吸。"""
        try:
            if self._stat_busy:
                self._stat_phase = (self._stat_phase + 1) % 100
                span = 170.0
                seg = 60.0
                # 三角波，让色块来回滑动
                t = self._stat_phase / 100.0
                x = (2 * t) if t <= 0.5 else (2 * (1 - t))
                x0 = x * (span - seg) - 0
                self._pg.coords(self._pg_bar, x0, 0, x0 + seg, 6)
                shade = 0.5 + 0.5 * abs(0.5 - t) * 2
                self._stat_dot.itemconfig(
                    self._dot_item,
                    fill="#6BBF8A" if shade > 0.6 else "#8FD3A6")
        except Exception:
            pass
        self.after(110, self._anim_tick)

    def show_page(self, key):
        self.current = key
        self._current = key
        for k, w in self.pages.items():
            if k == key:
                w.grid(row=0, column=0, sticky="nsew")
            else:
                w.grid_forget()
        try:
            self._paint_sidebar(self._nav.winfo_width(),
                                self._nav.winfo_height())
        except Exception:
            pass
        # 页面切换后补绑滚轮（新建的卡片/输入框也要能滚）
        self.after(120, self._rebind_all_wheels)

    # -- 页 1：基础设置 -----------------------------------------------------
    def _page_base(self):
        p = self.pages["base"]
        scroll = self._scroll(p)

        c1 = Card(scroll)
        c1.pack(fill="x", pady=(0, 16))
        inner = tk.Frame(c1.content, bg=CARD)
        inner.pack(fill="x", padx=26, pady=24)
        inner.grid_columnconfigure(0, weight=1)

        self._card_title(inner, 0, "\u2691", "BetterGI",
                         "指定 BetterGI 的安装目录；点下方「自动检测」通常无需手填。")

        Field(inner, "安装目录", self.vars["BgiDir"],
              hint="例：D:\\BetterGI",
              browse=self._pick_dir).grid(row=2, column=0, sticky="ew")

        self.detect_line = tk.Frame(inner, bg=CARD)
        self.detect_line.grid(row=3, column=0, sticky="w", pady=(14, 0))
        self.btn_detect = SoftButton(self.detect_line, "自动检测",
                                     command=self._detect,
                                     bg=ACCENT_SOFT, fg=ACCENT_DARK,
                                     hover="#E2D6FB", padx=16, pady=8, size=9)
        self.btn_detect.pack(side="left")
        self.btn_pick = SoftButton(self.detect_line, "选择 exe 文件",
                                   command=self._pick_exe,
                                   bg=ACCENT_SOFT, fg=ACCENT_DARK,
                                   hover="#E2D6FB", padx=16, pady=8, size=9)
        self.btn_pick.pack(side="left", padx=(10, 0))

        c2 = Card(scroll)
        c2.pack(fill="x", pady=(0, 16))
        inner2 = tk.Frame(c2.content, bg=CARD)
        inner2.pack(fill="x", padx=26, pady=24)
        inner2.grid_columnconfigure(0, weight=1)

        self._card_title(inner2, 0, "\u2726", "一条龙",
                         "要自动执行的配置名，必须与 BetterGI 里「一条龙」"
                         "页面中的名字完全一致。")

        od_row = tk.Frame(inner2, bg=CARD)
        od_row.grid(row=2, column=0, sticky="ew")
        od_row.grid_columnconfigure(0, weight=1)

        self.od_box = Rounded(od_row, bg="#FCFBFF", radius=10, border=BORDER)
        self.od_box.grid(row=0, column=0, sticky="ew")
        self.od_entry = tk.Entry(self.od_box.content,
                                 textvariable=self.vars["OneDragonConfig"],
                                 font=make_font(10), relief="flat", bd=0,
                                 highlightthickness=0, bg="#FCFBFF", fg=TEXT,
                                 justify="left")
        self.od_entry.pack(side="left", padx=(12, 4), pady=9, fill="y")
        self.od_arrow = tk.Label(self.od_box.content, text="\u25be",
                                 bg="#FCFBFF", fg=TEXT_DIM,
                                 font=make_font(10), cursor="hand2", width=3)
        self.od_arrow.pack(side="left", padx=(0, 8), pady=9, fill="y")
        self.od_arrow.bind("<Button-1>", lambda _e: self._show_od_menu())
        self.od_entry.bind("<Button-1>",
                           lambda _e: self.after(50, self._show_od_menu))
        self._od_names = []
        self.od_menu = tk.Menu(self, tearoff=0, font=make_font(10),
                               bg="#FBF8FF", fg=TEXT, relief="flat", bd=0,
                               activebackground=ACCENT_SOFT,
                               activeforeground=ACCENT_DARK, borderwidth=1)

        SoftButton(od_row, "重新读取列表", command=self._reload_od,
                   bg=PINK_SOFT, fg="#C75A8E", hover="#F9DCEA",
                   padx=14, pady=8, size=9).grid(row=0, column=1, padx=(10, 0))
        tk.Label(inner2, text="下拉框里的名字直接从你电脑的 BetterGI 读取，"
                              "选错就会启动失败。",
                 bg=CARD, fg=TEXT_DIM, font=make_font(9),
                 anchor="w").grid(row=3, column=0, sticky="w", pady=(11, 0))

        c3 = Card(scroll)
        c3.pack(fill="x", pady=(0, 24))
        inner3 = tk.Frame(c3.content, bg=CARD)
        inner3.pack(fill="x", padx=26, pady=24)
        inner3.grid_columnconfigure(0, weight=1)

        self._card_title(inner3, 0, "\u2638", "等待与容错",
                         "机器慢时把两个秒数调大一些，能避免误判失败。")
        Field(inner3, "等主窗口出现的秒数", self.vars["WaitWindowSec"],
              hint="冷启动 BetterGI 较慢时调大，默认 180").grid(
                  row=1, column=0, sticky="ew", pady=(0, 14))
        Field(inner3, "校验一条龙已启动的秒数", self.vars["VerifySec"],
              hint="等待 BGI 日志出现「一条龙任务执行」，默认 420").grid(
                  row=2, column=0, sticky="ew")

        tk.Frame(scroll, bg=BG, height=8).pack()

    def _card_title(self, parent, row, glyph, title, subtitle):
        """卡片标题：圆角小徽标 + 标题 + 说明。"""
        head = tk.Frame(parent, bg=CARD)
        head.grid(row=row, column=0, sticky="ew", pady=(0, 18))
        badge = tk.Canvas(head, width=30, height=30, bg=CARD,
                          highlightthickness=0)
        badge.pack(side="left")
        badge.create_oval(1, 1, 29, 29, fill=ACCENT_SOFT, outline="")
        badge.create_text(15, 15, text=glyph, fill=ACCENT_DARK,
                          font=make_font(12))
        box = tk.Frame(head, bg=CARD)
        box.pack(side="left", padx=(11, 0))
        tk.Label(box, text=title, bg=CARD, fg=TEXT, font=make_font(13, "bold"),
                 anchor="w").pack(anchor="w")
        tk.Label(box, text=subtitle, bg=CARD, fg=TEXT_DIM, font=make_font(9),
                 anchor="w", justify="left").pack(anchor="w", pady=(3, 0))

    def _scroll(self, parent):
        """可滚动容器：Canvas + 圆角胶囊滚动条 + 鼠标滚轮。

        坑：卡片（Rounded）和输入框都是 Canvas / Entry，会各自截获
        <MouseWheel> 事件，滚轮停在它们上面时页面不滚。
        解决：给内部 Frame 树里的每个控件都绑同一个 handler，
        并给只读控件（Text/Entry）额外处理。
        """
        holder = tk.Frame(parent, bg=BG)
        holder.pack(fill="both", expand=True)

        canvas = tk.Canvas(holder, bg=BG, highlightthickness=0)
        sb = _SlimScrollbar(holder, bg=BG, command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)

        def _on_resize(_e=None):
            canvas.itemconfigure(win, width=canvas.winfo_width())
        inner.bind("<Configure>",
                   lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", _on_resize)

        def _wheel(e):
            # 每次滚 3 行，Windows 上单格 delta 120 太小，手感偏慢
            step = 3 if abs(e.delta) >= 120 else 1
            canvas.yview_scroll(-step if e.delta > 0 else step, "units")
            return "break"

        self._bind_wheel(inner, _wheel)
        canvas.bind("<MouseWheel>", _wheel)

        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y", pady=(10, 10))
        canvas._inner = inner
        inner._canvas = canvas          # 供 _scroll_to_widget 使用
        return inner

    def _scroll_to_widget(self, w):
        """把可滚动页面滚到某个控件的位置。"""
        try:
            top = w
            cv = None
            while top is not None:
                cv = getattr(top, "_canvas", None)
                if cv is not None:
                    break
                top = top.master
            if cv is None:
                return
            inner = cv._inner
            inner.update_idletasks()
            total = max(1, inner.winfo_height())
            # 累加各级的 y 偏移，得到控件在 inner 坐标系里的位置
            y = w.winfo_y()
            cur = w.master
            while cur is not None and cur is not inner:
                y += cur.winfo_y()
                cur = cur.master
            frac = max(0.0, min(1.0, (y - 16) / float(total)))
            cv.yview_moveto(frac)
        except Exception:
            pass

    def _bind_wheel(self, widget, handler, depth=0):
        """递归给一棵控件树绑滚轮（Canvas 会吞事件，必须逐个绑）。"""
        if depth > 8:
            return
        for w in (widget,):
            try:
                w.bind("<MouseWheel>", handler)
                w.bind("<Button-4>", handler)
                w.bind("<Button-5>", handler)
            except Exception:
                pass
        for child in widget.winfo_children():
            self._bind_wheel(child, handler, depth + 1)

    def _rebind_all_wheels(self, _e=None):
        """页面切换后，把新出现的控件也绑上滚轮。"""
        try:
            for key in ("base", "auto", "wake"):
                holder = self.pages.get(key)
                if not holder:
                    continue
                for c in holder.winfo_children():
                    for c2 in c.winfo_children():
                        if c2.winfo_class() == "Canvas" and \
                                hasattr(c2, "_inner"):
                            def mk(cv):
                                def h(e, _cv=cv):
                                    step = 3 if abs(e.delta) >= 120 else 1
                                    _cv.yview_scroll(
                                        -step if e.delta > 0 else step, "units")
                                    return "break"
                                return h
                            self._bind_wheel(c2._inner, mk(c2))
        except Exception:
            pass

    # -- 运行链路总览条 -----------------------------------------------------
    def _chain_banner(self, parent):
        """自动运行页顶部的「运行链路」总览条。

        为什么要有：接力鸣潮的设置卡在页面靠下的位置，不滚动根本看不到，
        用户会以为"根本没这个功能"。这里用一条常驻横幅把链路状态
        摆到最显眼的地方，并可一键滚到设置卡。
        """
        wrap = Rounded(parent, bg="#F4EEFC", radius=14, border="#E0D3F2")
        wrap.pack(fill="x", pady=(0, 16))
        box = tk.Frame(wrap.content, bg="#F4EEFC")
        box.pack(fill="x", padx=20, pady=14)
        box.grid_columnconfigure(1, weight=1)

        icon = tk.Canvas(box, width=26, height=26, bg="#F4EEFC",
                         highlightthickness=0)
        icon.grid(row=0, column=0, rowspan=2, padx=(0, 12))
        icon.create_oval(2, 2, 24, 24, fill=ACCENT_SOFT, outline="")
        icon.create_text(13, 13, text="\U0001F517", font=make_font(11))

        tk.Label(box, text="运行链路", bg="#F4EEFC", fg=ACCENT_DARK,
                 font=make_font(11, "bold"), anchor="w").grid(
                     row=0, column=1, sticky="w")
        self.chain_sub = tk.Label(box, text="原神一条龙 → 鸣潮日常",
                                  bg="#F4EEFC", fg="#6B5B7B",
                                  font=make_font(9), anchor="w")
        self.chain_sub.grid(row=1, column=1, sticky="w", pady=(2, 0))

        self.chain_badge = tk.Label(box, text="读取中…", bg="#E7DFF2",
                                    fg="#6B5B7B", font=make_font(9, "bold"),
                                    padx=12, pady=4)
        self.chain_badge.grid(row=0, column=2, rowspan=2, padx=(10, 12))

        SoftButton(box, "去设置", command=self._goto_chain_card,
                   bg=ACCENT, padx=14, pady=7, size=9).grid(
                       row=0, column=3, rowspan=2)
        self._refresh_chain_banner()

    def _goto_chain_card(self):
        w = getattr(self, "chain_card", None)
        if w is not None:
            self._scroll_to_widget(w)

    def keep_on_cfg(self):
        """读配置里的「显示器保持常亮」开关（默认关）。"""
        try:
            return str(self.cfg.get("KeepScreenOn")).strip() in (
                "1", "true", "True", "yes")
        except Exception:  # noqa: BLE001
            return False

    def min_others_cfg(self):
        """读配置里的「开跑时最小化其它窗口」开关（默认开）。"""
        try:
            v = str(self.cfg.get("MinimizeOthers")).strip()
            return v in ("1", "true", "True", "yes")
        except Exception:  # noqa: BLE001
            return True

    def dismiss_float_cfg(self):
        """读配置里的「开跑时关闭开始菜单」开关（默认开）。"""
        try:
            v = str(self.cfg.get("DismissShellFloat")).strip()
            return v in ("1", "true", "True", "yes")
        except Exception:  # noqa: BLE001
            return True

    def okww_gui_cfg(self):
        """读配置里的「跑鸣潮时显示 ok-ww 界面」开关（默认开）。"""
        try:
            v = str(self.cfg.get("OkwwGuiMode")).strip()
            return v in ("1", "true", "True", "yes")
        except Exception:  # noqa: BLE001
            return True

    def _admin_hint_text(self):
        on = core.runasadmin_enabled()
        now = core.is_admin()
        if on and now:
            return ("已设置，且当前就是管理员 —— 一切操作都不再需要提权。")
        if on and not now:
            return ("已设置。**关掉本程序重新打开**即可生效"
                    "（之后不会再有任何提权提示）。")
        return ("勾上之后，双击图标就自动带管理员权限启动："
                "「安装任务」「立即接力」都不再需要提权。\n"
                "为什么需要：ok-ww 启动 PC 版鸣潮强制要求管理员身份，"
                "而临时提权靠 UAC 弹窗 —— 你这台机器 UAC 设为「不提示」，"
                "点了也看不到任何反应；若改成默认级别，凌晨又没人能点。")

    def _set_admin_flag(self):
        """写入/移除「以管理员身份运行」标记。"""
        want = bool(self.admin_var.get())
        ok = core.set_runasadmin(want)
        now = core.runasadmin_enabled()
        if not ok or now != want:
            self.log("✗ 写入「以管理员身份运行」标记失败。", "err")
            messagebox.showwarning(
                "设置失败",
                "没能修改注册表里的兼容性标记。\n\n"
                "可以手动设置：右键本程序 →「属性」→「兼容性」→\n"
                "勾选「以管理员身份运行」。")
            return
        self.log("已%s「始终以管理员身份运行」标记。%s"
                 % ("设置" if want else "取消",
                    "请关闭并重新打开本程序使其生效。" if want else ""),
                 "ok")
        try:
            self.admin_hint.config(text=self._admin_hint_text())
        except Exception:  # noqa: BLE001
            pass

    # -- 游戏与执行顺序 -----------------------------------------------------
    def _refresh_stages(self):
        """按 StageOrder 刷新四行的勾选状态、顺序号与就绪状态。"""
        if getattr(self, "_stage_refreshing", False):
            return
        self._stage_refreshing = True
        try:
            snap = {d["id"]: d for d in core.stage_snapshot(self.cfg)}
            order = core.stage_order(self.cfg)
            for sid, w in self.stage_rows.items():
                d = snap[sid]
                if bool(w["var"].get()) != d["enabled"]:
                    w["var"].set(d["enabled"])
                parts = []
                if d["enabled"]:
                    parts.append("第 %d 位" % d["order"])
                if d["ready"]:
                    parts.append("\u2713 已就绪")
                elif d["dir"]:
                    parts.append("\u2717 目录里没有主程序")
                else:
                    parts.append("\u2717 未安装")
                w["stat"].config(
                    text="　".join(parts),
                    fg="#2E7D52" if d["ready"] else
                    ("#D08700" if d["enabled"] else TEXT_DIM))
            self.order_lbl.config(
                text=("执行顺序：" + " → ".join(core.STAGE_MAP[i]["label"]
                                              for i in order))
                if order else
                "⚠ 一个游戏都没启用，任务跑起来也不会做任何事")
            miss = [snap[i] for i in order if not snap[i]["ready"]]
            if miss:
                txt = []
                for d in miss:
                    s = core.STAGE_MAP[d["id"]]
                    txt.append("%s —— 需要 %s\n    下载：%s\n    安装：%s"
                               % (s["label"], s["tool"], s["home"], s["howto"]))
                self.stage_hint.config(
                    text="已启用、但还没找到工具的：\n" + "\n".join(txt),
                    fg="#D08700")
            else:
                self.stage_hint.config(
                    text="已启用的游戏都找到工具了，可以正常运行。"
                         "（崩铁 / 绝区零 想启用的话，先装好它们的开源工具，"
                         "再在这里勾上并调好顺序。）",
                    fg="#2E7D52")
        finally:
            self._stage_refreshing = False

    def _stage_toggle(self, sid):
        """勾选 / 取消某个游戏（等于把它加入或移出执行顺序）。"""
        if getattr(self, "_stage_refreshing", False):
            return
        order = core.stage_order(self.cfg)
        want = bool(self.stage_rows[sid]["var"].get())
        if want and sid not in order:
            order.append(sid)
        elif not want and sid in order:
            order.remove(sid)
        core.set_stage_order(self.cfg, order)
        self._refresh_stages()
        self._refresh_chain_banner()

    def _stage_move(self, sid, delta):
        """把某个游戏在顺序里上移 / 下移一位。"""
        order = core.stage_order(self.cfg)
        if sid not in order:
            order.append(sid)
        i = order.index(sid)
        j = i + delta
        if 0 <= j < len(order):
            order[i], order[j] = order[j], order[i]
        core.set_stage_order(self.cfg, order)
        self._refresh_stages()

    def _stage_set_path(self, sid):
        """给某个游戏指定它那条龙工具的安装目录。"""
        s = core.STAGE_MAP[sid]
        d = filedialog.askdirectory(
            title="选择「%s」的安装目录 —— 里面应该有 %s"
                  % (s["tool"], " 或 ".join(s["exe_names"])))
        if not d:
            return
        d = os.path.normpath(d)
        hit = [n for n in s["exe_names"]
               if os.path.isfile(os.path.join(d, n))]
        if not hit and not messagebox.askyesno(
                "没找到主程序",
                "在\n%s\n里没找到 %s。\n\n"
                "如果这是 %s 的目录，它的主程序应该在里面。\n"
                "仍然保存这个路径吗？"
                % (d, " 或 ".join(s["exe_names"]), s["tool"])):
            return
        self.cfg.set(s["dir_key"], d)
        ok, msg = self.cfg.save()
        if ok:
            self.log("「%s」工具路径已设为：%s" % (s["label"], d), "ok")
        else:
            self.log("✗ 路径保存失败（改动没生效）：%s" % msg, "err")
        self._refresh_stages()

    def _stage_detect_all(self):
        """自动探测全部四个游戏工具的安装位置。"""
        def work(emit):
            emit("自动检测各游戏工具…", "info", (1, 2))
            got = 0
            for s in core.STAGES:
                try:
                    p = core.stage_tool_path(self.cfg, s, deep=True)
                except Exception as exc:  # noqa: BLE001
                    p = ""
                    emit("  检测 %s 时出错：%s" % (s["label"], exc), "warn")
                if p:
                    got += 1
                    emit("✓ %s：%s" % (s["label"], p), "ok")
                else:
                    emit("✗ %s：没找到 %s" % (s["label"], s["tool"]), "warn")
                    emit("     下载：%s" % s["home"])
                    emit("     装好后点这一行的「设置路径」指给它。")
            self.ui(self._refresh_stages)
            emit("检测完成：%d/%d 个工具已就绪。" % (got, len(core.STAGES)),
                 "info", (2, 2))
        self._bg(work, title="自动检测工具", btn=self.btn_stage_detect)

    def _stage_test_menu(self):
        """选一个游戏来测试启动。"""
        win = tk.Toplevel(self)
        win.title("测试启动哪个游戏？")
        win.configure(bg=CARD)
        try:
            win.transient(self)
            win.attributes("-topmost", True)
        except Exception:  # noqa: BLE001
            pass
        tk.Label(win, text="选一个游戏，测试它能不能按当前配置正常启动：",
                 bg=CARD, fg=TEXT, font=make_font(10)).pack(
                     padx=22, pady=(16, 10))
        for s in core.STAGES:
            SoftButton(win, "%s（%s）" % (s["label"], s["tool"]),
                       command=lambda x=s["id"]: (win.destroy(),
                                                  self._stage_test_launch(x)),
                       bg=BG, fg=TEXT, hover="#EEE8F8",
                       padx=14, pady=6, size=9).pack(padx=22, pady=3,
                                                     fill="x")
        SoftButton(win, "取消", command=win.destroy, bg=BG, fg=TEXT_DIM,
                   hover="#EEE8F8", padx=14, pady=6, size=9).pack(
                       padx=22, pady=(10, 18), fill="x")
        win.update_idletasks()
        win.geometry("+%d+%d" % (self.winfo_rootx() + 180,
                                 self.winfo_rooty() + 180))

    def _stage_test_launch(self, sid):
        """真启动一次，验证路径和参数对不对。"""
        s = core.STAGE_MAP[sid]
        p = core.stage_tool_path(self.cfg, s)
        if not p:
            messagebox.showwarning(
                "还没找到工具",
                "没找到 %s 的主程序。\n\n"
                "先点这一行的「设置路径」指定它的安装目录。\n\n"
                "下载：%s\n安装：%s" % (s["tool"], s["home"], s["howto"]))
            return
        argv = core.stage_argv(self.cfg, s, p)
        pretty = " ".join('"%s"' % a if " " in a else a for a in argv)
        if not messagebox.askyesno(
                "测试启动「%s」" % s["label"],
                "将执行：\n%s\n\n"
                "这会真的启动该工具——对应游戏的任务可能立刻开始跑。\n\n"
                "继续吗？" % pretty):
            return

        def work(emit):
            emit("测试启动：%s" % pretty, "info", (1, 2))
            try:
                core.subprocess.Popen(
                    argv, cwd=os.path.dirname(p),
                    creationflags=getattr(
                        core.subprocess, "DETACHED_PROCESS", 0)
                    | getattr(core.subprocess,
                              "CREATE_NEW_PROCESS_GROUP", 0))
                emit("已发出启动命令。工具正常的话，它自己的窗口会出现。",
                     "ok", (2, 2))
                emit("（如果什么都没出现，检查：路径对不对、"
                     "是否需要管理员权限、绝区零是否装在含中文或空格的目录。）")
            except Exception as exc:  # noqa: BLE001
                emit("✗ 启动失败：%s" % exc, "err", (2, 2))
        self._bg(work, title="测试启动", btn=self.btn_stage_test)

    def _refresh_chain_banner(self):
        """刷新顶部链路总览（按实际的「游戏与顺序」显示）。"""
        try:
            order = core.stage_order(self.cfg)
            if order:
                self.chain_badge.config(text="已启用", bg="#D6F0E0",
                                        fg="#2E7D52")
                short = {"genshin": "原神", "wuwa": "鸣潮",
                         "starrail": "崩铁", "zzz": "绝区零"}
                self.chain_sub.config(
                    text=" → ".join(short.get(i, i) for i in order))
            else:
                self.chain_badge.config(text="未启用", bg="#EFE9F7",
                                        fg="#8A7B99")
                self.chain_sub.config(text="一个游戏都没启用")
        except Exception:
            pass

    # -- 页 2：自动运行 -----------------------------------------------------
    def _page_auto(self):
        p = self.pages["auto"]
        scroll = self._scroll(p)

        # 顶部「运行链路」总览 —— 让人一眼看到整条链路配了没
        self._chain_banner(scroll)

        c1 = Card(scroll)
        c1.pack(fill="x", pady=(0, 16))
        i1 = tk.Frame(c1.content, bg=CARD)
        i1.pack(fill="x", padx=26, pady=24)
        i1.grid_columnconfigure(0, weight=1)

        head1 = tk.Frame(i1, bg=CARD)
        head1.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        badge = tk.Canvas(head1, width=30, height=30, bg=CARD,
                          highlightthickness=0)
        badge.pack(side="left")
        badge.create_oval(1, 1, 29, 29, fill=PINK_SOFT, outline="")
        badge.create_text(15, 15, text="\u23F0", fill="#C75A8E",
                          font=make_font(12))
        tbox = tk.Frame(head1, bg=CARD)
        tbox.pack(side="left", padx=(11, 0))
        tk.Label(tbox, text="每日定时任务", bg=CARD, fg=TEXT,
                 font=make_font(13, "bold"), anchor="w").pack(anchor="w")
        tk.Label(tbox, text="到点时若电脑在睡眠，会先把电脑唤醒再执行；"
                            "若已开机则直接启动。",
                 bg=CARD, fg=TEXT_DIM, font=make_font(9),
                 anchor="w").pack(anchor="w", pady=(3, 0))
        self.task_status = StatusDot(i1, "正在检查…", WARN)
        self.task_status.grid(row=0, column=1, sticky="ne", padx=(12, 0))

        for lbl, var, hint, row in (
            ("运行时间", "TaskTime",
             "24 小时制，例 03:30。实际触发会在此基础上加随机误差", 2),
            ("任务名称", "TaskName", "同一台电脑装多套时可改名区分", 4),
        ):
            Field(i1, lbl, self.vars[var], hint=hint).grid(
                row=row, column=0, sticky="ew", pady=(0, 14))

        # ---- 随机误差 ----
        rd = tk.Frame(i1, bg=CARD)
        rd.grid(row=3, column=0, sticky="ew", pady=(0, 16))
        hd = tk.Frame(rd, bg=CARD)
        hd.pack(fill="x")
        tk.Label(hd, text="随机误差上限", bg=CARD, fg=TEXT,
                 font=make_font(10), anchor="w").pack(side="left")
        tk.Label(hd, text="（分钟，0 = 每天固定时刻）", bg=CARD, fg=TEXT_DIM,
                 font=make_font(9)).pack(side="left", padx=(6, 0))
        rr = tk.Frame(rd, bg=CARD)
        rr.pack(anchor="w", pady=(8, 0))
        bx = Rounded(rr, bg="#FCFBFF", radius=9, border=BORDER, width=74)
        bx.pack(side="left")
        tk.Entry(bx.content, textvariable=self.vars["RandomMinutes"],
                 font=make_font(10), relief="flat", bd=0,
                 highlightthickness=0, bg="#FCFBFF", fg=TEXT,
                 width=6, justify="center").pack(padx=8, pady=7)
        tk.Label(rr, text="分钟", bg=CARD, fg=TEXT_DIM,
                 font=make_font(9)).pack(side="left", padx=(7, 0))
        for txt, val in (("不随机", "0"), ("15", "15"), ("30", "30"),
                         ("60", "60")):
            SoftButton(rr, txt,
                       command=lambda v=val: self._set_random(v),
                       bg=ACCENT_SOFT, fg=ACCENT_DARK, hover="#E2D6FB",
                       padx=11, pady=6, size=9).pack(side="left",
                                                     padx=(6, 0))
        tk.Label(rd, text="到点后会在「运行时间 ~ 运行时间+该分钟数」之间随机触发，"
                          "避免每天都卡在同一秒。默认 30 分钟。",
                 bg=CARD, fg=TEXT_DIM, font=make_font(9), anchor="w",
                 justify="left", wraplength=580).pack(anchor="w",
                                                       pady=(9, 0))

        tw = tk.Frame(i1, bg=CARD)
        tw.grid(row=5, column=0, sticky="ew", pady=(0, 4))
        tk.Label(tw, text="允许运行的时间窗（防误触发）", bg=CARD, fg=TEXT,
                 font=make_font(10), anchor="w").pack(anchor="w")
        row2 = tk.Frame(tw, bg=CARD)
        row2.pack(anchor="w", pady=(8, 0))
        # 默认不限制：任务时间本身就够精确，第二道闸门反而容易打架
        for var in ("WindowStart", "WindowEnd"):
            if not self.vars[var].get().strip():
                self.vars[var].set("0000")
        for text, var in (("从", "WindowStart"), ("到", "WindowEnd")):
            tk.Label(row2, text=text, bg=CARD, fg=TEXT_DIM,
                     font=make_font(10)).pack(side="left", padx=(0, 7))
            bx = Rounded(row2, bg="#FCFBFF", radius=9, border=BORDER,
                         width=74)
            bx.pack(side="left")
            tk.Entry(bx.content, textvariable=self.vars[var],
                     font=make_font(10), relief="flat", bd=0,
                     highlightthickness=0, bg="#FCFBFF", fg=TEXT,
                     width=6, justify="center").pack(padx=8, pady=7)
            if var == "WindowStart":
                tk.Label(row2, text="\u2192", bg=CARD, fg=TEXT_DIM,
                         font=make_font(10)).pack(side="left", padx=8)
        SoftButton(row2, "不限制（推荐）", command=self._window_off,
                   bg=ACCENT_SOFT, fg=ACCENT_DARK, hover="#E2D6FB",
                   padx=13, pady=7, size=9).pack(side="left", padx=(14, 0))
        tk.Label(tw, text="平时不用改。留 0000-0000 = 不限制，完全按上面的"
                          "「运行时间」触发；只有担心白天误跑时才设一个窄窗。",
                 bg=CARD, fg=TEXT_DIM, font=make_font(9), anchor="w",
                 justify="left", wraplength=560).pack(anchor="w",
                                                       pady=(9, 0))

        # 「下次实际运行」提示 —— 让随机误差的结果可见
        self.next_run_lbl = tk.Label(i1, text="下次实际运行：计算中…",
                                     bg=CARD, fg=ACCENT_DARK,
                                     font=make_font(10, "bold"), anchor="w")
        self.next_run_lbl.grid(row=6, column=0, sticky="w", pady=(14, 0))
        # 系统里**真实**的设置（安装后回读，和上面的"期望值"对照看）
        self.real_run_lbl = tk.Label(
            i1, text="系统里真实的设置：点「验证是否生效」查看",
            bg=CARD, fg=TEXT_DIM, font=make_font(9), anchor="w")
        self.real_run_lbl.grid(row=7, column=0, sticky="w", pady=(4, 0))

        btns = tk.Frame(i1, bg=CARD)
        btns.grid(row=8, column=0, sticky="w", pady=(16, 0))
        self.btn_install = SoftButton(btns, "安装 / 更新任务",
                                      command=self._install_task,
                                      padx=22, pady=10, bold=True)
        self.btn_install.pack(side="left")
        self.btn_runonce = SoftButton(btns, "立即执行一次",
                                      command=self._run_task_now,
                                      bg=PINK, hover="#DB6BA0",
                                      padx=20, pady=10)
        self.btn_runonce.pack(side="left", padx=(11, 0))
        # 中止：一条龙可能卡在战斗里几小时（2026-10-05 凌晨实测），
        # 必须有办法一键停掉，而不是去 BetterGI 界面里找。
        self.btn_abort = SoftButton(btns, "中止本次执行",
                                    command=self._abort_run,
                                    bg=WARN_SOFT, fg="#B57A16",
                                    hover="#FBEBCB", padx=20, pady=10)
        self.btn_abort.pack(side="left", padx=(11, 0))
        self.btn_uninst = SoftButton(btns, "卸载任务",
                                     command=self._remove_task,
                                     bg=DANGER_SOFT, fg="#C94F4F",
                                     hover="#FADCDC", padx=20, pady=10)
        self.btn_uninst.pack(side="left", padx=(11, 0))

        # 「装了什么 / 到底有几个」—— 用户最常问的问题，做成显式入口
        c1b = Card(scroll)
        c1b.pack(fill="x", pady=(0, 16))
        i1b = tk.Frame(c1b.content, bg=CARD)
        i1b.pack(fill="x", padx=26, pady=24)
        i1b.grid_columnconfigure(0, weight=1)
        self._card_title(i1b, 0, "\u2315", "系统里到底装了什么",
                         "每次点「安装 / 更新任务」都是先删后建，不会叠加。"
                         "点下面的按钮可以把全盘扫一遍给你看。")
        row9 = tk.Frame(i1b, bg=CARD)
        row9.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.btn_survey = SoftButton(row9, "查看全部启动项",
                                     command=self._survey,
                                     bg=ACCENT, padx=18, pady=9, size=9)
        self.btn_survey.pack(side="left")
        self.btn_verify = SoftButton(row9, "验证是否生效",
                                     command=self._verify_task,
                                     bg=SUCCESS_SOFT, fg="#2E7D52",
                                     hover="#D6F0E0",
                                     padx=18, pady=9, size=9)
        self.btn_verify.pack(side="left", padx=(10, 0))
        self.btn_clean = SoftButton(row9, "清理残留测试任务",
                                    command=self._clean_wake_task,
                                    bg=WARN_SOFT, fg="#B57A16",
                                    hover="#FBEBCB",
                                    padx=18, pady=9, size=9)
        self.btn_clean.pack(side="left", padx=(10, 0))
        tk.Label(i1b, text="「清理残留测试任务」会删掉唤醒测试留下的 "
                           "AutoBGI_WakeTest_OneShot —— 它本该跑完自删，"
                           "但偶尔会残留并反复触发。",
                 bg=CARD, fg=TEXT_DIM, font=make_font(9), anchor="w",
                 justify="left", wraplength=580).grid(row=2, column=0,
                                                      sticky="w",
                                                      pady=(10, 0))

        # 「下次实际运行」也要随时间/随机值变化刷新
        self._refresh_next_run_hint()

        # ---- 游戏与执行顺序（多游戏一条龙）----
        # 这是总调度：勾选要跑哪些游戏、用 ↑↓ 定顺序。
        # **默认只有原神 + 鸣潮**；崩铁 / 绝区零装了对应开源工具再打开。
        c1d = Card(scroll)
        c1d.pack(fill="x", pady=(0, 16), before=c1b)
        self.order_card = c1d
        i1d = tk.Frame(c1d.content, bg=CARD)
        i1d.pack(fill="x", padx=26, pady=24)
        i1d.grid_columnconfigure(0, weight=1)
        self._card_title(
            i1d, 0, "\U0001F3AE", "游戏与执行顺序",
            "每天按这个顺序依次跑各游戏的「一条龙」。勾选框决定跑不跑，"
            "右侧 ↑↓ 调整先后。默认只启用原神 + 鸣潮。")

        self.order_lbl = tk.Label(i1d, text="", bg=CARD, fg=ACCENT_DARK,
                                  font=make_font(11, "bold"), anchor="w",
                                  justify="left", wraplength=580)
        self.order_lbl.grid(row=1, column=0, sticky="w", pady=(6, 14))

        self.stage_rows = {}
        for _i, _s in enumerate(core.STAGES):
            _sid = _s["id"]
            row = tk.Frame(i1d, bg=CARD)
            row.grid(row=2 + _i, column=0, sticky="ew", pady=(0, 8))
            row.grid_columnconfigure(2, weight=1)

            # 初始值直接按配置来，避免"创建后要等一次异步刷新才正确"
            var = tk.BooleanVar(value=(_sid in core.stage_order(self.cfg)))
            SoftCheck(row, _s["label"], var).grid(row=0, column=0, sticky="w")
            tk.Label(row, text=_s["tool"], bg=CARD, fg=TEXT_DIM,
                     font=make_font(9), anchor="w").grid(
                         row=0, column=1, sticky="w", padx=(10, 0))
            stat = tk.Label(row, text="", bg=CARD, fg=TEXT_DIM,
                            font=make_font(9), anchor="w")
            stat.grid(row=0, column=2, sticky="w", padx=(12, 0))
            SoftButton(row, "设置路径",
                       command=lambda x=_sid: self._stage_set_path(x),
                       bg=BG, fg=TEXT_DIM, hover="#EEE8F8",
                       padx=10, pady=4, size=9, radius=8).grid(
                           row=0, column=3, padx=(6, 0))
            SoftButton(row, "\u2191",
                       command=lambda x=_sid: self._stage_move(x, -1),
                       bg=BG, fg=TEXT_DIM, hover="#EEE8F8",
                       padx=9, pady=4, size=9, radius=8).grid(
                           row=0, column=4, padx=(6, 0))
            SoftButton(row, "\u2193",
                       command=lambda x=_sid: self._stage_move(x, 1),
                       bg=BG, fg=TEXT_DIM, hover="#EEE8F8",
                       padx=9, pady=4, size=9, radius=8).grid(
                           row=0, column=5, padx=(4, 0))
            self.stage_rows[_sid] = {"var": var, "stat": stat, "row": row,
                                     "label": _s["label"]}
            var.trace_add("write",
                          lambda *a, x=_sid: self._stage_toggle(x))

        _rn = 2 + len(core.STAGES)
        rowb2 = tk.Frame(i1d, bg=CARD)
        rowb2.grid(row=_rn, column=0, sticky="w", pady=(12, 0))
        self.btn_stage_detect = SoftButton(
            rowb2, "自动检测全部", command=self._stage_detect_all,
            bg=ACCENT_SOFT, fg=ACCENT_DARK, hover="#E2D6FB",
            padx=16, pady=8, size=9)
        self.btn_stage_detect.pack(side="left")
        self.btn_stage_test = SoftButton(
            rowb2, "测试启动…", command=self._stage_test_menu,
            bg=PINK, hover="#DB6BA0", padx=16, pady=8, size=9)
        self.btn_stage_test.pack(side="left", padx=(10, 0))

        self.stage_hint = tk.Label(i1d, text="", bg=CARD, fg=TEXT_DIM,
                                   font=make_font(9), anchor="w",
                                   justify="left", wraplength=580)
        self.stage_hint.grid(row=_rn + 1, column=0, sticky="w",
                             pady=(10, 0))

        # ---- 接力鸣潮 ----
        # before=c1b：把它提到「系统里到底装了什么」之前，
        # 这样它是第 2 张卡（不滚动也能看到大半），不会像以前那样
        # 埋在页面底部、用户根本不知道有这个功能。
        c1c = Card(scroll)
        c1c.pack(fill="x", pady=(0, 16), before=c1b)
        self.chain_card = c1c
        i1c = tk.Frame(c1c.content, bg=CARD)
        i1c.pack(fill="x", padx=26, pady=24)
        i1c.grid_columnconfigure(0, weight=1)
        self._card_title(
            i1c, 0, "\U0001F3AE", "接力鸣潮（Wuthering Waves）",
            "一条龙跑完后自动：关闭 BetterGI 和原神 → 交给 ok-ww 跑鸣潮日常。"
            "鸣潮由 ok-ww 自己拉起，本程序不另外开游戏。")

        SoftCheck(i1c, "启用接力：原神一条龙结束后自动切到鸣潮",
                  self.run_okww_var).grid(row=1, column=0, sticky="w",
                                          pady=(10, 0))
        # 勾选/取消时同步刷新顶部总览条
        self.run_okww_var.trace_add(
            "write", lambda *a: self._refresh_chain_banner())

        Field(i1c, "ok-ww 安装目录", self.vars["OkwwDir"],
              hint="ok-ww 所在文件夹（里面有 ok-ww.exe）",
              browse=self._pick_okww, browse_text="自动检测"
              ).grid(row=2, column=0, sticky="ew", pady=(14, 0))
        Field(i1c, "鸣潮任务名", self.vars["OkwwTask"],
              hint="鸣潮「日常」在 ok-ww 里叫 DailyTask，一般不用改"
              ).grid(row=3, column=0, sticky="ew", pady=(12, 0))

        # ok-ww 自己会拉起鸣潮 —— 这里只显示它**实际会用**的游戏路径，
        # 并提供一键修正（ok-ww 的设备记录若指向旧盘符就永远开不了游戏）。
        gp = tk.Frame(i1c, bg=CARD)
        gp.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        tk.Label(gp, text="鸣潮主程序（ok-ww 会自动启动它）", bg=CARD,
                 fg=TEXT, font=make_font(10), anchor="w").pack(anchor="w")
        self.game_lbl = tk.Label(gp, text="检测中…", bg=CARD, fg=TEXT_DIM,
                                 font=make_font(9), anchor="w",
                                 justify="left", wraplength=580)
        self.game_lbl.pack(anchor="w", pady=(5, 0))
        SoftButton(gp, "修正 ok-ww 游戏路径",
                   command=self._fix_game_path,
                   bg=ACCENT_SOFT, fg=ACCENT_DARK, hover="#E2D6FB",
                   padx=14, pady=6, size=9).pack(anchor="w", pady=(8, 0))

        SoftCheck(i1c, "接力时关闭原神（腾出显卡/内存给鸣潮）",
                  self.kill_genshin_var).grid(row=5, column=0, sticky="w",
                                              pady=(14, 0))

        # ★ 跑 ok-ww 时显示它的程序界面。
        # 之前写死了 --headless，ok-ww 完全无界面地在后台跑，
        # 用户看不到任何东西 → 以为"ok-ww 根本没启动"（2026-10-05 反馈）。
        # 两种模式都会**自动**跑指定任务，不需要人工点按钮。
        self.okgui_var = tk.BooleanVar(value=self.okww_gui_cfg())
        SoftCheck(i1c, "跑鸣潮时显示 ok-ww 的程序界面（推荐）",
                  self.okgui_var).grid(row=6, column=0, sticky="w",
                                       pady=(10, 0))
        self.okgui_var.trace_add("write", lambda *a: self._sync_cfg())
        tk.Label(i1c,
                 text="打开后你会看到 ok-ww 的窗口（标题 OK-WW）自己跑「日常」"
                      "——登录、领月卡、刷声骸、领日常奖励。"
                      "关掉则完全在后台静默跑（更省资源）。"
                      "两种方式都是自动执行，不用点任何按钮。",
                 bg=CARD, fg=TEXT_DIM, font=make_font(9),
                 anchor="w", justify="left", wraplength=580).grid(
                     row=7, column=0, sticky="w", pady=(0, 4))

        rowt = tk.Frame(i1c, bg=CARD)
        rowt.grid(row=8, column=0, sticky="w", pady=(14, 0))
        for lab, var, unit in (("一条龙最长等", "OneDragonTimeoutMin", "分钟"),):
            tk.Label(rowt, text=lab, bg=CARD, fg=TEXT,
                     font=make_font(10)).pack(side="left", padx=(0, 6))
            bx = Rounded(rowt, bg="#FCFBFF", radius=9, border=BORDER, width=70)
            bx.pack(side="left")
            tk.Entry(bx.content, textvariable=self.vars[var],
                     font=make_font(10), relief="flat", bd=0,
                     highlightthickness=0, bg="#FCFBFF", fg=TEXT,
                     width=6, justify="center").pack(padx=8, pady=6)
            tk.Label(rowt, text=unit, bg=CARD, fg=TEXT_DIM,
                     font=make_font(9)).pack(side="left", padx=(6, 22))

        rowb = tk.Frame(i1c, bg=CARD)
        rowb.grid(row=9, column=0, sticky="w", pady=(16, 0))
        self.btn_chain = SoftButton(rowb, "立即接力一次",
                                    command=self._chain_now,
                                    bg=PINK, hover="#DB6BA0",
                                    padx=18, pady=9, size=9)
        self.btn_chain.pack(side="left")
        self.btn_chain_check = SoftButton(rowb, "检测各程序",
                                          command=self._chain_probe,
                                          bg=ACCENT_SOFT, fg=ACCENT_DARK,
                                          hover="#E2D6FB",
                                          padx=18, pady=9, size=9)
        self.btn_chain_check.pack(side="left", padx=(10, 0))

        self.chain_lbl = tk.Label(
            i1c,
            text=("点「检测各程序」看看都找到了没。　"
                  "当前权限：%s" % ("管理员 ✓（ok-ww 能自己启动鸣潮）"
                                    if core.is_admin() else
                                    "普通用户 —— 点「立即接力一次」会走"
                                    "「按需接力任务」（最高权限），"
                                    "**不需要点 UAC**")),
            bg=CARD, fg=TEXT_DIM, font=make_font(9),
            anchor="w", justify="left", wraplength=580)
        self.chain_lbl.grid(row=10, column=0, sticky="w", pady=(10, 0))
        self._refresh_game_path()

        c2 = Card(scroll)
        c2.pack(fill="x", pady=(0, 16))
        i2 = tk.Frame(c2.content, bg=CARD)
        i2.pack(fill="x", padx=26, pady=24)
        i2.grid_columnconfigure(0, weight=1)

        self._card_title(i2, 0, "\u26A1", "电源与登录",
                         "这两项直接影响「睡一觉起来能不能自己干活」。")

        self.lock_var = tk.BooleanVar(value=True)
        SoftCheck(i2, "自动关闭「唤醒时需要重新登录」",
                  self.lock_var).grid(row=1, column=0, sticky="w",
                                      pady=(8, 4))
        tk.Label(i2, text="必须关闭：唤醒后若停在登录界面，键鼠模拟全部失效，"
                          "一条龙等于白跑。",
                 bg=CARD, fg=TEXT_DIM, font=make_font(9),
                 anchor="w").grid(row=2, column=0, sticky="w", pady=(0, 16))

        self.auto_var = tk.BooleanVar(value=False)
        SoftCheck(i2, "开机登录后自动打开本程序",
                  self.auto_var).grid(row=3, column=0, sticky="w", pady=(4, 4))
        tk.Label(i2, text="只负责打开界面，不会触发一条龙。",
                 bg=CARD, fg=TEXT_DIM, font=make_font(9),
                 anchor="w").grid(row=4, column=0, sticky="w", pady=(0, 16))

        # ★ 开跑时清空桌面（只留原神）—— 窗口压在上面会让 BetterGI
        # 的截屏识别和模拟键鼠失效，反复「切换角色卡住，执行脱困」。
        self.minothers_var = tk.BooleanVar(value=self.min_others_cfg())
        SoftCheck(i2, "开跑一条龙时把其它窗口全部最小化（只留原神）",
                  self.minothers_var).grid(row=5, column=0, sticky="w",
                                           pady=(4, 4))
        self.minothers_var.trace_add("write", lambda *a: self._sync_cfg())
        tk.Label(i2, text="推荐打开。**BetterGI 自己的程序窗口"
                          "（标题「更好的原神」）也会一起最小化**，"
                          "屏幕上只剩原神；但它的「遮罩窗口」会保留，"
                          "不影响 BGI 工作。",
                 bg=CARD, fg=TEXT_DIM, font=make_font(9),
                 anchor="w", justify="left", wraplength=580).grid(
                     row=6, column=0, sticky="w", pady=(0, 16))

        # ★ 关掉「开始菜单 / 搜索」这种 shell 浮层。
        # 它跟普通窗口不是一回事：Win11 上它宿主在 explorer/searchhost 里，
        # 是空标题的 TOOLWINDOW，普通"最小化其它窗口"根本碰不到它。
        # 一旦它拿到焦点，游戏就收不到模拟键鼠 —— 用户实测：
        # 睡眠唤醒后卡在 Win 键打开的那个界面上，整条链路停摆。
        self.float_var = tk.BooleanVar(value=self.dismiss_float_cfg())
        SoftCheck(i2, "开跑时自动关掉开始菜单 / 搜索（推荐）",
                  self.float_var).grid(row=7, column=0, sticky="w",
                                       pady=(4, 4))
        self.float_var.trace_add("write", lambda *a: self._sync_cfg())
        tk.Label(i2, text="睡眠唤醒后有时会停在「按了 Win 键」那个界面上，"
                          "这时 BetterGI 既截不到画面也控制不了角色。"
                          "打开后会在开跑前、以及运行期间每分钟检查一次，"
                          "发现就按一下 Esc 关掉（不会影响游戏本身）。",
                 bg=CARD, fg=TEXT_DIM, font=make_font(9),
                 anchor="w", justify="left", wraplength=580).grid(
                     row=8, column=0, sticky="w", pady=(0, 16))

        self.keepon_var = tk.BooleanVar(value=self.keep_on_cfg())
        SoftCheck(i2, "一条龙运行时让显示器保持常亮",
                  self.keepon_var).grid(row=9, column=0, sticky="w",
                                        pady=(4, 4))
        # 勾选即刻落盘（这条开关要在无界面运行时生效，不能只在内存里）
        self.keepon_var.trace_add("write", lambda *a: self._sync_cfg())
        tk.Label(i2, text="默认只阻止「系统睡眠」，允许熄屏。"
                          "如果一条龙总在半夜卡住，勾上这一项往往能解决 ——"
                          "代价是屏幕整夜亮着。",
                 bg=CARD, fg=TEXT_DIM, font=make_font(9),
                 anchor="w", justify="left", wraplength=580).grid(
                     row=10, column=0, sticky="w", pady=(0, 16))

        # ★ 始终以管理员身份运行（一次性解决 UAC 的不确定性）
        self.admin_var = tk.BooleanVar(value=core.runasadmin_enabled())
        SoftCheck(i2, "让本程序始终以管理员身份运行（推荐）",
                  self.admin_var).grid(row=11, column=0, sticky="w",
                                       pady=(4, 4))
        self.admin_var.trace_add("write", lambda *a: self._set_admin_flag())
        self.admin_hint = tk.Label(
            i2, text=self._admin_hint_text(), bg=CARD, fg=TEXT_DIM,
            font=make_font(9), anchor="w", justify="left", wraplength=580)
        self.admin_hint.grid(row=12, column=0, sticky="w", pady=(0, 16))

        power = tk.Frame(i2, bg=CARD)
        power.grid(row=13, column=0, sticky="w")
        self.btn_checkpow = SoftButton(power, "检测电源唤醒能力",
                                       command=self._check_power,
                                       bg=ACCENT_SOFT, fg=ACCENT_DARK,
                                       hover="#E2D6FB", padx=16, pady=8,
                                       size=9)
        self.btn_checkpow.pack(side="left")
        self.btn_power = SoftButton(power, "应用电源设置",
                                    command=self._apply_power,
                                    bg=ACCENT, padx=16, pady=8, size=9)
        self.btn_power.pack(side="left", padx=(10, 0))

        tk.Frame(scroll, bg=BG, height=20).pack()

    # -- 页 3：唤醒测试 -----------------------------------------------------
    def _page_wake(self):
        p = self.pages["wake"]
        scroll = self._scroll(p)

        c1 = Card(scroll)
        c1.pack(fill="x", pady=(0, 16))
        i1 = tk.Frame(c1.content, bg=CARD)
        i1.pack(fill="x", padx=26, pady=24)
        i1.grid_columnconfigure(0, weight=1)

        self._card_title(i1, 0, "\u263E", "睡眠 → 自动唤醒 → 自动跑一条龙",
                         "做一次真实闭环验证：设 N 分钟后自动唤醒 → 电脑进入"
                         "睡眠 → 到点自己醒来并执行一条龙。")

        r1 = tk.Frame(i1, bg=CARD)
        r1.grid(row=2, column=0, sticky="w")
        tk.Label(r1, text="多少分钟后唤醒", bg=CARD, fg=TEXT_DIM,
                 font=make_font(10)).pack(side="left", padx=(0, 9))
        mbx = Rounded(r1, bg="#FCFBFF", radius=9, border=BORDER, width=70)
        mbx.pack(side="left")
        tk.Entry(mbx.content, textvariable=self.vars["wake_minutes"],
                 font=make_font(10), relief="flat", bd=0,
                 highlightthickness=0, bg="#FCFBFF", fg=TEXT,
                 width=5, justify="center").pack(padx=9, pady=7)
        tk.Label(r1, text="分钟", bg=CARD, fg=TEXT_DIM,
                 font=make_font(10)).pack(side="left", padx=(8, 0))

        self.btn_wake = SoftButton(i1, "开始测试",
                                   command=self._start_wake_test,
                                   padx=24, pady=10, bold=True)
        self.btn_wake.grid(row=3, column=0, sticky="w", pady=(20, 0))

        tip = tk.Frame(i1, bg=WARN_SOFT)
        tip.grid(row=4, column=0, sticky="ew", pady=(16, 0))
        tk.Label(tip, text="!", bg=WARN_SOFT, fg="#B57A16",
                 font=make_font(11, "bold")).pack(side="left", padx=(12, 2),
                                                   pady=10)
        tk.Label(tip, text="点下去之后：会弹一次 UAC 请点「是」→ 倒计时 30 秒 → "
                           "电脑自己进入睡眠 → 别碰键鼠、别合盖 → 到点它自己醒。",
                 bg=WARN_SOFT, fg="#8A6212", font=make_font(9), anchor="w",
                 justify="left", wraplength=520).pack(side="left",
                                                      padx=(4, 12), pady=10)

        c2 = Card(scroll)
        c2.pack(fill="x", pady=(0, 16))
        i2 = tk.Frame(c2.content, bg=CARD)
        i2.pack(fill="x", padx=26, pady=24)
        i2.grid_columnconfigure(0, weight=1)

        head2 = tk.Frame(i2, bg=CARD)
        head2.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        tk.Label(head2, text="测试结果", bg=CARD, fg=TEXT,
                 font=make_font(13, "bold"), anchor="w").pack(side="left")
        self.wake_status = StatusDot(i2, "尚未测试", WARN)
        self.wake_status.grid(row=0, column=1, sticky="ne", padx=(12, 0))

        btns2 = tk.Frame(i2, bg=CARD)
        btns2.grid(row=1, column=0, sticky="w", pady=(16, 0))
        self.btn_wake_result = SoftButton(btns2, "查看唤醒结果",
                                          command=self._show_wake_result,
                                          bg=ACCENT_SOFT, fg=ACCENT_DARK,
                                          hover="#E2D6FB", padx=16, pady=8,
                                          size=9)
        self.btn_wake_result.pack(side="left")
        SoftButton(btns2, "清理测试任务", command=self._clean_wake_task,
                   bg=BG, fg=TEXT_DIM, hover="#EEE8F8",
                   padx=16, pady=8, size=9).pack(side="left", padx=(10, 0))

        tk.Label(i2, text="判读标准：唤醒来源显示「计时器」才算数；"
                          "显示 USB / 键盘鼠标 说明是你碰醒的，不作数。",
                 bg=CARD, fg=TEXT_DIM, font=make_font(9),
                 anchor="w", justify="left").grid(row=2, column=0, sticky="w",
                                                  pady=(14, 0))

        tk.Frame(scroll, bg=BG, height=20).pack()

    # -- 页 4：日志 ---------------------------------------------------------
    def _page_log(self):
        p = self.pages["log"]
        wrap = tk.Frame(p, bg=BG)
        wrap.pack(fill="both", expand=True)

        c = Card(wrap)
        c.pack(fill="both", expand=True, padx=20, pady=20)
        i = tk.Frame(c.content, bg=CARD)
        i.pack(fill="both", expand=True, padx=22, pady=20)
        i.grid_columnconfigure(0, weight=1)
        i.grid_rowconfigure(1, weight=1)

        head = tk.Frame(i, bg=CARD)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        hb = tk.Canvas(head, width=28, height=28, bg=CARD, highlightthickness=0)
        hb.pack(side="left")
        hb.create_oval(1, 1, 27, 27, fill=ACCENT_SOFT, outline="")
        hb.create_text(14, 14, text="\u2637", fill=ACCENT_DARK,
                       font=make_font(11))
        tk.Label(head, text="运行日志", bg=CARD, fg=TEXT,
                 font=make_font(13, "bold")).pack(side="left", padx=(10, 0))
        SoftButton(head, "打开日志目录", command=self._open_logdir,
                   bg=ACCENT_SOFT, fg=ACCENT_DARK, hover="#E2D6FB",
                   padx=14, pady=7, size=9).pack(side="right", padx=(9, 0))
        SoftButton(head, "打开配置目录", command=self._open_cfgdir,
                   bg=ACCENT_SOFT, fg=ACCENT_DARK, hover="#E2D6FB",
                   padx=14, pady=7, size=9).pack(side="right", padx=(9, 0))
        SoftButton(head, "清空", command=self._clear_log,
                   bg=BG, fg=TEXT_DIM, hover="#EEE8F8",
                   padx=14, pady=7, size=9).pack(side="right")

        term = Rounded(i, bg=DARK_PANEL, radius=12, border="#4A4270")
        term.grid(row=1, column=0, sticky="nsew")
        box = tk.Frame(term.content, bg=DARK_PANEL)
        box.pack(fill="both", expand=True, padx=4, pady=4)
        self.log_text = tk.Text(box, bg=DARK_PANEL, fg="#D6CCF0",
                                font=("Consolas", 9), relief="flat", bd=0,
                                highlightthickness=0, wrap="word",
                                padx=14, pady=12, state="disabled",
                                insertbackground="#D6CCF0")
        lsb = tk.Scrollbar(box, orient="vertical",
                            command=self.log_text.yview,
                            bg=DARK_PANEL, activebackground="#6B5F96",
                            troughcolor=DARK_PANEL, bd=0, width=12,
                            highlightthickness=0)
        self.log_text.configure(yscrollcommand=lsb.set)
        lsb.pack(side="right", fill="y", pady=6)
        self.log_text.pack(side="left", fill="both", expand=True)
        self.log_text.tag_configure("info", foreground="#C3B7E4")
        self.log_text.tag_configure("ok", foreground="#7FE0BE")
        self.log_text.tag_configure("warn", foreground="#FFD489")
        self.log_text.tag_configure("err", foreground="#FFA0A0")
        self.log_text.tag_configure("ts", foreground="#7A6EA3")

        self.log("界面已就绪。先到「基础设置」点自动检测，再去「自动运行」"
                 "安装定时任务。", "info")

    # ======================================================================
    # 功能实现
    # ======================================================================

    def log(self, msg, kind="info"):
        try:
            self.msg_q.put((msg, kind))
        except Exception:
            pass

    def _drain(self):
        # 先处理跨线程投递的 UI 调用（必须在主线程）
        try:
            while True:
                fn, args = self.ui_q.get_nowait()
                try:
                    fn(*args)
                except Exception as exc:  # noqa: BLE001
                    self.msg_q.put(("界面更新失败：%s" % exc, "err"))
        except queue.Empty:
            pass
        try:
            while True:
                msg, kind = self.msg_q.get_nowait()
                ts = datetime.now().strftime("%H:%M:%S")
                self.log_text.config(state="normal")
                self.log_text.insert("end", ts + "  ", "ts")
                self.log_text.insert("end", msg + "\n", kind)
                self.log_text.see("end")
                self.log_text.config(state="disabled")
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def ui(self, fn, *args):
        """线程安全地在主线程执行 fn。

        tkinter 不是线程安全的：后台线程里直接调 widget 方法或
        self.after() 会抛 "main thread is not in main loop"。
        统一把调用塞进队列，由 _drain（主线程的定时器）执行。
        """
        try:
            self.ui_q.put((fn, args))
        except Exception:
            pass

    def _bg(self, fn, silent_busy=False, refresh=True, title=None, btn=None,
            total=None):
        """后台线程执行 + 日志回调 + 底部状态条 + 按钮忙碌态。

        fn 收到一个 emit(msg, kind="info", progress=None) 回调：
        - emit("…")                      → 只写日志
        - emit("…", "ok")                → 带颜色写日志
        - emit("…", "info", (2, 5))      → 同时更新状态条为"第 2 / 5 步"

        title: 状态条上显示的总体任务名，例如"安装定时任务"
        btn:   触发这次操作的按钮（会显示"处理中…"动画）
        total: 总步骤数（配合 emit 的 progress 使用）
        """
        if self.busy:
            if not silent_busy:
                self.log("还有操作在进行中，请稍候…", "warn")
            return
        self.busy = True
        self._busy_btn = btn
        self._busy_ok = True
        self._set_busy(True, btn=btn)

        label = title or "处理中"
        self.ui(self._status_set, "%s…" % label, True, None)

        def emit(msg, kind="info", progress=None):
            if kind == "err":
                self._busy_ok = False
            self.log(msg, kind)
            if progress:
                cur, tot = progress
                self.ui(self._status_set,
                        "%s…（第 %d / %d 步）%s" % (label, cur, tot, msg),
                        True, (cur, tot))
            elif title:
                self.ui(self._status_set, "%s…%s" % (label, msg), True, None)

        def runner():
            try:
                fn(emit)
            except Exception as exc:  # noqa: BLE001
                self._busy_ok = False
                self.log("发生错误：%s" % exc, "err")
                self.ui(self._status_set, "出错了：%s" % exc, False, None)
            finally:
                self.ui(self._done, refresh)

        threading.Thread(target=runner, daemon=True).start()

    def _done(self, refresh=True):
        """后台任务收尾（务必在主线程执行）。

        注意：这里不能无条件调用 _refresh_status —— 状态栏刷新本身
        也是 _bg，它的收尾又会回到这里，形成无限递归，busy 永远
        不释放（表现为"自动检测"等所有按钮都无响应）。
        """
        btn = getattr(self, "_busy_btn", None)
        ok = getattr(self, "_busy_ok", True)
        self.busy = False
        self._busy_btn = None
        self._set_busy(False)
        self._status_set("就绪", False, None)
        # 结果用按钮变色告知：绿=成功，红=出错（比"什么都没发生"清楚）
        if btn is not None:
            try:
                btn.flash("ok" if ok else "err")
            except Exception:
                pass
        if refresh:
            # _refresh_status 的 refresh=False：它自己是状态刷新，
            # 收尾时不再触发下一轮刷新。
            self._refresh_status()

    def _all_buttons(self):
        out = []
        for n in ("btn_install", "btn_runonce", "btn_abort", "btn_uninst",
                  "btn_wake", "btn_clean", "btn_survey", "btn_detect",
                  "btn_power", "btn_wake_result",
                  "btn_stage_detect", "btn_stage_test"):
            b = getattr(self, n, None)
            if b:
                out.append(b)
        return out

    def _set_busy(self, flag, btn=None):
        """忙碌时禁用所有主要按钮；被点击的那个显示"处理中…"动画。

        注意：**只**给当前被点击的按钮加"处理中…"文字 ——
        如果每个按钮都改文字，界面反而更乱、更看不懂。
        """
        for b in self._all_buttons():
            if flag:
                b.set_enabled(False)
            else:
                b.end_busy()
        if flag and btn is not None:
            btn.begin_busy("处理中")

    def _sync_cfg(self, strict=False):
        """把界面上的值写进配置文件。

        strict=True 时**保存失败会明确报错并返回 False** —— 绝不能像以前
        那样静默吞掉：配置存不下去，后面"安装任务"自然毫无变化，
        而用户完全看不出原因（2026-10-04 事故）。
        """
        for k in ("BgiDir", "OneDragonConfig", "TaskName", "TaskTime",
                  "RandomMinutes", "WindowStart", "WindowEnd",
                  "WaitWindowSec", "VerifySec",
                  "OkwwDir", "OkwwTask", "WuwaExe", "GenshinExe",
                  "OneDragonTimeoutMin"):
            if k in self.vars:
                self.cfg.set(k, self.vars[k].get())
        # 两个开关是 BooleanVar，要转成 0/1
        self.cfg.set("RunOkww", "1" if self.run_okww_var.get() else "0")
        self.cfg.set("KillGenshin",
                     "1" if self.kill_genshin_var.get() else "0")
        try:
            self.cfg.set("KeepScreenOn",
                         "1" if self.keepon_var.get() else "0")
            self.cfg.set("MinimizeOthers",
                         "1" if self.minothers_var.get() else "0")
            self.cfg.set("DismissShellFloat",
                         "1" if self.float_var.get() else "0")
            self.cfg.set("OkwwGuiMode",
                         "1" if self.okgui_var.get() else "0")
        except Exception:  # noqa: BLE001
            pass
        ok, msg = self.cfg.save()
        self._refresh_next_run_hint()
        if not ok:
            self.log("✗ 配置保存失败，改动没有生效！%s" % msg, "err")
            if strict:
                messagebox.showerror(
                    "配置保存失败（改动未生效）",
                    "无法写入配置文件：\n%s\n\n"
                    "这意味着你刚才改的设置**没有被保存**，"
                    "继续安装也会用旧设置。\n\n"
                    "常见原因：程序放在 C:\\ProgramData 这类目录下，\n"
                    "Windows 不允许普通用户修改已存在的文件。\n"
                    "请把程序换到「文档」「桌面」等普通目录再试。" % msg)
            return False
        self.log("设置已保存到：%s" % msg, "ok")
        return True

    def _refresh_next_run_hint(self):
        """把"下次实际运行时刻"（含随机误差）显示出来。"""
        lbl = getattr(self, "next_run_lbl", None)
        if not lbl:
            return
        try:
            t, why = self.cfg.describe_run()
            lbl.config(text="下次实际运行：%s　·　%s" % (t, why),
                       fg=ACCENT_DARK)
        except Exception:
            pass

    def _valid_bgi(self):
        if not self.vars["BgiDir"].get().strip():
            messagebox.showinfo("还差一步", "请先填写 BetterGI 安装目录，"
                                          "或点「自动检测」。")
            self.show_page("base")
            return False
        return True

    # -- 基础设置 -----------------------------------------------------------
    def _detect(self):
        def work(emit):
            emit("正在搜索 BetterGI.exe（先查运行中的进程、快捷方式、"
                 "注册表，最后才全盘扫描）…", "info", (1, 3))
            exe = core.find_bgi_exe()
            if not exe:
                emit("没找到。请手动点「选择 exe 文件」指定。", "err")
                self.ui(messagebox.showwarning, "未找到",
                         "没自动找到 BetterGI.exe。\n\n"
                         "请点「选择 exe 文件」手动指定，或把 BetterGI "
                         "装在默认位置。")
                return
            d = os.path.dirname(exe)
            self.ui(self._set_bgi_dir, d)
            emit("已找到：%s" % exe, "ok", (2, 3))
            names = core.list_onedragon_configs(d)
            self.ui(self._fill_od, names, True)
            if names:
                emit("读取到 %d 个一条龙配置：%s"
                     % (len(names), "、".join(names)), "ok", (3, 3))
            else:
                emit("没在 %s\\User\\OneDragon 下找到配置 json，"
                     "请手动填写配置名" % d, "warn", (3, 3))
        self._bg(work, title="自动检测 BetterGI", btn=self.btn_detect)

    def _set_bgi_dir(self, d):
        """在主线程里写入 BetterGI 目录（后台线程已扫完）。"""
        self.vars["BgiDir"].set(d)
        self._sync_cfg()

    # -- 接力鸣潮 -----------------------------------------------------------
    def _pick_okww(self):
        d = core.detect_okww(self.vars["OkwwDir"].get().strip() or None)
        if not d:
            f = filedialog.askdirectory(title="选择 ok-ww 安装目录")
            if f and os.path.isfile(os.path.join(f, "ok-ww.exe")):
                d = f
            elif f:
                messagebox.showwarning(
                    "这个目录不对",
                    "选中的目录里没有 ok-ww.exe。\n\n"
                    "请选到 ok-ww 的安装目录（通常长这样：\nD:\\ok-ww）")
                return
            else:
                return
        self.vars["OkwwDir"].set(d)
        self._sync_cfg()
        self.log("ok-ww 目录：%s" % d, "ok")
        self._chain_probe()

    def _refresh_game_path(self):
        """显示 ok-ww 会自动启动的那个鸣潮主程序，并标出是否有效。"""
        lbl = getattr(self, "game_lbl", None)
        if not lbl:
            return
        try:
            game, src = core.okww_game_exe(self.cfg)
        except Exception as exc:  # noqa: BLE001
            lbl.config(text="检测失败：%s" % exc, fg="#D08700")
            return
        if game:
            lbl.config(text="✓ %s\n（%s，文件确实存在）" % (game, src),
                       fg="#2E7D52")
        else:
            lbl.config(
                text="✗ %s\n路径无效 —— ok-ww 会报「Game path does not "
                     "exist」而开不了游戏，点下面按钮修正。" % src,
                fg="#C94F4F")

    def _fix_game_path(self):
        """把 ok-ww 的设备记录指向真实存在的鸣潮主程序。"""
        def work(emit):
            emit("正在检查 ok-ww 的设备记录…", "info", (1, 2))
            cfg = core.Config()
            game = core.find_wuwa_game_exe()
            if not game:
                emit("✗ 没找到鸣潮主程序（Wuthering Waves Game\\"
                     "Wuthering Waves.exe）。请确认游戏装好了。", "err")
                return
            ok, msg = core.fix_okww_game_path(cfg, emit)
            emit(("✓ 已修正" if ok else "✗ 修正失败") + "：" + msg,
                 "ok" if ok else "err", (2, 2))
            self.ui(self._refresh_game_path)
        self._bg(work, title="修正 ok-ww 游戏路径", refresh=False)

    def _chain_probe(self):
        """检测 ok-ww / 鸣潮 / 原神 都找齐了没。"""
        okww = self.vars["OkwwDir"].get().strip() or core.detect_okww()

        class _NS(object):
            pass
        ns = _NS()
        ns.okww_dir = okww
        ns.okww_task = self.vars["OkwwTask"].get() or "DailyTask"
        _argv, _cwd, why = core.okww_spec(ns)

        wuwa, wuwa_src = core.okww_game_exe(ns)
        gen = self.vars["GenshinExe"].get().strip() or core.find_genshin_exe()
        py = os.path.join(okww or "", "data", "apps", "ok-ww", "python",
                          "python.exe")
        adm = core.is_admin()
        lines = [
            ("ok-ww", okww if os.path.isfile(os.path.join(okww or "",
                                                          "ok-ww.exe")) else ""),
            ("ok-ww 自带 Python", py if os.path.isfile(py) else ""),
            ("鸣潮主程序(ok-ww 启动)", wuwa if wuwa and os.path.isfile(wuwa)
             else ""),
            ("原神主程序", gen if gen and os.path.isfile(gen) else ""),
        ]
        parts = []
        allok = True
        for name, p in lines:
            good = bool(p)
            if not good:
                allok = False
            parts.append("%s %s" % ("✓" if good else "✗", name))
        # 管理员权限是**决定性条件**：ok-ww 非管理员时启动不了 PC 版鸣潮，
        # 而且它是静默失败（只报 Start task failed，日志里查不出原因）。
        # 所以必须显式列出，不能让它隐形。
        parts.append("%s 管理员权限%s"
                     % ("✓" if adm else "✗",
                        "" if adm else "（点接力会自动走按需任务，不用点 UAC）"))
        txt = "　".join(parts)
        self.chain_lbl.config(
            text=("检测结果：" + txt +
                  ("　—　全部就绪，可以接力了。" if allok else
                   "　—　还差一些，按上面的提示补齐。")),
            fg=("#2E7D52" if allok else "#D08700"))
        for name, p in lines:
            self.log("  %s %s%s" % ("✓" if p else "✗", name,
                                    ("：" + p) if p else "（未找到）"),
                     "ok" if p else "warn")
        if adm:
            self.log("  ✓ 当前是管理员权限 —— ok-ww 可以自己启动鸣潮。", "ok")
        else:
            self.log("  ⚠ 当前不是管理员权限。ok-ww 启动 PC 版鸣潮时会检查"
                     "管理员身份，不满足就**静默放弃**（日志里只会看到 "
                     "Start task failed，查不出原因）。\n"
                     "     → 点「立即接力一次」时会自动走「按需接力任务」"
                     "（最高权限，由计划任务服务拉起）——\n"
                     "       **不需要点 UAC、也不需要人在场**；\n"
                     "       前提是先点过一次「安装 / 更新任务」把该任务建好。\n"
                     "     → 凌晨自动那次本身就以最高权限运行，完全不受影响。",
                     "warn")
        if not okww:
            self.log("找不到 ok-ww。它通常装在 D:\\ok-ww，"
                     "请用「ok-ww 安装目录」右边按钮指定。", "warn")
        if not wuwa:
            self.log("✗ 定位不到 ok-ww 会启动的鸣潮主程序（%s）。"
                     "这样它开不了游戏 —— 点「修正 ok-ww 游戏路径」。"
                     % wuwa_src, "err")
        else:
            self.log("鸣潮主程序有效（%s），ok-ww 接力时会自己拉起它。"
                     % wuwa_src, "ok")
        if why:
            self.log("ok-ww 运行命令有问题：%s" % why, "warn")
        self._refresh_game_path()

    def _chain_now(self):
        """立刻执行一次接力（跳过跑原神那条龙）。"""
        if not messagebox.askyesno(
                "确认接力",
                "将立刻执行接力流程：\n\n"
                "① 关闭 BetterGI 和原神（如果正在运行）\n"
                "② 让 ok-ww 启动鸣潮并跑鸣潮日常\n\n"
                "注意：这会关掉正在运行的原神！\n\n确定继续吗？"):
            return
        if not self._sync_cfg(strict=True):
            return
        self.show_page("log")

        if not core.is_admin():
            # ok-ww 启动 PC 版鸣潮强制要求管理员身份，非管理员会静默放弃。
            # 【2026-10-05 改】优先走「按需接力任务」：
            #   触发一个**已存在的最高权限计划任务**，由计划任务服务拉起，
            #   **不需要 UAC、也不需要人点任何东西**。
            #   原来直接提权重启自己 —— 这台机器 UAC 是
            #   ConsentPromptBehaviorAdmin=0（提权不提示），表现就是
            #   "什么都没发生"；而默认 UAC 级别下凌晨又没人能点。
            self.log("接力需要管理员身份，正在启动「按需接力任务」"
                     "（最高权限，不需要点 UAC）…", "info")
            ok, msg = core.start_relay_task(self.log)
            if ok:
                self.log("✓ %s  跑完会自动把结果回显到这里。" % msg, "ok")
                self._wait_chain_report(core.relay_report_path())
                return
            self.log("✗ 走按需任务失败：%s" % msg, "warn")
            self.log("退回提权方式（如果 UAC 没弹出来，多半是系统设置为"
                     "「不提示」，其实已经提权成功）。", "warn")
            rpath = core.relay_report_path()
            try:
                os.remove(rpath)
            except OSError:
                pass
            exe = os.path.abspath(sys.executable if getattr(sys, "frozen", False)
                                  else sys.argv[0])
            if not core.run_as_admin_with_args(
                    [exe, "--chain-now", "--report", rpath]):
                messagebox.showwarning(
                    "接力没能启动",
                    "两条路都没走通：\n\n"
                    "① 按需接力任务：%s\n"
                    "② 提权启动：失败\n\n"
                    "请先在「自动运行」页点一次「安装 / 更新任务」"
                    "（这一步需要提权，会创建按需接力任务），"
                    "之后手动接力就不用再点任何东西了。" % msg)
                return
            self.log("已发起提权接力（在管理员窗口中执行），"
                     "跑完会自动把结果回显到这里。", "info")
            self._wait_chain_report(rpath)
            return

        def work(emit):
            emit("手动触发接力流程…", "info", (1, 3))
            cfg = core.Config()
            if not cfg.okww_dir:
                emit("✗ 没找到 ok-ww，先去上面点「自动检测」。", "err")
                return
            emit("关闭原神/BetterGI，然后交给 ok-ww（鸣潮由它启动）…",
                 "info", (2, 3))
            ok, msg = core.chain_to_wuwa(cfg, emit)
            emit("接力结果：%s" % msg, "ok" if ok else "err", (3, 3))
        self._bg(work, title="接力鸣潮", btn=self.btn_chain, refresh=False)

    def _wait_chain_report(self, rpath, timeout=6 * 3600):
        """等提权进程把接力报告写出来，然后在界面上显示。

        接力可能要跑很久（鸣潮日常），所以这里不阻塞主线程 ——
        丢到后台线程里等文件出现，出现后回主线程显示。
        """
        self._status_set("接力鸣潮…（正在管理员窗口中执行，完成后自动显示结果）",
                         True, None)
        if getattr(self, "btn_chain", None):
            self.btn_chain.begin_busy("接力中")

        def waiter():
            t0 = time.time()
            while time.time() - t0 < timeout:
                time.sleep(3)
                if os.path.isfile(rpath):
                    # 等它写完（大小连续两次一致才算写完）
                    try:
                        s1 = os.path.getsize(rpath)
                        if s1 <= 0:
                            continue
                        time.sleep(2)
                        if os.path.getsize(rpath) == s1:
                            break
                    except OSError:
                        continue
            self.ui(self._show_chain_report, rpath)

        threading.Thread(target=waiter, daemon=True).start()

    def _show_chain_report(self, rpath):
        """把提权接力跑出来的报告显示出来（日志页 + 弹窗）。"""
        if getattr(self, "btn_chain", None):
            self.btn_chain.end_busy()
        try:
            text = open(rpath, encoding="utf-8", errors="replace").read()
        except OSError:
            self._status_set("接力结束，但没拿到报告文件", False, None)
            self.log("✗ 没读到接力报告（%s）。"
                     "可能是提权窗口还没跑完；ok-ww 的原始输出在 %s。"
                     % (rpath, os.path.join(core.user_data_dir(),
                                            "okww_last_run.log")), "warn")
            return
        ok = "接力结果   : 成功" in text
        self._status_set("接力完成" if ok else "接力失败", False, None)
        for line in text.splitlines():
            self.log(line)
        self.log("===== 接力%s =====" % ("成功" if ok else "失败"),
                 "ok" if ok else "err")
        body = text.strip()
        if len(body) > 1600:
            body = "…\n" + body[-1600:]
        try:
            (messagebox.showinfo if ok else messagebox.showwarning)(
                "接力鸣潮" + ("完成" if ok else "失败"), body)
        except Exception:
            pass

    def _set_random(self, val):
        """快捷设置随机误差（分钟）。"""
        self.vars["RandomMinutes"].set(str(val))
        self._sync_cfg()
        if str(val) == "0":
            self.log("已关闭随机误差，每天固定 %s 触发。"
                     % self.cfg.task_time, "ok")
        else:
            self.log("随机误差上限已设为 %s 分钟：实际触发在 %s ~ %s 之间随机。"
                     % (val, self.cfg.task_time,
                        self.cfg.next_run(rand=1.0).strftime("%H:%M")), "ok")

    def _window_off(self):
        """把时间窗设为不限制。"""
        self.vars["WindowStart"].set("0000")
        self.vars["WindowEnd"].set("0000")
        self._sync_cfg()
        self.log("时间窗已设为「不限制」，任务将完全按「运行时间」%s 触发。"
                 % self.vars["TaskTime"].get(), "ok")

    def _show_od_menu(self):
        """弹出可选的一条龙配置列表。"""
        if not self._od_names:
            self._reload_od(silent=True)
        self.od_menu.delete(0, "end")
        if not self._od_names:
            self.od_menu.add_command(label="（未读取到配置，请先填安装目录）",
                                     state="disabled")
        else:
            for nm in self._od_names:
                self.od_menu.add_command(
                    label=nm,
                    command=lambda v=nm: self.vars["OneDragonConfig"].set(v))
        try:
            self.od_menu.tk_popup(self.od_arrow.winfo_rootx(),
                                  self.od_arrow.winfo_rooty()
                                  + self.od_arrow.winfo_height())
        finally:
            self.od_menu.grab_release()

    def _fill_od(self, names, silent=False):
        self._od_names = list(names)
        cur = self.vars["OneDragonConfig"].get()
        if names and cur not in names:
            self.vars["OneDragonConfig"].set(names[0])
        elif not names and not cur:
            self.vars["OneDragonConfig"].set("默认配置")
        if not silent:
            pass

    def _reload_od(self, silent=False):
        d = self.vars["BgiDir"].get().strip()
        if not d:
            if not silent:
                messagebox.showinfo("提示", "请先填写 BetterGI 安装目录。")
            return
        names = core.list_onedragon_configs(d)
        self._fill_od(names)
        if not silent:
            if names:
                self.log("已读取 %d 个一条龙配置：%s"
                         % (len(names), "、".join(names)), "ok")
            else:
                self.log("目录下没有配置 json，请手动填写名称。", "warn")

    def _pick_dir(self):
        d = filedialog.askdirectory(title="选择 BetterGI 安装目录")
        if d:
            self.vars["BgiDir"].set(d)
            self._sync_cfg()
            names = core.list_onedragon_configs(d)
            if names:
                self._fill_od(names)
                self.log("已读取 %d 个一条龙配置" % len(names), "ok")

    def _pick_exe(self):
        f = filedialog.askopenfilename(
            title="选择 BetterGI.exe",
            filetypes=[("BetterGI 主程序", "BetterGI.exe"), ("可执行文件", "*.exe"),
                       ("所有文件", "*.*")])
        if f:
            d = os.path.dirname(f)
            self.vars["BgiDir"].set(d)
            self._sync_cfg()
            names = core.list_onedragon_configs(d)
            if names:
                self._fill_od(names)
            self.log("已选择：%s" % f, "ok")

    # -- 自动运行 -----------------------------------------------------------
    def _install_task(self):
        if not self._valid_bgi():
            return
        # 关键：先保存，并且**检查保存结果**。
        # 存不下去就立刻报错停止 —— 否则后面装出来的还是旧设置，
        # 用户会看到"点了更新但完全没变"（2026-10-04 事故根因）。
        if not self._sync_cfg(strict=True):
            return

        if not core.is_admin():
            self._install_by_elevation()
            return

        def work(emit):
            emit("开始安装定时任务…", "info", (1, 4))
            if self.lock_var.get():
                emit("关闭「唤醒时需要重新登录」…", "info", (2, 4))
                ok, msg = core._disable_console_lock()
                emit(msg if ok else "该项处理异常：%s" % msg,
                     "ok" if ok else "warn")
            emit("写入计划任务（先删后建）…", "info", (3, 4))
            ok, msg = core.install_task(self.cfg, emit)
            if ok:
                emit("回读校验任务设置…", "info", (4, 4))
                self._report_after_install(emit)
            else:
                emit("安装失败：%s" % msg, "err")
        self._bg(work, title="安装定时任务", btn=self.btn_install)

    def _report_after_install(self, emit):
        """安装成功后统一回读真实设置并告诉用户。"""
        info = core.task_info(self.cfg.task_name)
        run_at, why = self.cfg.describe_run()
        if info:
            emit("✓ 已生效 | 下次实际运行 %s（%s）" % (run_at, why), "ok")
            emit("★ 系统里真实的设置：下次=%s ｜ 唤醒=%s ｜ 电池限制=%s"
                 % (info.get("NEXT"), info.get("WAKETORUN"),
                    info.get("BATTERY")), "ok")
            got = (info.get("NEXT") or "").strip()
            want = self.cfg.next_run()
            # 容忍几分钟误差（任务按分钟对齐）
            try:
                from datetime import datetime as _dt
                g = _dt.strptime(got[:16], "%Y-%m-%d %H:%M")
                if abs((g - want.replace(second=0, microsecond=0))
                       .total_seconds()) > 120:
                    emit("⚠ 校验不一致：期望 %s，实际 %s"
                         % (want.strftime("%Y-%m-%d %H:%M"), got[:16]), "warn")
            except Exception:
                pass
        else:
            emit("✓ 安装完成，但读不到任务信息，请点「查看全部启动项」核对。",
                 "warn")
        if self.cfg.window_enabled:
            allowed, why2 = self.cfg.in_window(
                self.cfg.task_time.replace(":", ""))
            if not allowed:
                emit("⚠ 时间窗是 %s，不含任务时间 %s —— 任务会被自己的"
                     "时间窗挡住而静默跳过！建议点「不限制（推荐）」。"
                     % (why2, self.cfg.task_time), "warn")
        emit("系统登记的唤醒定时器：\n%s" % core.wake_timers().strip(), "info")
        self.ui(self._refresh_next_run_hint)
        self.ui(self._refresh_status, True)

    def _install_by_elevation(self):
        """非管理员时的安装流程。

        刻意**不关闭本窗口**（旧版会 destroy，用户什么都看不到、
        还得重开程序），而是：
          ① 起一个提权进程，让它把报告写到固定文件；
          ② 本窗口继续显示"等待授权 → 正在安装"，轮询那个文件；
          ③ 拿到结果后直接把内容打在日志里。
        """
        report = core.install_report_path()
        try:
            if os.path.isfile(report):
                os.remove(report)          # 清掉上一次的，避免读到旧结果
        except OSError:
            pass

        exe = os.path.abspath(sys.executable if getattr(sys, "frozen", False)
                              else sys.argv[0])
        self.show_page("log")
        self.log("需要管理员权限，正在请求提权…"
                 "（**请在 UAC 弹窗点「是」**）", "warn")
        if not core.run_as_admin_with_args([exe, "--install",
                                            "--report", report]):
            messagebox.showwarning(
                "提权失败",
                "没能以管理员身份启动。\n\n"
                "请右键「以管理员身份运行」打开本程序，"
                "然后再点一次「安装 / 更新任务」。")
            return

        self.busy = True
        self._busy_btn = self.btn_install
        self._busy_ok = True
        self._set_busy(True, btn=self.btn_install)
        self._status_set("等待管理员授权…（UAC 弹窗请点「是」）", True, None)
        self._wait_report(report, deadline=180)

    def _wait_report(self, report, deadline):
        """轮询提权进程写出的报告文件（主线程 after 驱动）。"""
        if deadline <= 0:
            self.busy = False
            self._busy_btn = None
            self._set_busy(False)
            self._status_set("就绪", False, None)
            self.log("等待安装结果超时。若 UAC 弹窗被取消，请重新点一次"
                     "「安装 / 更新任务」。", "warn")
            return

        if os.path.isfile(report):
            try:
                txt = open(report, encoding="utf-8", errors="replace").read()
            except OSError:
                txt = ""
            self.busy = False
            self._busy_btn = None
            self._set_busy(False)
            self._status_set("就绪", False, None)
            self.log("=" * 46)
            for ln in (txt or "(报告为空)").splitlines():
                self.log("  " + ln if ln.strip() else "")
            self.log("=" * 46)
            ok = ("安装结果   : 成功" in txt)
            if self.btn_install:
                self.btn_install.flash("ok" if ok else "err")
            self._refresh_status()
            self._refresh_next_run_hint()
            # 回读一次真实设置，直接打在日志里
            info = core.task_info(self.cfg.task_name)
            if info:
                self.log("★ 系统里真实的设置：下次=%s ｜ 唤醒=%s"
                         % (info.get("NEXT"), info.get("WAKETORUN")), "ok")
            return

        self._status_set("等待管理员授权…（UAC 弹窗请点「是」）", True, None)
        self.after(500, lambda: self._wait_report(report, deadline - 1))

    def _remove_task(self):
        if not messagebox.askyesno("确认", "确定要删除定时任务「%s」吗？"
                                          % self.cfg.task_name):
            return
        if not core.is_admin():
            exe = os.path.abspath(sys.executable if getattr(sys, "frozen", False)
                                  else sys.argv[0])
            if core.run_as_admin_with_args([exe, "--clean-all"]):
                messagebox.showinfo("已提权清理",
                                    "已请求管理员权限执行清理，"
                                    "请在 UAC 弹窗点「是」。")
                self.after(1500, lambda: self.destroy())
            else:
                messagebox.showwarning(
                    "提权失败", "请右键「以管理员身份运行」本程序后重试。")
            return

        def work(emit):
            ok, msg = core.remove_task(self.cfg.task_name)
            emit(msg if ok else "删除失败：%s" % msg, "ok" if ok else "err")
            self.ui(self._refresh_status, True)
        self._bg(work, title="卸载定时任务", btn=self.btn_uninst, refresh=False)

    def _run_task_now(self):
        """立即执行一次（走和凌晨那条完全一样的路径）。

        【2026-10-05 改】以前这里写成：
            if not core.is_admin():
                messagebox.showinfo("需要权限", ...); return
        结果是**弹一个框、然后什么都不做** —— 用户看到的就是
        「弹出来就根本无法更新并且无法执行」。现在改成：
          ① 先照常触发计划任务（最高权限的 GUI 一定能触发）；
          ② 失败了且不是管理员 → 直接带 --run-daily 提权跑一次；
          ③ 再失败 → 给出可操作的自救步骤（勾上"始终管理员"）。
        """
        def work(emit):
            emit("正在通知 Windows 立即启动该任务…", "info", (1, 3))
            ok, msg = core.run_task_now(self.cfg.task_name)
            if ok:
                emit("已触发。BetterGI 会在十几秒内打开，然后自动开始"
                     "一条龙；进度看「运行日志」页。", "ok", (3, 3))
                return

            emit("直接触发计划任务失败：%s" % msg, "warn")
            if core.is_admin():
                emit("当前已是管理员却仍触发不了 —— 请先点一次"
                     "「安装 / 更新任务」把任务重建干净，再试。", "err")
                return

            emit("当前不是管理员（非最高权限的进程启动不了 Highest 任务）。"
                 "改为直接以管理员身份跑一次…", "warn", (2, 3))
            exe = os.path.abspath(sys.executable if getattr(sys, "frozen", False)
                                  else sys.argv[0])
            if core.run_as_admin_with_args([exe, "--run-daily"]):
                emit("已发起。BetterGI 会在十几秒内打开，然后自动开始一条龙。"
                     "（如果 UAC 没有弹窗，说明这台机器设置为「提权不提示」，"
                     "属正常。）", "ok", (3, 3))
            else:
                emit("✗ 提权也没成功。请到「自动运行」页 →「电源与登录」，"
                     "勾上「让本程序始终以管理员身份运行」，然后关掉本程序"
                     "重新打开，再点这个按钮。", "err", (3, 3))
        self._bg(work, title="立即执行一次", btn=self.btn_runonce)

    def _abort_run(self):
        """强制中止当前执行：停任务 + 关掉所有游戏进程。

        为什么需要：一条龙可能卡在战斗里空转几小时（2026-10-05 凌晨
        实测 806 次脱困、白等 5 小时）。虽然新版本会自动判定卡死，
        但用户仍然需要一个立刻停下来的开关。
        """
        if not messagebox.askyesno(
                "中止本次执行",
                "将强制结束正在运行的任务，并关闭 BetterGI / 原神 / 鸣潮。\n\n"
                "（不会影响明天的定时任务，只是停掉这一次。）\n\n确定吗？"):
            return

        def work(emit):
            emit("正在停止计划任务…", "info", (1, 3))
            ok, msg = core.stop_task_now(self.cfg.task_name)
            emit("停止任务：%s" % msg, "ok" if ok else "warn")
            emit("正在关闭游戏进程…", "info", (2, 3))
            for nm, names in (("BetterGI", core.BGI_PROC),
                              ("原神", core.GENSHIN_PROC),
                              ("鸣潮", core.WUWA_PROC)):
                if core.proc_list(names):
                    k, m = core.kill_proc(names, emit)
                    emit("关闭 %s：%s" % (nm, m), "ok" if k else "warn")
                else:
                    emit("%s 本来就没在运行。" % nm)
            emit("已中止。明天的定时任务不受影响。", "ok", (3, 3))
        self._bg(work, title="中止执行", btn=self.btn_abort,
                 refresh=True)

    def _check_power(self):
        def work(emit):
            emit("正在读取唤醒定时器设置…", "info", (1, 2))
            ac, dc = core._win_verify_waketimer()
            emit("允许使用唤醒定时器：交流=%s 直流=%s（1=启用）" % (ac, dc),
                 "ok" if (ac == 1 or dc == 1) else "warn", (2, 2))
            emit(core.available_sleep_states(), "info")
        self._bg(work, title="检测电源唤醒能力", btn=self.btn_checkpow,
                 refresh=False)

    def _apply_power(self):
        if not core.is_admin():
            # 必须带明确参数提权，否则只是重开界面（什么都不做）
            exe = os.path.abspath(sys.executable if getattr(sys, "frozen", False)
                                  else sys.argv[0])
            if core.run_as_admin_with_args([exe, "--apply-power"]):
                messagebox.showinfo("已提权执行",
                                    "已请求管理员权限应用电源设置，"
                                    "请在 UAC 弹窗点「是」，"
                                    "完成后会弹窗显示结果。")
                self.after(1500, lambda: self.destroy())
            else:
                messagebox.showwarning(
                    "提权失败", "请右键「以管理员身份运行」本程序后重试。")
            return
        self._sync_cfg()

        def work(emit):
            emit("启用「允许使用唤醒定时器」…", "info", (1, 3))
            core._set_waketimer(True)
            ac, dc = core._win_verify_waketimer()
            emit("已启用（交流=%s 直流=%s）" % (ac, dc), "ok", (2, 3))
            if self.lock_var.get():
                emit("关闭「唤醒时需要重新登录」…", "info", (3, 3))
                ok, msg = core._disable_console_lock()
                emit("「唤醒时需要重新登录」：%s" % msg,
                     "ok" if ok else "warn")
            if self.auto_var.get():
                exe = os.path.abspath(sys.executable if getattr(sys, "frozen", False)
                                      else sys.argv[0])
                ok, _ = core.set_autostart(True, exe)
                emit("已设置开机自启。" if ok else "开机自启设置失败。",
                     "ok" if ok else "err")
        self._bg(work, title="应用电源设置", btn=self.btn_power,
                 refresh=False)

    def _verify_task(self):
        """把"期望设置"和"系统里真实的设置"摆在一起对照。

        用户最需要的其实就是这一件事：我说要 A，系统里到底是不是 A。
        """
        self.show_page("log")

        def work(emit):
            emit("正在读取系统里真实的计划任务设置…", "info", (1, 2))
            cfg = self.cfg
            info = core.task_info(cfg.task_name)
            emit("【你的设置（界面上的）】", "info")
            emit("  运行时间   : 每天 %s" % cfg.task_time)
            emit("  随机误差   : %s"
                 % ("上限 %d 分钟" % cfg.random_minutes
                    if cfg.random_minutes else "关闭（固定时刻）"))
            want, why = cfg.describe_run()
            emit("  下次应运行 : %s（%s）" % (want, why))
            emit("  时间窗     : %s-%s" % cfg.window)
            emit("  配置文件   : %s" % cfg.path)
            emit("")
            emit("【系统里真实的】", "info")
            if not info:
                emit("  ✗ 没有找到任务「%s」——安装没成功。" % cfg.task_name,
                     "err")
                emit("  请点上面的「安装 / 更新任务」。", "warn")
                return
            emit("  状态       : %s" % info.get("STATE"))
            emit("  下次运行   : %s" % info.get("NEXT"))
            emit("  唤醒电脑   : %s" % info.get("WAKETORUN"))
            emit("  电池限制   : %s" % info.get("BATTERY"))
            emit("  上次运行   : %s（%s）"
                 % (info.get("LAST"), info.get("RESULT_TEXT")))
            emit("")
            # 逐项对比
            got = (info.get("NEXT") or "").strip()
            try:
                from datetime import datetime as _dt
                g = _dt.strptime(got[:16], "%Y-%m-%d %H:%M")
                w = cfg.next_run().replace(second=0, microsecond=0)
                diff = abs((g - w).total_seconds())
                if diff <= 120:
                    emit("✓ 一致：系统里的下次运行时间与你的设置相符。", "ok")
                    self.ui(self.real_run_lbl.config, {
                        "text": "系统里真实的设置：下次 %s（与设置一致 ✓）"
                                % got[:16], "fg": "#2E7D52"})
                else:
                    emit("⚠ 不一致！你期望 %s，系统里是 %s（差 %.0f 分钟）。"
                         % (w.strftime("%Y-%m-%d %H:%M"), got[:16],
                            diff / 60.0), "warn")
                    emit("  说明安装没有真正生效 —— 请重新点"
                         "「安装 / 更新任务」，并注意看日志里的失败原因。",
                         "warn")
                    self.ui(self.real_run_lbl.config, {
                        "text": "系统里真实的设置：下次 %s（与设置不一致 ⚠）"
                                % got[:16], "fg": "#D08700"})
            except Exception:
                emit("  （无法解析系统里的时间：%r）" % got, "warn")
            emit("")
            emit("任务指向的程序：")
            code, out = core.ps(
                "$t=Get-ScheduledTask -TaskName '%s' -ErrorAction "
                "SilentlyContinue;if($t){ ($t.Actions | ForEach-Object {"
                " $_.Execute }) -join ' | ' } else { 'N/A' }"
                % cfg.task_name.replace("'", "''"))
            emit("  %s" % out.strip())
            emit("  当前程序：%s"
                 % os.path.abspath(sys.executable
                                   if getattr(sys, "frozen", False)
                                   else sys.argv[0]))
            emit("  ↑ 这两行必须指向同一个程序，否则你改的是 A、跑的是 B。",
                 "info")
        self._bg(work, title="验证是否生效", btn=self.btn_verify,
                 refresh=False)

    def _survey(self):
        """全盘扫描并把结果打到日志页。"""
        self.show_page("log")

        def work(emit):
            emit("正在扫描系统里所有与本程序 / BetterGI 相关的启动项…",
                 "info", (1, 2))
            text, n = core.survey_autostarts()
            emit("扫描完成，共发现 %d 个相关计划任务。" % n,
                 "ok" if n else "info", (2, 2))
            for line in text.splitlines():
                emit("  " + line if line.strip() else "")
        self._bg(work, title="扫描启动项", btn=self.btn_survey,
                 refresh=False)

    def _refresh_status(self, inner=False):
        """刷新任务状态栏。inner=True 时静默（用于 _bg 收尾链）。"""
        def work(emit):
            name = self.cfg.task_name
            info = core.task_info(name)
            if info:
                self.ui(self.task_status.set,
                        "已安装 · %s" % info.get("STATE", "?"), SUCCESS)
                emit("定时任务「%s」：状态=%s 上次=%s 下次=%s 唤醒=%s 电池限制=%s"
                     % (name, info.get("STATE"), info.get("LAST"),
                        info.get("NEXT"), info.get("WAKETORUN"),
                        info.get("BATTERY")), "info")
                rc_txt = info.get("RESULT_TEXT") or ""
                if rc_txt:
                    emit("上次结果：%s（%s）"
                         % (rc_txt, info.get("RESULT_HEX") or
                            info.get("RESULT")),
                         "ok" if info.get("RESULT") == "0" else "info")
            else:
                self.ui(self.task_status.set, "未安装", WARN)
            at = core.get_autostart()
            self.ui(self.auto_var.set, bool(at))
            # 只在首次运行时读电源能力，避免刷屏
            if not getattr(self, "_power_read", False):
                self._power_read = True
                ac, dc = core._win_verify_waketimer()
                emit("唤醒定时器：交流=%s 直流=%s（1=启用）" % (ac, dc),
                     "ok" if (ac == 1 or dc == 1) else "warn")
        # refresh=False：状态刷新不再触发下一轮，避免 _done 无限递归
        self._bg(work, silent_busy=inner, refresh=False)

    # -- 唤醒测试 -----------------------------------------------------------
    def _start_wake_test(self):
        if not self._valid_bgi():
            return
        self._sync_cfg()
        try:
            mins = max(1, int(self.vars["wake_minutes"].get()))
        except ValueError:
            mins = 2
        if not core.is_admin():
            # 唤醒测试同样必须带参数提权：新进程要负责
            # 建任务 → 倒计时 → 睡眠，否则提权后只是重开界面
            exe = os.path.abspath(sys.executable if getattr(sys, "frozen", False)
                                  else sys.argv[0])
            if core.run_as_admin_with_args([exe, "--wake-test", str(mins)]):
                messagebox.showinfo(
                    "已提权执行唤醒测试",
                    "已请求管理员权限。请在 UAC 弹窗点「是」。\n\n"
                    "之后会弹一个确认框，确认后电脑将进入睡眠，\n"
                    "%d 分钟后自动唤醒并执行一条龙。" % mins)
                self.after(1500, lambda: self.destroy())
            else:
                messagebox.showwarning(
                    "提权失败", "请右键「以管理员身份运行」本程序后重试。")
            return
        self.show_page("log")

        def work(emit):
            emit("创建一次性唤醒任务…", "info", (1, 3))
            ok, msg = core.create_wake_test(mins, emit)
            if not ok:
                emit("创建失败：%s" % msg, "err", (3, 3))
                return
            emit("已创建，%d 分钟后触发。30 秒后进入睡眠…" % mins,
                 "warn", (2, 3))
            for i in range(30, 0, -1):
                emit("倒计时 %d 秒" % i, "info", (3, 3))
                time.sleep(1)
            emit("执行睡眠…", "info", (3, 3))
            core.sleep_now()
        self._bg(work, title="唤醒测试", btn=self.btn_wake, refresh=False)

    def _show_wake_result(self):
        def work(emit):
            emit("正在读取测试任务状态…", "info", (1, 2))
            info = core.task_info(core.WAKE_TEST_TASK)
            if not info:
                emit("没有找到测试任务（可能已跑完自动删除，或从未成功创建）。",
                     "warn", (2, 2))
            else:
                emit("测试任务：状态=%s 上次运行=%s 返回码=%s"
                     % (info.get("STATE"), info.get("LAST"),
                        info.get("RESULT")), "info", (2, 2))
                if info.get("RESULT") not in ("0", "267009"):
                    emit("返回码非 0：0x41303 表示从未运行（电脑没被唤醒）；"
                         "267011 表示任务被终止。", "warn")
            emit("--- 上次唤醒来源 ---", "info")
            emit(core.last_wake_info(), "info")
            code, out = core.run("powercfg /waketimers")
            emit("--- 已登记的唤醒定时器 ---\n%s" % out.strip(), "info")
            logf = os.path.join(core.log_dir(), "autobgi.log")
            if os.path.isfile(logf):
                code2, text = core.read_tail(logf, 40)
                emit("--- 本程序日志尾部 ---\n%s" % text.strip(), "info")
        self._bg(work, title="查看唤醒结果", btn=self.btn_wake_result,
                 refresh=False)
        self.show_page("log")

    def _clean_wake_task(self):
        """清理唤醒测试留下的任务（提权执行）。

        这个任务本该跑完自删，但曾经因为触发器里带了 RepetitionInterval
        而变成「每分钟启动一次 BetterGI、持续一整周」，必须能一键清掉。
        """
        if not core.is_admin():
            exe = os.path.abspath(sys.executable if getattr(sys, "frozen", False)
                                  else sys.argv[0])
            if core.run_as_admin_with_args([exe, "--clean-all"]):
                messagebox.showinfo(
                    "已提权清理", "已请求管理员权限执行清理。\n\n"
                                  "请在 UAC 弹窗点「是」，"
                                  "完成后会弹窗显示结果。")
                self.after(1500, lambda: self.destroy())
            else:
                messagebox.showwarning(
                    "提权失败", "请右键「以管理员身份运行」本程序后重试。")
            return

        def work(emit):
            emit("删除唤醒测试任务…", "info", (1, 3))
            ok, msg = core.remove_task(core.WAKE_TEST_TASK)
            emit("唤醒测试任务：%s" % msg, "ok" if ok else "err", (2, 3))
            code, out = core.ps(
                "$p=Get-Process BetterGI -ErrorAction SilentlyContinue;"
                "if($p){ $p | Stop-Process -Force; 'KILLED' } "
                "else { 'NONE' }")
            if "KILLED" in out:
                emit("已结束正在运行的 BetterGI 进程。", "ok")
            else:
                emit("当前没有 BetterGI 在运行。", "info")
            text, n = core.survey_autostarts()
            emit("清理后复查：仍存在 %d 个相关计划任务。" % n,
                 "ok" if n == 1 else "warn", (3, 3))
            for line in text.splitlines():
                if line.strip():
                    emit("  " + line)
        self._bg(work, title="清理残留测试任务", btn=self.btn_clean,
                 refresh=False)

    # -- 日志 ---------------------------------------------------------------
    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    def _open_logdir(self):
        d = core.log_dir()
        try:
            os.startfile(d)
        except Exception:
            messagebox.showinfo("日志目录", d)

    def _open_cfgdir(self):
        d = os.path.dirname(core.config_path())
        try:
            os.startfile(d)
        except Exception:
            messagebox.showinfo("配置目录", core.config_path())

    def _on_close(self):
        try:
            self._sync_cfg()
        except Exception:
            pass
        self.destroy()


def main():
    rc = core.main_cli()
    if rc is not None:
        return rc
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)