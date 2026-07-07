"""벨로그 자동 포스팅 엔진.

설계 원칙
- 사용자의 실제 Google Chrome을 '시크릿 모드'로 직접 실행한 뒤 CDP로 연결한다.
  (Playwright 번들 크로미움이 아닌 실제 Chrome을 쓰므로 핑거프린트가 자연스럽다.)
- navigator.webdriver 등 자동화 흔적을 init script 로 제거한다.
- 입력은 실제 키 이벤트 / 클립보드 붙여넣기로 처리해 사람과 구분되지 않게 한다.
- 모든 대기/입력 사이에 무작위 지터를 둔다.
"""

from __future__ import annotations

import os
import random
import re
import shutil
import socket
import string
import subprocess
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
    parser = _LinkParser()
    parser.feed(body)
    candidates: list[str] = []
    for link in parser.links:
        parsed = urlparse(link)
        if parsed.scheme == "https" and parsed.netloc.lower() in {"velog.io", "www.velog.io"}:
            if parsed.path != "/" or parsed.query:
                candidates.append(link)
    if not candidates:
        raise PostingError("인증 메일에서 벨로그 인증 링크를 찾지 못했습니다.")
    return candidates[-1]


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


def extract_summary(body: str, max_len: int = 150) -> str:
    """출간 소개란용 — 원고 상단 본문을 평문으로 추출."""
    text = body
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"#{1,6}\s+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    for chunk in re.split(r"(?<=[.!?。])\s+|\n+", text):
        chunk = chunk.strip()
        if chunk:
            return chunk[:max_len]
    return text[:max_len]


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


def find_chrome() -> Path:
    """설치된 실제 Google Chrome 실행 파일을 찾는다."""
    keys = ["LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"]
    for key in keys:
        base = os.environ.get(key)
        if not base:
            continue
        candidate = Path(base) / "Google/Chrome/Application/chrome.exe"
        if candidate.is_file():
            return candidate
    raise PostingError("Google Chrome을 찾지 못했습니다. Chrome을 설치해 주세요.")


def _jitter(base: float, spread: float = 0.5) -> float:
    return base + random.uniform(0, spread)


# 신규 계정 회원가입에 쓰이는 기본값
DEFAULT_PROFILE_NAMES = [
    "카드깡", "카드깡업체", "카드깡수수료", "신용카드현금화",
    "신용카드한도대출", "카드현금화", "신용카드한도현금화",
]
BIO_SUFFIXES = [
    "안전 신속 상담", "최저 수수료 안내", "24시 문의 환영",
    "친절 상담 진행", "빠른 한도 안내", "당일 진행 가능",
]


def make_user_id() -> str:
    """영어 소문자 6개 + 숫자 4개를 무작위로 섞은 ID. (첫 글자는 알파벳 보장)"""
    chars = random.choices(string.ascii_lowercase, k=6) + random.choices(string.digits, k=4)
    random.shuffle(chars)
    if chars[0].isdigit():
        for i, c in enumerate(chars):
            if c.isalpha():
                chars[0], chars[i] = chars[i], chars[0]
                break
    return "".join(chars)


def make_bio(name: str) -> str:
    """프로필 이름을 앞에 넣은 20자 내외의 한 줄 소개."""
    bio = f"{name} {random.choice(BIO_SUFFIXES)}"
    return bio[:20]


