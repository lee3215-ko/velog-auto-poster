"""Tkinter update dialog."""

from __future__ import annotations

import tempfile
import threading
import urllib.error
import webbrowser
from tkinter import messagebox, ttk

from paths import APP_NAME, EXE_NAME, ZIP_INNER_FOLDER
from updater import (
    UpdateInfo,
    can_auto_update,
    check_for_update,
    download_file,
    schedule_apply_update,
)


def _center_on_parent(window, parent) -> None:
    window.update_idletasks()
    parent.update_idletasks()
    pw = max(parent.winfo_width(), parent.winfo_reqwidth())
    ph = max(parent.winfo_height(), parent.winfo_reqheight())
    px = parent.winfo_rootx()
    py = parent.winfo_rooty()
    w = max(window.winfo_width(), window.winfo_reqwidth())
    h = max(window.winfo_height(), window.winfo_reqheight())
    x = px + max((pw - w) // 2, 0)
    y = py + max((ph - h) // 2, 0)
    window.geometry(f"+{x}+{y}")


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

    frame = ttk.Frame(dialog, padding=16)
    frame.pack(fill="both", expand=True)

    ttk.Label(frame, text=message, justify="left", wraplength=360).pack(anchor="w")
    btn_row = ttk.Frame(frame)
    btn_row.pack(fill="x", pady=(16, 0))

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
        info = check_for_update(version_url, current_version, app_name=app_name)
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
                "\n\n앱을 종료한 뒤 자동으로 업데이트합니다."
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
    from pathlib import Path

    dialog = tk.Toplevel(root)
    dialog.title("업데이트 중")
    dialog.geometry("340x100")
    dialog.transient(root)
    dialog.grab_set()
    dialog.resizable(False, False)

    status = ttk.Label(dialog, text="다운로드 중...")
    status.pack(padx=16, pady=(16, 8))
    bar = ttk.Progressbar(dialog, length=300, mode="determinate")
    bar.pack(padx=16, pady=8)
    _center_on_parent(dialog, root)

    def on_progress(done: int, total: int) -> None:
        if total > 0:
            pct = min(int(done * 100 / total), 100)
            root.after(0, lambda: (bar.configure(value=pct), status.configure(text=f"다운로드 {pct}%")))
        else:
            root.after(0, lambda: status.configure(text="다운로드 중..."))

    def worker() -> None:
        zip_path = Path(tempfile.gettempdir()) / f"{app_name}-{info.version}.zip"
        try:
            download_file(
                info.url,
                zip_path,
                user_agent=f"{app_name}/{info.version}",
                on_progress=on_progress,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            root.after(0, dialog.destroy)
            root.after(0, lambda: messagebox.showerror("업데이트 실패", str(exc), parent=root))
            return

        def finish() -> None:
            try:
                schedule_apply_update(
                    zip_path,
                    exe_name=exe_name,
                    zip_inner_folder=zip_inner_folder,
                    app_slug=app_name,
                )
            except RuntimeError as exc:
                messagebox.showerror("업데이트 실패", str(exc), parent=root)
                dialog.destroy()
                return
            dialog.destroy()
            root.quit()

        root.after(0, lambda: status.configure(text="설치 준비 중..."))
        root.after(500, finish)

    threading.Thread(target=worker, daemon=True).start()
