"""벨로그 자동 포스팅 — 다계정 데스크톱 GUI.

사용 흐름
1. 이미지 폴더(공통)를 한 번 지정한다. 다시 바꾸기 전까지 모든 계정에 적용된다.
2. 벨로그 아이디 + 인증 메일함만 입력해도 [계정 추가] 가 된다. 원고는 나중에 지정 가능.
3. 목록에서 '원고 파일' 칸을 더블클릭하면 그 계정의 원고를 고를 수 있다.
4. 여러 계정을 선택해 [원고 일괄 지정] 으로 원고를 한 번에 배정할 수 있다.
5. [전체 출간 시작] → 등록된 계정을 순서대로 자동 출간한다. 이미지는 원고 제일 상단에 등록된다.
"""

from __future__ import annotations

import json
import multiprocessing
import queue
import re
import sys
import threading
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from paths import APP_VERSION, EXE_NAME, UPDATE_VERSION_URL
from tempmail_generator import TempMailGenerator
from update_ui import schedule_update_check
from velog_poster import (
    DEFAULT_PROFILE_NAMES,
    PostingError,
    VelogPoster,
    normalize_url,
    parse_tempmail_address,
    read_manuscript,
)


BASE_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
SETTINGS_PATH = BASE_DIR / "velog_settings.json"

BG = "#eef1f6"
CARD = "#ffffff"
INK = "#1a2332"
SUBTLE = "#64748b"
ACCENT = "#0d9488"
ACCENT_DARK = "#0f766e"
ACCENT_LIGHT = "#ccfbf1"
BORDER = "#e2e8f0"
FONT = "Malgun Gothic"
NAV_BAR_BG = "#dde3ec"
NAV_ACTIVE_BG = ACCENT
NAV_ACTIVE_FG = "#ffffff"
NAV_INACTIVE_BG = "#f8fafc"
NAV_INACTIVE_FG = "#64748b"
NAV_HOVER_BG = "#e2e8f0"

ACCOUNT_KEYS = (
    "velog_id", "inbox_url", "manuscript_path",
    "published_url", "published_at", "created_at", "mail_mismatch",
)
IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
NONE_MARK = "더블클릭하여 지정"
DONE_BG = "#dcfce7"
DONE_FG = "#166534"
GAUGE_DAYS = 6.0
GAUGE_SEGMENTS = 6


class ToolTip:
    """간단한 호버 툴팁."""

    def __init__(self, widget: tk.Widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _event=None) -> None:
        if self._tip is not None:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        lbl = tk.Label(
            self._tip, text=self.text, background="#1e293b", foreground="#f8fafc",
            font=(FONT, 9), padx=10, pady=6, relief="flat",
        )
        lbl.pack()

    def _hide(self, _event=None) -> None:
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


class VelogApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"벨로그 자동 포스팅 v{APP_VERSION}")
        self.geometry("1520x860")
        self.minsize(1200, 720)
        self.configure(bg=BG)

        self.velog_id = tk.StringVar()
        self.inbox_url = tk.StringVar()
        self.manuscript = tk.StringVar()
        self.image_folder = tk.StringVar()
        self.anchor_text = tk.StringVar()
        self.anchor_url = tk.StringVar()
        self.homepage_search = tk.StringVar()
        self._collapse_state = {"image": False, "advanced": False, "account": False}
        self.status = tk.StringVar(value="대기 중 — 계정을 등록한 뒤 [전체 출간 시작]을 누르세요.")
        self.tab_summary = tk.StringVar(value="계정 0개")
        self.progress_text = tk.StringVar(value="")

        self.tabs: list[dict] = []
        self._active_tab: dict | None = None
        self.anchors: list[dict[str, str]] = []
        self.homepages: list[str] = []
        self._events: queue.Queue[tuple[str, str]] = queue.Queue()
        self._poster: VelogPoster | None = None
        self._worker: threading.Thread | None = None
        self._run_total = 0
        self._run_done = 0

        # 임시 메일 생성 탭
        self.generated_emails: list[dict[str, str]] = []
        self.tm_count = tk.IntVar(value=1)
        self.tm_status = tk.StringVar(value="대기 중 — 생성할 개수를 입력하고 [생성 시작]을 누르세요.")
        self.tm_progress_text = tk.StringVar(value="")
        self._tm_events: queue.Queue[tuple[str, str]] = queue.Queue()
        self._tm_generator: TempMailGenerator | None = None
        self._tm_worker: threading.Thread | None = None
        self._tm_run_total = 0
        self._tm_run_done = 0

        self._build_style()
        self._build_ui()
        self._bind_shortcuts()
        self._load_settings()
        schedule_update_check(
            self,
            version_url=UPDATE_VERSION_URL,
            current_version=APP_VERSION,
            exe_name=EXE_NAME,
        )
        self.after(100, self._drain_events)
        self.after(100, self._drain_tm_events)
        self.after(1000, self._tick_gauges)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _current_tab(self) -> dict | None:
        if not self.tabs:
            return None
        try:
            i = self.notebook.index(self.notebook.select())
        except Exception:
            i = 0
        return self.tabs[i] if i < len(self.tabs) else self.tabs[0]

    @property
    def accounts(self) -> list:
        tab = self._current_tab()
        return tab["accounts"] if tab else []

    @property
    def tree(self):
        tab = self._current_tab()
        return tab["tree"] if tab else None

    # -- 스타일 -----------------------------------------------------------
    def _build_style(self) -> None:
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure("Bg.TFrame", background=BG)
        st.configure("Card.TFrame", background=CARD)
        st.configure("CardBorder.TFrame", background=BORDER)
        st.configure("Title.TLabel", background=BG, foreground=INK, font=(FONT, 20, "bold"))
        st.configure("Sub.TLabel", background=BG, foreground=SUBTLE, font=(FONT, 9))
        st.configure("Section.TLabel", background=CARD, foreground=INK, font=(FONT, 11, "bold"))
        st.configure("SectionSub.TLabel", background=CARD, foreground=SUBTLE, font=(FONT, 8))
        st.configure("Field.TLabel", background=CARD, foreground=INK, font=(FONT, 9, "bold"))
        st.configure("Hint.TLabel", background=CARD, foreground=SUBTLE, font=(FONT, 8))
        st.configure("Stat.TLabel", background=ACCENT_LIGHT, foreground=ACCENT_DARK,
                     font=(FONT, 9, "bold"), padding=(10, 6))
        st.configure("LogTitle.TLabel", background=BG, foreground=INK, font=(FONT, 11, "bold"))
        st.configure("Status.TLabel", background=ACCENT_LIGHT, foreground=ACCENT_DARK,
                     font=(FONT, 10), padding=(14, 10))
        st.configure("Primary.TButton", background=ACCENT, foreground="#ffffff",
                     font=(FONT, 11, "bold"), borderwidth=0, padding=(18, 12))
        st.map("Primary.TButton", background=[("active", ACCENT_DARK), ("disabled", "#99f6e4")])
        st.configure("Ghost.TButton", background="#f1f5f9", foreground="#334155",
                     font=(FONT, 9, "bold"), borderwidth=0, padding=(12, 8))
        st.map("Ghost.TButton", background=[("active", "#e2e8f0")])
        st.configure("Pick.TButton", background="#f1f5f9", foreground="#334155",
                     font=(FONT, 9), borderwidth=0, padding=(10, 6))
        st.map("Pick.TButton", background=[("active", "#e2e8f0")])
        st.configure("Danger.TButton", background="#fee2e2", foreground="#b91c1c",
                     font=(FONT, 9, "bold"), borderwidth=0, padding=(12, 8))
        st.map("Danger.TButton", background=[("active", "#fecaca")])
        st.configure("Treeview", rowheight=32, font=(FONT, 9), background=CARD,
                     fieldbackground=CARD, borderwidth=0)
        st.configure("Treeview.Heading", font=(FONT, 9, "bold"), background="#f8fafc",
                     foreground=INK, relief="flat")
        st.configure("green.Horizontal.TProgressbar", troughcolor="#e2e8f0",
                     background=ACCENT, borderwidth=0, lightcolor=ACCENT, darkcolor=ACCENT)

    def _build_main_nav(self, parent: ttk.Frame) -> ttk.Frame:
        """상단 메인 메뉴 — 버튼 형식 탭 전환."""
        wrap = tk.Frame(parent, bg=BG)
        wrap.pack(fill="x", pady=(0, 12))

        top_row = tk.Frame(wrap, bg=BG)
        top_row.pack(fill="x", pady=(0, 8))
        tk.Label(
            top_row, text="벨로그 자동화", bg=BG, fg=SUBTLE,
            font=(FONT, 9, "bold"),
        ).pack(side="left")
        tk.Label(
            top_row, text=f"v{APP_VERSION}", bg=BG, fg=ACCENT_DARK,
            font=(FONT, 9, "bold"),
        ).pack(side="right")

        bar = tk.Frame(wrap, bg=NAV_BAR_BG, highlightthickness=0)
        bar.pack(fill="x")

        inner = tk.Frame(bar, bg=NAV_BAR_BG)
        inner.pack(fill="x", padx=4, pady=4)

        self._main_views: dict[str, ttk.Frame] = {}
        self._nav_buttons: dict[str, tk.Button] = {}
        self._nav_indicators: dict[str, tk.Frame] = {}
        self._active_main_view = "posting"

        items = (
            ("posting", "벨로그 포스팅"),
            ("tempmail", "임시 메일 생성"),
        )
        for index, (key, label) in enumerate(items):
            cell = tk.Frame(inner, bg=NAV_BAR_BG)
            cell.pack(side="left", fill="both", expand=True, padx=(0 if index == 0 else 4, 0))

            btn = tk.Button(
                cell, text=label, font=(FONT, 11, "bold"),
                relief="flat", borderwidth=0, cursor="hand2",
                padx=20, pady=12,
                command=lambda k=key: self._switch_main_view(k),
            )
            btn.pack(fill="x")
            self._nav_buttons[key] = btn
            btn.bind("<Enter>", lambda _e, k=key: self._on_nav_enter(k), add="+")
            btn.bind("<Leave>", lambda _e, k=key: self._on_nav_leave(k), add="+")

            indicator = tk.Frame(cell, bg=NAV_BAR_BG, height=3)
            indicator.pack(fill="x")
            self._nav_indicators[key] = indicator

        content = ttk.Frame(parent, style="Bg.TFrame")
        content.pack(fill="both", expand=True)
        return content

    def _switch_main_view(self, key: str) -> None:
        if key not in self._main_views:
            return
        self._active_main_view = key
        for k, frame in self._main_views.items():
            if k == key:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()
        self._update_nav_styles()

    def _update_nav_styles(self) -> None:
        for key, btn in self._nav_buttons.items():
            active = key == self._active_main_view
            btn.configure(
                bg=NAV_ACTIVE_BG if active else NAV_INACTIVE_BG,
                fg=NAV_ACTIVE_FG if active else NAV_INACTIVE_FG,
                activebackground=ACCENT_DARK if active else NAV_HOVER_BG,
                activeforeground=NAV_ACTIVE_FG if active else INK,
            )
            self._nav_indicators[key].configure(
                bg=NAV_ACTIVE_BG if active else NAV_BAR_BG,
            )

    def _on_nav_enter(self, key: str) -> None:
        if key == self._active_main_view:
            return
        self._nav_buttons[key].configure(bg=NAV_HOVER_BG, fg=INK)

    def _on_nav_leave(self, key: str) -> None:
        self._update_nav_styles()

    def _section(self, parent: ttk.Frame, title: str, subtitle: str = "") -> ttk.Frame:
        """카드형 섹션: 제목 + 내용 영역."""
        outer = ttk.Frame(parent, style="CardBorder.TFrame")
        outer.pack(fill="x", pady=(0, 10))
        card = ttk.Frame(outer, style="Card.TFrame", padding=(12, 10))
        card.pack(fill="x", padx=1, pady=1)
        ttk.Label(card, text=title, style="Section.TLabel").pack(anchor="w")
        if subtitle:
            ttk.Label(card, text=subtitle, style="SectionSub.TLabel").pack(anchor="w", pady=(2, 8))
        else:
            ttk.Frame(card, style="Card.TFrame", height=6).pack()
        body = ttk.Frame(card, style="Card.TFrame")
        body.pack(fill="x")
        return body

    def _scrollable_sidebar(self, parent: ttk.Frame) -> ttk.Frame:
        canvas = tk.Canvas(parent, bg=BG, highlightthickness=0, width=360)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, style="Bg.TFrame")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _on_canvas_configure(event) -> None:
            canvas.itemconfigure(win, width=event.width)

        canvas.bind("<Configure>", _on_canvas_configure)

        def _wheel(event) -> None:
            canvas.yview_scroll(int(-event.delta / 120), "units")

        canvas.bind_all("<MouseWheel>", _wheel, add="+")
        return inner

    # -- 레이아웃 ---------------------------------------------------------
    def _build_ui(self) -> None:
        root = ttk.Frame(self, style="Bg.TFrame", padding=(12, 10))
        root.pack(fill="both", expand=True)

        content = self._build_main_nav(root)

        posting_frame = ttk.Frame(content, style="Bg.TFrame", padding=(18, 14))
        tempmail_frame = ttk.Frame(content, style="Bg.TFrame", padding=(18, 14))
        self._main_views["posting"] = posting_frame
        self._main_views["tempmail"] = tempmail_frame

        self._build_posting_tab(posting_frame)
        self._build_tempmail_tab(tempmail_frame)
        self._switch_main_view("posting")

    def _build_posting_tab(self, root: ttk.Frame) -> None:
        # 헤더
        header = ttk.Frame(root, style="Bg.TFrame")
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text="벨로그 자동 포스팅", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="① 계정 등록  →  ② 원고 지정  →  ③ 전체 출간 시작",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        # 본문 (좌 | 중 | 우) — PanedWindow 로 크기 조절 가능
        body = ttk.Panedwindow(root, orient="horizontal")
        body.pack(fill="both", expand=True)

        left_wrap = ttk.Frame(body, style="Bg.TFrame")
        center = ttk.Frame(body, style="Bg.TFrame")
        logf = ttk.Frame(body, style="Bg.TFrame")
        body.add(left_wrap, weight=0)
        body.add(center, weight=3)
        body.add(logf, weight=1)

        sidebar = self._scrollable_sidebar(left_wrap)
        self._build_inputs(sidebar)
        self._build_list(center)
        self._build_log(logf)

        # 하단 실행 바
        bottom = ttk.Frame(root, style="Bg.TFrame")
        bottom.pack(fill="x", pady=(12, 0))
        bottom.columnconfigure(0, weight=1)

        prog_row = ttk.Frame(bottom, style="Bg.TFrame")
        prog_row.pack(fill="x", pady=(0, 8))
        self.progress = ttk.Progressbar(
            prog_row, mode="determinate", style="green.Horizontal.TProgressbar",
        )
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 10))
        ttk.Label(prog_row, textvariable=self.progress_text, style="Sub.TLabel").pack(side="right")

        action_row = ttk.Frame(bottom, style="Bg.TFrame")
        action_row.pack(fill="x")
        action_row.columnconfigure(0, weight=3)
        action_row.columnconfigure(1, weight=1)
        self.start_btn = ttk.Button(
            action_row, text="▶  전체 출간 시작", style="Primary.TButton", command=self._start,
        )
        self.start_btn.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.stop_btn = ttk.Button(
            action_row, text="■  중단", style="Danger.TButton",
            command=self._stop, state="disabled",
        )
        self.stop_btn.grid(row=0, column=1, sticky="ew")
        ttk.Label(bottom, textvariable=self.status, style="Status.TLabel").pack(
            fill="x", pady=(10, 0),
        )

    def _build_tempmail_tab(self, root: ttk.Frame) -> None:
        header = ttk.Frame(root, style="Bg.TFrame")
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text="TempMail 임시 메일 자동 생성", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="tempmail.co 접속 → New Email → 확인 → Save address → Copy Link 순서로 자동 진행",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        body = ttk.Panedwindow(root, orient="horizontal")
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body, style="Bg.TFrame")
        right = ttk.Frame(body, style="Bg.TFrame")
        body.add(left, weight=2)
        body.add(right, weight=1)

        # 생성 설정
        ctrl = self._section(left, "생성 설정", "Chrome 시크릿 창에서 tempmail.co 를 자동 조작합니다.")
        ctrl.columnconfigure(1, weight=1)
        ttk.Label(ctrl, text="생성 개수", style="Field.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        spin = ttk.Spinbox(ctrl, from_=1, to=50, textvariable=self.tm_count, width=8, font=(FONT, 10))
        spin.grid(row=0, column=1, sticky="w")
        ttk.Label(ctrl, text="1~50개 · 각각 New Email 로 새 주소를 만듭니다.", style="Hint.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(6, 0),
        )

        btns = ttk.Frame(left, style="Bg.TFrame")
        btns.pack(fill="x", pady=(0, 10))
        btns.columnconfigure(0, weight=1)
        btns.columnconfigure(1, weight=1)
        self.tm_start_btn = ttk.Button(
            btns, text="▶  생성 시작", style="Primary.TButton", command=self._start_tempmail,
        )
        self.tm_start_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.tm_stop_btn = ttk.Button(
            btns, text="■  중단", style="Danger.TButton", command=self._stop_tempmail, state="disabled",
        )
        self.tm_stop_btn.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        prog = ttk.Frame(left, style="Bg.TFrame")
        prog.pack(fill="x", pady=(0, 10))
        self.tm_progress = ttk.Progressbar(prog, mode="determinate", style="green.Horizontal.TProgressbar")
        self.tm_progress.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Label(prog, textvariable=self.tm_progress_text, style="Sub.TLabel").pack(side="right")

        # 생성 결과 목록
        list_card = ttk.Frame(left, style="CardBorder.TFrame")
        list_card.pack(fill="both", expand=True)
        list_inner = ttk.Frame(list_card, style="Card.TFrame", padding=10)
        list_inner.pack(fill="both", expand=True, padx=1, pady=1)
        list_inner.rowconfigure(1, weight=1)
        list_inner.columnconfigure(0, weight=1)

        head = ttk.Frame(list_inner, style="Card.TFrame")
        head.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(head, text="생성된 임시 메일", style="Section.TLabel").pack(side="left")
        ttk.Label(head, text="더블클릭=URL 열기 · Ctrl+C=URL 복사", style="Hint.TLabel").pack(side="right")

        tree_wrap = ttk.Frame(list_inner, style="Card.TFrame")
        tree_wrap.grid(row=1, column=0, sticky="nsew")
        tree_wrap.rowconfigure(0, weight=1)
        tree_wrap.columnconfigure(0, weight=1)
        self.tm_tree = ttk.Treeview(
            tree_wrap, columns=("email", "url", "created"), show="headings", selectmode="extended",
        )
        self.tm_tree.heading("email", text="이메일 주소")
        self.tm_tree.heading("url", text="복사한 URL (메일함)")
        self.tm_tree.heading("created", text="생성 시각")
        self.tm_tree.column("email", width=220, stretch=False)
        self.tm_tree.column("url", width=420, stretch=True)
        self.tm_tree.column("created", width=130, stretch=False)
        self.tm_tree.grid(row=0, column=0, sticky="nsew")
        self.tm_tree.bind("<Double-1>", self._on_tm_double_click)
        self.tm_tree.bind("<Control-c>", self._copy_tm_urls)
        self.tm_tree.bind("<Control-C>", self._copy_tm_urls)
        sb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tm_tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tm_tree.configure(yscrollcommand=sb.set)

        act = ttk.Frame(list_inner, style="Card.TFrame")
        act.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        for c in range(3):
            act.columnconfigure(c, weight=1)
        ttk.Button(act, text="선택 → 계정 추가", style="Primary.TButton",
                   command=self._add_generated_to_accounts).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(act, text="전체 → 계정 추가", style="Ghost.TButton",
                   command=lambda: self._add_generated_to_accounts(all_items=True)).grid(
            row=0, column=1, sticky="ew", padx=4,
        )
        ttk.Button(act, text="선택 삭제", style="Ghost.TButton",
                   command=self._delete_generated).grid(row=0, column=2, sticky="ew", padx=(4, 0))

        ttk.Label(left, textvariable=self.tm_status, style="Status.TLabel").pack(fill="x", pady=(10, 0))

        # 로그
        ttk.Label(right, text="생성 로그", style="LogTitle.TLabel").pack(anchor="w", pady=(0, 6))
        log_head = ttk.Frame(right, style="Bg.TFrame")
        log_head.pack(fill="x")
        ttk.Button(log_head, text="지우기", style="Pick.TButton", command=self._clear_tm_log).pack(anchor="e")
        log_wrap = ttk.Frame(right, style="CardBorder.TFrame")
        log_wrap.pack(fill="both", expand=True, pady=(6, 0))
        log_inner = ttk.Frame(log_wrap, style="Bg.TFrame")
        log_inner.pack(fill="both", expand=True, padx=1, pady=1)
        self.tm_log_box = tk.Text(
            log_inner, bg="#0f172a", fg="#cbd5e1", insertbackground="#fff",
            relief="flat", padx=12, pady=10, font=("Consolas", 9),
            state="disabled", wrap="word", highlightthickness=0,
        )
        self.tm_log_box.pack(side="left", fill="both", expand=True)
        log_sb = ttk.Scrollbar(log_inner, orient="vertical", command=self.tm_log_box.yview)
        log_sb.pack(side="right", fill="y")
        self.tm_log_box.configure(yscrollcommand=log_sb.set)
        self.tm_log_box.tag_config("success", foreground="#4ade80")
        self.tm_log_box.tag_config("error", foreground="#f87171")
        self.tm_log_box.tag_config("info", foreground="#94a3b8")

    def _build_inputs(self, parent: ttk.Frame) -> None:
        # 이미지 · 사이트 URL (접기/펴기)
        img_outer = ttk.Frame(parent, style="CardBorder.TFrame")
        img_outer.pack(fill="x", pady=(0, 10))
        img_card = ttk.Frame(img_outer, style="Card.TFrame", padding=(12, 10))
        img_card.pack(fill="x", padx=1, pady=1)
        self.image_btn = ttk.Button(
            img_card,
            text="▸  이미지 · 사이트 URL",
            style="Ghost.TButton",
            command=lambda: self._toggle_section("image"),
        )
        self.image_btn.pack(fill="x")
        self.image_body = ttk.Frame(img_card, style="Card.TFrame")
        self.image_body.columnconfigure(1, weight=1)

        ttk.Label(self.image_body, text="이미지 폴더", style="Field.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=(8, 0),
        )
        ttk.Entry(self.image_body, textvariable=self.image_folder, font=(FONT, 9)).grid(
            row=0, column=1, sticky="ew", ipady=4, pady=(8, 0),
        )
        btn_folder = ttk.Button(
            self.image_body, text="찾기", style="Pick.TButton", command=self._browse_folder,
        )
        btn_folder.grid(row=0, column=2, padx=(6, 0), pady=(8, 0))
        ToolTip(btn_folder, "글 상단에 넣을 이미지가 있는 폴더를 선택합니다.")

        ttk.Label(self.image_body, text="사이트 URL", style="Field.TLabel").grid(
            row=1, column=0, sticky="nw", padx=(0, 8), pady=(12, 0),
        )
        hp_wrap = ttk.Frame(self.image_body, style="Card.TFrame")
        hp_wrap.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(12, 0))
        hp_wrap.columnconfigure(0, weight=1)
        ttk.Label(
            hp_wrap,
            text="이미지 클릭 시 무작위로 연결 · 아래에 URL을 붙여넣어 일괄 추가",
            style="Hint.TLabel",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        ttk.Label(hp_wrap, text="검색", style="Hint.TLabel").grid(row=1, column=0, sticky="w")
        search_entry = ttk.Entry(hp_wrap, textvariable=self.homepage_search, font=(FONT, 9))
        search_entry.grid(row=2, column=0, columnspan=2, sticky="ew", ipady=3, pady=(2, 4))
        search_entry.bind("<KeyRelease>", lambda _e: self._refresh_homepage_list())
        self.homepage_list = tk.Listbox(
            hp_wrap, height=4, font=(FONT, 9), relief="solid", borderwidth=1,
            activestyle="none", highlightthickness=0, selectmode="extended",
        )
        self.homepage_list.grid(row=3, column=0, columnspan=2, sticky="ew")
        hp_sb = ttk.Scrollbar(hp_wrap, orient="vertical", command=self.homepage_list.yview)
        hp_sb.grid(row=3, column=2, sticky="ns")
        self.homepage_list.configure(yscrollcommand=hp_sb.set)
        ttk.Button(
            hp_wrap, text="선택 삭제", style="Pick.TButton", command=self._delete_homepage,
        ).grid(row=4, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Label(hp_wrap, text="URL 일괄 추가 (줄바꿈·쉼표 구분)", style="Hint.TLabel").grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(10, 2),
        )
        self.homepage_bulk = tk.Text(
            hp_wrap, height=3, font=(FONT, 9), relief="solid", borderwidth=1,
            wrap="word", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT,
        )
        self.homepage_bulk.grid(row=6, column=0, columnspan=3, sticky="ew")
        ttk.Button(
            hp_wrap, text="일괄 추가", style="Pick.TButton", command=self._bulk_add_homepages,
        ).grid(row=7, column=0, columnspan=3, sticky="ew", pady=(6, 0))

        # 고급 설정 (접기/펴기)
        adv_outer = ttk.Frame(parent, style="CardBorder.TFrame")
        adv_outer.pack(fill="x", pady=(0, 10))
        adv_card = ttk.Frame(adv_outer, style="Card.TFrame", padding=(12, 10))
        adv_card.pack(fill="x", padx=1, pady=1)
        self.adv_btn = ttk.Button(
            adv_card, text="▸  고급 설정 (프로필 · 앵커)", style="Ghost.TButton",
            command=lambda: self._toggle_section("advanced"),
        )
        self.adv_btn.pack(fill="x")
        self.adv_body = ttk.Frame(adv_card, style="Card.TFrame")

        ttk.Label(self.adv_body, text="프로필 이름 후보", style="Field.TLabel").pack(anchor="w", pady=(8, 2))
        ttk.Label(
            self.adv_body, text="신규 가입 시 무작위로 사용 (쉼표로 구분)",
            style="Hint.TLabel",
        ).pack(anchor="w")
        self.profile_text = tk.Text(
            self.adv_body, height=3, font=(FONT, 9), relief="solid",
            borderwidth=1, wrap="word", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT,
        )
        self.profile_text.pack(fill="x", pady=(4, 10))

        ttk.Label(self.adv_body, text="앵커 링크", style="Field.TLabel").pack(anchor="w")
        ttk.Label(
            self.adv_body, text="본문 하단에 무작위로 삽입되는 텍스트 링크",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(0, 4))
        anc = ttk.Frame(self.adv_body, style="Card.TFrame")
        anc.pack(fill="x")
        anc.columnconfigure(0, weight=1)
        ttk.Entry(anc, textvariable=self.anchor_text, font=(FONT, 9)).grid(
            row=0, column=0, sticky="ew", ipady=4,
        )
        ttk.Label(anc, text="표시 텍스트", style="Hint.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 0))
        ttk.Entry(anc, textvariable=self.anchor_url, font=(FONT, 9)).grid(
            row=2, column=0, sticky="ew", ipady=4, pady=(2, 0),
        )
        ttk.Label(anc, text="이동 URL", style="Hint.TLabel").grid(row=3, column=0, sticky="w", pady=(2, 0))
        btn_add = ttk.Button(anc, text="추가", style="Pick.TButton", command=self._add_anchor)
        btn_add.grid(row=0, column=1, rowspan=4, sticky="nsew", padx=(8, 0))
        lr = ttk.Frame(self.adv_body, style="Card.TFrame")
        lr.pack(fill="x", pady=(6, 0))
        lr.columnconfigure(0, weight=1)
        self.anchor_list = tk.Listbox(
            lr, height=3, font=(FONT, 9), relief="solid", borderwidth=1,
            activestyle="none", highlightthickness=0,
        )
        self.anchor_list.grid(row=0, column=0, sticky="ew")
        ab = ttk.Scrollbar(lr, orient="vertical", command=self.anchor_list.yview)
        ab.grid(row=0, column=1, sticky="ns")
        self.anchor_list.configure(yscrollcommand=ab.set)
        ttk.Button(
            self.adv_body, text="선택 삭제", style="Pick.TButton", command=self._delete_anchor,
        ).pack(anchor="e", pady=(4, 0))

        # 계정 등록 (접기/펴기)
        acc_outer = ttk.Frame(parent, style="CardBorder.TFrame")
        acc_outer.pack(fill="x", pady=(0, 10))
        acc_card = ttk.Frame(acc_outer, style="Card.TFrame", padding=(12, 10))
        acc_card.pack(fill="x", padx=1, pady=1)
        self.account_btn = ttk.Button(
            acc_card,
            text="▸  계정 등록",
            style="Ghost.TButton",
            command=lambda: self._toggle_section("account"),
        )
        self.account_btn.pack(fill="x")
        self.account_body = ttk.Frame(acc_card, style="Card.TFrame")
        ttk.Label(
            self.account_body,
            text="아이디와 인증 메일함만 입력해도 추가할 수 있습니다.",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(8, 6))
        card_body = ttk.Frame(self.account_body, style="Card.TFrame")
        card_body.pack(fill="x")
        card_body.columnconfigure(1, weight=1)
        self._row(card_body, 0, "벨로그 아이디", self.velog_id, None)
        self._row(card_body, 1, "인증 메일함", self.inbox_url, None)
        self._row(card_body, 2, "원고 파일", self.manuscript, self._browse_manuscript)

        fb = ttk.Frame(card_body, style="Card.TFrame")
        fb.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        for c in range(2):
            fb.columnconfigure(c, weight=1)
        btn_add_acc = ttk.Button(fb, text="＋ 계정 추가", style="Primary.TButton", command=self._add_account)
        btn_add_acc.grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=(0, 4))
        ttk.Button(fb, text="✎ 선택 수정", style="Ghost.TButton", command=self._update_account).grid(
            row=0, column=1, sticky="ew", padx=(4, 0), pady=(0, 4),
        )
        ttk.Button(fb, text="✕ 선택 삭제", style="Ghost.TButton", command=self._delete_accounts).grid(
            row=1, column=0, sticky="ew", padx=(0, 4),
        )
        ttk.Button(fb, text="↺ 입력 초기화", style="Ghost.TButton", command=self._clear_form).grid(
            row=1, column=1, sticky="ew", padx=(4, 0),
        )
        btn_bulk = ttk.Button(
            card_body, text="📋  여러 계정 일괄 등록", style="Ghost.TButton",
            command=self._open_bulk_dialog,
        )
        btn_bulk.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ToolTip(btn_bulk, "아이디와 메일함 URL을 번갈아 붙여넣어 한 번에 등록합니다.")

    _SECTION_LABELS = {
        "image": ("▸  이미지 · 사이트 URL", "▾  이미지 · 사이트 URL"),
        "advanced": ("▸  고급 설정 (프로필 · 앵커)", "▾  고급 설정 (프로필 · 앵커)"),
        "account": ("▸  계정 등록", "▾  계정 등록"),
    }

    def _toggle_section(self, key: str) -> None:
        bodies = {
            "image": (self.image_btn, self.image_body),
            "advanced": (self.adv_btn, self.adv_body),
            "account": (self.account_btn, self.account_body),
        }
        btn, body = bodies[key]
        collapsed, expanded = self._SECTION_LABELS[key]
        if self._collapse_state[key]:
            body.pack_forget()
            btn.configure(text=collapsed)
        else:
            body.pack(fill="x", pady=(8, 0))
            btn.configure(text=expanded)
        self._collapse_state[key] = not self._collapse_state[key]

    def _build_list(self, parent: ttk.Frame) -> None:
        top = ttk.Frame(parent, style="Bg.TFrame")
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="계정 목록", style="LogTitle.TLabel").pack(side="left")
        ttk.Label(top, textvariable=self.tab_summary, style="Stat.TLabel").pack(side="right")

        btns = ttk.Frame(parent, style="Bg.TFrame")
        btns.pack(fill="x", pady=(0, 6))
        for text, cmd in (
            ("＋ 탭 추가", self._add_tab),
            ("이름 변경", self._rename_tab),
            ("탭 삭제", self._delete_tab),
            ("원고 일괄 지정", self._bulk_assign_manuscripts),
        ):
            ttk.Button(btns, text=text, style="Pick.TButton", command=cmd).pack(
                side="left", padx=(0, 4),
            )

        nb_wrap = ttk.Frame(parent, style="CardBorder.TFrame")
        nb_wrap.pack(fill="both", expand=True)
        nb_inner = ttk.Frame(nb_wrap, style="Card.TFrame", padding=4)
        nb_inner.pack(fill="both", expand=True, padx=1, pady=1)
        self.notebook = ttk.Notebook(nb_inner)
        self.notebook.pack(fill="both", expand=True)
        self.notebook.bind("<<NotebookTabChanged>>", lambda _e: self._update_summary())
        self.notebook.bind("<Double-1>", self._on_tab_double)

        self.empty_label = ttk.Label(
            nb_inner,
            text="등록된 계정이 없습니다.\n왼쪽에서 계정을 추가하거나 일괄 등록하세요.",
            style="Sub.TLabel", justify="center",
        )

        # 범례
        legend = ttk.Frame(parent, style="Bg.TFrame")
        legend.pack(fill="x", pady=(8, 0))
        for color, fg, label in (
            (DONE_BG, DONE_FG, "발행 완료"),
            ("#fff3bf", "#5c3c00", "메일 불일치"),
            ("#ffe3e3", "#c92a2a", "6일 만료"),
        ):
            chip = tk.Label(
                legend, text=f"  {label}  ", bg=color, fg=fg,
                font=(FONT, 8), padx=4, pady=2,
            )
            chip.pack(side="left", padx=(0, 6))
        ttk.Label(
            legend,
            text="원고 더블클릭 · 결과 더블클릭=URL · Ctrl+C=복사 · Del=삭제",
            style="Sub.TLabel",
        ).pack(side="right")

    def _make_tree(self, parent):
        frame = ttk.Frame(parent, style="Card.TFrame")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        tree = ttk.Treeview(
            frame,
            columns=("no", "id", "status", "inbox", "manuscript", "result"),
            show="headings", selectmode="extended",
        )
        tree.heading("no", text="#")
        tree.heading("id", text="아이디")
        tree.heading("status", text="유효기간")
        tree.heading("inbox", text="인증 메일함")
        tree.heading("manuscript", text="원고")
        tree.heading("result", text="발행 결과")
        tree.column("no", width=36, stretch=False, anchor="center")
        tree.column("id", width=150, stretch=False)
        tree.column("status", width=100, stretch=False, anchor="center")
        tree.column("inbox", width=200, stretch=False)
        tree.column("manuscript", width=120, stretch=False)
        tree.column("result", width=280, stretch=True)
        tree.tag_configure("done", background=DONE_BG, foreground=DONE_FG)
        tree.tag_configure("expired", background="#ffe3e3", foreground="#c92a2a")
        tree.tag_configure("mismatch", background="#fff3bf", foreground="#5c3c00")
        tree.grid(row=0, column=0, sticky="nsew")
        tree.bind("<<TreeviewSelect>>", self._on_select)
        tree.bind("<Double-1>", self._on_double_click)
        tree.bind("<Control-c>", self._copy_urls)
        tree.bind("<Control-C>", self._copy_urls)
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=sb.set)
        return frame, tree

    def _rebuild_tabs(self) -> None:
        for tid in self.notebook.tabs():
            self.notebook.forget(tid)
        if not self.tabs:
            self.tabs.append({"title": "기본", "accounts": []})
        for tab in self.tabs:
            frame, tree = self._make_tree(self.notebook)
            tab["frame"] = frame
            tab["tree"] = tree
            self.notebook.add(frame, text=tab["title"])
            self._fill_tree(tab)
        self._update_summary()

    def _update_summary(self) -> None:
        tab = self._current_tab()
        if tab is None:
            self.tab_summary.set("계정 0개")
            return
        accs = tab["accounts"]
        total = len(accs)
        done = sum(1 for a in accs if a.get("published_url"))
        pending = total - done
        expired = sum(1 for a in accs if self._remaining_days(a.get("created_at", "")) <= 0)
        mismatch = sum(1 for a in accs if a.get("mail_mismatch"))
        parts = [f"전체 {total}", f"대기 {pending}", f"완료 {done}"]
        if expired:
            parts.append(f"만료 {expired}")
        if mismatch:
            parts.append(f"⚠ {mismatch}")
        self.tab_summary.set(" · ".join(parts))

        if total == 0:
            self.empty_label.place(relx=0.5, rely=0.5, anchor="center")
        else:
            self.empty_label.place_forget()

    def _add_tab(self) -> None:
        name = simpledialog.askstring(
            "탭 추가", "새 탭 이름을 입력하세요.",
            initialvalue=f"탭 {len(self.tabs) + 1}", parent=self,
        )
        if not name:
            return
        self.tabs.append({"title": name.strip(), "accounts": []})
        self._rebuild_tabs()
        self.notebook.select(len(self.tabs) - 1)
        self._save_settings()

    def _rename_tab(self) -> None:
        tab = self._current_tab()
        if tab is None:
            return
        i = self.notebook.index(self.notebook.select())
        name = simpledialog.askstring(
            "탭 이름 변경", "새 이름을 입력하세요.",
            initialvalue=tab["title"], parent=self,
        )
        if not name:
            return
        tab["title"] = name.strip()
        self.notebook.tab(i, text=tab["title"])
        self._save_settings()

    def _delete_tab(self) -> None:
        if len(self.tabs) <= 1:
            messagebox.showinfo("탭 삭제", "탭은 최소 1개는 있어야 합니다.", parent=self)
            return
        i = self.notebook.index(self.notebook.select())
        if not messagebox.askyesno(
            "탭 삭제",
            f"'{self.tabs[i]['title']}' 탭과 그 안의 계정을 모두 삭제할까요?",
            parent=self,
        ):
            return
        del self.tabs[i]
        self._rebuild_tabs()
        self._save_settings()

    def _on_tab_double(self, event) -> None:
        try:
            i = self.notebook.index(f"@{event.x},{event.y}")
        except Exception:
            return
        self.notebook.select(i)
        self._rename_tab()

    def _build_log(self, parent: ttk.Frame) -> None:
        head = ttk.Frame(parent, style="Bg.TFrame")
        head.pack(fill="x", pady=(0, 6))
        ttk.Label(head, text="실행 로그", style="LogTitle.TLabel").pack(side="left")
        ttk.Button(head, text="지우기", style="Pick.TButton", command=self._clear_log).pack(side="right")

        wrap = ttk.Frame(parent, style="CardBorder.TFrame")
        wrap.pack(fill="both", expand=True)
        inner = ttk.Frame(wrap, style="Bg.TFrame")
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        self.log_box = tk.Text(
            inner, bg="#0f172a", fg="#cbd5e1", insertbackground="#fff",
            relief="flat", padx=12, pady=10, font=("Consolas", 9),
            state="disabled", wrap="word", highlightthickness=0,
        )
        self.log_box.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(inner, orient="vertical", command=self.log_box.yview)
        sb.pack(side="right", fill="y")
        self.log_box.configure(yscrollcommand=sb.set)
        self.log_box.tag_config("success", foreground="#4ade80")
        self.log_box.tag_config("error", foreground="#f87171")
        self.log_box.tag_config("info", foreground="#94a3b8")

    def _clear_log(self) -> None:
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _row(self, parent, row, label, var, browse) -> None:
        ttk.Label(parent, text=label, style="Field.TLabel").grid(
            row=row, column=0, sticky="w", padx=(0, 8), pady=(0 if row == 0 else 8, 0),
        )
        ttk.Entry(parent, textvariable=var, font=(FONT, 9)).grid(
            row=row, column=1, sticky="ew", ipady=5, pady=(0 if row == 0 else 8, 0),
        )
        if browse is not None:
            ttk.Button(parent, text="찾기", style="Pick.TButton", command=browse).grid(
                row=row, column=2, padx=(6, 0), pady=(0 if row == 0 else 8, 0),
            )

    def _bind_shortcuts(self) -> None:
        self.bind("<Control-Return>", lambda _e: self._add_account())
        self.bind("<Delete>", lambda _e: self._delete_accounts())

    # -- 파일/폴더 선택 ---------------------------------------------------
    def _browse_manuscript(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="원고 파일 선택",
            filetypes=[
                ("원고 파일", "*.txt *.html *.htm"),
                ("텍스트", "*.txt"),
                ("HTML", "*.html *.htm"),
                ("모든 파일", "*.*"),
            ],
        )
        if path:
            self.manuscript.set(path)

    def _browse_folder(self) -> None:
        path = filedialog.askdirectory(parent=self, title="공통 이미지 폴더 선택")
        if path:
            self.image_folder.set(path)
            self._save_settings()

    # -- 계정 검증/추가/수정/삭제 ----------------------------------------
    def _collect_form(self) -> dict[str, str]:
        account = {
            "velog_id": self.velog_id.get().strip(),
            "inbox_url": self.inbox_url.get().strip(),
            "manuscript_path": self.manuscript.get().strip(),
        }
        if not account["velog_id"]:
            raise PostingError("벨로그 아이디를 입력해 주세요.")
        normalize_url(account["inbox_url"])
        parse_tempmail_address(account["inbox_url"])
        if account["manuscript_path"]:
            read_manuscript(account["manuscript_path"])
        account["mail_mismatch"] = "1" if self._is_mail_mismatch(
            account["velog_id"], account["inbox_url"],
        ) else ""
        return account

    def _add_account(self) -> None:
        try:
            account = self._collect_form()
        except PostingError as exc:
            messagebox.showwarning("입력 확인", str(exc), parent=self)
            return
        account["created_at"] = datetime.now().isoformat(timespec="seconds")
        self.accounts.append(account)
        self._refresh_tree()
        self._save_settings()
        if account.get("mail_mismatch"):
            messagebox.showwarning(
                "메일 불일치 확인",
                "아이디와 인증 메일함 주소의 이메일이 서로 다릅니다.\n"
                "목록에 ⚠ 표시됩니다. 인증 메일함 주소를 다시 확인해 주세요.",
                parent=self,
            )
        self._clear_form()

    @staticmethod
    def _looks_like_url(line: str) -> bool:
        low = line.lower()
        return low.startswith("http") or "tempmail.co" in low

    @staticmethod
    def _extract_url(line: str) -> str:
        m = re.search(r"\]\((https?://[^)]+)\)", line)
        if m:
            return m.group(1)
        m = re.search(r"https?://\S+", line)
        if m:
            return m.group(0)
        return line

    @staticmethod
    def _is_mail_mismatch(velog_id: str, inbox_url: str) -> bool:
        try:
            email, _ = parse_tempmail_address(inbox_url)
        except PostingError:
            return True
        return velog_id.strip().lower() != email.strip().lower()

    def _parse_bulk(self, text: str) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        cur_id: str | None = None
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if self._looks_like_url(line):
                if cur_id:
                    pairs.append((cur_id, self._extract_url(line)))
                    cur_id = None
            else:
                cur_id = line
        return pairs

    def _open_bulk_dialog(self) -> None:
        win = tk.Toplevel(self)
        win.title("여러 계정 일괄 등록")
        win.configure(bg=BG)
        win.geometry("680x560")
        win.minsize(560, 480)
        win.transient(self)
        win.grab_set()

        wrap = ttk.Frame(win, style="Bg.TFrame", padding=20)
        wrap.pack(fill="both", expand=True)
        ttk.Label(wrap, text="여러 계정 일괄 등록", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            wrap,
            text="아이디와 인증 메일함(tempmail.co) URL을 한 줄씩 번갈아 붙여넣으세요.",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(4, 12))

        example = tk.Text(
            wrap, height=4, font=("Consolas", 9), bg="#f8fafc", fg=SUBTLE,
            relief="solid", borderwidth=1, state="disabled",
        )
        example.pack(fill="x", pady=(0, 10))
        example.configure(state="normal")
        example.insert("1.0", "user1@email.com\nhttps://tempmail.co/address/user1@email.com/키값\nuser2@email.com\nhttps://tempmail.co/address/...")
        example.configure(state="disabled")

        box = ttk.Frame(wrap, style="Bg.TFrame")
        box.pack(fill="both", expand=True)
        txt = tk.Text(
            box, font=("Consolas", 10), relief="solid", borderwidth=1, wrap="none",
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
        )
        txt.pack(side="left", fill="both", expand=True)
        bsb = ttk.Scrollbar(box, orient="vertical", command=txt.yview)
        bsb.pack(side="right", fill="y")
        txt.configure(yscrollcommand=bsb.set)
        txt.focus_set()

        def do_register() -> None:
            pairs = self._parse_bulk(txt.get("1.0", "end"))
            now_iso = datetime.now().isoformat(timespec="seconds")
            added = skipped = mismatch = 0
            existing = {(a.get("velog_id"), a.get("inbox_url")) for a in self.accounts}
            for vid, url in pairs:
                try:
                    norm = normalize_url(url)
                except PostingError:
                    skipped += 1
                    continue
                if "tempmail.co/address/" not in norm.lower():
                    skipped += 1
                    continue
                if (vid, norm) in existing:
                    skipped += 1
                    continue
                bad = self._is_mail_mismatch(vid, norm)
                if bad:
                    mismatch += 1
                self.accounts.append({
                    "velog_id": vid, "inbox_url": norm,
                    "manuscript_path": "", "created_at": now_iso,
                    "mail_mismatch": "1" if bad else "",
                })
                existing.add((vid, norm))
                added += 1
            self._refresh_tree()
            self._save_settings()
            msg = f"{added}개 등록 완료, {skipped}개 건너뜀 (중복/메일함 아님)."
            if mismatch:
                msg += (
                    f"\n\n⚠ 확인 필요: {mismatch}개\n"
                    "인증 메일함 주소에 토큰(key)이 없거나, 아이디와 주소의 이메일이 다릅니다.\n"
                    "정상 형식: .../address/이메일/토큰\n"
                    "목록에서 ⚠ 표시(노란 행)를 확인해 주세요."
                )
                messagebox.showwarning("일괄 등록 — 확인 필요", msg, parent=win)
            else:
                messagebox.showinfo("일괄 등록", msg, parent=win)
            win.destroy()

        btns = ttk.Frame(wrap, style="Bg.TFrame")
        btns.pack(fill="x", pady=(14, 0))
        ttk.Button(btns, text="취소", style="Ghost.TButton", command=win.destroy).pack(side="right")
        ttk.Button(btns, text="등록하기", style="Primary.TButton", command=do_register).pack(
            side="right", padx=(0, 8),
        )

    def _selected_indices(self) -> list[int]:
        if self.tree is None:
            return []
        return sorted(int(iid) for iid in self.tree.selection())

    def _update_account(self) -> None:
        sel = self._selected_indices()
        if len(sel) != 1:
            messagebox.showwarning("선택 확인", "수정할 계정 1개를 선택해 주세요.", parent=self)
            return
        try:
            updated = self._collect_form()
        except PostingError as exc:
            messagebox.showwarning("입력 확인", str(exc), parent=self)
            return
        updated["created_at"] = self.accounts[sel[0]].get("created_at") \
            or datetime.now().isoformat(timespec="seconds")
        self.accounts[sel[0]] = updated
        self._refresh_tree()
        self._save_settings()

    def _delete_accounts(self) -> None:
        sel = self._selected_indices()
        if not sel:
            return
        if not messagebox.askyesno(
            "삭제 확인", f"선택한 {len(sel)}개 계정을 삭제할까요?", parent=self,
        ):
            return
        for index in reversed(sel):
            del self.accounts[index]
        self._refresh_tree()
        self._save_settings()
        self._clear_form()

    def _bulk_assign_manuscripts(self) -> None:
        sel = self._selected_indices()
        if not sel:
            messagebox.showwarning(
                "선택 확인", "원고를 배정할 계정들을 목록에서 선택해 주세요.", parent=self,
            )
            return
        paths = filedialog.askopenfilenames(
            parent=self, title=f"{len(sel)}개 계정에 배정할 원고 파일 선택",
            filetypes=[
                ("원고 파일", "*.txt *.html *.htm"),
                ("텍스트", "*.txt"),
                ("HTML", "*.html *.htm"),
                ("모든 파일", "*.*"),
            ],
        )
        if not paths:
            return
        paths = list(paths)
        if len(paths) != len(sel):
            messagebox.showinfo(
                "개수 안내",
                f"선택 계정 {len(sel)}개, 고른 원고 {len(paths)}개입니다.\n"
                f"앞에서부터 {min(len(sel), len(paths))}개만 순서대로 배정합니다.",
                parent=self,
            )
        for index, path in zip(sel, paths):
            self.accounts[index]["manuscript_path"] = path
            self._clear_published(index)
        self._refresh_tree()
        self._save_settings()

    def _on_double_click(self, event) -> None:
        if self.tree is None:
            return
        if self.tree.identify("region", event.x, event.y) != "cell":
            return
        col = self.tree.identify_column(event.x)
        row = self.tree.identify_row(event.y)
        if not row:
            return
        index = int(row)

        if col == "#6":
            url = self.accounts[index].get("published_url", "")
            if url:
                webbrowser.open(url)
            return

        if col != "#5":
            return
        path = filedialog.askopenfilename(
            parent=self, title="원고 파일 선택",
            filetypes=[
                ("원고 파일", "*.txt *.html *.htm"),
                ("텍스트", "*.txt"),
                ("HTML", "*.html *.htm"),
                ("모든 파일", "*.*"),
            ],
        )
        if not path:
            return
        try:
            read_manuscript(path)
        except PostingError as exc:
            messagebox.showwarning("원고 확인", str(exc), parent=self)
            return
        self.accounts[index]["manuscript_path"] = path
        self._clear_published(index)
        self._refresh_tree()
        self._save_settings()

    def _copy_urls(self, _event=None) -> str:
        urls = [
            self.accounts[i].get("published_url", "")
            for i in self._selected_indices()
            if self.accounts[i].get("published_url")
        ]
        if urls:
            self.clipboard_clear()
            self.clipboard_append("\n".join(urls))
            self.status.set(f"URL {len(urls)}개를 클립보드에 복사했습니다.")
        return "break"

    def _clear_form(self) -> None:
        self.velog_id.set("")
        self.inbox_url.set("")
        self.manuscript.set("")
        if self.tree is not None:
            self.tree.selection_remove(self.tree.selection())

    def _on_select(self, _event=None) -> None:
        sel = self._selected_indices()
        if len(sel) != 1:
            return
        acc = self.accounts[sel[0]]
        self.velog_id.set(acc.get("velog_id", ""))
        self.inbox_url.set(acc.get("inbox_url", ""))
        self.manuscript.set(acc.get("manuscript_path", ""))

    def _refresh_tree(self) -> None:
        tab = self._current_tab()
        if tab is not None:
            self._fill_tree(tab)
        self._update_summary()

    def _fill_tree(self, tab: dict) -> None:
        tree = tab.get("tree")
        if tree is None:
            return
        tree.delete(*tree.get_children())
        for index, acc in enumerate(tab["accounts"]):
            man = Path(acc.get("manuscript_path", "")).name if acc.get("manuscript_path") else NONE_MARK
            url = acc.get("published_url", "")
            at = acc.get("published_at", "")
            result = f"{at}  {url}" if url else "—"
            status = self._status_text(acc)
            vid = acc.get("velog_id", "")
            if acc.get("mail_mismatch"):
                vid = "⚠ " + vid
            tree.insert("", "end", iid=str(index), tags=self._row_tags(acc),
                        values=(index + 1, vid, status, acc.get("inbox_url", ""), man, result))

    def _status_text(self, acc: dict) -> str:
        if acc.get("published_url"):
            return "완료"
        rem = self._remaining_days(acc.get("created_at", ""))
        if rem <= 0:
            return "만료"
        return f"{rem:.1f}일"

    def _row_tags(self, acc: dict) -> tuple:
        tags = []
        if acc.get("published_url"):
            tags.append("done")
        if self._remaining_days(acc.get("created_at", "")) <= 0:
            tags.append("expired")
        if acc.get("mail_mismatch"):
            tags.append("mismatch")
        return tuple(tags)

    @staticmethod
    def _remaining_days(created_at: str) -> float:
        if not created_at:
            return GAUGE_DAYS
        try:
            t = datetime.fromisoformat(created_at)
        except ValueError:
            return GAUGE_DAYS
        elapsed = (datetime.now() - t).total_seconds() / 86400.0
        return max(0.0, GAUGE_DAYS - elapsed)

    def _tick_gauges(self) -> None:
        for tab in self.tabs:
            tree = tab.get("tree")
            if tree is None:
                continue
            for iid in tree.get_children():
                idx = int(iid)
                if idx >= len(tab["accounts"]):
                    continue
                acc = tab["accounts"][idx]
                tree.set(iid, "status", self._status_text(acc))
                tree.item(iid, tags=self._row_tags(acc))
        self._update_summary()
        self.after(60_000, self._tick_gauges)

    def _clear_published(self, index: int) -> None:
        self.accounts[index].pop("published_url", None)
        self.accounts[index].pop("published_at", None)

    def _set_progress(self, done: int, total: int) -> None:
        if total <= 0:
            self.progress.configure(value=0, maximum=100)
            self.progress_text.set("")
            return
        self.progress.configure(maximum=total, value=done)
        self.progress_text.set(f"{done} / {total} 완료")

    # -- 실행 / 중단 ------------------------------------------------------
    def _start(self) -> None:
        tab = self._current_tab()
        if tab is None or not tab["accounts"]:
            messagebox.showwarning("계정 확인", "현재 탭에 계정을 하나 이상 등록해 주세요.", parent=self)
            return
        self._active_tab = tab
        pending = [a for a in tab["accounts"] if not a.get("published_url")]
        if not pending:
            messagebox.showinfo(
                "발행 확인",
                "모든 계정이 이미 발행되었습니다.\n원고를 바꾸면 다시 발행 대상이 됩니다.",
                parent=self,
            )
            return

        image_folder = self.image_folder.get().strip()
        try:
            if image_folder:
                self._check_image_folder(image_folder)
            for acc in pending:
                normalize_url(acc["inbox_url"])
                parse_tempmail_address(acc["inbox_url"])
                if not acc.get("manuscript_path"):
                    raise PostingError(f"{acc['velog_id']} 계정의 원고가 지정되지 않았습니다.")
                read_manuscript(acc["manuscript_path"])
        except PostingError as exc:
            messagebox.showwarning("등록 정보 확인", str(exc), parent=self)
            return

        self._run_total = len(pending)
        self._run_done = 0
        self._set_progress(0, self._run_total)
        self._save_settings()
        self._set_running(True)
        self._append(f"[{tab['title']}] 미발행 {len(pending)}개 계정 자동 출간을 시작합니다.", "info")
        self._poster = VelogPoster(self._post_event, self._on_result)
        profile_names = self._parse_profile_names()
        anchors = [dict(a) for a in self.anchors]
        homepages = list(self.homepages)
        accounts = [
            dict(
                a,
                image_folder=image_folder,
                profile_names=profile_names,
                anchors=anchors,
                homepages=homepages,
            )
            for a in pending
        ]
        self._worker = threading.Thread(target=self._run, args=(accounts,), daemon=True)
        self._worker.start()

    def _on_result(self, velog_id: str, url: str) -> None:
        self._events.put((f"{velog_id}\t{url}", "result"))

    def _add_anchor(self) -> None:
        text = self.anchor_text.get().strip()
        url = self.anchor_url.get().strip()
        if not text or not url:
            messagebox.showwarning("입력 확인", "앵커텍스트와 사이트 주소를 모두 입력해 주세요.", parent=self)
            return
        try:
            url = normalize_url(url)
        except PostingError as exc:
            messagebox.showwarning("주소 확인", str(exc), parent=self)
            return
        self.anchors.append({"anchor": text, "url": url})
        self.anchor_text.set("")
        self.anchor_url.set("")
        self._refresh_anchor_list()
        self._save_settings()

    def _delete_anchor(self) -> None:
        sel = list(self.anchor_list.curselection())
        if not sel:
            return
        for i in reversed(sel):
            del self.anchors[i]
        self._refresh_anchor_list()
        self._save_settings()

    def _refresh_anchor_list(self) -> None:
        self.anchor_list.delete(0, "end")
        for a in self.anchors:
            self.anchor_list.insert("end", f"{a['anchor']}  →  {a['url']}")

    def _delete_homepage(self) -> None:
        selected = {self.homepage_list.get(i) for i in self.homepage_list.curselection()}
        if not selected:
            return
        self.homepages = [u for u in self.homepages if u not in selected]
        self._refresh_homepage_list()
        self._save_settings()

    def _bulk_add_homepages(self) -> None:
        raw = self.homepage_bulk.get("1.0", "end").strip()
        if not raw:
            messagebox.showwarning("입력 확인", "추가할 URL을 입력해 주세요.", parent=self)
            return
        parts = re.split(r"[\s,;]+", raw)
        added = 0
        skipped = 0
        for part in parts:
            token = part.strip()
            if not token:
                continue
            try:
                url = normalize_url(token)
            except PostingError:
                skipped += 1
                continue
            if url in self.homepages:
                skipped += 1
                continue
            self.homepages.append(url)
            added += 1
        self.homepage_bulk.delete("1.0", "end")
        self._refresh_homepage_list()
        self._save_settings()
        messagebox.showinfo(
            "일괄 추가 완료",
            f"추가: {added}개\n건너뜀(중복·오류): {skipped}개",
            parent=self,
        )

    def _refresh_homepage_list(self) -> None:
        query = self.homepage_search.get().strip().lower()
        self.homepage_list.delete(0, "end")
        for url in self.homepages:
            if not query or query in url.lower():
                self.homepage_list.insert("end", url)

    def _parse_profile_names(self) -> list[str]:
        raw = self.profile_text.get("1.0", "end")
        names: list[str] = []
        for chunk in raw.replace("\n", ",").split(","):
            name = chunk.strip()
            if name and name not in names:
                names.append(name)
        return names or list(DEFAULT_PROFILE_NAMES)

    @staticmethod
    def _check_image_folder(folder: str) -> None:
        path = Path(folder)
        if not path.is_dir():
            raise PostingError("이미지 폴더 경로가 올바르지 않습니다.")
        if not any(p.suffix.lower() in IMG_EXTS for p in path.iterdir() if p.is_file()):
            raise PostingError("이미지 폴더에 등록할 이미지 파일이 없습니다.")

    def _run(self, accounts: list[dict[str, str]]) -> None:
        try:
            assert self._poster is not None
            self._poster.run_batch(accounts)
        except PostingError as exc:
            self._post_event(str(exc), "error")
        except Exception as exc:  # noqa: BLE001
            self._post_event(f"예상하지 못한 오류: {exc}", "error")
        finally:
            self._events.put(("__done__", "done"))

    def _stop(self) -> None:
        if self._poster is not None:
            self.status.set("중단하는 중...")
            self._append("중단을 요청했습니다.", "info")
            self._poster.stop()
            self.stop_btn.configure(state="disabled")

    def _post_event(self, message: str, level: str) -> None:
        self._events.put((message, level))

    def _drain_events(self) -> None:
        try:
            while True:
                message, level = self._events.get_nowait()
                if level == "done":
                    self._set_running(False)
                    self._poster = None
                    self._worker = None
                    self._set_progress(self._run_done, self._run_total)
                    continue
                if level == "result":
                    velog_id, _, url = message.partition("\t")
                    self._run_done += 1
                    self._set_progress(self._run_done, self._run_total)
                    self._mark_published(velog_id, url)
                    continue
                self.status.set(message)
                tag = "success" if level == "success" else ("error" if level == "error" else "info")
                self._append(message, tag)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _mark_published(self, velog_id: str, url: str) -> None:
        tab = self._active_tab or self._current_tab()
        if tab is None:
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        accounts = tab["accounts"]
        for acc in accounts:
            if acc.get("velog_id") == velog_id and not acc.get("published_url"):
                acc["published_url"] = url
                acc["published_at"] = now
                break
        else:
            for acc in accounts:
                if acc.get("velog_id") == velog_id:
                    acc["published_url"] = url
                    acc["published_at"] = now
                    break
        self._fill_tree(tab)
        self._update_summary()
        self._save_settings()
        self._append(f"{velog_id} 발행됨: {url}", "success")

    def _set_running(self, running: bool) -> None:
        self.start_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        if running:
            self.status.set("출간 작업 진행 중...")
        elif self.status.get().endswith("중단하는 중..."):
            self.status.set("중단됨")
        elif not running:
            self.status.set("작업이 완료되었습니다.")

    def _append(self, message: str, tag: str = "") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{ts}] {message}\n", tag)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    @staticmethod
    def _clean_accounts(raw: list, now_iso: str) -> list:
        result = []
        for a in raw:
            if not (isinstance(a, dict) and a.get("velog_id")):
                continue
            acc = {k: str(a.get(k, "")) for k in ACCOUNT_KEYS}
            if not acc.get("created_at"):
                acc["created_at"] = now_iso
            result.append(acc)
        return result

    # -- 임시 메일 생성 ---------------------------------------------------
    def _refresh_tm_tree(self) -> None:
        self.tm_tree.delete(*self.tm_tree.get_children())
        for index, item in enumerate(self.generated_emails):
            self.tm_tree.insert(
                "", "end", iid=str(index),
                values=(item.get("email", ""), item.get("inbox_url", ""), item.get("created_at", "")),
            )

    def _append_tm_log(self, message: str, tag: str = "") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.tm_log_box.configure(state="normal")
        self.tm_log_box.insert("end", f"[{ts}] {message}\n", tag)
        self.tm_log_box.see("end")
        self.tm_log_box.configure(state="disabled")

    def _clear_tm_log(self) -> None:
        self.tm_log_box.configure(state="normal")
        self.tm_log_box.delete("1.0", "end")
        self.tm_log_box.configure(state="disabled")

    def _post_tm_event(self, message: str, level: str) -> None:
        self._tm_events.put((message, level))

    def _drain_tm_events(self) -> None:
        try:
            while True:
                message, level = self._tm_events.get_nowait()
                if level == "done":
                    self._set_tm_running(False)
                    self._tm_generator = None
                    self._tm_worker = None
                    continue
                if level == "created":
                    email, _, url = message.partition("\t")
                    self._on_tempmail_created(email, url)
                    continue
                self.tm_status.set(message)
                tag = "success" if level == "success" else ("error" if level == "error" else "info")
                self._append_tm_log(message, tag)
        except queue.Empty:
            pass
        self.after(100, self._drain_tm_events)

    def _on_tempmail_created(self, email: str, inbox_url: str) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.generated_emails.append({
            "email": email,
            "inbox_url": inbox_url,
            "created_at": now,
        })
        self._tm_run_done += 1
        self._set_tm_progress(self._tm_run_done, self._tm_run_total)
        self._refresh_tm_tree()
        self._save_settings()
        self._append_tm_log(f"저장됨: {email}", "success")

    def _set_tm_progress(self, done: int, total: int) -> None:
        if total <= 0:
            self.tm_progress.configure(value=0, maximum=100)
            self.tm_progress_text.set("")
            return
        self.tm_progress.configure(maximum=total, value=done)
        self.tm_progress_text.set(f"{done} / {total}")

    def _set_tm_running(self, running: bool) -> None:
        self.tm_start_btn.configure(state="disabled" if running else "normal")
        self.tm_stop_btn.configure(state="normal" if running else "disabled")
        if running:
            self.tm_status.set("임시 메일 생성 중...")
        elif not running and self.tm_status.get().endswith("중단하는 중..."):
            self.tm_status.set("중단됨")
        elif not running:
            self.tm_status.set("작업이 완료되었습니다.")

    def _start_tempmail(self) -> None:
        if self._tm_worker is not None:
            return
        try:
            count = int(self.tm_count.get())
        except (tk.TclError, ValueError):
            messagebox.showwarning("입력 확인", "생성 개수를 확인해 주세요.", parent=self)
            return
        if count < 1 or count > 50:
            messagebox.showwarning("입력 확인", "생성 개수는 1~50 사이로 입력해 주세요.", parent=self)
            return

        self._tm_run_total = count
        self._tm_run_done = 0
        self._set_tm_progress(0, count)
        self._set_tm_running(True)
        self._append_tm_log(f"{count}개 임시 메일 생성을 시작합니다.", "info")

        def on_created(email: str, url: str) -> None:
            self._tm_events.put((f"{email}\t{url}", "created"))

        self._tm_generator = TempMailGenerator(self._post_tm_event, on_created)
        self._tm_worker = threading.Thread(
            target=self._run_tempmail, args=(count,), daemon=True,
        )
        self._tm_worker.start()

    def _run_tempmail(self, count: int) -> None:
        try:
            assert self._tm_generator is not None
            self._tm_generator.run_batch(count)
        except PostingError as exc:
            self._post_tm_event(str(exc), "error")
        except Exception as exc:  # noqa: BLE001
            self._post_tm_event(f"예상하지 못한 오류: {exc}", "error")
        finally:
            self._tm_events.put(("__done__", "done"))

    def _stop_tempmail(self) -> None:
        if self._tm_generator is not None:
            self.tm_status.set("중단하는 중...")
            self._append_tm_log("중단을 요청했습니다.", "info")
            self._tm_generator.stop()
            self.tm_stop_btn.configure(state="disabled")

    def _tm_selected_indices(self) -> list[int]:
        return sorted(int(iid) for iid in self.tm_tree.selection())

    def _on_tm_double_click(self, event) -> None:
        if self.tm_tree.identify("region", event.x, event.y) != "cell":
            return
        row = self.tm_tree.identify_row(event.y)
        if not row:
            return
        url = self.generated_emails[int(row)].get("inbox_url", "")
        if url:
            webbrowser.open(url)

    def _copy_tm_urls(self, _event=None) -> str:
        urls = [
            self.generated_emails[i].get("inbox_url", "")
            for i in self._tm_selected_indices()
            if self.generated_emails[i].get("inbox_url")
        ]
        if urls:
            self.clipboard_clear()
            self.clipboard_append("\n".join(urls))
            self.tm_status.set(f"URL {len(urls)}개를 클립보드에 복사했습니다.")
        return "break"

    def _add_generated_to_accounts(self, all_items: bool = False) -> None:
        if all_items:
            indices = list(range(len(self.generated_emails)))
        else:
            indices = self._tm_selected_indices()
        if not indices:
            messagebox.showwarning(
                "선택 확인", "계정 목록에 추가할 항목을 선택해 주세요.", parent=self,
            )
            return

        now_iso = datetime.now().isoformat(timespec="seconds")
        existing = {(a.get("velog_id"), a.get("inbox_url")) for a in self.accounts}
        added = 0
        for i in indices:
            item = self.generated_emails[i]
            email = item.get("email", "").strip()
            url = item.get("inbox_url", "").strip()
            if not email or not url:
                continue
            if (email, url) in existing:
                continue
            self.accounts.append({
                "velog_id": email,
                "inbox_url": url,
                "manuscript_path": "",
                "created_at": now_iso,
                "mail_mismatch": "",
            })
            existing.add((email, url))
            added += 1

        if added == 0:
            messagebox.showinfo("추가 결과", "추가할 새 계정이 없습니다 (이미 등록됨).", parent=self)
            return

        self._refresh_tree()
        self._save_settings()
        self._switch_main_view("posting")
        messagebox.showinfo("추가 완료", f"{added}개 계정이 포스팅 탭 목록에 추가되었습니다.", parent=self)

    def _delete_generated(self) -> None:
        sel = self._tm_selected_indices()
        if not sel:
            return
        if not messagebox.askyesno("삭제 확인", f"선택한 {len(sel)}개 항목을 삭제할까요?", parent=self):
            return
        for index in reversed(sel):
            del self.generated_emails[index]
        self._refresh_tm_tree()
        self._save_settings()

    def _load_settings(self) -> None:
        names = list(DEFAULT_PROFILE_NAMES)
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            self.image_folder.set(str(data.get("image_folder", "")))
            now_iso = datetime.now().isoformat(timespec="seconds")

            tabs = data.get("tabs")
            if isinstance(tabs, list) and tabs:
                for t in tabs:
                    if not isinstance(t, dict):
                        continue
                    self.tabs.append({
                        "title": str(t.get("title", "탭")),
                        "accounts": self._clean_accounts(t.get("accounts", []), now_iso),
                    })
            else:
                self.tabs.append({
                    "title": "기본",
                    "accounts": self._clean_accounts(data.get("accounts", []), now_iso),
                })

            nm = data.get("profile_names")
            if isinstance(nm, list) and nm:
                names = nm
            anchors = data.get("anchors", [])
            if isinstance(anchors, list):
                self.anchors = [
                    {"anchor": str(a.get("anchor", "")), "url": str(a.get("url", ""))}
                    for a in anchors
                    if isinstance(a, dict) and a.get("anchor") and a.get("url")
                ]
            pages = data.get("homepages", [])
            if isinstance(pages, list):
                self.homepages = []
                for item in pages:
                    raw = str(item).strip()
                    if not raw:
                        continue
                    try:
                        self.homepages.append(normalize_url(raw))
                    except PostingError:
                        continue
            generated = data.get("generated_emails", [])
            if isinstance(generated, list):
                self.generated_emails = [
                    {
                        "email": str(g.get("email", "")),
                        "inbox_url": str(g.get("inbox_url", "")),
                        "created_at": str(g.get("created_at", "")),
                    }
                    for g in generated
                    if isinstance(g, dict) and g.get("email") and g.get("inbox_url")
                ]
        except (OSError, ValueError):
            pass
        if not self.tabs:
            self.tabs.append({"title": "기본", "accounts": []})
        self.profile_text.delete("1.0", "end")
        self.profile_text.insert("1.0", ", ".join(names))
        self._refresh_anchor_list()
        self._refresh_homepage_list()
        self._rebuild_tabs()
        if hasattr(self, "tm_tree"):
            self._refresh_tm_tree()

    def _save_settings(self) -> None:
        try:
            tabs = [{"title": t["title"], "accounts": t["accounts"]} for t in self.tabs]
            SETTINGS_PATH.write_text(
                json.dumps(
                    {
                        "image_folder": self.image_folder.get().strip(),
                        "profile_names": self._parse_profile_names(),
                        "anchors": self.anchors,
                        "homepages": self.homepages,
                        "tabs": tabs,
                        "generated_emails": self.generated_emails,
                    },
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _on_close(self) -> None:
        if self._poster is not None:
            self._poster.stop()
        if self._tm_generator is not None:
            self._tm_generator.stop()
        self.destroy()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    VelogApp().mainloop()
