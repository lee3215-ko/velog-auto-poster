"""벨로그 자동 포스팅 엔진.

설계 원칙
- 사용자의 실제 Google Chrome을 '시크릿 모드'로 직접 실행한 뒤 CDP로 연결한다.
  (Playwright 번들 크로미움이 아닌 실제 Chrome을 쓰므로 핑거프린트가 자연스럽다.)
- navigator.webdriver 등 자동화 흔적을 init script 로 제거한다.
- 제목/본문/태그는 시스템 클립보드를 쓰지 않고 직접 입력한다.
- 모든 대기/입력 사이에 무작위 지터를 둔다.
"""

from __future__ import annotations

import html
import os
import random
import re
import shutil
import socket
import string
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from playwright.sync_api import (
    BrowserContext,
    Error,
    Page,
    TimeoutError as PWTimeoutError,
    sync_playwright,
)


LogCallback = Callable[[str, str], None]


# ---------------------------------------------------------------------------
# 자동화 탐지 회피용 init script
# 시크릿 모드 + 실제 Chrome 이지만, CDP 연결 시 webdriver 플래그가 노출되므로 제거한다.
# ---------------------------------------------------------------------------
# 실제 Chrome 의 진짜 핑거프린트를 최대한 보존한다.
# plugins / languages / chrome 객체를 가짜로 덮어쓰면 오히려 '불일치'가 생겨
# 탐지를 유발하므로, 자동화가 노출시키는 navigator.webdriver 만 정상값으로 되돌린다.
# (일반 Chrome 에서 navigator.webdriver 는 false 다.)
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => false });
"""


class PostingError(RuntimeError):
    """사용자에게 그대로 보여줄 수 있는 명확한 오류."""


class BrowserCheckFailed(PostingError):
    """AdGuard가 현재 브라우저를 거부함. 다른 브라우저로 재시도."""


def classify_failure(message: str) -> str:
    """실패 사유를 login / publish / locked / image / other 로 분류한다."""
    text = (message or "").strip()
    if not text:
        return "other"
    if "중단" in text:
        return "other"
    if any(k in text for k in (
        "계정 잠김", "포스트 작성 실패", "포스트 수정 실패", "재가입", "이미 가입",
    )):
        return "locked"
    if any(k in text for k in ("이미지 등록", "이미지 첨부", "이미지 업로드", "이미지 파일 선택")):
        return "image"
    login_keys = (
        "로그인", "인증 메일", "인증 링크", "TempMail", "가입 인증",
        "새 글 작성 버튼", "Cloudflare 인증", "AdGuard",
    )
    publish_keys = ("출간", "게시글")
    for key in login_keys:
        if key.lower() in text.lower():
            return "login"
    for key in publish_keys:
        if key in text:
            return "publish"
    return "other"


# ---------------------------------------------------------------------------
# 입력 검증 / 파싱 헬퍼
# ---------------------------------------------------------------------------
class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for name, value in attrs:
            if name.lower() == "href" and value:
                self.links.append(value)


def normalize_url(value: str) -> str:
    value = value.strip()
    if not value:
        raise PostingError("메일함 주소를 입력해 주세요.")
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PostingError("올바른 웹 주소가 아닙니다.")
    return value


def parse_tempmail_address(inbox_url: str) -> tuple[str, str]:
    """TempMail.co 메일함 URL에서 (이메일, 키) 를 추출한다."""
    parsed = urlparse(normalize_url(inbox_url))
    if parsed.netloc.lower() not in {"tempmail.co", "www.tempmail.co"}:
        raise PostingError("인증 메일 확인은 TempMail.co 주소만 지원합니다.")
    parts = [unquote(p) for p in parsed.path.split("/") if p]
    if len(parts) < 3 or parts[0] != "address":
        raise PostingError("TempMail.co 메일함 전체 주소를 입력해 주세요.")
    email, key = parts[1], parts[2]
    if "@" not in email or not key:
        raise PostingError("TempMail.co 메일함 주소를 확인해 주세요.")
    return email, key


def extract_velog_link(body: str) -> str:
    text = html.unescape(body or "")
    parser = _LinkParser()
    parser.feed(text)
    candidates: list[str] = []
    for link in parser.links:
        parsed = urlparse(link)
        if parsed.scheme == "https" and parsed.netloc.lower() in {"velog.io", "www.velog.io"}:
            if parsed.path != "/" or parsed.query:
                candidates.append(link)
    if not candidates:
        for match in re.finditer(r"https://(?:www\.)?velog\.io/[^\s\"'<>\\]+", text, re.I):
            link = match.group(0).rstrip(").,;")
            parsed = urlparse(link)
            if parsed.path != "/" or parsed.query:
                candidates.append(link)
    if not candidates:
        raise PostingError("인증 메일에서 벨로그 인증 링크를 찾지 못했습니다.")
    for link in reversed(candidates):
        low = link.lower()
        if "email" in low or "login" in low or "code=" in low or "token" in low:
            return link
    return candidates[-1]


ADGUARD_TEMPMAIL_URL = "https://tempmail.adguard.com/?_locale=ko"
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_ADGUARD_CREATE_SELECTORS = (
    "button.address__get-address-btn",
    "button:has-text('주소 생성')",
    "button:has-text('Get address')",
    "button:has-text('Get email')",
    "button:has-text('Create address')",
)


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def _is_html_manuscript(path: Path, text: str) -> bool:
    if path.suffix.lower() in {".html", ".htm"}:
        return True
    return bool(re.search(r"<h1[\s>]", text, re.IGNORECASE))


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def _parse_tag_line(line: str) -> list[str] | None:
    """한 줄이 #태그 #태그 형식이면 태그 목록을 반환."""
    stripped = line.strip()
    if "#" not in stripped:
        return None
    tags = [part.strip() for part in stripped.split("#") if part.strip()]
    if not tags:
        return None
    rebuilt = " ".join(f"#{tag}" for tag in tags)
    if _normalize_ws(rebuilt) != _normalize_ws(stripped):
        return None
    return tags


def extract_hashtags(text: str) -> tuple[str, list[str]]:
    """원고 맨 아래 #태그 줄을 분리한다. (#키워드 #키워드2 형식, 키워드에 공백 가능)"""
    lines = text.splitlines()
    tags: list[str] = []
    while lines:
        stripped = lines[-1].strip()
        if not stripped:
            lines.pop()
            continue
        parsed = _parse_tag_line(stripped)
        if not parsed:
            break
        tags = parsed + tags
        lines.pop()
    body = "\n".join(lines).rstrip("\n")
    return body, tags


def _parse_markdown_manuscript(text: str) -> tuple[str, str, list[str]]:
    lines = text.splitlines()
    if not lines or not lines[0].strip():
        raise PostingError("원고 첫 줄에 제목이 없습니다.")
    title = lines[0].strip()
    body, tags = extract_hashtags("\n".join(lines[1:]).lstrip("\n"))
    if not body.strip():
        raise PostingError("원고 본문이 없습니다.")
    return title, body, tags


def _parse_html_manuscript(text: str) -> tuple[str, str, list[str]]:
    match = re.search(r"<h1\b[^>]*>(.*?)</h1>", text, re.IGNORECASE | re.DOTALL)
    if not match:
        raise PostingError("HTML 원고에서 <h1> 제목을 찾지 못했습니다.")
    title = _strip_html(match.group(1))
    if not title:
        raise PostingError("HTML 원고의 <h1> 제목이 비어 있습니다.")
    body_without_h1 = (text[: match.start()] + text[match.end() :]).strip()
    body, tags = extract_hashtags(body_without_h1)
    if not body.strip():
        raise PostingError("HTML 원고 본문이 없습니다.")
    return title, body, tags


def read_manuscript(file_path: str) -> tuple[str, str, list[str]]:
    """원고 파일을 (제목, 본문, 태그) 로 읽는다.

  - 마크다운: 첫 줄=제목, 맨 아래 #태그
  - HTML: <h1> 제목(위치 무관), 맨 아래 #태그
    """
    path = Path(file_path)
    if not path.is_file():
        raise PostingError(f"원고 파일을 찾지 못했습니다: {path.name}")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PostingError(f"원고 파일이 UTF-8 형식이 아닙니다: {path.name}") from exc
    except OSError as exc:
        raise PostingError(f"원고 파일을 읽지 못했습니다: {path.name}") from exc

    if _is_html_manuscript(path, text):
        return _parse_html_manuscript(text)
    return _parse_markdown_manuscript(text)


MANUSCRIPT_DONE_DIR = "발행완료"
MANUSCRIPT_FAILED_DIR = "발행실패"


def relocate_manuscript(manuscript_path: str, *, success: bool) -> str:
    """원고 파일을 같은 폴더의 발행완료/발행실패 하위 폴더로 옮긴다."""
    src = Path(manuscript_path)
    if not src.is_file():
        raise PostingError(f"원고 파일을 찾지 못했습니다: {src.name}")
    dest_dir = src.parent / (MANUSCRIPT_DONE_DIR if success else MANUSCRIPT_FAILED_DIR)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        stem, suffix = src.stem, src.suffix
        n = 2
        while dest.exists():
            dest = dest_dir / f"{stem}_{n}{suffix}"
            n += 1
    shutil.move(str(src), str(dest))
    return str(dest)


def find_chrome() -> Path:
    """설치된 실제 Google Chrome 실행 파일을 찾는다."""
    for path in _browser_search_paths("Google/Chrome/Application/chrome.exe"):
        return path
    raise PostingError("Google Chrome을 찾지 못했습니다. Chrome을 설치해 주세요.")


def _browser_search_paths(relative: str) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for key in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        base = os.environ.get(key)
        if not base:
            continue
        candidate = Path(base) / relative
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if candidate.is_file() and resolved not in seen:
            seen.add(resolved)
            found.append(candidate)
    return found


def find_chromium_browsers() -> list[tuple[str, Path]]:
    """CDP 연결 가능한 Chromium 계열 브라우저. Chrome → Edge → Brave 순."""
    specs = (
        ("Chrome", "Google/Chrome/Application/chrome.exe"),
        ("Edge", "Microsoft/Edge/Application/msedge.exe"),
        ("Brave", "BraveSoftware/Brave-Browser/Application/brave.exe"),
        ("Chrome Beta", "Google/Chrome Beta/Application/chrome.exe"),
        ("Chrome Canary", "Google/Chrome SxS/Application/chrome.exe"),
        ("Vivaldi", "Vivaldi/Application/vivaldi.exe"),
        ("Opera", "Programs/Opera/opera.exe"),
    )
    result: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for name, relative in specs:
        for path in _browser_search_paths(relative):
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            result.append((name, path))
    return result