# ---------------------------------------------------------------------------
# 메인 엔진
# ---------------------------------------------------------------------------
class VelogPoster:
    def __init__(self, log: LogCallback, on_result=None) -> None:
        self._emit = log
        self._prefix = ""  # 진행 중 계정 번호 표시 (예: "[2/5] ")
        # on_result(velog_id, url): 한 계정 발행이 끝나면 결과 URL 을 알린다.
        self.on_result = on_result
        self._stop = threading.Event()
        self._process: subprocess.Popen | None = None
        self._browser = None
        self._context: BrowserContext | None = None
        self._temp_profile: Path | None = None
        self._endpoint: str | None = None
        self._handoff = False

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
                self._reset_account_state()
                try:
                    self._run_one(pw, chrome, account)
                    completed += 1
                    self.log(f"{label}: 출간 완료 ✅", "success")
                except PostingError as exc:
                    self.log(f"{label}: {exc}", "error")
                except PWTimeoutError:
                    self.log(f"{label}: 화면 요소를 시간 내에 찾지 못했습니다.", "error")
                except Error as exc:
                    if self._stop.is_set():
                        break
                    self.log(f"{label}: 브라우저 오류 - {exc}", "error")
                finally:
                    # 다음 계정을 위해 이 계정의 Chrome 을 완전히 닫고 정리한다.
                    self._teardown_account()

        self._prefix = ""
        if self._stop.is_set():
            self.log("작업을 중단했습니다.", "info")
        else:
            self.log(f"전체 완료: {completed}/{total}개 계정 출간 🎉", "success")

    def _run_one(self, pw, chrome: Path, account: dict[str, str]) -> None:
        """한 계정의 전체 흐름을 수행한다."""
        velog_id = account["velog_id"].strip()
        inbox_url = normalize_url(account["inbox_url"])
        title, body, tags = read_manuscript(account["manuscript_path"])
        summary = extract_summary(body)
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

        self._launch_incognito(pw, chrome)

        page = self._first_page()
        self._inject_stealth(page)

        prev_uuid = self._latest_email_uuid(inbox_url)
        self._request_login(pw, page, velog_id)

        link = self._wait_for_verification(page, inbox_url, prev_uuid)
        self._open_link(pw, page, link)
        page = self._first_page()  # cloudflare 우회로 재연결됐을 수 있음
        is_signup = self._handle_signup_if_needed(pw, page, account)
        if not is_signup:
            self.log("로그인 중.. (기존 계정)", "info")
        # 회원가입 단계에서 재연결했을 수 있으니 연결을 보장한다.
        self._ensure_connected(pw)
        target = self._write_post(self._first_page(), title, body, tags=tags)

        if image_folder:
            image_link = random.choice(homepages) if homepages else ""
            self._insert_random_image(target, image_folder, link_url=image_link)

        url = self._publish(pw, target, tags, summary=summary)
        if url and self.on_result is not None:
            try:
                self.on_result(velog_id, url)
            except Exception:  # noqa: BLE001
                pass

    # -- Chrome 실행 / 연결 -----------------------------------------------
    def _launch_incognito(self, pw, chrome: Path) -> None:
        port = self._free_port()
        # 이미 실행 중인 사용자 Chrome과 충돌하지 않도록 전용 임시 프로필을 쓴다.
        # (전용 user-data-dir 가 없으면 새 chrome.exe 가 기존 인스턴스에 명령만
        #  넘기고 즉시 종료되어 디버깅 포트가 열리지 않는다.)
        self._temp_profile = Path(tempfile.mkdtemp(prefix="velog-chrome-"))
        # 시크릿 모드 + 자동화 배너가 뜨지 않는 안전한 플래그만 사용.
        command = [
            str(chrome),
            "--incognito",
            f"--user-data-dir={self._temp_profile}",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=*",
            "--start-maximized",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            "about:blank",
        ]
        self.log("Chrome 시크릿 창을 여는 중...", "info")
        try:
            self._process = subprocess.Popen(command, close_fds=True)
        except OSError as exc:
            raise PostingError("Chrome 시크릿 창을 실행하지 못했습니다.") from exc

        endpoint = f"http://127.0.0.1:{port}"
        self._endpoint = endpoint  # 출간 단계에서 재연결할 때 사용
        self._wait_for_endpoint(endpoint)
        try:
            self._browser = pw.chromium.connect_over_cdp(endpoint, timeout=20_000)
        except Error as exc:
            raise PostingError("Chrome에 연결하지 못했습니다.") from exc

        if not self._browser.contexts:
            raise PostingError("Chrome 컨텍스트를 찾지 못했습니다.")
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
                raise PostingError("Chrome이 실행 직후 종료되었습니다.")
            try:
                if requests.get(f"{endpoint}/json/version", timeout=0.5).ok:
                    return
            except requests.RequestException:
                pass
            time.sleep(0.1)
        raise PostingError("Chrome 연결 준비 시간이 초과되었습니다.")

    # -- 사람같은 입력 헬퍼 ------------------------------------------------
    def _sleep(self, seconds: float) -> None:
        if self._stop.wait(seconds):
            raise PostingError("사용자가 작업을 중단했습니다.")

    def _wait(self, message: str, seconds: float) -> None:
        self.log(message, "info")
        self._sleep(seconds)

    def _type_like_human(self, page: Page, text: str) -> None:
        """실제 keydown/keyup 이벤트로 한 글자씩 입력한다."""
        page.keyboard.type(text, delay=random.randint(45, 110))

    def _copy_to_clipboard(self, page: Page, text: str) -> None:
        """텍스트를 클립보드에 복사한다. (포커스를 잠깐 가져가므로
        반드시 이 호출 '이후' 에 대상칸을 클릭/포커스해야 한다.)"""
        page.evaluate(
            """(text) => {
                const ta = document.createElement('textarea');
                ta.value = text;
                ta.style.position = 'fixed';
                ta.style.top = '-9999px';
                document.body.appendChild(ta);
                ta.focus();
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
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
                p = self._find_velog_page(browser)
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
                return
        raise PostingError("Cloudflare 인증을 시간 내에 통과하지 못했습니다.")

    # -- 벨로그 로그인 요청 ------------------------------------------------
    def _request_login(self, pw, page: Page, velog_id: str) -> None:
        self.log("벨로그에 접속하는 중...", "info")
        self._goto(page, "https://velog.io/")
        self._wait("화면이 준비되기를 기다리는 중...", _jitter(3))
        self._wait_if_cloudflare(pw, page)
        page = self._first_page()  # 우회로 재연결됐을 수 있어 페이지를 다시 잡는다

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
        self._sleep(_jitter(0.3, 0.2))
        self._type_like_human(page, velog_id)
        self._sleep(_jitter(0.8, 0.4))

        self.log("인증 메일을 요청하는 중...", "info")
        submit = self._visible_last(page.get_by_role("button", name="로그인", exact=True))
        if submit is None:
            raise PostingError("이메일 입력 후 로그인 버튼을 찾지 못했습니다.")
        self._sleep(_jitter(0.4, 0.3))
        submit.click()
        self._wait("인증 메일 발송을 기다리는 중...", _jitter(5))

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
                    "Chrome/137.0.0.0 Safari/537.36"
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
        self._goto(page, link)
        self._wait("로그인 처리가 끝나기를 기다리는 중...", _jitter(8, 2))
        self._wait_if_cloudflare(pw, page)

    # -- 신규 계정 회원가입 ----------------------------------------------
    def _handle_signup_if_needed(self, pw, page: Page, account: dict) -> bool:
        """인증 링크를 열었을 때 '환영합니다' 회원가입 폼이 뜨면 자동으로 가입한다.
        가입을 진행했으면 True, 이미 가입된 계정이면 False 를 반환한다."""
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
            return False  # 이미 가입된 계정

        self.log("회원가입 중.. (신규 계정)", "info")
        names = account.get("profile_names") or DEFAULT_PROFILE_NAMES
        name = random.choice(names)
        user_id = make_user_id()
        bio = make_bio(name)
        self.log(f"프로필 이름: {name} / 사용자 ID: {user_id}", "info")

        # 프로필 이름 (사람처럼 천천히 입력)
        profile_input.first.click()
        self._sleep(_jitter(0.5, 0.4))
        self._type_like_human(page, name)
        self._sleep(_jitter(0.8, 0.5))

        # 사용자 ID
        uid = page.get_by_placeholder("사용자 ID를 입력하세요.")
        uid.first.wait_for(state="visible", timeout=10_000)
        uid.first.click()
        self._sleep(_jitter(0.5, 0.4))
        self._type_like_human(page, user_id)
        self._sleep(_jitter(0.8, 0.5))

        # 한 줄 소개
        bio_box = page.get_by_placeholder("당신을 한 줄로 소개해보세요")
        if bio_box.count() > 0 and bio_box.first.is_visible():
            bio_box.first.click()
            self._sleep(_jitter(0.5, 0.4))
            self._type_like_human(page, bio)
            self._sleep(_jitter(0.8, 0.5))

        # 약관 동의
        self._check_agreement(page)
        self._sleep(_jitter(1.0, 0.6))

        # 가입 (Cloudflare 인증 대비 → 출간과 동일하게 연결 해제·재연결로 클릭)
        self._submit_signup(pw, page, profile_input)
        self.log("회원가입을 완료했습니다.", "success")
        self._sleep(_jitter(2))
        return True

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

    def _ensure_connected(self, pw) -> None:
        """signup 단계에서 연결을 끊었을 수 있으니, 끊겨 있으면 재연결한다."""
        if self._browser is not None and self._context is not None:
            return
        if not self._endpoint:
            return
        browser = pw.chromium.connect_over_cdp(self._endpoint, timeout=20_000)
        self._browser = browser
        self._handoff = False
        if browser.contexts:
            self._context = browser.contexts[0]
            self._context.add_init_script(STEALTH_SCRIPT)

    def _check_agreement(self, page: Page) -> None:
        """이용약관 동의 체크박스를 켠다.

        체크박스는 체크마크 svg(path d^='M20.285 2')를 감싼 div 이고,
        그 div 를 클릭하면 토글된다.
        """
        # 1) 체크마크 svg 를 가진 div 를 직접 클릭
        try:
            box_div = page.locator("div:has(> svg path[d^='M20.285 2'])").first
            if box_div.count() > 0:
                box_div.click(timeout=5_000)
                return
        except Error:
            pass
        # 2) 표준 체크박스 폴백
        try:
            cb = page.locator("input[type='checkbox']")
            if cb.count() > 0:
                cb.first.check(timeout=3_000)
                return
        except Error:
            pass
        # 3) '이용약관' 텍스트 왼쪽 좌표 클릭 폴백
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
    def _write_post(self, page: Page, title: str, body: str, tags: list[str] | None = None) -> Page:
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

        tag_list = [t.strip().lstrip("#").strip() for t in (tags or []) if t.strip()]
        if tag_list:
            self.log(f"원고 해시태그 {len(tag_list)}개 → 태그 입력란에 먼저 등록합니다.", "info")
            if not self._fill_tags(target, tag_list):
                self.log("태그 자동 입력에 실패했습니다. 작성 화면에서 직접 입력해 주세요.", "error")

        # 제목 입력 (붙여넣기) — 포커스 경쟁으로 가끔 실패하므로 검증 후 재시도.
        self.log("제목을 입력하는 중...", "info")
        title_box = target.get_by_placeholder("제목을 입력하세요")
        title_box.wait_for(state="visible", timeout=15_000)
        self._fill_with_retry(
            target,
            label="제목",
            text=title,
            focus=lambda: title_box.click(),
            read_back=lambda: self._read_title(title_box).strip(),
            expected=title.strip(),
        )

        # 본문 입력 (CodeMirror)
        self.log("본문을 입력하는 중...", "info")
        editor = target.locator(".CodeMirror")
        editor.wait_for(state="visible", timeout=15_000)
        self._fill_with_retry(
            target,
            label="본문",
            text=body,
            focus=lambda: target.locator(".CodeMirror-scroll").click(),
            read_back=lambda: editor.evaluate("(el) => el.CodeMirror.getValue()"),
            expected=body,
            select_all=True,
        )

        self.log("제목과 본문이 올바르게 입력되었습니다.", "info")
        target.bring_to_front()
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

    def _fill_with_retry(self, target: Page, label: str, text: str,
                         focus, read_back, expected: str,
                         select_all: bool = False, attempts: int = 3) -> None:
        """클립보드 붙여넣기로 입력하고, 값이 들어갔는지 확인 후 실패 시 재시도."""
        for attempt in range(1, attempts + 1):
            self._copy_to_clipboard(target, text)
            self._sleep(_jitter(0.3, 0.2))
            focus()
            self._sleep(_jitter(0.4, 0.2))
            if select_all:
                target.keyboard.press("Control+a")
                self._sleep(0.2)
            target.keyboard.press("Control+v")
            self._sleep(_jitter(1.5))
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
    def _insert_random_image(self, target: Page, folder: str, link_url: str = "") -> None:
        """폴더 안 이미지 중 하나를 무작위로 골라 본문에 등록한다.
        link_url 이 주어지면 등록된 이미지를 클릭 시 그 주소로 이동하도록 링크를 건다."""
        exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
        images = [
            p for p in Path(folder).iterdir()
            if p.is_file() and p.suffix.lower() in exts
        ]
        if not images:
            self.log("이미지 폴더에 이미지가 없어 등록을 건너뜁니다.", "info")
            return

        chosen = random.choice(images)
        self.log(f"이미지를 등록하는 중: {chosen.name}", "info")

        editor = target.locator(".CodeMirror")
        before = ""
        try:
            before = editor.evaluate("(el) => el.CodeMirror.getValue()")
        except Error:
            pass

        # 본문 맨 위에 커서를 두어 이미지가 원고 제일 상단에 삽입되도록 한다.
        try:
            target.locator(".CodeMirror-scroll").click()
            self._sleep(_jitter(0.4, 0.2))
            target.keyboard.press("Control+Home")
            self._sleep(_jitter(0.3, 0.2))
        except Error:
            pass

        # 에디터 툴바의 이미지 버튼(아이콘 path 로 식별)을 눌러 파일 선택창을 연다.
        image_button = target.locator(
            "button:has(svg path[d^='M21 19V5'])"
        ).first
        try:
            image_button.wait_for(state="visible", timeout=10_000)
        except Error:
            raise PostingError("본문 이미지 등록 버튼을 찾지 못했습니다.")

        try:
            with target.expect_file_chooser(timeout=15_000) as fc:
                image_button.click()
            fc.value.set_files(str(chosen))
        except (Error, PWTimeoutError) as exc:
            raise PostingError("이미지 파일 선택창을 열지 못했습니다.") from exc

        # 업로드 완료(본문에 이미지 markdown 이 추가됨)를 기다린다.
        self.log("이미지 업로드 완료를 기다리는 중...", "info")
        for _ in range(40):  # 최대 약 80초
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
                return
        raise PostingError("이미지 업로드가 시간 내에 끝나지 않았습니다.")

    def _link_image(self, target: Page, editor, link_url: str) -> None:
        """본문 첫 이미지에 홈페이지 링크를 연결한다 (마크다운/HTML)."""
        try:
            wrapped = editor.evaluate(
                """(el, url) => {
                    const cm = el.CodeMirror;
                    let v = cm.getValue();
                    const mdRe = /!\\[[^\\]]*\\]\\([^)]*\\)/;
                    const md = mdRe.exec(v);
                    if (md) {
                        const img = md[0];
                        const idx = md.index;
                        if (idx > 0 && v.charAt(idx - 1) === '[') return true;
                        v = v.slice(0, idx) + '[' + img + '](' + url + ')' + v.slice(idx + img.length);
                    } else {
                        const htmlRe = /<img\\b[^>]*>/i;
                        const hm = htmlRe.exec(v);
                        if (!hm) return false;
                        const tag = hm[0];
                        const idx = hm.index;
                        const before = v.slice(Math.max(0, idx - 80), idx);
                        if (/<a\\b[^>]*>\\s*$/i.test(before)) return true;
                        v = v.slice(0, idx)
                          + '<a href="' + url + '" target="_blank" rel="noopener noreferrer">'
                          + tag + '</a>' + v.slice(idx + tag.length);
                    }
                    // 이미지 링크 감싸기 실패로 남는 고립된 '[' 제거
                    v = v.replace(/^\\[\\s*\\r?\\n(?=!\\[)/, '');
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
                self._copy_to_clipboard(target, keyword)
                tag_input.fill("")
                self._sleep(_jitter(0.2, 0.1))
                target.keyboard.press("Control+v")
                self._sleep(_jitter(0.4, 0.2))
                target.keyboard.press("Enter")
                self._sleep(_jitter(0.55, 0.3))
                added += 1
            except Error:
                self.log(f"태그 입력 실패: {keyword}", "error")
        if added:
            self.log(f"태그 {added}개 입력 완료.", "success")
        return added > 0

    def _fill_publish_summary(self, page: Page, summary: str) -> None:
        """출간 패널 소개란 입력."""
        if not summary:
            return
        self._sleep(_jitter(0.8, 0.4))
        desc = page.get_by_placeholder("당신의 포스트를 짧게 소개해보세요")
        desc.wait_for(state="visible", timeout=12_000)
        desc.click(timeout=5_000)
        self._sleep(_jitter(0.3, 0.2))
        desc.fill("")
        desc.fill(summary)
        self._sleep(_jitter(0.5, 0.3))
        self.log(f"소개 입력: {summary[:40]}{'…' if len(summary) > 40 else ''}", "info")

    # -- 출간 ------------------------------------------------------------
    def _publish(self, pw, target: Page, tags: list[str] | None = None, summary: str = "") -> str | None:
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
        summary_filled = False
        clicked_publish = False
        summary_text = (summary or "").strip()
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
                        if summary_text and not summary_filled:
                            try:
                                self._fill_publish_summary(page, summary_text)
                                summary_filled = True
                            except (Error, PWTimeoutError):
                                self.log(
                                    "소개 자동 입력에 실패했습니다. 출간 패널에서 직접 입력해 주세요.",
                                    "error",
                                )
                        if not final.first.is_disabled() and not clicked_publish:
                            # 인증 통과 상태 → 최종 출간 클릭 후, 연결을 끊고
                            # 깨끗한 상태에서 게시글 주소로 리다이렉트되길 기다린다.
                            self.log("인증 통과 확인 → 자동으로 출간합니다.", "info")
                            self._sleep(_jitter(0.4, 0.3))
                            final.first.click(timeout=15_000)
                            clicked_publish = True
                    # else: 패널은 열렸지만 아직 인증 미통과 → 끊고 대기.
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

    def _wait_published_url(self, pw) -> str | None:
        """출간 클릭 후, 게시글 주소(@아이디/제목)로 리다이렉트될 때까지 기다려
        그 정확한 URL 을 반환한다. (최대 약 100초)"""
        self.log("출간 완료 후 게시글 주소를 확인하는 중...", "info")
        last_url = ""
        for _ in range(50):
            if self._stop.wait(2):
                raise PostingError("사용자가 작업을 중단했습니다.")
            browser = None
            try:
                browser = pw.chromium.connect_over_cdp(self._endpoint, timeout=8_000)
                page = self._find_velog_page(browser)
                if page is not None:
                    last_url = page.url or last_url
                    if self._is_post_url(last_url):
                        self.log(f"출간 완료 🎉 {last_url}", "success")
                        return last_url
            except (Error, PWTimeoutError):
                pass
            finally:
                if browser is not None:
                    try:
                        browser.close()
                    except Error:
                        pass
        # 게시글 주소를 끝내 못 잡으면, 중복 출간 방지를 위해 마지막 URL 이라도 반환.
        self.log("게시글 주소 확인에 실패했습니다. 창에서 직접 확인해 주세요.", "error")
        return last_url or None

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

    @staticmethod
    def _find_velog_page(browser) -> Page | None:
        for ctx in browser.contexts:
            for p in ctx.pages:
                try:
                    if "velog.io" in p.url:
                        return p
                except Error:
                    continue
        for ctx in browser.contexts:
            if ctx.pages:
                return ctx.pages[0]
        return None

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

    def _teardown_account(self) -> None:
        """현재 계정의 Chrome 창/프로세스/임시 프로필을 완전히 정리한다."""
        browser, self._browser = self._browser, None
        process, self._process = self._process, None
        profile, self._temp_profile = self._temp_profile, None
        self._context = None
        self._endpoint = None

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
