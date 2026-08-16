"""앱 버전·업데이트 URL (배포 시 publish.ps1 이 자동 갱신)."""

from __future__ import annotations

import os
from pathlib import Path

APP_VERSION = "1.0.45"
APP_NAME = "VelogPoster"
EXE_NAME = "VelogPoster.exe"
ZIP_INNER_FOLDER = "VelogPoster"

GITHUB_OWNER = "lee3215-ko"
GITHUB_REPO = "velog-auto-poster"

UPDATE_VERSION_URL = (
    f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/main/version.json"
)


def get_data_dir() -> Path:
    """빌드/업데이트와 무관하게 설정을 보관하는 폴더."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        root = Path(base) / APP_NAME
    else:
        root = Path.home() / f".{APP_NAME}"
    root.mkdir(parents=True, exist_ok=True)
    return root


DATA_DIR = get_data_dir()
SETTINGS_PATH = DATA_DIR / "velog_settings.json"
