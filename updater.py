"""GitHub version.json check and Windows onedir auto-update."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_RAW_GITHUB_RE = re.compile(
    r"^https://raw\.githubusercontent\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?P<branch>[^/]+)/(?P<path>.+)$"
)

# 업데이트 시 덮어쓰지 않을 사용자 데이터
_PRESERVE_FILES = frozenset({"velog_settings.json"})


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    url: str
    notes: str


def parse_version(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for part in version.strip().split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts or (0,))


def is_newer(remote_version: str, local_version: str) -> bool:
    return parse_version(remote_version) > parse_version(local_version)


def _github_api_url(raw_url: str) -> str | None:
    match = _RAW_GITHUB_RE.match(raw_url.strip())
    if match is None:
        return None
    owner = match.group("owner")
    repo = match.group("repo")
    branch = match.group("branch")
    path = match.group("path")
    return (
        f"https://api.github.com/repos/{owner}/{repo}/contents/"
        f"{urllib.parse.quote(path)}?ref={urllib.parse.quote(branch)}"
    )


def _decode_json_bytes(data: bytes) -> dict:
    text = data.decode("utf-8-sig")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("version.json must be a JSON object")
    return payload


def _fetch_via_github_api(api_url: str, user_agent: str) -> dict | None:
    request = urllib.request.Request(
        api_url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        meta = json.loads(response.read().decode("utf-8-sig"))
    content = base64.b64decode(meta["content"]).decode("utf-8-sig")
    return _decode_json_bytes(content.encode("utf-8"))


def _fetch_via_raw_url(raw_url: str, user_agent: str) -> dict:
    parsed = urllib.parse.urlparse(raw_url.strip())
    query = urllib.parse.parse_qs(parsed.query)
    query["_"] = [str(int(time.time()))]
    busted_url = parsed._replace(query=urllib.parse.urlencode(query, doseq=True)).geturl()
    request = urllib.request.Request(
        busted_url,
        headers={"User-Agent": user_agent, "Cache-Control": "no-cache"},
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        return _decode_json_bytes(response.read())


def _fetch_via_releases_api(owner: str, repo: str, user_agent: str) -> dict | None:
    """version.json 이 깨져 있어도 최신 Release 정보를 가져온다."""
    import requests

    api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    response = requests.get(
        api_url,
        headers={"User-Agent": user_agent, "Accept": "application/vnd.github+json"},
        timeout=12,
    )
    if not response.ok:
        return None
    data = response.json()
    tag = str(data.get("tag_name", "")).strip().lstrip("v")
    if not tag:
        return None
    download = ""
    for asset in data.get("assets", []):
        name = str(asset.get("name", ""))
        if name.lower().endswith(".zip"):
            download = str(asset.get("browser_download_url", "")).strip()
            break
    if not download:
        download = f"https://github.com/{owner}/{repo}/releases/latest/download/VelogPoster.zip"
    return {
        "version": tag,
        "url": download,
        "notes": str(data.get("body", "")).strip(),
    }


def fetch_version_payload(version_url: str, user_agent: str) -> dict | None:
    url = version_url.strip()
    if not url:
        return None
    match = _RAW_GITHUB_RE.match(url)
    owner = match.group("owner") if match else ""
    repo = match.group("repo") if match else ""

    api_url = _github_api_url(url)
    if api_url:
        try:
            return _fetch_via_github_api(api_url, user_agent)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError, KeyError):
            pass
    try:
        return _fetch_via_raw_url(url, user_agent)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
        pass
    if owner and repo:
        try:
            return _fetch_via_releases_api(owner, repo, user_agent)
        except Exception:  # noqa: BLE001
            pass
    return None


def check_for_update(version_url: str, current_version: str, *, app_name: str = "App") -> UpdateInfo | None:
    user_agent = f"{app_name}/{current_version}"
    payload = fetch_version_payload(version_url, user_agent)
    if payload is None:
        return None
    remote_version = str(payload.get("version", "")).strip()
    if not remote_version or not is_newer(remote_version, current_version):
        return None
    return UpdateInfo(
        version=remote_version,
        url=str(payload.get("url", "")).strip(),
        notes=str(payload.get("notes", "")).strip(),
    )


def can_auto_update() -> bool:
    return getattr(sys, "frozen", False) and sys.platform == "win32"


def get_install_dir() -> Path:
    if can_auto_update():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ProgressCallback = Callable[[int, int], None]


def download_file(
    url: str,
    dest: Path,
    *,
    user_agent: str,
    on_progress: ProgressCallback | None = None,
) -> None:
    request = urllib.request.Request(url.strip(), headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=120) as response:
        total = int(response.headers.get("Content-Length", 0) or 0)
        downloaded = 0
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as handle:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                downloaded += len(chunk)
                if on_progress is not None:
                    on_progress(downloaded, total)


def _write_update_batch(batch_path: Path, preserve_files: frozenset[str]) -> None:
    preserve_list = ",".join(sorted(preserve_files))
    batch_path.write_text(
        rf"""@echo off
