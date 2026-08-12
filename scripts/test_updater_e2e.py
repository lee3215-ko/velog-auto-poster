"""자동 업데이트 설치기 E2E 테스트.

실제 사용자 증상(게이지 100% → 앱 종료 → 파일 미교체)을 재현한다.
1) 가짜 설치 폴더 + zip 준비
2) 자식 프로세스가 schedule_apply_update 후 os._exit(0)
3) 설치 스크립트가 살아남아 파일을 교체했는지 확인
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from updater import get_update_log_path, schedule_apply_update  # noqa: E402


def _make_fake_exe(path: Path, label: str) -> None:
    cmd = Path(os.environ.get("ComSpec", r"C:\Windows\System32\cmd.exe"))
    shutil.copy2(cmd, path)
    path.with_suffix(".label").write_text(label, encoding="utf-8")


def _build_zip(zip_path: Path, inner: str, exe_name: str, label: str) -> None:
    staging = zip_path.parent / "zip_src"
    if staging.exists():
        shutil.rmtree(staging)
    app_dir = staging / inner
    app_dir.mkdir(parents=True)
    _make_fake_exe(app_dir / exe_name, label)
    (app_dir / "VERSION.txt").write_text(label, encoding="utf-8")
    (app_dir / "new_only.txt").write_text("from-zip", encoding="utf-8")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file in app_dir.rglob("*"):
            if file.is_file():
                zf.write(file, file.relative_to(staging).as_posix())


def run_child() -> None:
    install = Path(os.environ["VELO_UPD_INSTALL"])
    zip_path = Path(os.environ["VELO_UPD_ZIP"])
    exe_name = os.environ["VELO_UPD_EXE"]
    inner = os.environ["VELO_UPD_INNER"]
    marker = schedule_apply_update(
        zip_path,
        install_dir=install,
        exe_name=exe_name,
        zip_inner_folder=inner,
        app_slug="VelogPosterTest",
        require_frozen=False,
        verify_started=True,
    )
    print(f"CHILD_MARKER={marker}", flush=True)
    time.sleep(0.5)
    os._exit(0)


def _print_log() -> None:
    log = get_update_log_path()
    print("--- update log ---")
    if log.is_file():
        print(log.read_text(encoding="utf-8", errors="ignore")[-4000:])
    else:
        print("(missing)")


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="velog_upd_e2e_"))
    install = work / "install" / "VelogPoster"
    install.mkdir(parents=True)
    exe_name = "VelogPoster.exe"
    inner = "VelogPoster"

    _make_fake_exe(install / exe_name, "OLD")
    (install / "VERSION.txt").write_text("OLD", encoding="utf-8")
    (install / "velog_settings.json").write_text('{"keep": true}', encoding="utf-8")
    (install / "old_only.txt").write_text("should-remain-or-overwrite-ok", encoding="utf-8")

    zip_path = work / "VelogPoster.zip"
    _build_zip(zip_path, inner, exe_name, "NEW")

    print(f"work={work}")
    print(f"log={get_update_log_path()}")

    env = os.environ.copy()
    env["VELO_UPD_CHILD"] = "1"
    env["VELO_UPD_INSTALL"] = str(install)
    env["VELO_UPD_ZIP"] = str(zip_path)
    env["VELO_UPD_EXE"] = exe_name
    env["VELO_UPD_INNER"] = inner

    child = subprocess.Popen([sys.executable, str(Path(__file__).resolve())], env=env)
    child_rc = child.wait(timeout=90)
    print(f"child_exit={child_rc}")
    if child_rc != 0:
        print("FAIL: child did not exit cleanly (installer may not have started)")
        _print_log()
        return 1

    deadline = time.time() + 90
    ok = False
    while time.time() < deadline:
        version = (install / "VERSION.txt").read_text(encoding="utf-8").strip()
        label = install / f"{Path(exe_name).stem}.label"
        label_text = label.read_text(encoding="utf-8").strip() if label.exists() else ""
        settings = (install / "velog_settings.json").read_text(encoding="utf-8")
        new_only = install / "new_only.txt"
        if version == "NEW" and label_text == "NEW" and new_only.is_file():
            if '"keep": true' not in settings:
                print("FAIL: settings not preserved")
                _print_log()
                return 1
            ok = True
            break
        time.sleep(0.5)

    _print_log()
    if not ok:
        print("FAIL: install files were not updated after parent exit")
        print(f"VERSION={(install / 'VERSION.txt').read_text(encoding='utf-8')!r}")
        return 1

    print("PASS: updater survived parent exit and replaced files; settings preserved")
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    if os.environ.get("VELO_UPD_CHILD") == "1":
        run_child()
    else:
        raise SystemExit(main())
