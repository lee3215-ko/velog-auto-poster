"""Tkinter update dialog."""

from __future__ import annotations

import os
import tempfile
import threading
import time
import urllib.error
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk

from paths import APP_NAME, EXE_NAME, ZIP_INNER_FOLDER
from updater import (
    UpdateInfo,
    can_auto_update,
    check_for_update,
    download_file_with_fallbacks,
    format_network_error,
    get_update_log_path,
    schedule_apply_update,
    validate_zip_file,
)


def _work_area() -> tuple[int, int, int, int]:
    try:
        import ctypes

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        rect = RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            width = int(rect.right - rect.left)
            height = int(rect.bottom - rect.top)
            if width > 200 and height > 200:
                return int(rect.left), int(rect.top), width, height
    except Exception:  # noqa: BLE001
        pass
    return 0, 0, 1280, 720


def _center_on_parent(window, parent) -> None:
    window.update_idletasks()
    parent.update_idletasks()
    ax, ay, aw, ah = _work_area()
    pw = max(parent.winfo_width(), parent.winfo_reqwidth(), 1)
    ph = max(parent.winfo_height(), parent.winfo_reqheight(), 1)
    px = parent.winfo_rootx()
    py = parent.winfo_rooty()
    w = min(max(window.winfo_width(), window.winfo_reqwidth(), 280), max(aw - 24, 280))
    h = min(max(window.winfo_height(), window.winfo_reqheight(), 120), max(ah - 24, 120))
    x = px + max((pw - w) // 2, 0)
    y = py + max((ph - h) // 2, 0)
    x = min(max(x, ax), ax + max(aw - w, 0))
    y = min(max(y, ay), ay + max(ah - h, 0))
    window.geometry(f"{w}x{h}+{x}+{y}")


def _ask_update(root, title: str, message: str, *, kind: str) -> bool | None:
    """부모 창 중앙에 업데이트 확인 대화상자를 띄운다."""
    import tkinter as tk

    root.update_idletasks()
    result: bool | None = None
    dialog = tk.Toplevel(root)
    dialog.title(title)
    dialog.transient(root)
    dialog.grab_set()
    dialog.resizable(False, False)
    dialog.attributes("-topmost", True)

    frame = ttk.Frame(dialog, padding=16)
    frame.pack(fill="both", expand=True)

    btn_row = ttk.Frame(frame)
    btn_row.pack(side="bottom", fill="x", pady=(16, 0))
    ttk.Label(frame, text=message, justify="left", wraplength=360).pack(
        fill="both", expand=True, anchor="w",
    )

    def close(value: bool | None) -> None:
        nonlocal result
        result = value
        dialog.destroy()

    if kind == "okcancel":
        ttk.Button(btn_row, text="확인", command=lambda: close(True)).pack(side="right")
        ttk.Button(btn_row, text="취소", command=lambda: close(False)).pack(side="right", padx=(0, 8))
    elif kind == "yesnocancel":
        ttk.Button(btn_row, text="예", command=lambda: close(True)).pack(side="right")
        ttk.Button(btn_row, text="아니오", command=lambda: close(False)).pack(side="right", padx=(0, 8))
        ttk.Button(btn_row, text="취소", command=lambda: close(None)).pack(side="right", padx=(0, 8))
    else:
        ttk.Button(btn_row, text="예", command=lambda: close(True)).pack(side="right")
        ttk.Button(btn_row, text="아니오", command=lambda: close(False)).pack(side="right", padx=(0, 8))

    dialog.protocol("WM_DELETE_WINDOW", lambda: close(None if kind == "yesnocancel" else False))
    _center_on_parent(dialog, root)
    dialog.lift()
    dialog.focus_force()
    root.wait_window(dialog)
    return result


def schedule_update_check(
    root,
    *,
    version_url: str,
    current_version: str,
    app_name: str = APP_NAME,
    exe_name: str = EXE_NAME,
    delay_ms: int = 1500,
    zip_inner_folder: str | None = ZIP_INNER_FOLDER,
    auto_apply: bool = True,
) -> None:
    """앱 시작 시 GitHub version.json 확인 → 새 버전이면 업데이트 안내(또는 자동 적용)."""
    if not version_url.strip():
        return

    def worker() -> None:
        try:
            info = check_for_update(version_url, current_version, app_name=app_name)
        except Exception:  # noqa: BLE001
            return
        if info is not None:
            root.after(
                0,
                lambda: _show_dialog(
                    root, info, current_version, app_name, exe_name, zip_inner_folder, auto_apply,
                ),
            )

    root.after(delay_ms, lambda: threading.Thread(target=worker, daemon=True).start())


def _show_dialog(
    root,
    info: UpdateInfo,
    current_version: str,
    app_name: str,
    exe_name: str,
    zip_inner_folder,
    auto_apply: bool,
):
    message = f"새 버전 {info.version}이 있습니다.\n(현재: {current_version})"
    if info.notes:
        message += f"\n\n{info.notes}"

    if can_auto_update() and info.url:
        if auto_apply:
            message += (
                "\n\n확인을 누르면 다운로드 후 앱을 종료하고 자동 업데이트합니다."
                "\n(계정·설정은 그대로 유지됩니다)"
            )
            if _ask_update(root, "업데이트", message, kind="okcancel"):
                _auto_update(root, info, app_name, exe_name, zip_inner_folder)
            return
        message += (
            "\n\n「예」= 자동 업데이트 후 재실행"
            "\n(계정·설정은 그대로 유지됩니다)"
            "\n「아니오」= 브라우저에서 받기"
        )
        choice = _ask_update(root, "업데이트", message, kind="yesnocancel")
        if choice is True:
            _auto_update(root, info, app_name, exe_name, zip_inner_folder)
        elif choice is False:
            webbrowser.open(info.url)
        return

    message += "\n\nzip을 받아 설치 폴더에 덮어쓴 뒤 다시 실행하세요.\n다운로드 페이지를 열까요?"
    if _ask_update(root, "업데이트", message, kind="yesno") and info.url:
        webbrowser.open(info.url)


def _auto_update(root, info: UpdateInfo, app_name: str, exe_name: str, zip_inner_folder):
    import tkinter as tk

    dialog = tk.Toplevel(root)
    dialog.title("업데이트 중")
    dialog.minsize(320, 120)
    dialog.transient(root)
    dialog.grab_set()
    dialog.resizable(False, False)
    dialog.attributes("-topmost", True)
    dialog.protocol("WM_DELETE_WINDOW", lambda: None)

    status = ttk.Label(dialog, text="다운로드 준비 중...")
    status.pack(padx=16, pady=(16, 8))
    bar = ttk.Progressbar(dialog, length=320, mode="indeterminate")
    bar.pack(padx=16, pady=8)
    bar.start(12)
    _center_on_parent(dialog, root)
    dialog.lift()
    dialog.focus_force()
    dialog.update()

    def set_status(text: str, *, determinate: bool = False, value: int = 0) -> None:
        def apply() -> None:
            status.configure(text=text)
            if determinate:
                if str(bar["mode"]) != "determinate":
                    bar.stop()
                    bar.configure(mode="determinate", maximum=100, value=value)
                else:
                    bar.configure(value=value)
            dialog.lift()

        root.after(0, apply)

    def fail(exc: BaseException) -> None:
        detail = format_network_error(exc)

        def apply() -> None:
            try:
                dialog.destroy()
            except Exception:  # noqa: BLE001
                pass
            messagebox.showerror(
                "업데이트 실패",
                f"업데이트에 실패했습니다.\n\n{detail}\n\n"
                "브라우저에서 최신 zip을 받아 설치 폴더에 덮어써 주세요.",
                parent=root,
            )
            if info.url:
                webbrowser.open(info.url)

        root.after(0, apply)

    def worker() -> None:
        zip_path = Path(tempfile.gettempdir()) / f"{app_name}-{info.version}.zip"
        log_path = get_update_log_path()
        try:
            set_status("다운로드 중...")

            def on_progress(done: int, total: int) -> None:
                if total > 0:
                    pct = min(int(done * 100 / total), 100)
                    set_status(f"다운로드 {pct}%", determinate=True, value=pct)
                else:
                    mb = done / (1024 * 1024)
                    set_status(f"다운로드 중... {mb:.1f} MB")

            urls = list(info.download_urls) if info.download_urls else [info.url]
            download_file_with_fallbacks(
                urls,
                zip_path,
                user_agent=f"{app_name}/{info.version}",
                on_progress=on_progress,
            )
            # onedir 빌드는 수 MB 이상이어야 정상
            validate_zip_file(zip_path, min_bytes=1024 * 1024)
            set_status("설치 준비 중...", determinate=True, value=100)
        except Exception as exc:  # noqa: BLE001
            fail(exc)
            return

        def finish() -> None:
            try:
                status.configure(text="설치 스크립트 시작 중...")
                dialog.update_idletasks()
                schedule_apply_update(
                    zip_path,
                    exe_name=exe_name,
                    zip_inner_folder=zip_inner_folder,
                    app_slug=app_name,
                    verify_started=True,
                )
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror(
                    "업데이트 실패",
                    f"{exc}\n\n로그: {log_path}\n\n"
                    "자동 설치가 시작되지 않았습니다. 앱은 종료하지 않습니다.\n"
                    "브라우저에서 zip을 받아 설치 폴더에 덮어써 주세요.",
                    parent=root,
                )
                try:
                    dialog.destroy()
                except Exception:  # noqa: BLE001
                    pass
                if info.url:
                    webbrowser.open(info.url)
                return

            try:
                status.configure(text="설치 중... 앱을 종료합니다.")
                dialog.update_idletasks()
            except Exception:  # noqa: BLE001
                pass
            try:
                dialog.destroy()
            except Exception:  # noqa: BLE001
                pass
            # 설치 스크립트가 WaitPid 를 감지할 시간을 준다.
            time.sleep(1.0)
            os._exit(0)

        root.after(0, finish)

    threading.Thread(target=worker, daemon=True).start()
