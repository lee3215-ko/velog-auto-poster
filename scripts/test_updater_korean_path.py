"""한글 경로 설치 폴더에서도 업데이트가 되는지 확인."""

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
sys.path.insert(0, str(ROOT))


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="velog_ko_"))
    install = work / "설치폴더" / "VelogPoster"
    install.mkdir(parents=True)
    exe_name, inner = "VelogPoster.exe", "VelogPoster"
    cmd = Path(os.environ.get("ComSpec", r"C:\Windows\System32\cmd.exe"))
    shutil.copy2(cmd, install / exe_name)
    (install / "VERSION.txt").write_text("OLD", encoding="utf-8")
    (install / "velog_settings.json").write_text('{"keep":1}', encoding="utf-8")

    src = work / "zip_src" / inner
    src.mkdir(parents=True)
    shutil.copy2(cmd, src / exe_name)
    (src / "VERSION.txt").write_text("NEW", encoding="utf-8")
    (src / "new_only.txt").write_text("x", encoding="utf-8")
    zpath = work / "VelogPoster.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        for f in src.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(work / "zip_src").as_posix())

    child_py = work / "child.py"
    child_py.write_text(
        "import os, sys, time\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, r'{ROOT}')\n"
        "from updater import schedule_apply_update\n"
        f"schedule_apply_update(Path(r'{zpath}'), install_dir=Path(r'{install}'), "
        f"exe_name='{exe_name}', zip_inner_folder='{inner}', app_slug='VelogKo', "
        "require_frozen=False, verify_started=True)\n"
        "time.sleep(0.3)\n"
        "os._exit(0)\n",
        encoding="utf-8",
    )
    rc = subprocess.call([sys.executable, str(child_py)])
    print("child_rc", rc)
    if rc != 0:
        return 1

    deadline = time.time() + 60
    while time.time() < deadline:
        version = (install / "VERSION.txt").read_text(encoding="utf-8").strip()
        if version == "NEW" and (install / "new_only.txt").is_file():
            settings = (install / "velog_settings.json").read_text(encoding="utf-8")
            if "keep" in settings:
                print("PASS korean path update")
                shutil.rmtree(work, ignore_errors=True)
                return 0
            print("FAIL settings")
            return 1
        time.sleep(0.4)
    print("FAIL timeout", install)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
