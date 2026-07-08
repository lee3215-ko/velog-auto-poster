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


def _write_update_script(script_path: Path, preserve_files: frozenset[str]) -> None:
    preserve_array = ", ".join(f'"{name}"' for name in sorted(preserve_files))
    script_path.write_text(
        rf"""param(
    [string]$Zip,
    [string]$Install,
    [string]$Exe,
    [string]$Inner,
    [string]$WaitExe
)
$ErrorActionPreference = 'Stop'
$Staging = Join-Path $env:TEMP ("app_update_" + [guid]::NewGuid().ToString('N').Substring(0, 8))
$Backup = Join-Path $env:TEMP ("app_update_backup_" + [guid]::NewGuid().ToString('N').Substring(0, 8))
$Preserve = @({preserve_array})
$ProcName = [System.IO.Path]::GetFileNameWithoutExtension($WaitExe)

while (Get-Process -Name $ProcName -ErrorAction SilentlyContinue) {{
    Start-Sleep -Seconds 2
}}

foreach ($fileName in $Preserve) {{
    $src = Join-Path $Install $fileName
    if (Test-Path -LiteralPath $src) {{
        New-Item -ItemType Directory -Path $Backup -Force | Out-Null
        Copy-Item -LiteralPath $src -Destination (Join-Path $Backup $fileName) -Force
    }}
}}

Expand-Archive -LiteralPath $Zip -DestinationPath $Staging -Force

$source = $Staging
$innerPath = Join-Path $Staging $Inner
if (Test-Path -LiteralPath $innerPath) {{
    $source = $innerPath
}}

$xfArgs = @()
foreach ($fileName in $Preserve) {{
    $xfArgs += $fileName
}}

function Copy-UpdateTree {{
    param(
        [string]$Source,
        [string]$Dest,
        [string[]]$Exclude
    )
    $excludeSet = [System.Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    foreach ($name in $Exclude) {{ [void]$excludeSet.Add($name) }}
    New-Item -ItemType Directory -Path $Dest -Force | Out-Null
    foreach ($item in Get-ChildItem -LiteralPath $Source -Force) {{
        if ($excludeSet.Contains($item.Name)) {{ continue }}
        $target = Join-Path $Dest $item.Name
        if ($item.PSIsContainer) {{
            Copy-UpdateTree -Source $item.FullName -Dest $target -Exclude $Exclude
        }} else {{
            Copy-Item -LiteralPath $item.FullName -Destination $target -Force
        }}
    }}
}}

Copy-UpdateTree -Source $source -Dest $Install -Exclude $xfArgs

foreach ($fileName in $Preserve) {{
    $bak = Join-Path $Backup $fileName
    if (Test-Path -LiteralPath $bak) {{
        Copy-Item -LiteralPath $bak -Destination (Join-Path $Install $fileName) -Force
    }}
}}

Remove-Item -LiteralPath $Staging -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $Backup -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $Zip -Force -ErrorAction SilentlyContinue
Start-Process -FilePath $Exe
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
""",
        encoding="utf-8",
    )


def _quote_cmd_arg(value: str) -> str:
    text = str(value)
    if not text or any(ch in text for ch in ' \t"'):
        return '"' + text.replace('"', '""') + '"'
    return text


def _launch_hidden(script_path: Path, script_args: list[str]) -> None:
    """WScript로 PowerShell을 완전히 숨긴 상태로 실행한다."""
    quoted_args = " ".join(_quote_cmd_arg(arg) for arg in script_args)
    ps_command = (
        "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden "
        f'-File "{script_path}" {quoted_args}'
    )
    vbs_path = script_path.with_suffix(".vbs")
    vbs_path.write_text(
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.Run "{ps_command.replace(chr(34), chr(34) * 2)}", 0, False\r\n',
        encoding="utf-8",
    )

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    subprocess.Popen(
        ["wscript.exe", "//B", "//Nologo", str(vbs_path)],
        startupinfo=startupinfo,
        creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True,
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
    script_path = Path(tempfile.gettempdir()) / f"{app_slug}_update_{os.getpid()}.ps1"
    _write_update_script(script_path, _PRESERVE_FILES)

    _launch_hidden(
        script_path,
        [str(zip_path), str(target_dir), str(exe_path), inner, exe_name],
    )
