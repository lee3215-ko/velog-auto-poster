"""GitHub version.json check and Windows onedir auto-update."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_RAW_GITHUB_RE = re.compile(
    r"^https://raw\.githubusercontent\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?P<branch>[^/]+)/(?P<path>.+)$"
)

# 업데이트 시 덮어쓰지 않을 사용자 설정 파일
_PRESERVE_FILES = frozenset({"velog_settings.json"})
_UPDATE_LOG_NAME = "VelogPoster_update.log"
_RELEASE_ASSET = "VelogPoster.zip"


def _ca_bundle_path() -> str | None:
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", "")
        for candidate in (
            os.path.join(base, "certifi", "cacert.pem"),
            os.path.join(base, "cacert.pem"),
        ):
            if candidate and os.path.isfile(candidate):
                return candidate
    try:
        import certifi

        return certifi.where()
    except ImportError:
        return None


def _ssl_context() -> ssl.SSLContext:
    cafile = _ca_bundle_path()
    if cafile:
        return ssl.create_default_context(cafile=cafile)
    return ssl.create_default_context()


def _urlopen(request: urllib.request.Request, *, timeout: int):
    return urllib.request.urlopen(request, timeout=timeout, context=_ssl_context())


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    url: str
    notes: str
    download_urls: tuple[str, ...] = ()


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


def _decode_json_bytes(raw: bytes) -> dict:
    text = raw.decode("utf-8-sig").strip()
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
    with _urlopen(request, timeout=15) as response:
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
    with _urlopen(request, timeout=15) as response:
        return _decode_json_bytes(response.read())


def _fetch_via_releases_api(owner: str, repo: str, user_agent: str) -> dict | None:
    api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    request = urllib.request.Request(
        api_url,
        headers={"User-Agent": user_agent, "Accept": "application/vnd.github+json"},
    )
    with _urlopen(request, timeout=15) as response:
        data = json.loads(response.read().decode("utf-8-sig"))
    tag = str(data.get("tag_name", "")).strip().lstrip("v")
    if not tag:
        return None
    asset_id = None
    download = ""
    for asset in data.get("assets") or []:
        name = str(asset.get("name", ""))
        if name.lower().endswith(".zip"):
            download = str(asset.get("browser_download_url", "")).strip()
            try:
                asset_id = int(asset.get("id"))
            except (TypeError, ValueError):
                asset_id = None
            break
    if not download:
        download = (
            f"https://github.com/{owner}/{repo}/releases/latest/download/{_RELEASE_ASSET}"
        )
    payload: dict = {
        "version": tag,
        "url": download,
        "notes": str(data.get("body", "")).strip(),
    }
    if asset_id is not None:
        payload["asset_id"] = asset_id
        payload["api_download_url"] = _github_api_asset_url(owner, repo, asset_id)
    return payload


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
    download_urls = collect_download_urls(
        payload,
        version_url=version_url,
        user_agent=user_agent,
    )
    primary = download_urls[0] if download_urls else str(payload.get("url", "")).strip()
    return UpdateInfo(
        version=remote_version,
        url=primary,
        notes=str(payload.get("notes", "")).strip(),
        download_urls=download_urls,
    )


def can_auto_update() -> bool:
    return getattr(sys, "frozen", False) and sys.platform == "win32"


def get_install_dir() -> Path:
    if can_auto_update():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_update_log_path() -> Path:
    return Path(tempfile.gettempdir()) / _UPDATE_LOG_NAME


ProgressCallback = Callable[[int, int], None]


def validate_zip_file(zip_path: Path, min_bytes: int = 1024) -> None:
    if not zip_path.is_file():
        raise ValueError("다운로드 파일이 없습니다.")
    size = zip_path.stat().st_size
    if size < min_bytes:
        raise ValueError(f"다운로드 파일이 너무 작습니다 ({size} bytes).")
    with zip_path.open("rb") as handle:
        header = handle.read(4)
    if header[:2] != b"PK":
        raise ValueError("다운로드 파일이 zip 형식이 아닙니다 (GitHub 오류 페이지일 수 있습니다).")
    with zipfile.ZipFile(zip_path, "r") as archive:
        if archive.testzip() is not None:
            raise ValueError("zip 파일이 손상되었습니다.")


def _github_repo_from_version_url(version_url: str) -> tuple[str, str] | None:
    match = _RAW_GITHUB_RE.match(version_url.strip())
    if match is None:
        return None
    return match.group("owner"), match.group("repo")


def _release_tag(version: str) -> str:
    version = version.strip()
    return version if version.startswith("v") else f"v{version}"


def _versioned_release_url(owner: str, repo: str, version: str, asset: str) -> str:
    return (
        f"https://github.com/{owner}/{repo}/releases/download/"
        f"{_release_tag(version)}/{asset}"
    )


def _github_api_asset_url(owner: str, repo: str, asset_id: int) -> str:
    return f"https://api.github.com/repos/{owner}/{repo}/releases/assets/{asset_id}"


def _fetch_release_asset_id(
    owner: str,
    repo: str,
    version: str,
    asset_name: str,
    user_agent: str,
) -> int | None:
    tag = _release_tag(version)
    endpoints = (
        f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}",
        f"https://api.github.com/repos/{owner}/{repo}/releases/latest",
    )
    for endpoint in endpoints:
        try:
            request = urllib.request.Request(
                endpoint,
                headers={
                    "User-Agent": user_agent,
                    "Accept": "application/vnd.github+json",
                },
            )
            with _urlopen(request, timeout=20) as response:
                release = json.loads(response.read().decode("utf-8-sig"))
            for asset in release.get("assets") or []:
                if asset.get("name") == asset_name and asset.get("id"):
                    return int(asset["id"])
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError, TypeError, KeyError):
            continue
    return None


def _dedupe_urls(urls: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in urls:
        url = raw.strip()
        if not url or url in seen:
            continue
        seen.add(url)
        ordered.append(url)
    return tuple(ordered)


def collect_download_urls(
    payload: dict,
    *,
    version_url: str = "",
    user_agent: str = "App",
    asset_name: str = _RELEASE_ASSET,
) -> tuple[str, ...]:
    """다운로드 URL 후보. api.github.com 자산 URL을 우선한다."""
    urls: list[str] = []
    version = str(payload.get("version", "")).strip()

    for key in ("url", "download_url", "api_download_url"):
        value = str(payload.get(key, "")).strip()
        if value:
            urls.append(value)
    for item in payload.get("download_urls") or []:
        value = str(item).strip()
        if value:
            urls.append(value)

    owner_repo = _github_repo_from_version_url(version_url)
    if owner_repo and version:
        owner, repo = owner_repo
        asset_id = payload.get("asset_id")
        try:
            asset_id = int(asset_id) if asset_id is not None else None
        except (TypeError, ValueError):
            asset_id = None
        if asset_id is None:
            asset_id = _fetch_release_asset_id(owner, repo, version, asset_name, user_agent)
        if asset_id is not None:
            urls.insert(0, _github_api_asset_url(owner, repo, asset_id))
        urls.append(_versioned_release_url(owner, repo, version, asset_name))
        urls.append(
            f"https://github.com/{owner}/{repo}/releases/latest/download/{asset_name}"
        )

    return _dedupe_urls(urls)


def format_network_error(exc: BaseException) -> str:
    message = str(exc).strip()
    lowered = message.lower()
    if "getaddrinfo failed" in lowered or "11001" in message or "name or service not known" in lowered:
        return (
            "인터넷 연결 또는 DNS 설정을 확인해 주세요.\n"
            "(GitHub 서버 주소를 찾지 못했습니다)\n\n"
            "· Wi-Fi/유선 연결 확인\n"
            "· 회사망·보안 프로그램이 GitHub 차단 여부 확인\n"
            "· 「아니오」로 브라우저에서 직접 받기"
        )
    if "timed out" in lowered or "timeout" in lowered:
        return "다운로드 시간이 초과되었습니다. 네트워크 상태를 확인한 뒤 다시 시도해 주세요."
    if "certificate" in lowered or "ssl" in lowered:
        return "보안 인증서(SSL) 오류입니다. PC 날짜/시간이 맞는지 확인해 주세요."
    return message or repr(exc)


def _download_request(url: str, user_agent: str) -> urllib.request.Request:
    headers = {"User-Agent": user_agent}
    if "api.github.com" in url and "/releases/assets/" in url:
        headers["Accept"] = "application/octet-stream"
    return urllib.request.Request(url.strip(), headers=headers)


def download_file(
    url: str,
    dest: Path,
    *,
    user_agent: str,
    on_progress: ProgressCallback | None = None,
    timeout: int = 600,
) -> None:
    request = _download_request(url, user_agent)
    with _urlopen(request, timeout=timeout) as response:
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


def download_file_with_fallbacks(
    urls: list[str] | tuple[str, ...],
    dest: Path,
    *,
    user_agent: str,
    on_progress: ProgressCallback | None = None,
    timeout: int = 600,
    retries: int = 1,
) -> str:
    candidates = _dedupe_urls(list(urls))
    if not candidates:
        raise ValueError("다운로드 URL이 없습니다.")

    errors: list[str] = []
    for url in candidates:
        for attempt in range(retries + 1):
            try:
                if attempt > 0:
                    time.sleep(1.5 * attempt)
                if dest.exists():
                    dest.unlink(missing_ok=True)
                download_file(
                    url,
                    dest,
                    user_agent=user_agent,
                    on_progress=on_progress,
                    timeout=timeout,
                )
                validate_zip_file(dest, min_bytes=1024)
                return url
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                errors.append(f"{url} → {format_network_error(exc)}")
                if dest.exists():
                    dest.unlink(missing_ok=True)
    raise urllib.error.URLError("\n\n".join(errors))


def extract_zip_to_staging(zip_path: Path, staging_dir: Path) -> Path:
    if staging_dir.exists():
        shutil.rmtree(staging_dir, ignore_errors=True)
    staging_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as archive:
        archive.extractall(staging_dir)
    return staging_dir


def _write_update_script(script_path: Path) -> None:
    # 일반 문자열(비 f-string) — PowerShell 중괄호를 그대로 둔다.
    preserve = ", ".join(f'"{name}"' for name in sorted(_PRESERVE_FILES))
    script = r"""param(
    [string]$Staging,
    [string]$Install,
    [string]$Exe,
    [string]$Inner,
    [int]$WaitPid
)
$ErrorActionPreference = "Continue"
$Log = Join-Path $env:TEMP "VelogPoster_update.log"
$Preserve = @(__PRESERVE__)