setlocal EnableExtensions
set "ZIP=%~1"
set "INSTALL=%~2"
set "EXE=%~3"
set "INNER=%~4"
set "WAITEXE=%~5"
set "STAGING=%TEMP%\app_update_%RANDOM%"
set "BACKUP=%TEMP%\app_update_backup_%RANDOM%"

:wait
timeout /t 2 /nobreak >nul
tasklist /FI "IMAGENAME eq %WAITEXE%" 2>nul | find /I "%WAITEXE%" >nul
if not errorlevel 1 goto wait

for %%F in ({preserve_list}) do (
  if exist "%INSTALL%\%%F" (
    if not exist "%BACKUP%" mkdir "%BACKUP%"
    copy /Y "%INSTALL%\%%F" "%BACKUP%\%%F" >nul
  )
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath $env:ZIP -DestinationPath $env:STAGING -Force"
if errorlevel 1 goto fail

if exist "%STAGING%\%INNER%" (
  robocopy "%STAGING%\%INNER%" "%INSTALL%" /E /IS /IT /R:3 /W:1 /XF {preserve_list} >nul
) else (
  robocopy "%STAGING%" "%INSTALL%" /E /IS /IT /R:3 /W:1 /XF {preserve_list} >nul
)
if errorlevel 8 goto fail

for %%F in ({preserve_list}) do (
  if exist "%BACKUP%\%%F" copy /Y "%BACKUP%\%%F" "%INSTALL%\%%F" >nul
)

rd /s /q "%STAGING%" 2>nul
rd /s /q "%BACKUP%" 2>nul
del /f /q "%ZIP%" 2>nul
start "" "%EXE%"
endlocal
del "%~f0"
exit /b 0

:fail
rd /s /q "%STAGING%" 2>nul
rd /s /q "%BACKUP%" 2>nul
msg * "Update failed. Download the zip manually from GitHub Releases."
endlocal
del "%~f0"
exit /b 1
""",
        encoding="utf-8",
    )


def schedule_apply_update(
    zip_path: Path,
    *,
    install_dir: Path | None = None,
    exe_name: str,
    zip_inner_folder: str | None = None,
    app_slug: str = "app",
) -> None:
    if not can_auto_update():
        raise RuntimeError("Auto-update works only in packaged exe builds.")

    target_dir = install_dir or get_install_dir()
    inner = zip_inner_folder or target_dir.name
    exe_path = target_dir / exe_name
    batch_path = Path(tempfile.gettempdir()) / f"{app_slug}_update_{os.getpid()}.bat"
    _write_update_batch(batch_path, _PRESERVE_FILES)

    creationflags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    subprocess.Popen(
        [
            "cmd.exe",
            "/c",
            str(batch_path),
            str(zip_path),
            str(target_dir),
            str(exe_path),
            inner,
            exe_name,
        ],
        creationflags=creationflags,
        close_fds=True,
    )