def _jitter(base: float, spread: float = 0.5) -> float:
    return base + random.uniform(0, spread)


def _human_delay(lo: float, hi: float) -> float:
    """사람이 멈추는 듯한 불규칙 대기(초)."""
    if hi < lo:
        lo, hi = hi, lo
    # 가끔 더 길게 멈추는 꼬리 분포
    if random.random() < 0.12:
        return random.uniform(hi, hi * random.uniform(1.3, 2.2))
    return random.uniform(lo, hi)


# 신규 계정 회원가입에 쓰이는 기본값
DEFAULT_PROFILE_NAMES = [
    "카드깡", "카드깡업체", "카드깡수수료", "신용카드현금화",
    "신용카드한도대출", "카드현금화", "신용카드한도현금화",
]
BIO_SUFFIXES = [
    "안전 신속 상담", "최저 수수료 안내", "24시 문의 환영",
    "친절 상담 진행", "빠른 한도 안내", "당일 진행 가능",
]

# 사람처럼 보이는 사용자 ID 조각 (완전 난수 6+4 패턴 회피)
_USER_ID_STEMS = (
    "sky", "blue", "mint", "nova", "luna", "haze", "wave", "pine", "coral", "amber",
    "jun", "min", "seo", "han", "yoon", "park", "kim", "lee", "choi", "jung",
    "note", "blog", "page", "daily", "soft", "calm", "bright", "quiet", "fresh",
)


def make_user_id() -> str:
    """읽기 쉬운 형태의 사용자 ID (소문자·숫자, 8~14자)."""
    a = random.choice(_USER_ID_STEMS)
    b = random.choice(_USER_ID_STEMS)
    while b == a:
        b = random.choice(_USER_ID_STEMS)
    style = random.randint(0, 4)
    if style == 0:
        raw = f"{a}{b}{random.randint(10, 99)}"
    elif style == 1:
        raw = f"{a}{random.randint(1988, 2006)}"
    elif style == 2:
        raw = f"{a}_{b}{random.randint(1, 9)}"
    elif style == 3:
        raw = f"{a}{random.randint(100, 999)}{b[:2]}"
    else:
        raw = f"{a}{b}{random.choice(string.digits)}{random.choice(string.digits)}"
    # velog 사용자 ID 제약: 보통 영문/숫자/_ , 소문자 유지
    cleaned = re.sub(r"[^a-z0-9_]", "", raw.lower())
    if not cleaned or cleaned[0].isdigit():
        cleaned = random.choice(string.ascii_lowercase) + cleaned
    return cleaned[:14]


def make_bio(name: str) -> str:
    """프로필 이름을 앞에 넣은 20자 내외의 한 줄 소개."""
    bio = f"{name} {random.choice(BIO_SUFFIXES)}"
    return bio[:20]