function Write-Log([string]$Message) {
    try {
        Add-Content -LiteralPath $Log -Value ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message) -Encoding UTF8
    } catch {}
}

Write-Log "update start (powershell)"
Write-Log ("Staging=" + $Staging)
Write-Log ("Install=" + $Install)
Write-Log ("Exe=" + $Exe)
Write-Log ("WaitPid=" + $WaitPid)

if ($WaitPid -gt 0) {
    $deadline = (Get-Date).AddSeconds(120)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Id $WaitPid -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Seconds 1
    }
    $leftover = Get-Process -Id $WaitPid -ErrorAction SilentlyContinue
    if ($leftover) {
        Write-Log ("force stop pid " + $WaitPid)
        Stop-Process -Id $WaitPid -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
}
# 설치 폴더의 exe가 아직 잠겨 있으면 조금 더 기다린다.
$exeName = [System.IO.Path]::GetFileNameWithoutExtension($Exe)
$deadline2 = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline2) {
    $running = Get-Process -Name $exeName -ErrorAction SilentlyContinue
    if (-not $running) { break }
    Start-Sleep -Seconds 1
}
Write-Log "process wait done"
Start-Sleep -Seconds 2

$src = Join-Path $Staging $Inner
if (-not (Test-Path -LiteralPath $src)) { $src = $Staging }
if (-not (Test-Path -LiteralPath $src)) {
    Write-Log ("staging missing: " + $Staging)
    exit 1
}
Write-Log ("robocopy " + $src + " -> " + $Install)

$xfArgs = @()
foreach ($name in $Preserve) {
    $xfArgs += $name
}
if ($xfArgs.Count -gt 0) {
    & robocopy $src $Install /E /IS /IT /XF @xfArgs /R:8 /W:3 /NFL /NDL /NJH /NJS | Out-Null
} else {
    & robocopy $src $Install /E /IS /IT /R:8 /W:3 /NFL /NDL /NJH /NJS | Out-Null
}
Write-Log ("robocopy code " + $LASTEXITCODE)
if ($LASTEXITCODE -ge 8) {
    Write-Log ("robocopy failed code " + $LASTEXITCODE)
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show(
            ("업데이트 복사 실패 (코드 " + $LASTEXITCODE + "). 로그: " + $Log),
            "VelogPoster",
            "OK",
            "Error"
        ) | Out-Null
    } catch {}
    exit 1
}

Remove-Item -LiteralPath $Staging -Recurse -Force -ErrorAction SilentlyContinue
Write-Log ("starting " + $Exe)
if (Test-Path -LiteralPath $Exe) {
    Start-Process -FilePath $Exe -WorkingDirectory $Install
} else {
    Write-Log ("exe missing: " + $Exe)
}
Write-Log "update success"
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
exit 0
"""
    script_path.write_text(script.replace("__PRESERVE__", preserve), encoding="utf-8")


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

    validate_zip_file(zip_path)

    target_dir = install_dir or get_install_dir()
    inner = zip_inner_folder or target_dir.name
    exe_path = target_dir / exe_name
    staging_dir = Path(tempfile.gettempdir()) / f"VelogPoster_staging_{os.getpid()}"

    try:
        extract_zip_to_staging(zip_path, staging_dir)
    except (zipfile.BadZipFile, OSError) as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise RuntimeError(f"업데이트 zip 풀기 실패: {exc}") from exc

    script_path = Path(tempfile.gettempdir()) / f"{app_slug}_update_{os.getpid()}.ps1"
    _write_update_script(script_path)

    log_path = get_update_log_path()
    try:
        log_path.write_text(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] launcher ready pid={os.getpid()}\n"
            f"script={script_path}\ninstall={target_dir}\n",
            encoding="utf-8",
        )
    except OSError:
        pass

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    # 앱이 종료돼도 설치 스크립트가 같이 죽지 않도록 새 프로세스 그룹으로 분리한다.
    creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    detached = getattr(subprocess, "DETACHED_PROCESS", 0)
    if detached:
        creationflags |= detached
    proc = subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-File",
            str(script_path),
            str(staging_dir),
            str(target_dir),
            str(exe_path),
            inner,
            str(os.getpid()),
        ],
        startupinfo=startupinfo,
        creationflags=creationflags,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        log_path = get_update_log_path()
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] spawned updater pid={proc.pid}\n")
    except OSError:
        pass

    try:
        zip_path.unlink(missing_ok=True)
    except OSError:
        pass