# ---------------------------------------------------------------------------
# 메인 엔진
# ---------------------------------------------------------------------------
class VelogPoster:
    def __init__(
        self,
        log: LogCallback,
        on_result=None,
        on_failed=None,
        *,
        signed_up_emails: set[str] | None = None,
        on_signup=None,
        on_account=None,
    ) -> None:
        self._emit = log
        self._prefix = ""  # 진행 중 계정 번호 표시 (예: "[2/5] ")
        # on_result(velog_id, url): 한 계정 발행이 끝나면 결과 URL 을 알린다.
        # on_failed(velog_id, manuscript_path, reason): 발행 실패 시 원고 정리·재시도 카운트용.
        # on_signup(email): 신규 가입 성공 시 기억용.
        # on_account(email, manuscript_path): AdGuard 등에서 주소가 만들어지면 목록 등록용.
        self.on_result = on_result
        self.on_failed = on_failed
        self.on_signup = on_signup
        self.on_account = on_account
        self._signed_up_emails = {
            str(e).strip().lower() for e in (signed_up_emails or set()) if str(e).strip()
        }
        self._stop = threading.Event()
        self._process: subprocess.Popen | None = None
        self._browser = None
        self._context: BrowserContext | None = None
        self._temp_profile: Path | None = None
        self._endpoint: str | None = None
        self._handoff = False
        self._browser_label = "Chrome"
        self._user_foreground_hwnd = 0
        # 업로드 실패 이미지 → 이후 N계정(10~20) 동안 보류
        self._image_hold: dict[str, int] = {}
        self._image_hold_default = (10, 20)
        self._image_try_limit = 3
        # 연속으로 이미지 등록이 전부 실패하면 일정 계정 동안 이미지 업로드 휴식
        self._image_fail_streak = 0
        self._image_skip_remaining = 0
        self._image_streak_trigger = 3  # 연속 3계정 실패 시
        self._image_skip_range = (5, 8)  # 5~8계정 동안 이미지 생략
        # 최근 사용한 이미지는 가급적 재사용하지 않음
        self._recent_images: list[str] = []
        self._recent_image_limit = 8
        self._temp_upload_files: list[Path] = []

    def log(self, message: str, level: str) -> None:
        """모든 로그에 현재 진행 중인 계정 번호를 붙여 내보낸다."""
        self._emit(f"{self._prefix}{message}", level)

    # -- 외부 제어 ---------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    def run(self, account: dict[str, str]) -> None:
        """단일 계정 작업 (하위 호환용)."""
        self.run_batch([account])

    def run_batch(self, accounts: list[dict[str, str]]) -> None:
        """여러 계정을 순서대로 처리한다. 각 계정은 자기 전용 Chrome 창에서
        로그인 → 인증 → 작성 → 이미지 → 출간까지 끝낸 뒤 창을 닫고 다음으로
        넘어간다."""
        if not accounts:
            raise PostingError("실행할 계정이 없습니다.")

        chrome = find_chrome()
        self._prefix = ""
        self.log(f"Chrome 확인: {chrome.name}", "info")
        total = len(accounts)
        completed = 0
        with sync_playwright() as pw:
            for index, account in enumerate(accounts, start=1):
                if self._stop.is_set():
                    break
                label = account.get("velog_id", "").strip() or f"계정 {index}"
                # 이후 모든 로그에 [현재/전체] 가 자동으로 붙는다.
                self._prefix = f"[{index}/{total}] "
                self.log(f"━━━ {label} 작업 시작 ━━━", "info")
                self._emit(label, "working")
                self._reset_account_state()
                self._tick_image_holds()
                try:
                    self._run_one(pw, chrome, account)
                    completed += 1
                    self.log(f"{label}: 출간 완료 ✅", "success")
                except PostingError as exc:
                    self.log(f"{label}: {exc}", "error")
                    self._notify_failed(label, account, str(exc))
                except PWTimeoutError:
                    msg = "화면 요소를 시간 내에 찾지 못했습니다."
                    self.log(f"{label}: {msg}", "error")
                    self._notify_failed(label, account, msg)
                except Error as exc:
                    if self._stop.is_set():
                        break
                    msg = f"브라우저 오류 - {exc}"
                    self.log(f"{label}: {msg}", "error")
                    self._notify_failed(label, account, msg)
                finally:
                    # 다음 계정을 위해 이 계정의 Chrome 을 완전히 닫고 정리한다.
                    self._teardown_account()
                    self._emit("", "working")

                if index < total and not self._stop.is_set():
                    # 계정 간 간격을 사람처럼 불규칙하게 (봇 패턴 완화)
                    if self._image_skip_remaining > 0 or self._image_fail_streak > 0:
                        wait_s = _human_delay(18, 42)
                        self._wait(f"다음 계정 전 대기(이미지 제한 완화) {wait_s:.0f}초...", wait_s)
                    else:
                        wait_s = _human_delay(12, 35)
                        self._wait(f"다음 계정 전 대기 {wait_s:.0f}초...", wait_s)

        self._prefix = ""
        self._emit("", "working")
        if self._stop.is_set():
            self.log("작업을 중단했습니다.", "info")
        else:
            self.log(f"전체 완료: {completed}/{total}개 계정 출간 🎉", "success")

    def _notify_failed(self, velog_id: str, account: dict[str, str], message: str = "") -> None:
        if self.on_failed is None:
            return
        path = str(account.get("manuscript_path", "")).strip()
        reason = classify_failure(message)
        try:
            self.on_failed(velog_id, path, reason)
        except TypeError:
            try:
                self.on_failed(velog_id, path)
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass

    def _run_one(self, pw, chrome: Path, account: dict[str, str]) -> None:
        """한 계정의 전체 흐름을 수행한다."""
        if str(account.get("mail_provider") or "").strip().lower() == "adguard":
            self._run_one_adguard(pw, chrome, account)
            return
        velog_id = account["velog_id"].strip()
        inbox_url = normalize_url(account["inbox_url"])
        title, body, tags = read_manuscript(account["manuscript_path"])
        image_folder = (account.get("image_folder") or "").strip()
        homepages = [str(u).strip() for u in (account.get("homepages") or []) if str(u).strip()]
        parse_tempmail_address(inbox_url)  # 사전 검증

        # 원고 제일 아래에 앵커텍스트 링크를 추가한다. (등록된 것 중 무작위)
        anchors = account.get("anchors") or []
        anchor_url = ""
        if anchors:
            a = random.choice(anchors)
            anchor_text = (a.get("anchor") or "").strip()
            anchor_url = (a.get("url") or "").strip()
            if anchor_text and anchor_url:
                body = f"{body.rstrip()}\n\n[{anchor_text}]({anchor_url})"

        self._user_foreground_hwnd = self._capture_foreground_hwnd()
        self._launch_incognito(pw, chrome)
        self._restore_user_focus()

        page = self._first_page()
        self._inject_stealth(page)

        prev_uuid = self._latest_email_uuid(inbox_url)
        self._request_login(pw, page, velog_id)

        link = self._wait_for_verification(page, inbox_url, prev_uuid)
        self._open_link(pw, page, link)
        page = self._first_page()  # cloudflare 우회로 재연결됐을 수 있음
        is_signup, profile_name = self._handle_signup_if_needed(pw, page, account)
        # 가입 단계에서 CDP 를 끊었다가 다시 쓰므로, 이후 작업 전에 반드시 재연결한다.
        self._ensure_connected(pw)
        self._restore_user_focus()
        page = self._first_page()
        if not is_signup:
            self.log("로그인 중.. (기존 계정)", "info")
        elif profile_name:
            self._set_blog_title(page, profile_name)
        image_link = random.choice(homepages) if (image_folder and homepages) else ""
        target = self._write_post(
            self._first_page(),
            title,
            body,
            tags=tags,
            image_folder=image_folder,
            image_link=image_link,
        )
        self._restore_user_focus()

        url = self._publish(pw, target)
        if not url or not self._is_post_url(url):
            raise PostingError("출간에 실패했습니다. 게시글 주소를 확인하지 못했습니다.")
        if self.on_result is not None:
            try:
                self.on_result(velog_id, url)
            except Exception:  # noqa: BLE001
                pass

    def _adguard_browser_candidates(self, preferred: Path) -> list[tuple[str, Path]]:
        found = find_chromium_browsers()
        ordered: list[tuple[str, Path]] = []
        seen: set[Path] = set()
        try:
            preferred_res = preferred.resolve()
        except OSError:
            preferred_res = preferred
        name = next((n for n, p in found if p.resolve() == preferred_res), "Chrome")
        ordered.append((name, preferred))
        seen.add(preferred_res)
        for name, path in found:
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            ordered.append((name, path))
        return ordered

    def _run_one_adguard(self, pw, chrome: Path, account: dict[str, str]) -> None:
        """AdGuard 임시메일 생성 → 벨로그 가입/로그인 → 작성 → 출간."""
        title, body, tags = read_manuscript(account["manuscript_path"])
        image_folder = (account.get("image_folder") or "").strip()
        homepages = [str(u).strip() for u in (account.get("homepages") or []) if str(u).strip()]
        anchors = account.get("anchors") or []
        if anchors:
            a = random.choice(anchors)
            anchor_text = (a.get("anchor") or "").strip()
            anchor_url = (a.get("url") or "").strip()
            if anchor_text and anchor_url:
                body = f"{body.rstrip()}\n\n[{anchor_text}]({anchor_url})"

        browsers = self._adguard_browser_candidates(chrome)
        if len(browsers) > 1:
            names = ", ".join(n for n, _ in browsers)
            self.log(f"AdGuard 브라우저 후보: {names}", "info")

        email = ""
        last_blocked: BrowserCheckFailed | None = None
        for index, (name, path) in enumerate(browsers):
            if index > 0:
                self.log(
                    f"브라우저 확인 실패 → {name}으로 다시 엽니다.",
                    "info",
                )
                self._teardown_account()
                self._sleep(_jitter(1.2, 0.5))
            try:
                self._user_foreground_hwnd = self._capture_foreground_hwnd()
                self._launch_incognito(pw, path, browser_name=name)
                self._restore_user_focus()
                mail = self._first_page()
                self._inject_stealth(mail)
                email = self._adguard_create_address(pw, mail)
                break
            except BrowserCheckFailed as exc:
                last_blocked = exc
                self.log(f"{name}: AdGuard가 이 브라우저를 거부했습니다.", "info")
                continue
            except PostingError as exc:
                if self._stop.is_set() or "중단" in str(exc):
                    raise
                if index + 1 < len(browsers):
                    self.log(f"{name} 실행/접속 실패 → 다음 브라우저로 시도합니다.", "info")
                    continue
                raise
        else:
            raise PostingError(
                "AdGuard 브라우저 확인에 실패했습니다. "
                "Edge 등 다른 브라우저를 설치한 뒤 다시 시도해 주세요.",
            ) from last_blocked
        account["velog_id"] = email
        self._emit(email, "working")
        if self.on_account is not None:
            try:
                self.on_account(email, str(account.get("manuscript_path") or ""))
            except Exception:  # noqa: BLE001
                pass

        self._ensure_connected(pw)
        assert self._context is not None
        velog = self._context.new_page()
        self._inject_stealth(velog)
        self._request_login(pw, velog, email)

        self._ensure_connected(pw)
        mail = self._adguard_mail_page()
        link = self._wait_adguard_velog_mail(mail)
        velog = self._page_by_host("velog.io")
        self._open_link(pw, velog, link)

        self._ensure_connected(pw)
        velog = self._page_by_host("velog.io")
        is_signup, profile_name = self._handle_signup_if_needed(pw, velog, account)
        self._ensure_connected(pw)
        self._restore_user_focus()
        velog = self._page_by_host("velog.io")
        if not is_signup:
            self.log("로그인 중.. (기존 계정)", "info")
        elif profile_name:
            self._set_blog_title(velog, profile_name)

        image_link = random.choice(homepages) if (image_folder and homepages) else ""
        target = self._write_post(
            self._page_by_host("velog.io"),
            title,
            body,
            tags=tags,
            image_folder=image_folder,
            image_link=image_link,
        )
        self._restore_user_focus()

        url = self._publish(pw, target)
        if not url or not self._is_post_url(url):
            raise PostingError("출간에 실패했습니다. 게시글 주소를 확인하지 못했습니다.")
        if self.on_result is not None:
            try:
                self.on_result(email, url)
            except Exception:  # noqa: BLE001
                pass

    # -- Chrome 실행 / 연결 -----------------------------------------------
    def _launch_incognito(self, pw, chrome: Path, *, browser_name: str = "Chrome") -> None:
        port = self._free_port()
        # 이미 실행 중인 사용자 Chrome과 충돌하지 않도록 전용 임시 프로필을 쓴다.
        # (전용 user-data-dir 가 없으면 새 chrome.exe 가 기존 인스턴스에 명령만
        #  넘기고 즉시 종료되어 디버깅 포트가 열리지 않는다.)
        self._browser_label = browser_name or "Chrome"
        slug = re.sub(r"[^a-z0-9]+", "-", self._browser_label.lower()).strip("-") or "chrome"
        self._temp_profile = Path(tempfile.mkdtemp(prefix=f"velog-{slug}-"))
        private_flag = "--inprivate" if "msedge" in chrome.name.lower() else "--incognito"
        # 시크릿 모드 + 자동화 배너가 뜨지 않는 안전한 플래그만 사용.
        # --start-maximized 는 작업 창을 강제로 전면 최대화하므로 쓰지 않는다.
        command = [
            str(chrome),
            private_flag,
            f"--user-data-dir={self._temp_profile}",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=*",
            f"--window-size=1500,1000",
            f"--window-position={random.randint(20, 80)},{random.randint(20, 60)}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            "about:blank",
        ]
        self.log(f"{self._browser_label} 시크릿 창을 여는 중...", "info")
        try:
            self._process = subprocess.Popen(command, close_fds=True)
        except OSError as rest:
            raise PostingError(f"{self._browser_label} 시크릿 창을 실행하지 못했습니다.") from rest

        endpoint = f"http://127.0.0.1:{port}"
        self._endpoint = endpoint  # 출간 단계에서 재연결할 때 사용
        self._wait_for_endpoint(endpoint)
        try:
            self._browser = pw.chromium.connect_over_cdp(endpoint, timeout=20_000)
        except Error as exc:
            raise PostingError(f"{self._browser_label}에 연결하지 못했습니다.") from exc

        if not self._browser.contexts:
            raise PostingError(f"{self._browser_label} 컨텍스트를 찾지 못했습니다.")
        self._context = self._browser.contexts[0]
        # 이후 열리는 모든 페이지에도 stealth 적용
        self._context.add_init_script(STEALTH_SCRIPT)

    def _first_page(self) -> Page:
        assert self._context is not None
        pages = self._context.pages
        return pages[0] if pages else self._context.new_page()

    def _inject_stealth(self, page: Page) -> None:
        try:
            page.evaluate(STEALTH_SCRIPT)
        except Error:
            pass

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _wait_for_endpoint(self, endpoint: str) -> None:
        for _ in range(120):
            if self._process and self._process.poll() is not None:
                raise PostingError(f"{self._browser_label}이(가) 실행 직후 종료되었습니다.")
            try:
                if requests.get(f"{endpoint}/json/version", timeout=0.5).ok:
                    return
            except requests.RequestException:
                pass
            time.sleep(0.1)
        raise PostingError(f"{self._browser_label} 연결 준비 시간이 초과되었습니다.")

    # -- 사람같은 입력 헬퍼 ------------------------------------------------
    def _sleep(self, seconds: float) -> None:
        if self._stop.wait(seconds):
            raise PostingError("사용자가 작업을 중단했습니다.")

    def _wait(self, message: str, seconds: float) -> None:
        self.log(message, "info")
        self._sleep(seconds)

    @staticmethod
    def _capture_foreground_hwnd() -> int:
        if sys.platform != "win32":
            return 0
        try:
            import ctypes

            return int(ctypes.windll.user32.GetForegroundWindow() or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _restore_user_focus(self) -> None:
        """자동화 Chrome이 전면을 뺏은 뒤, 사용자가 쓰던 창으로 포커스를 되돌린다."""
        hwnd = int(self._user_foreground_hwnd or 0)
        if sys.platform != "win32" or not hwnd:
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            if not user32.IsWindow(hwnd):
                return
            # 현재 전면이 이미 사용자 창이면 건드리지 않는다.
            if int(user32.GetForegroundWindow() or 0) == hwnd:
                return
            user32.SetForegroundWindow(hwnd)
        except Exception:  # noqa: BLE001
            pass

    def _type_like_human(self, page: Page, text: str, *, slow: bool = False) -> None:
        """한 글자씩 입력. 오타·과도한 멈춤은 쓰지 않는다(탐지·입력 오류 유발)."""
        if not text:
            return
        delay = random.randint(55, 120) if slow else random.randint(40, 95)
        page.keyboard.type(text, delay=delay)

    def _scroll_into_view(self, locator) -> None:
        """화면 밖(작은 창)에 있는 버튼을 가운데로 스크롤한다."""
        target = locator.first if hasattr(locator, "first") else locator
        try:
            target.scroll_into_view_if_needed(timeout=5_000)
        except Error:
            try:
                target.evaluate(
                    "el => el.scrollIntoView({block:'center', inline:'nearest', behavior:'instant'})"
                )
            except Error:
                pass
        self._sleep(0.2)

    def _stable_click(self, locator, *, timeout: int = 15_000) -> None:
        """스크롤 후 Playwright 기본 클릭(가입 등 중요 버튼용)."""
        target = locator.first if hasattr(locator, "first") else locator
        self._scroll_into_view(target)
        try:
            target.click(timeout=timeout)
        except Error:
            self._scroll_into_view(target)
            target.click(timeout=timeout, force=True)

    def _human_idle(self, page: Page | None = None, *, lo: float = 0.4, hi: float = 1.2) -> None:
        """짧은 대기만. 마우스 궤적/휠은 쓰지 않는다(봇 패턴·스크롤 밀림 유발)."""
        del page  # API 호환
        self._sleep(_human_delay(lo, hi))

    def _set_codemirror_value(self, editor, text: str) -> None:
        """CodeMirror 본문을 클립보드 없이 직접 넣는다."""
        editor.evaluate(
            """(el, value) => {
                const cm = el.CodeMirror;
                if (!cm) throw new Error('CodeMirror missing');
                cm.setValue(value);
                cm.focus();
                cm.refresh();
                try {
                    const ta = cm.getTextArea && cm.getTextArea();
                    if (ta) {
                        ta.dispatchEvent(new Event('input', { bubbles: true }));
                        ta.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                } catch (e) {}
            }""",
            text,
        )

    def _goto(self, page: Page, url: str) -> None:
        last: Error | None = None
        for attempt in range(3):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                return
            except Error as exc:
                last = exc
                if attempt < 2:
                    self._wait("페이지 이동을 다시 시도하는 중...", _jitter(4))
        assert last is not None
        raise last

    # -- Cloudflare 전체화면 챌린지 우회 ----------------------------------
    @staticmethod
    def _is_interstitial(page: Page) -> bool:
        """Cloudflare 전체화면 '사람 확인' 인터스티셜인지 판별한다.
        (가입 폼에 박혀 있는 turnstile 위젯은 제외한다.)"""
        try:
            title = (page.title() or "").lower()
            html = page.content().lower()
        except Error:
            return False
        if "just a moment" in title or "잠시만" in title or "잠시 기다" in title:
            return True
        return (
            "challenge-platform" in html
            or "checking your browser" in html
            or "cf-chl" in html
            or "_cf_chl_opt" in html
        )

    def _wait_if_cloudflare(self, pw, page: Page) -> None:
        """Cloudflare 전체화면 챌린지가 뜨면, 자동화 연결을 끊어 깨끗한 상태에서
        자동 통과되게 한 뒤 재연결한다. (사람 클릭 불필요)"""
        if not self._is_interstitial(page):
            return
        needle = "velog.io"
        try:
            host = (urlparse(page.url).netloc or "").lower()
            if "adguard.com" in host:
                needle = "adguard.com"
            elif host:
                needle = host
        except Exception:  # noqa: BLE001
            pass
        self.log("Cloudflare 사람 확인 감지 → 연결을 끊어 자동 우회합니다...", "info")
        if not self._endpoint:
            return
        self._disconnect_only()
        for _ in range(40):  # 최대 약 200초
            if self._stop.wait(0):
                raise PostingError("사용자가 작업을 중단했습니다.")
            self._sleep(4)  # 끊긴(깨끗한) 상태에서 챌린지가 자동 통과될 시간
            browser = None
            passed = False
            try:
                browser = pw.chromium.connect_over_cdp(self._endpoint, timeout=8_000)
                p = self._find_page_containing(browser, needle)
                if p is not None and not self._is_interstitial(p):
                    passed = True
            except (Error, PWTimeoutError):
                pass
            finally:
                if browser is not None:
                    try:
                        browser.close()
                    except Error:
                        pass
            if passed:
                self.log("Cloudflare 통과 확인 → 작업을 계속합니다.", "info")
                self._ensure_connected(pw)
                self._restore_user_focus()
                return
        raise PostingError("Cloudflare 인증을 시간 내에 통과하지 못했습니다.")

    # -- 벨로그 로그인 요청 ------------------------------------------------
    def _request_login(self, pw, page: Page, velog_id: str) -> None:
        self.log("벨로그에 접속하는 중...", "info")
        self._goto(page, "https://velog.io/")
        self._wait("화면이 준비되기를 기다리는 중...", _jitter(2.5, 1.0))
        self._wait_if_cloudflare(pw, page)
        self._ensure_connected(pw)
        page = self._page_by_host("velog.io")

        email_input = page.get_by_placeholder("이메일을 입력하세요.")
        if not email_input.is_visible():
            # 상단 '로그인' 버튼을 눌러 로그인 패널을 연다.
            login_btn = self._visible_last(page.get_by_role("button", name="로그인", exact=True))
            if login_btn is None:
                raise PostingError("로그인 버튼을 찾지 못했습니다.")
            login_btn.click(timeout=15_000)
            self._wait("로그인 창이 열리기를 기다리는 중...", _jitter(2))

        self.log("아이디(이메일)를 입력하는 중...", "info")
        email_input.wait_for(state="visible", timeout=15_000)
        email_input.click()
        self._sleep(_jitter(0.35, 0.25))
        self._type_like_human(page, velog_id)
        self._sleep(_jitter(0.7, 0.4))

        self.log("인증 메일을 요청하는 중...", "info")
        submit = self._visible_last(page.get_by_role("button", name="로그인", exact=True))
        if submit is None:
            raise PostingError("이메일 입력 후 로그인 버튼을 찾지 못했습니다.")
        self._sleep(_jitter(0.35, 0.25))
        submit.click()
        self._wait("인증 메일 발송을 기다리는 중...", _jitter(5, 1.5))

    @staticmethod
    def _visible_last(locator):
        """여러 매칭 중 화면에 보이는 마지막 요소를 반환."""
        for i in range(locator.count() - 1, -1, -1):
            item = locator.nth(i)
            if item.is_visible():
                return item
        return None

    # -- TempMail 메일 확인 (HTTP API) ------------------------------------
    def _fetch_emails(self, inbox_url: str) -> list[dict]:
        email, key = parse_tempmail_address(inbox_url)
        s = requests.Session()
        s.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
                "Origin": "https://www.tempmail.co",
                "Referer": inbox_url,
            }
        )
        try:
            r = s.get("https://www.tempmail.co/", timeout=20)
            r.raise_for_status()
            token = s.cookies.get("XSRF-TOKEN")
            if not token:
                raise PostingError("TempMail 보안 토큰을 받지 못했습니다.")
            s.headers["X-XSRF-TOKEN"] = unquote(token)
            r = s.post(
                "https://www.tempmail.co/emails",
                json={"email": email, "key": key},
                timeout=20,
            )
            r.raise_for_status()
            return r.json().get("data", {}).get("emails", [])
        except requests.RequestException as exc:
            raise PostingError("TempMail 메일 목록을 불러오지 못했습니다.") from exc
        except ValueError as exc:
            raise PostingError("TempMail 응답을 읽지 못했습니다.") from exc

    def _latest_velog_email(self, inbox_url: str) -> dict | None:
        """verify@velog.io 가 보낸 최신 메일. 로그인/회원가입 메일 모두 포함한다.
        (회원가입 인증 메일은 제목이 'Velog 로그인'이 아닐 수 있다.)"""
        for email in self._fetch_emails(inbox_url):
            sender = email.get("from", "").lower()
            if sender == "verify@velog.io" or "velog.io" in sender:
                # 본문에 벨로그 인증 링크가 들어 있는지 한 번 더 확인.
                body = email.get("body", "")
                if "velog.io" in body:
                    return email
        return None

    @staticmethod
    def _email_kind(email: dict) -> str:
        """메일 제목으로 회원가입/로그인 여부를 추정한다."""
        subject = (email.get("subject") or "")
        if "로그인" in subject:
            return "로그인"
        if any(k in subject for k in ("가입", "환영", "인증", "Welcome", "Sign")):
            return "회원가입"
        return "인증"

    def _latest_email_uuid(self, inbox_url: str) -> str | None:
        self.log("기존 인증 메일을 확인하는 중...", "info")
        email = self._latest_velog_email(inbox_url)
        return email.get("uuid") if email else None

    def _wait_for_verification(self, page: Page, inbox_url: str, prev_uuid: str | None) -> str:
        self.log("새 인증 메일이 도착하기를 기다리는 중...", "info")
        for _ in range(36):  # 약 3분
            if self._stop.wait(5):
                raise PostingError("사용자가 작업을 중단했습니다.")
            email = self._latest_velog_email(inbox_url)
            if email and email.get("uuid") != prev_uuid:
                kind = self._email_kind(email)
                subject = email.get("subject") or "(제목 없음)"
                self.log(f"새 인증 메일 도착: '{subject}' → {kind} 메일로 인식", "info")
                return extract_velog_link(email.get("body", ""))
        raise PostingError("3분 안에 새 인증 메일이 도착하지 않았습니다.")

    def _open_link(self, pw, page: Page, link: str) -> None:
        self.log("인증 링크를 여는 중...", "info")
        # 메일을 읽고 링크를 누르는 듯한 짧은 간격
        self._sleep(_human_delay(1.0, 2.8))
        self._goto(page, link)
        self._wait("로그인 처리가 끝나기를 기다리는 중...", _human_delay(6.0, 11.0))
        self._wait_if_cloudflare(pw, page)

    # -- AdGuard 임시 메일 ------------------------------------------------
    def _adguard_mail_page(self) -> Page:
        try:
            return self._page_by_host("tempmail.adguard.com")
        except PostingError:
            return self._page_by_host("adguard.com")

    def _adguard_scope(self, page: Page):
        """임시메일 UI 문서. overview 페이지면 교차출처 iframe 안을 쓴다."""
        try:
            iframe = page.locator("iframe.temp-mail-app__iframe")
            if iframe.count() > 0:
                return page.frame_locator("iframe.temp-mail-app__iframe")
        except Error:
            pass
        try:
            for frame in page.frames:
                if "tempmail.adguard.com" in (frame.url or "").lower():
                    return frame
        except Error:
            pass
        return page

    def _adguard_create_address(self, pw, page: Page) -> str:
        self.log("AdGuard 임시 메일에 접속하는 중...", "info")
        self._goto(page, ADGUARD_TEMPMAIL_URL)
        self._wait("AdGuard 화면이 준비되기를 기다리는 중...", _jitter(3.0, 1.2))
        self._wait_if_cloudflare(pw, page)
        self._ensure_connected(pw)
        page = self._adguard_mail_page()
        self._adguard_dismiss_banners(page)
        root = self._adguard_scope(page)
        self._adguard_raise_if_blocked(page, root)

        btn = None
        for _ in range(40):
            if self._stop.wait(0):
                raise PostingError("사용자가 작업을 중단했습니다.")
            self._adguard_raise_if_blocked(page, root)
            existing = self._adguard_read_email(root)
            if existing:
                self.log(f"이미 생성된 AdGuard 주소: {existing}", "success")
                return existing
            btn = self._adguard_find_create_button(root)
            if btn is not None:
                break
            self._sleep(0.5)
        if btn is None:
            self._adguard_raise_if_blocked(page, root)
            raise PostingError("AdGuard '주소 생성' 버튼을 찾지 못했습니다.")

        self._sleep(_jitter(0.6, 0.3))
        self._stable_click(btn)
        self.log("주소 생성 버튼을 눌렀습니다. 메일 주소를 기다리는 중...", "info")

        for _ in range(40):
            if self._stop.wait(1):
                raise PostingError("사용자가 작업을 중단했습니다.")
            self._adguard_raise_if_blocked(page, root)
            email = self._adguard_read_email(root)
            if email:
                self.log(f"AdGuard 임시 메일 생성: {email}", "success")
                return email
        self._adguard_raise_if_blocked(page, root)
        raise PostingError("AdGuard 임시 메일 주소가 시간 내에 나타나지 않았습니다.")

    def _adguard_raise_if_blocked(self, page: Page, root=None) -> None:
        if self._adguard_browser_blocked(page, root):
            raise BrowserCheckFailed(
                "AdGuard 브라우저 확인 실패 — 다른 브라우저로 다시 엽니다.",
            )

    def _adguard_browser_blocked(self, page: Page, root=None) -> bool:
        scopes = []
        if root is not None:
            scopes.append(root)
        scopes.append(page)
        for scope in scopes:
            try:
                loc = scope.locator(".error-screen, .error-screen__title")
                if loc.count() > 0:
                    try:
                        if loc.first.is_visible():
                            return True
                    except Error:
                        return True
            except Error:
                pass
            try:
                loc = scope.get_by_text("브라우저 확인 실패")
                if loc.count() > 0:
                    return True
            except Error:
                pass
            try:
                loc = scope.get_by_text("다른 브라우저에서 열어")
                if loc.count() > 0:
                    return True
            except Error:
                pass
            try:
                text = scope.locator("body").inner_text(timeout=1_500) or ""
                if "브라우저 확인 실패" in text or "다른 브라우저에서 열어" in text:
                    return True
            except Error:
                pass
        return False

    def _adguard_find_create_button(self, root):
        for sel in _ADGUARD_CREATE_SELECTORS:
            loc = root.locator(sel)
            try:
                if loc.count() > 0 and loc.first.is_visible():
                    return loc.first
            except Error:
                continue
        try:
            loc = root.get_by_role(
                "button",
                name=re.compile(r"(주소\s*생성|Get address|Create address|Generate)", re.I),
            )
            if loc.count() > 0 and loc.first.is_visible():
                return loc.first
        except Error:
            pass
        try:
            loc = root.locator("section.address button, .address__in button, .address__field button")
            for i in range(loc.count()):
                btn = loc.nth(i)
                try:
                    if not btn.is_visible():
                        continue
                    cls = btn.get_attribute("class") or ""
                    text = (btn.inner_text() or "").replace("\n", " ").strip().lower()
                except Error:
                    continue
                if "address__copy" in cls:
                    continue
                if "변경" in text or "change" in text or "복사" in text:
                    continue
                if "생성" in text or "get" in text or "create" in text or "address__get" in cls:
                    return btn
        except Error:
            pass
        return None

    def _adguard_dismiss_banners(self, page: Page) -> None:
        for name in ("동의", "수락", "Accept", "I agree", "확인"):
            try:
                loc = page.get_by_role("button", name=name)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=3_000)
                    self._sleep(0.4)
                    return
            except Error:
                continue

    @staticmethod
    def _adguard_read_email(root) -> str:
        selectors = (
            "span.address__copy-text",
            "button.address__copy .address__copy-text",
            "button.address__copy",
        )
        for sel in selectors:
            try:
                loc = root.locator(sel)
                if loc.count() == 0:
                    continue
                text = (loc.first.inner_text(timeout=2_000) or "").strip()
                match = _EMAIL_RE.search(text)
                if match:
                    return match.group(0)
            except Error:
                continue
        return ""

    def _wait_adguard_velog_mail(self, page: Page) -> str:
        self.log("AdGuard 받은편지함에서 벨로그 인증 메일을 기다리는 중...", "info")
        root = self._adguard_scope(page)
        for _ in range(36):
            if self._stop.wait(5):
                raise PostingError("사용자가 작업을 중단했습니다.")
            try:
                rows = root.locator(".email-row")
                for index in range(rows.count()):
                    row = rows.nth(index)
                    try:
                        sender = (row.locator(".email-row__from").inner_text() or "").strip()
                        desc = (row.locator(".email-row__desc").inner_text() or "").strip()
                    except Error:
                        continue
                    blob = f"{sender} {desc}".lower()
                    if "adguard.com" in blob or "adguard 임시" in blob or "환영합니다" in desc:
                        continue
                    if "velog" not in blob and "verify@" not in blob:
                        continue
                    self.log(f"인증 메일 확인: {sender} / {desc}", "info")
                    self._stable_click(row)
                    self._sleep(_jitter(1.6, 0.6))
                    return self._adguard_extract_link(root)
            except Error:
                pass
        raise PostingError("3분 안에 AdGuard 받은편지함에 벨로그 인증 메일이 도착하지 않았습니다.")

    def _adguard_extract_link(self, page) -> str:
        iframe = page.locator("iframe.letter-iframe__content, .letter-iframe iframe")
        try:
            iframe.first.wait_for(state="attached", timeout=15_000)
        except (Error, PWTimeoutError) as exc:
            raise PostingError("인증 메일 본문을 열지 못했습니다.") from exc
        srcdoc = ""
        try:
            srcdoc = iframe.first.get_attribute("srcdoc") or ""
        except Error:
            srcdoc = ""
        if srcdoc.strip():
            return extract_velog_link(srcdoc)
        try:
            frame = iframe.first.content_frame
            if frame is not None:
                return extract_velog_link(frame.content())
        except Error:
            pass
        raise PostingError("인증 메일에서 벨로그 인증 링크를 찾지 못했습니다.")

    # -- 신규 계정 회원가입 ----------------------------------------------
    def _handle_signup_if_needed(self, pw, page: Page, account: dict) -> tuple[bool, str | None]:
        """인증 링크를 열었을 때 '환영합니다' 회원가입 폼이 뜨면 자동으로 가입한다.
        (가입 여부, 사용한 프로필 이름) 을 반환한다."""
        profile_input = page.get_by_placeholder("프로필 이름을 입력하세요")
        appeared = False
        for _ in range(6):  # 폼이 뜰 시간을 잠깐 준다
            try:
                if profile_input.count() > 0 and profile_input.first.is_visible():
                    appeared = True
                    break
            except Error:
                pass
            self._sleep(1)
        if not appeared:
            # 이미 가입된 계정 — 이후 재가입 폼이 뜨면 삭제하도록 이력에 남긴다.
            email = str(account.get("velog_id", "")).strip().lower()
            if email and email not in self._signed_up_emails:
                self._signed_up_emails.add(email)
                if self.on_signup is not None:
                    try:
                        self.on_signup(email)
                    except Exception:  # noqa: BLE001
                        pass
            return False, None

        email = str(account.get("velog_id", "")).strip().lower()
        if email and email in self._signed_up_emails:
            raise PostingError(
                "재가입 시도 — 이전에 가입했던 아이디입니다. 목록에서 삭제합니다.",
            )

        self.log("회원가입 중.. (신규 계정)", "info")
        names = account.get("profile_names") or DEFAULT_PROFILE_NAMES
        name = random.choice(names)
        user_id = make_user_id()
        bio = make_bio(name)
        self.log(f"프로필 이름: {name} / 사용자 ID: {user_id}", "info")

        self._sleep(_jitter(0.8, 0.5))

        # 프로필 이름
        profile_input.first.click()
        self._sleep(_jitter(0.4, 0.3))
        self._type_like_human(page, name)
        self._sleep(_jitter(0.7, 0.4))

        # 사용자 ID
        uid = page.get_by_placeholder("사용자 ID를 입력하세요.")
        uid.first.wait_for(state="visible", timeout=10_000)
        uid.first.click()
        self._sleep(_jitter(0.4, 0.3))
        self._type_like_human(page, user_id)
        self._sleep(_jitter(0.7, 0.4))

        # 한 줄 소개
        bio_box = page.get_by_placeholder("당신을 한 줄로 소개해보세요")
        if bio_box.count() > 0 and bio_box.first.is_visible():
            bio_box.first.click()
            self._sleep(_jitter(0.35, 0.25))
            self._type_like_human(page, bio)
            self._sleep(_jitter(0.7, 0.4))

        # 약관 동의
        self._sleep(_jitter(0.5, 0.3))
        self._check_agreement(page)
        self._sleep(_jitter(0.7, 0.4))
        try:
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            self._sleep(0.35)
        except Error:
            pass

        # 가입 — CDP 연결 중 클릭하지 않음 (Turnstile 실패 원인)
        self._submit_signup(pw, page, profile_input)
        self.log("회원가입을 완료했습니다.", "success")
        if email:
            self._signed_up_emails.add(email)
            if self.on_signup is not None:
                try:
                    self.on_signup(email)
                except Exception:  # noqa: BLE001
                    pass
        self._sleep(_jitter(1.5, 0.8))
        return True, name

    def _submit_signup(self, pw, page: Page, profile_input) -> None:
        """가입 버튼을 누른다. 버튼이 곧바로 활성화돼 있으면 바로 클릭하고,
        Cloudflare 로 비활성/실패면 연결을 끊어 깨끗한 상태에서 인증을 통과시킨
        뒤 재연결하여 클릭한다. (출간 단계와 같은 방식)"""
        join = self._visible_last(page.get_by_role("button", name="가입", exact=True))
        # 1) 빠르게 활성화돼 있으면 바로 클릭
        if join is not None and self._enabled_within(join, 6):
            self._sleep(_jitter(0.4, 0.3))
            try:
                join.click(timeout=15_000)
                if self._signup_form_gone(page, profile_input):
                    return
            except Error:
                pass

        # 2) Cloudflare 차단 가능성 → 연결 해제 후 재연결-감시로 클릭
        self.log("가입 인증(Cloudflare) 통과를 위해 연결을 잠시 해제합니다...", "info")
        if not self._endpoint:
            raise PostingError("가입 인증을 진행할 수 없습니다.")
        self._disconnect_only()
        for _ in range(80):  # 약 4분
            if self._stop.wait(0):
                raise PostingError("사용자가 작업을 중단했습니다.")
            browser = None
            try:
                browser = pw.chromium.connect_over_cdp(self._endpoint, timeout=8_000)
                p = self._find_velog_page(browser)
                if p is not None:
                    pf = p.get_by_placeholder("프로필 이름을 입력하세요")
                    if pf.count() == 0 or not pf.first.is_visible():
                        return  # 폼이 사라짐 = 가입 완료
                    jb = self._visible_last(p.get_by_role("button", name="가입", exact=True))
                    if jb is not None and not self._is_disabled(jb):
                        self.log("가입 인증 통과 → 가입을 진행합니다.", "info")
                        self._sleep(_jitter(0.4, 0.3))
                        jb.click(timeout=15_000)
                        self._sleep(2)
            except (Error, PWTimeoutError):
                pass
            finally:
                if browser is not None:
                    try:
                        browser.close()
                    except Error:
                        pass
            self._sleep(3)
        raise PostingError("가입 인증을 시간 내에 통과하지 못했습니다.")

    @staticmethod
    def _is_disabled(locator) -> bool:
        try:
            return locator.is_disabled()
        except Error:
            return False

    def _enabled_within(self, locator, seconds: float) -> bool:
        steps = max(1, int(seconds / 1.0))
        for _ in range(steps):
            if not self._is_disabled(locator):
                return True
            self._sleep(1)
        return not self._is_disabled(locator)

    def _signup_form_gone(self, page: Page, profile_input) -> bool:
        for _ in range(15):
            if self._stop.wait(1):
                raise PostingError("사용자가 작업을 중단했습니다.")
            try:
                if profile_input.count() == 0 or not profile_input.first.is_visible():
                    return True
            except Error:
                return True
        return False

    def _set_blog_title(self, page: Page, title: str) -> None:
        """신규 가입 직후 설정 페이지에서 벨로그 제목을 프로필 이름으로 바꾼다."""
        self.log("벨로그 제목을 설정하는 중...", "info")
        try:
            self._goto(page, "https://velog.io/setting")
            self._sleep(_jitter(2, 0.5))

            row = page.locator('div[class*="SettingRow"]').filter(
                has=page.locator('h3', has_text="벨로그 제목"),
            ).first
            row.wait_for(state="visible", timeout=15_000)

            edit_candidates = row.locator(
                'button[class*="SettingEditButton"], div.editWrapper button, button:has-text("수정")'
            )
            if edit_candidates.count() > 0:
                edit_btn = edit_candidates.first
            else:
                edit_btn = page.locator(
                    'xpath=//h3[normalize-space()="벨로그 제목"]'
                    '/ancestor::div[contains(@class,"SettingRow")][1]'
                    '//button[contains(@class,"SettingEditButton") or normalize-space()="수정"]'
                ).first
            edit_btn.wait_for(state="visible", timeout=10_000)
            edit_btn.scroll_into_view_if_needed()
            edit_btn.click()
            self._sleep(_jitter(0.6, 0.3))

            title_input = row.locator('input[placeholder="벨로그 제목"]').first
            if title_input.count() == 0 or not title_input.is_visible():
                title_input = page.get_by_placeholder("벨로그 제목").first
            title_input.wait_for(state="visible", timeout=10_000)
            title_input.click()
            self._sleep(_jitter(0.3, 0.2))
            title_input.fill("")
            self._sleep(_jitter(0.2, 0.1))
            title_input.fill(title)
            self._sleep(_jitter(0.5, 0.3))

            clicked = False
            for selector in (
                row.locator('button:has-text("저장")'),
                row.locator('button:has-text("확인")'),
                row.locator('button:has-text("완료")'),
                row.locator('button[class*="SettingEditButton"]'),
                page.locator('button:has-text("저장")'),
                page.locator('button:has-text("확인")'),
            ):
                if selector.count() == 0:
                    continue
                candidate = selector.first
                try:
                    if candidate.is_visible():
                        candidate.click()
                        clicked = True
                        break
                except Error:
                    continue
            if not clicked:
                title_input.press("Enter")

            self._sleep(_jitter(1.2, 0.4))
            self.log(f"벨로그 제목을 '{title}'(으)로 변경했습니다.", "success")
        except Error as exc:
            # 제목 설정 실패해도 출간은 계속 (페이지 재연결 직후 레이스 등)
            self.log(f"벨로그 제목 설정 실패(출간은 계속): {exc}", "info")

    def _ensure_connected(self, pw) -> None:
        """signup 단계에서 연결을 끊었을 수 있으니, 끊겨 있으면 재연결한다."""
        if self._browser is not None and self._context is not None:
            try:
                _ = list(self._context.pages)
                return
            except Error:
                self._browser = None
                self._context = None
        if not self._endpoint:
            return
        browser = pw.chromium.connect_over_cdp(self._endpoint, timeout=20_000)
        self._browser = browser
        self._handoff = False
        if browser.contexts:
            self._context = browser.contexts[0]
            self._context.add_init_script(STEALTH_SCRIPT)

    def _check_agreement(self, page: Page) -> None:
        """이용약관 동의 체크박스를 켠다. 이미 체크된 경우 다시 눌러 해제하지 않는다."""
        # 이미 체크됨
        try:
            cb = page.locator("input[type='checkbox']")
            if cb.count() > 0:
                if cb.first.is_checked():
                    return
                cb.first.check(timeout=3_000)
                return
        except Error:
            pass
        # 체크마크 svg 를 가진 div
        try:
            box_div = page.locator("div:has(> svg path[d^='M20.285 2'])").first
            if box_div.count() > 0:
                # aria / data 로 이미 선택됐는지 추정
                try:
                    pressed = box_div.get_attribute("aria-checked") or box_div.get_attribute("data-checked")
                    if str(pressed).lower() in {"true", "1", "checked"}:
                        return
                except Error:
                    pass
                box_div.click(timeout=5_000)
                return
        except Error:
            pass
        try:
            link = page.get_by_text("이용약관", exact=False).first
            box = link.bounding_box()
            if box:
                page.mouse.click(box["x"] - 16, box["y"] + box["height"] / 2)
                return
        except Error:
            pass
        self.log("약관 동의 체크박스를 자동으로 누르지 못했습니다. 창에서 직접 체크해 주세요.", "error")

    # -- 글 작성 ----------------------------------------------------------
    def _write_post(
        self,
        page: Page,
        title: str,
        body: str,
        tags: list[str] | None = None,
        image_folder: str = "",
        image_link: str = "",
    ) -> Page:
        assert self._context is not None
        # 인증 후 velog 탭으로 이동
        target = next(
            (p for p in reversed(self._context.pages) if "velog.io" in p.url),
            page,
        )
        self._sleep(_jitter(3))
        if target.url.rstrip("/") != "https://velog.io":
            self._goto(target, "https://velog.io/")
        self._wait("로그인된 화면이 준비되기를 기다리는 중...", _jitter(4))

        # 새 글 작성 버튼: 로그인 UI 가 늦게 렌더링될 수 있으므로 폴링 + 1회 새로고침.
        self.log("새 글 작성 버튼을 누르는 중...", "info")
        self._click_write_button(target)
        self._wait("작성 화면이 준비되기를 기다리는 중...", _jitter(4))

        # 원고(제목/본문)보다 이미지를 먼저 첨부한다.
        if image_folder:
            target.locator(".CodeMirror").wait_for(state="visible", timeout=15_000)
            self._insert_random_image(target, image_folder, link_url=image_link)

        tag_list = [t.strip().lstrip("#").strip() for t in (tags or []) if t.strip()]
        if tag_list:
            self.log(f"원고 해시태그 {len(tag_list)}개 → 태그 입력란에 먼저 등록합니다.", "info")
            if not self._fill_tags(target, tag_list):
                self.log("태그 자동 입력에 실패했습니다. 작성 화면에서 직접 입력해 주세요.", "error")

        # 제목 입력 — 시스템 클립보드를 쓰지 않고 fill 로 직접 넣는다.
        self.log("제목을 입력하는 중...", "info")
        title_box = target.get_by_placeholder("제목을 입력하세요")
        title_box.wait_for(state="visible", timeout=15_000)
        self._fill_with_retry(
            target,
            label="제목",
            text=title,
            focus=lambda: title_box.click(),
            write=lambda: (title_box.fill(""), title_box.fill(title)),
            read_back=lambda: self._read_title(title_box).strip(),
            expected=title.strip(),
        )

        # 본문 입력 (CodeMirror) — 이미 넣은 이미지 뒤에 원고를 이어 붙인다.
        self.log("본문을 입력하는 중...", "info")
        editor = target.locator(".CodeMirror")
        editor.wait_for(state="visible", timeout=15_000)
        body_to_write = body
        if image_folder:
            try:
                current = editor.evaluate("(el) => el.CodeMirror.getValue()") or ""
            except Error:
                current = ""
            current = str(current).rstrip()
            if current:
                body_to_write = f"{current}\n\n{body.lstrip()}"
        self._fill_with_retry(
            target,
            label="본문",
            text=body_to_write,
            focus=lambda: target.locator(".CodeMirror-scroll").click(),
            write=lambda: self._set_codemirror_value(editor, body_to_write),
            read_back=lambda: editor.evaluate("(el) => el.CodeMirror.getValue()"),
            expected=body_to_write,
        )

        self.log("제목과 본문이 올바르게 입력되었습니다.", "info")
        return target

    def _click_write_button(self, target: Page) -> None:
        """'새 글 작성' 버튼을 최대 약 30초간 폴링하며, 중간에 1회 새로고침한다."""
        for attempt in range(6):
            for locator in (
                target.get_by_role("button", name="새 글 작성", exact=True),
                target.get_by_role("link", name="새 글 작성", exact=True),
                target.get_by_text("새 글 작성", exact=True),
            ):
                item = self._visible_last(locator)
                if item is not None:
                    self._sleep(_jitter(0.5, 0.3))
                    item.click(timeout=15_000)
                    return
            if attempt == 2:
                # 절반쯤 지나도 안 보이면 로그인 상태 반영을 위해 새로고침.
                self.log("화면을 새로고침하고 다시 찾는 중...", "info")
                self._goto(target, "https://velog.io/")
            self._sleep(_jitter(4))
        raise PostingError("새 글 작성 버튼을 찾지 못했습니다.")

    def _fill_with_retry(
        self,
        target: Page,
        label: str,
        text: str,
        focus,
        write,
        read_back,
        expected: str,
        attempts: int = 3,
    ) -> None:
        """클립보드 없이 직접 입력하고, 값이 들어갔는지 확인 후 실패 시 재시도."""
        for attempt in range(1, attempts + 1):
            focus()
            self._sleep(_jitter(0.35, 0.2))
            write()
            self._sleep(_jitter(1.2))
            try:
                if read_back() == expected:
                    return
            except Error:
                pass
            if attempt < attempts:
                self.log(f"{label} 입력을 다시 시도하는 중... ({attempt}/{attempts})", "info")
        raise PostingError(f"{label} 입력 검증에 실패했습니다.")

    @staticmethod
    def _read_title(title_box) -> str:
        """제목 칸이 input 이면 value, contenteditable 이면 inner_text 를 읽는다."""
        try:
            value = title_box.input_value()
            if value:
                return value
        except Error:
            pass
        try:
            return title_box.inner_text() or ""
        except Error:
            return ""

    # -- 이미지 등록 ------------------------------------------------------
    def _tick_image_holds(self) -> None:
        """계정 1건 진행할 때마다 보류 카운트를 1씩 줄인다."""
        if not self._image_hold:
            return
        expired: list[str] = []
        for path, left in list(self._image_hold.items()):
            if left <= 1:
                expired.append(path)
            else:
                self._image_hold[path] = left - 1
        for path in expired:
            self._image_hold.pop(path, None)
            self.log(f"이미지 보류 해제: {Path(path).name}", "info")

    def _hold_image(self, path: Path) -> None:
        lo, hi = self._image_hold_default
        turns = random.randint(lo, hi)
        key = str(path.resolve()) if path.exists() else str(path)
        self._image_hold[key] = turns
        self.log(f"이미지 보류 {turns}건: {path.name} (당분간 다른 파일 우선)", "info")

    def _list_upload_images(self, folder: str) -> list[Path]:
        exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
        root = Path(folder)
        if not root.is_dir():
            return []
        images: list[Path] = []
        for p in root.iterdir():
            if not p.is_file() or p.suffix.lower() not in exts:
                continue
            try:
                key = str(p.resolve())
            except OSError:
                key = str(p)
            if self._image_hold.get(key, 0) > 0:
                continue
            images.append(p)
        if not images:
            return []
        # 최근 사용한 이미지는 가능한 한 제외
        recent = set(self._recent_images)
        fresh = []
        for p in images:
            try:
                key = str(p.resolve())
            except OSError:
                key = str(p)
            if key not in recent:
                fresh.append(p)
        return fresh if fresh else images

    def _remember_image(self, path: Path) -> None:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        self._recent_images = [k for k in self._recent_images if k != key]
        self._recent_images.append(key)
        overflow = len(self._recent_images) - self._recent_image_limit
        if overflow > 0:
            self._recent_images = self._recent_images[overflow:]

    def _note_image_outcome(self, success: bool) -> None:
        if success:
            self._image_fail_streak = 0
            return
        self._image_fail_streak += 1

    def _prepare_image_for_upload(self, src: Path) -> Path:
        """업로드 전 긴 변 1600px 이하로 줄이고 용량을 낮춘 임시 파일을 만든다."""
        try:
            from PIL import Image
        except ImportError:
            return src
        try:
            with Image.open(src) as raw:
                im = raw.copy()
            max_side = 1600
            w, h = im.size
            if max(w, h) > max_side:
                ratio = max_side / float(max(w, h))
                im = im.resize((max(1, int(w * ratio)), max(1, int(h * ratio))), Image.Resampling.LANCZOS)

            suffix = src.suffix.lower()
            if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
                suffix = ".jpg"
            if suffix in {".jpg", ".jpeg"} and im.mode not in {"RGB", "L"}:
                if im.mode == "RGBA":
                    bg = Image.new("RGB", im.size, (255, 255, 255))
                    bg.paste(im, mask=im.split()[-1])
                    im = bg
                else:
                    im = im.convert("RGB")

            fd, name = tempfile.mkstemp(prefix="velog-img-", suffix=suffix)
            os.close(fd)
            out = Path(name)
            if suffix in {".jpg", ".jpeg"}:
                im.save(out, format="JPEG", quality=82, optimize=True)
            elif suffix == ".png":
                im.save(out, format="PNG", optimize=True)
            else:
                im.save(out)
            self._temp_upload_files.append(out)
            return out
        except Exception:  # noqa: BLE001
            return src

    def _insert_random_image(self, target: Page, folder: str, link_url: str = "") -> None:
        """폴더 안 이미지를 무작위로 올려 본문에 넣는다. 실패하면 계정 실패로 처리한다."""
        images = self._list_upload_images(folder)
        if not images:
            raise PostingError("이미지 등록 실패 — 사용할 이미지가 없습니다.")

        random.shuffle(images)
        candidates = images[: max(1, self._image_try_limit)]
        last_error = ""

        for attempt, chosen in enumerate(candidates, start=1):
            try:
                if self._try_upload_image(target, chosen, link_url=link_url):
                    self._remember_image(chosen)
                    self._note_image_outcome(True)
                    return
                last_error = "업로드 시간 초과"
                self._hold_image(chosen)
            except PostingError as exc:
                last_error = str(exc)
                self.log(f"이미지 시도 실패 ({chosen.name}): {exc}", "error")
                if "이미지 등록 버튼" in last_error:
                    break
                self._hold_image(chosen)
            if attempt < len(candidates):
                self.log(
                    f"다른 이미지로 재시도합니다... ({attempt}/{len(candidates)})",
                    "info",
                )
                self._sleep(_jitter(4, 1.5))

        self._note_image_outcome(False)
        raise PostingError(
            f"이미지 등록에 실패했습니다. ({last_error or '업로드 실패'})",
        )

    def _try_upload_image(self, target: Page, chosen: Path, link_url: str = "") -> bool:
        """한 장 업로드. 성공 True, 시간 초과 False. UI 오류는 PostingError."""
        upload_path = self._prepare_image_for_upload(chosen)
        self.log(f"이미지를 등록하는 중: {chosen.name}", "info")

        editor = target.locator(".CodeMirror")
        before = ""
        try:
            before = editor.evaluate("(el) => el.CodeMirror.getValue()")
        except Error:
            pass

        try:
            target.locator(".CodeMirror-scroll").click()
            self._sleep(_jitter(0.4, 0.2))
            target.keyboard.press("Control+Home")
            self._sleep(_jitter(0.3, 0.2))
        except Error:
            pass

        image_button = None
        for sel in (
            "button:has(svg path[d^='M21 19V5'])",
            "button[aria-label*='이미지']",
            "button[title*='이미지']",
            "button:has-text('이미지')",
        ):
            loc = target.locator(sel)
            try:
                if loc.count() > 0 and loc.first.is_visible():
                    image_button = loc.first
                    break
            except Error:
                continue
        if image_button is None:
            raise PostingError("본문 이미지 등록 버튼을 찾지 못했습니다.")

        try:
            image_button.scroll_into_view_if_needed(timeout=5_000)
        except Error:
            pass
        try:
            with target.expect_file_chooser(timeout=15_000) as fc:
                image_button.click()
            fc.value.set_files(str(upload_path))
        except (Error, PWTimeoutError) as exc:
            raise PostingError("이미지 파일 선택창을 열지 못했습니다.") from exc

        self.log("이미지 업로드 완료를 기다리는 중...", "info")
        for _ in range(20):
            if self._stop.wait(2):
                raise PostingError("사용자가 작업을 중단했습니다.")
            try:
                now = editor.evaluate("(el) => el.CodeMirror.getValue()")
            except Error:
                continue
            if ("![" in now or "<img" in now.lower()) and now != before:
                self.log("이미지가 본문에 등록되었습니다.", "info")
                if link_url:
                    self._link_image(target, editor, link_url)
                return True
        return False

    def _link_image(self, target: Page, editor, link_url: str) -> None:
        """본문 첫 이미지에 홈페이지 링크를 연결한다 (마크다운/HTML)."""
        try:
            wrapped = editor.evaluate(
                """(el, url) => {
                    const cm = el.CodeMirror;
                    let v = cm.getValue();
                    const esc = (s) => String(s)
                        .replace(/&/g, '&amp;')
                        .replace(/"/g, '&quot;')
                        .replace(/</g, '&lt;');

                    const mdRe = /!\\[([^\\]]*)\\]\\(([^)]+)\\)/;
                    const md = mdRe.exec(v);
                    if (md) {
                        const idx = md.index;
                        const alt = md[1] || '';
                        const imgUrl = md[2].trim();
                        const before = v.slice(Math.max(0, idx - 2), idx);
                        if (/\\]\\($/.test(before)) return true;
                        const linked = '<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">'
                            + '<img src="' + esc(imgUrl) + '" alt="' + esc(alt) + '">'
                            + '</a>';
                        v = v.slice(0, idx) + linked + v.slice(idx + md[0].length);
                    } else {
                        const htmlRe = /<img\\b[^>]*>/i;
                        const hm = htmlRe.exec(v);
                        if (!hm) return false;
                        const tag = hm[0];
                        const idx = hm.index;
                        const before = v.slice(Math.max(0, idx - 80), idx);
                        if (/<a\\b[^>]*>\\s*$/i.test(before)) return true;
                        v = v.slice(0, idx)
                          + '<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">'
                          + tag + '</a>' + v.slice(idx + tag.length);
                    }
                    v = v.replace(/^\\[\\s*\\r?\\n(?=!<a\\b|!\\[)/, '');
                    cm.setValue(v);
                    return true;
                }""",
                link_url,
            )
            if wrapped:
                self.log(f"이미지에 홈페이지 링크를 연결했습니다: {link_url}", "info")
        except Error:
            self.log("이미지 링크 연결에 실패했습니다(이미지는 정상 등록됨).", "info")

    def _fill_tags(self, target: Page, tags: list[str]) -> bool:
        """작성 화면/출간 패널의 '태그를 입력하세요' input 에 태그를 등록."""
        if not tags:
            return True
        try:
            tag_input = target.locator('input[placeholder="태그를 입력하세요"]').first
            tag_input.wait_for(state="visible", timeout=15_000)
        except Error:
            return False

        added = 0
        for keyword in tags:
            if not keyword:
                continue
            try:
                tag_input.click(timeout=5_000)
                self._sleep(_jitter(0.35, 0.2))
                tag_input.fill(keyword)
                self._sleep(_jitter(0.35, 0.2))
                tag_input.press("Enter")
                self._sleep(_jitter(0.55, 0.3))
                added += 1
            except Error:
                self.log(f"태그 입력 실패: {keyword}", "error")
        if added:
            self.log(f"태그 {added}개 입력 완료.", "success")
        return added > 0

    # -- 출간 ------------------------------------------------------------
    def _publish(self, pw, target: Page) -> str | None:
        """출간 단계.

        Cloudflare Turnstile 은 출간 패널이 '처음 렌더링되는 순간'의 브라우저
        상태로 판정한다. 자동화(CDP)가 연결된 채 패널을 열면 그 자리에서
        '확인 실패'로 낙인찍힌다. 그래서:
          1) 패널을 열기 전에 먼저 연결을 끊는다(깨끗한 일반 브라우저).
          2) 사용자(또는 안내)가 '출간하기'로 패널을 열면 인증이 통과되어
             최종 버튼이 활성화된다.
          3) 인증이 통과된 '뒤'에 잠깐 다시 연결한다. 이 시점엔 토큰이 이미
             발급돼 있으므로, 우리가 버튼만 눌러도 그대로 출간된다.
        """
        self.log("입력 완료. 출간 인증 통과를 위해 자동화 연결을 잠시 해제합니다...", "info")
        self._disconnect_only()
        self.log("✅ 연결 해제됨 — 이제 평범한 일반 브라우저 상태입니다.", "success")
        self.log("출간 패널을 자동으로 열고, 인증이 통과되면 자동으로 출간합니다...", "info")

        if not self._endpoint:
            return None

        panel_opened = False
        clicked_publish = False
        # 패널 열기 → (끊고 인증 통과 대기) → 활성화되면 최종 출간 클릭, 을 감시한다.
        for _ in range(120):  # 약 6분
            if self._stop.wait(0):
                raise PostingError("사용자가 작업을 중단했습니다.")

            browser = None
            try:
                browser = pw.chromium.connect_over_cdp(self._endpoint, timeout=8_000)
                page = self._find_velog_page(browser)
                if page is not None:
                    url = page.url
                    # 이미 게시글 페이지(@아이디/제목)로 이동한 경우 = 출간 완료
                    if self._is_post_url(url):
                        self.log(f"출간 완료 🎉 {url}", "success")
                        return url

                    if self._has_publish_fail_toast(page):
                        raise self._publish_fail_error()

                    final = page.locator('[data-testid="publish"]')
                    final_visible = final.count() > 0 and final.first.is_visible()

                    if not final_visible:
                        # 아직 패널이 안 열림 → '출간하기'(첫 버튼)를 눌러 연다.
                        first = self._visible_last(page.locator(
                            "button:has-text('출간하기'):not([data-testid='publish'])"
                        ))
                        if first is not None:
                            if not panel_opened:
                                self.log("출간 패널을 여는 중...", "info")
                            self._sleep(_jitter(0.4, 0.3))
                            first.click(timeout=15_000)
                            panel_opened = True
                        # 클릭 직후 곧바로 연결을 끊어(아래 finally),
                        # 인증이 깨끗한 상태에서 검증되도록 한다.
                    else:
                        if not final.first.is_disabled() and not clicked_publish:
                            # 인증 통과 상태 → 최종 출간 클릭 후, 연결을 끊고
                            # 깨끗한 상태에서 게시글 주소로 리다이렉트되길 기다린다.
                            self.log("인증 통과 확인 → 자동으로 출간합니다.", "info")
                            self._sleep(_jitter(0.4, 0.3))
                            final.first.click(timeout=15_000)
                            clicked_publish = True
                    # else: 패널은 열렸지만 아직 인증 미통과 → 끊고 대기.
            except PostingError:
                raise
            except (Error, PWTimeoutError):
                pass
            finally:
                # 감시/조작용 연결은 즉시 끊어, 인증이 깨끗한 상태에서 통과되게 한다.
                if browser is not None:
                    try:
                        browser.close()
                    except Error:
                        pass

            if clicked_publish:
                # 출간 버튼을 눌렀으니, 게시글 주소를 폴링해 정확한 URL 을 잡는다.
                return self._wait_published_url(pw)

            self._sleep(3)

        # 시간 초과 — 사용자가 직접 마무리하도록 안내.
        self.log(
            "자동 출간 대기 시간이 지났습니다. 활성화된 '출간하기'를 직접 눌러 완료해 주세요.",
            "success",
        )
        return None

    @staticmethod


    def _is_post_url(url: str) -> bool:
        """발행된 게시글 주소(velog.io/@아이디/제목)인지 판별."""
        return "velog.io/@" in url and "/write" not in url

    @staticmethod
    def _has_publish_fail_toast(page: Page) -> bool:
        """Toastify '포스트 작성/수정 실패' 알림이 떠 있으면 계정 잠김으로 본다."""
        try:
            return bool(page.evaluate(
                """() => {
                    const nodes = document.querySelectorAll(
                        '.Toastify__toast-body, [role="alert"], .Toastify__toast'
                    );
                    for (const n of nodes) {
                        const t = (n.textContent || '').replace(/\\s+/g, ' ').trim();
                        if (t.includes('포스트 작성 실패') || t.includes('포스트 수정 실패')) {
                            return true;
                        }
                    }
                    return false;
                }"""
            ))
        except Error:
            return False

    @staticmethod
    def _publish_fail_error() -> PostingError:
        return PostingError(
            "계정 잠김: 포스트 작성/수정 실패 — 목록에서 삭제합니다.",
        )
    def _wait_published_url(self, pw) -> str | None:
        """출간 클릭 후, 게시글 주소(@아이디/제목)로 리다이렉트될 때까지 기다려
        그 정확한 URL 을 반환한다."""
        self.log("출간 완료 후 게시글 주소를 확인하는 중...", "info")
        last_url = ""
        username = ""
        for _ in range(50):
            if self._stop.wait(2):
                raise PostingError("사용자가 작업을 중단했습니다.")
            browser = None
            try:
                browser = pw.chromium.connect_over_cdp(self._endpoint, timeout=8_000)
                page = self._find_velog_page(browser)
                if page is not None:
                    if self._has_publish_fail_toast(page):
                        raise self._publish_fail_error()
                    last_url = page.url or last_url
                    if self._is_post_url(last_url):
                        self.log(f"출간 완료 🎉 {last_url}", "success")
                        return last_url
                    if not username:
                        username = self._read_velog_username(page)
            except PostingError:
                raise
            except (Error, PWTimeoutError):
                pass
            finally:
                if browser is not None:
                    try:
                        browser.close()
                    except Error:
                        pass

        resolved = self._resolve_post_url_from_profile(pw, username)
        if resolved:
            self.log(f"출간 완료 🎉 {resolved}", "success")
            return resolved

        self.log("게시글 주소 확인에 실패했습니다. 창에서 직접 확인해 주세요.", "error")
        # /write?id= 는 초안 주소이므로 성공으로 반환하지 않는다.
        if last_url and self._is_post_url(last_url):
            return last_url
        return None

    @staticmethod
    def _read_velog_username(page: Page) -> str:
        try:
            href = page.evaluate(
                """() => {
                    const a = document.querySelector('a[href^="/@"]');
                    return a ? a.getAttribute('href') : '';
                }"""
            ) or ""
        except Error:
            return ""
        m = re.match(r"^/@([^/?#]+)", str(href).strip())
        return m.group(1) if m else ""

    def _resolve_post_url_from_profile(self, pw, username: str) -> str | None:
        """'/write?id=' 에 머문 경우 프로필에서 최신 글 URL을 한 번 더 찾는다."""
        if not username or not self._endpoint:
            return None
        self.log(f"프로필(@{username})에서 최신 글 주소를 찾는 중...", "info")
        browser = None
        try:
            browser = pw.chromium.connect_over_cdp(self._endpoint, timeout=8_000)
            page = self._find_velog_page(browser)
            if page is None:
                return None
            self._goto(page, f"https://velog.io/@{username}")
            self._sleep(_jitter(2.5, 1.0))
            href = page.evaluate(
                """(user) => {
                    const links = Array.from(document.querySelectorAll('a[href*="/@"]'));
                    const prefix = '/@' + user + '/';
                    for (const a of links) {
                        const h = a.getAttribute('href') || '';
                        if (h.startsWith(prefix) && h !== prefix && !h.includes('/about') && !h.includes('/series')) {
                            return h.startsWith('http') ? h : ('https://velog.io' + h);
                        }
                    }
                    return '';
                }""",
                username,
            )
            href = str(href or "").strip()
            if href and self._is_post_url(href):
                return href
        except (Error, PWTimeoutError, PostingError):
            return None
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Error:
                    pass
        return None

    def _confirm_published(self, page: Page) -> bool:
        """출간 후 작성 화면(/write)에서 벗어나면 성공으로 본다."""
        for _ in range(20):  # 약 40초
            if self._stop.wait(2):
                raise PostingError("사용자가 작업을 중단했습니다.")
            try:
                if "/write" not in page.url:
                    return True
            except Error:
                return True
        return False

    def _page_by_host(self, host: str) -> Page:
        """현재 컨텍스트에서 host 가 들어간 탭을 찾는다. 최신 탭 우선."""
        if self._context is None:
            raise PostingError("브라우저 연결이 없습니다.")
        for page in reversed(self._context.pages):
            try:
                if page.is_closed():
                    continue
                if host in (page.url or ""):
                    return page
            except Error:
                continue
        raise PostingError(f"{host} 탭을 찾지 못했습니다.")

    @staticmethod
    def _find_page_containing(browser, needle: str) -> Page | None:
        for ctx in browser.contexts:
            for page in ctx.pages:
                try:
                    if needle in (page.url or ""):
                        return page
                except Error:
                    continue
        return None

    @staticmethod
    def _find_velog_page(browser) -> Page | None:
        return VelogPoster._find_page_containing(browser, "velog.io")

    def _disconnect_only(self) -> None:
        """Chrome 창/프로세스는 그대로 두고 현재 CDP 연결만 해제한다."""
        self._handoff = True  # 이후 cleanup 이 Chrome 을 닫지 않도록.
        browser, self._browser = self._browser, None
        self._context = None
        if browser is not None:
            try:
                browser.close()  # connect_over_cdp: 연결만 해제, Chrome 유지
            except Error:
                pass

    # -- 종료 처리 --------------------------------------------------------
    def _wait_until_stopped(self) -> None:
        while not self._stop.wait(0.25):
            if self._context is None:
                return

    def _reset_account_state(self) -> None:
        """다음 계정 처리를 위해 계정별 상태를 초기화한다."""
        self._browser = None
        self._context = None
        self._process = None
        self._temp_profile = None
        self._endpoint = None
        self._handoff = False
        self._browser_label = "Chrome"

    def _teardown_account(self) -> None:
        """현재 계정의 Chrome 창/프로세스/임시 프로필을 완전히 정리한다."""
        browser, self._browser = self._browser, None
        process, self._process = self._process, None
        profile, self._temp_profile = self._temp_profile, None
        self._context = None
        self._endpoint = None

        for tmp in self._temp_upload_files:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        self._temp_upload_files = []

        if browser is not None:
            try:
                browser.close()
            except Error:
                pass
        # 출간이 끝났으면 Chrome 창을 닫아 다음 계정으로 넘어간다.
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    process.kill()
                except OSError:
                    pass
        if profile is not None:
            for _ in range(10):
                shutil.rmtree(profile, ignore_errors=True)
                if not profile.exists():
                    break
                time.sleep(0.3)
