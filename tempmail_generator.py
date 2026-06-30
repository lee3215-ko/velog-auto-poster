"""TempMail.co 임시 이메일 자동 생성.

흐름: New Email → 확인 팝업 → Cloudflare 대기 → Save address → Copy Link
"""

from __future__ import annotations

import random
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
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

from velog_poster import (
    PostingError,
    STEALTH_SCRIPT,
    _jitter,
    find_chrome,
    parse_tempmail_address,
)


LogCallback = Callable[[str, str], None]
CreatedCallback = Callable[[str, str], None]  # (email, inbox_url)

TEMPMAIL_HOME = "https://www.tempmail.co/"

# 단계별 최소 대기(초) — 너무 빠르면 생성이 실패할 수 있음
DELAY_BEFORE_NEW = (1.2, 0.8)
DELAY_AFTER_NEW = (4.0, 2.0)
DELAY_INBOX_POLL = (3.0, 1.5)
DELAY_BEFORE_SAVE = (2.5, 1.0)
DELAY_AFTER_SAVE = (2.5, 1.0)
DELAY_AFTER_COPY = (1.5, 0.8)
DELAY_AFTER_CLOSE = (3.0, 1.5)
DELAY_BETWEEN_BATCH = (6.0, 2.0)


class TempMailGenerator:
    """TempMail.co 에서 임시 메일함을 자동으로 만든다."""

    def __init__(self, log: LogCallback, on_created: CreatedCallback | None = None) -> None:
        self._emit = log
        self.on_created = on_created
        self._stop = threading.Event()
        self._process: subprocess.Popen | None = None
        self._browser = None
        self._context: BrowserContext | None = None
        self._temp_profile: Path | None = None
        self._endpoint: str | None = None
        self._handoff = False
        self._last_completed_email = ""

    def log(self, message: str, level: str = "info") -> None:
        self._emit(message, level)

    def stop(self) -> None:
        self._stop.set()

    def run_batch(self, count: int) -> list[tuple[str, str]]:
        """count 개의 임시 메일을 순서대로 생성한다."""
        if count < 1:
            raise PostingError("생성할 개수는 1 이상이어야 합니다.")

        chrome = find_chrome()
        results: list[tuple[str, str]] = []
        self.log(f"Chrome 확인: {chrome.name}", "info")

        with sync_playwright() as pw:
            self._launch(pw, chrome)
            page = self._first_page()
            self._inject_stealth(page)
            self._goto(page, TEMPMAIL_HOME)
            self._human_pause(page, "TempMail 접속 중...", _jitter(3, 1))
            self._wait_if_cloudflare(pw, page)
            page = self._first_page()

            for index in range(1, count + 1):
                if self._stop.is_set():
                    break
                self.log(f"[{index}/{count}] 새 임시 메일 생성 중...", "info")
                try:
                    email, url = self._generate_one(pw)
                    results.append((email, url))
                    self.log(f"[{index}/{count}] 생성 완료: {email}", "success")
                    if self.on_created is not None:
                        self.on_created(email, url)
                except PostingError as exc:
                    self.log(f"[{index}/{count}] {exc}", "error")
                except (Error, PWTimeoutError) as exc:
                    if self._stop.is_set():
                        break
                    self.log(f"[{index}/{count}] 브라우저 오류: {exc}", "error")
                if index < count:
                    self._sleep(_jitter(*DELAY_BETWEEN_BATCH))

            self._teardown()

        if self._stop.is_set():
            self.log("작업을 중단했습니다.", "info")
        elif results:
            self.log(f"총 {len(results)}개 임시 메일 생성 완료.", "success")
        return results

    # -- Chrome ------------------------------------------------------------
    def _launch(self, pw, chrome: Path) -> None:
        port = self._free_port()
        self._temp_profile = Path(tempfile.mkdtemp(prefix="tempmail-chrome-"))
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
            TEMPMAIL_HOME,
        ]
        self.log("Chrome 시크릿 창을 여는 중...", "info")
        try:
            self._process = subprocess.Popen(command, close_fds=True)
        except OSError as exc:
            raise PostingError("Chrome을 실행하지 못했습니다.") from exc

        self._endpoint = f"http://127.0.0.1:{port}"
        self._wait_for_endpoint(self._endpoint)
        try:
            self._browser = pw.chromium.connect_over_cdp(self._endpoint, timeout=20_000)
        except Error as exc:
            raise PostingError("Chrome에 연결하지 못했습니다.") from exc
        if not self._browser.contexts:
            raise PostingError("Chrome 컨텍스트를 찾지 못했습니다.")
        self._context = self._browser.contexts[0]
        self._context.add_init_script(STEALTH_SCRIPT)
        self._context.on("dialog", lambda dialog: dialog.accept())

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

    def _disconnect_only(self) -> None:
        self._handoff = True
        browser, self._browser = self._browser, None
        self._context = None
        if browser is not None:
            try:
                browser.close()
            except Error:
                pass

    def _ensure_connected(self, pw) -> None:
        if self._browser is not None and self._context is not None:
            return
        if not self._endpoint:
            raise PostingError("브라우저 연결이 끊어졌습니다.")
        browser = pw.chromium.connect_over_cdp(self._endpoint, timeout=20_000)
        self._browser = browser
        self._handoff = False
        if browser.contexts:
            self._context = browser.contexts[0]
            self._context.add_init_script(STEALTH_SCRIPT)
            self._context.on("dialog", lambda dialog: dialog.accept())

    def _active_page(self, pw) -> Page:
        """CDP 재연결 후에도 쓸 수 있는 최신 페이지를 반환한다."""
        self._ensure_connected(pw)
        assert self._context is not None
        for p in self._context.pages:
            try:
                if not p.is_closed() and "tempmail.co" in p.url:
                    self._inject_stealth(p)
                    return p
            except Error:
                continue
        for p in self._context.pages:
            try:
                if not p.is_closed():
                    self._inject_stealth(p)
                    return p
            except Error:
                continue
        page = self._context.new_page()
        self._goto(page, TEMPMAIL_HOME)
        self._inject_stealth(page)
        return page

    def _teardown(self) -> None:
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

    # -- 대기 / 사람처럼 행동 ----------------------------------------------
    def _sleep(self, seconds: float) -> None:
        if self._stop.wait(seconds):
            raise PostingError("사용자가 작업을 중단했습니다.")

    def _human_pause(self, page: Page, message: str, seconds: float) -> None:
        self.log(message, "info")
        self._human_wiggle(page)
        self._sleep(seconds)

    @staticmethod
    def _human_wiggle(page: Page) -> None:
        """마우스를 살짝 움직여 사람처럼 보이게 한다."""
        try:
            vp = page.viewport_size or {"width": 1280, "height": 800}
            x = random.randint(80, max(100, vp["width"] - 80))
            y = random.randint(80, max(100, vp["height"] - 80))
            page.mouse.move(x, y, steps=random.randint(8, 18))
            if random.random() < 0.4:
                page.mouse.wheel(0, random.randint(-120, 120))
        except Error:
            pass

    def _goto(self, page: Page, url: str) -> None:
        last: Error | None = None
        for attempt in range(3):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                return
            except Error as exc:
                last = exc
                if attempt < 2:
                    self._sleep(_jitter(3))
        assert last is not None
        raise last

    # -- Cloudflare / 화면 준비 --------------------------------------------
    @staticmethod
    def _is_verify_modal_blocking(page: Page) -> bool:
        """Verify you're human 모달이 화면을 가리고 있는지 (보이는 것만)."""
        for text in ("Verify you're human", "Verify you are human", "사람인지 확인"):
            try:
                loc = page.get_by_text(text, exact=False)
                if loc.count() > 0 and loc.first.is_visible():
                    return True
            except Error:
                pass
        return False

    @staticmethod
    def _is_interstitial(page: Page) -> bool:
        """전체화면 Cloudflare 인터스티셜."""
        try:
            title = (page.title() or "").lower()
            html = page.content().lower()
        except Error:
            return False
        if "just a moment" in title or "잠시만" in title:
            return True
        return "checking your browser" in html and "tempmail" not in html[:500]

    def _is_tempmail_ready(self, page: Page, previous_email: str = "") -> bool:
        """메인 화면(이메일·New Email)이 사용 가능한 상태인지."""
        if self._is_verify_modal_blocking(page):
            return False
        email = self._read_displayed_email(page)
        if not email:
            return False
        if previous_email and email.lower() == previous_email.lower():
            return False
        try:
            btn = page.get_by_role("button", name="New Email")
            if btn.count() == 0 or not btn.first.is_visible():
                btn = page.locator("button:has-text('New Email')")
            if btn.count() == 0 or not btn.first.is_visible():
                return False
        except Error:
            return False
        return True

    def _try_click_turnstile(self, page: Page) -> bool:
        """Turnstile 체크박스를 사람처럼 클릭 시도."""
        clicked = False
        self._human_wiggle(page)

        for frame in page.frames:
            furl = (frame.url or "").lower()
            if "challenges.cloudflare.com" not in furl and "turnstile" not in furl:
                continue
            for sel in (
                "input[type='checkbox']",
                "label.ctp-checkbox-label",
                ".ctp-checkbox-label",
                "#challenge-stage",
                "body",
            ):
                try:
                    loc = frame.locator(sel).first
                    if loc.count() == 0:
                        continue
                    box = loc.bounding_box()
                    if box:
                        cx = box["x"] + box["width"] / 2
                        cy = box["y"] + box["height"] / 2
                        page.mouse.move(cx, cy, steps=random.randint(10, 22))
                        self._sleep(_jitter(0.5, 0.3))
                    loc.click(timeout=5_000, force=True)
                    clicked = True
                    break
                except Error:
                    continue
            if clicked:
                break

        if not clicked:
            try:
                iframe = page.locator(
                    "iframe[src*='challenges.cloudflare'], iframe[src*='turnstile']"
                ).first
                if iframe.count() > 0 and iframe.is_visible():
                    box = iframe.bounding_box()
                    if box:
                        x = box["x"] + min(30, box["width"] * 0.12)
                        y = box["y"] + box["height"] / 2
                        page.mouse.move(x, y, steps=random.randint(12, 20))
                        self._sleep(_jitter(0.5, 0.3))
                        page.mouse.click(x, y)
                        clicked = True
            except Error:
                pass

        if not clicked:
            try:
                loc = page.get_by_text("사람인지 확인", exact=False)
                if loc.count() > 0 and loc.first.is_visible():
                    box = loc.first.bounding_box()
                    if box:
                        page.mouse.click(box["x"] - 22, box["y"] + box["height"] / 2)
                        clicked = True
            except Error:
                pass

        if clicked:
            self.log("Turnstile 체크박스 자동 클릭 시도.", "info")
            self._sleep(_jitter(2.5, 1.0))
        return clicked

    def _wait_if_cloudflare(self, pw, page: Page) -> None:
        if self._is_tempmail_ready(page):
            self.log("TempMail 화면이 이미 준비되어 있습니다.", "info")
            return
        if not self._is_verify_modal_blocking(page) and not self._is_interstitial(page):
            return

        self.log("봇 인증 감지 → 자동 우회를 시도합니다...", "info")
        if not self._endpoint:
            return
        self._disconnect_only()
        turnstile_tried = False

        for attempt in range(60):
            if self._stop.is_set():
                raise PostingError("사용자가 작업을 중단했습니다.")
            self._sleep(_jitter(3, 1.5))

            browser = None
            page = None
            try:
                browser = pw.chromium.connect_over_cdp(self._endpoint, timeout=8_000)
                page = self._find_tempmail_page(browser)

                if page is None:
                    continue

                # 이메일이 이미 생성됐으면 HTML에 turnstile 흔적이 있어도 진행
                if self._is_welcome_verified(page) or self._is_tempmail_ready(page):
                    self.log("이메일 화면 확인 → 계속 진행합니다.", "success")
                    self._ensure_connected(pw)
                    return

                if self._is_verify_modal_blocking(page):
                    if not turnstile_tried or attempt % 4 == 3:
                        self._try_click_turnstile(page)
                        turnstile_tried = True
                elif not self._is_interstitial(page):
                    if self._read_displayed_email(page):
                        self.log("인증 없이 TempMail 준비됨 → 계속 진행.", "success")
                        self._ensure_connected(pw)
                        return
            except (Error, PWTimeoutError):
                pass
            finally:
                if browser is not None:
                    try:
                        browser.close()
                    except Error:
                        pass

        # 마지막으로 한 번 더 — 이미 생성된 경우 놓치지 않도록
        self._ensure_connected(pw)
        page = self._active_page(pw)
        if self._is_welcome_verified(page) or self._is_tempmail_ready(page):
            self.log("대기 종료 후 이메일 화면 확인 → 계속 진행.", "success")
            return
        raise PostingError("봇 인증을 시간 내에 통과하지 못했습니다.")

    def _wait_after_new_email(self, pw, previous_email: str) -> None:
        """New Email 클릭 후 새 주소·Inbox 준비를 기다린다."""
        if not self._endpoint:
            raise PostingError("브라우저 연결이 끊어졌습니다.")

        self.log("새 메일 생성 및 봇 인증 통과를 기다리는 중...", "info")
        self._disconnect_only()
        turnstile_tried = False

        for attempt in range(90):
            if self._stop.is_set():
                raise PostingError("사용자가 작업을 중단했습니다.")
            self._sleep(_jitter(3, 1.5))

            browser = None
            try:
                browser = pw.chromium.connect_over_cdp(self._endpoint, timeout=8_000)
                page = self._find_tempmail_page(browser)
                if page is None:
                    continue

                # 상단·본문 이메일 일치 = 생성 완료
                if self._is_welcome_verified(page, require_new=previous_email):
                    email = self._read_displayed_email(page)
                    self.log(f"새 이메일+환영메일 확인: {email}", "success")
                    self._ensure_connected(pw)
                    return

                if self._is_tempmail_ready(page, previous_email):
                    self.log(f"새 이메일 확인: {self._read_displayed_email(page)}", "success")
                    self._ensure_connected(pw)
                    return

                if self._is_verify_modal_blocking(page):
                    if not turnstile_tried or attempt % 4 == 3:
                        self._try_click_turnstile(page)
                        turnstile_tried = True
            except (Error, PWTimeoutError):
                pass
            finally:
                if browser is not None:
                    try:
                        browser.close()
                    except Error:
                        pass

        # 타임아웃 직전 — 화면에 새 이메일만 있어도 진행
        self._ensure_connected(pw)
        page = self._active_page(pw)
        if self._is_welcome_verified(page, require_new=previous_email):
            self.log(f"대기 종료 후 확인: {self._read_displayed_email(page)}", "success")
            return
        email = self._read_displayed_email(page)
        if email and email.lower() != previous_email.lower() and not self._is_verify_modal_blocking(page):
            self.log(f"대기 종료 후 새 이메일 확인: {email}", "success")
            return
        raise PostingError("새 이메일이 시간 내에 생성되지 않았습니다.")

    @staticmethod
    def _find_tempmail_page(browser) -> Page | None:
        for ctx in browser.contexts:
            for p in ctx.pages:
                try:
                    if "tempmail.co" in p.url:
                        return p
                except Error:
                    continue
        for ctx in browser.contexts:
            if ctx.pages:
                return ctx.pages[0]
        return None

    # -- Inbox / 환영 메일 확인 --------------------------------------------
    _EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}")

    @staticmethod
    def _is_system_email(email: str) -> bool:
        low = email.lower()
        return "noreply" in low or "tempmail.co" in low

    @classmethod
    def _pick_user_email(cls, text: str) -> str:
        for m in cls._EMAIL_RE.finditer(text or ""):
            email = m.group(0)
            if not cls._is_system_email(email):
                return email
        return ""

    def _ensure_welcome_selected(self, page: Page) -> None:
        """Inbox 목록에서 Welcome 메일을 선택해 본문 iframe을 연다."""
        try:
            header = page.locator("h3").filter(has_text=re.compile(r"Welcome to TempMail", re.I))
            if header.count() > 0 and header.first.is_visible():
                return
        except Error:
            pass
        try:
            for text in ("Welcome to TempMail.co!", "Welcome to TempMail"):
                loc = page.get_by_text(text, exact=False)
                for i in range(loc.count()):
                    item = loc.nth(i)
                    try:
                        if item.is_visible():
                            item.click(timeout=5_000)
                            self._sleep(_jitter(1.2, 0.6))
                            return
                    except Error:
                        continue
        except Error:
            pass

    def _read_welcome_body_email(self, page: Page) -> str:
        """환영 메일 본문(iframe)에서 임시 이메일 주소를 읽는다."""
        self._ensure_welcome_selected(page)

        for _ in range(3):
            found = self._extract_email_from_mail_body(page)
            if found:
                return found
            self._sleep(_jitter(0.8, 0.4))

        try:
            body_text = page.inner_text("body")
            m = re.search(
                r"temporary email address[:\s]*([\w.+-]+@[\w.-]+\.[a-zA-Z]{2,})",
                body_text,
                re.IGNORECASE,
            )
            if m and not self._is_system_email(m.group(1)):
                return m.group(1)
            return self._pick_user_email(body_text)
        except Error:
            pass
        return ""

    def _extract_email_from_mail_body(self, page: Page) -> str:
        """메일 본문 영역(iframe 우선)에서 사용자 이메일을 추출."""
        iframe_selectors = (
            "div.flex-1.p-6.overflow-y-auto iframe",
            "div.overflow-y-auto iframe",
            "iframe.w-full",
        )
        for sel in iframe_selectors:
            try:
                iframe_loc = page.locator(sel).first
                if iframe_loc.count() == 0 or not iframe_loc.is_visible():
                    continue
                frame_loc = iframe_loc.content_frame
                body = frame_loc.locator("body")
                for getter in (
                    lambda b=body: b.inner_text(timeout=3_000),
                    lambda b=body: b.inner_html(timeout=3_000),
                ):
                    try:
                        text = getter()
                        found = self._pick_user_email(str(text or ""))
                        if found:
                            return found
                        m = re.search(
                            r"temporary email address[:\s]*([\w.+-]+@[\w.-]+\.[a-zA-Z]{2,})",
                            str(text or ""),
                            re.IGNORECASE,
                        )
                        if m and not self._is_system_email(m.group(1)):
                            return m.group(1)
                    except Error:
                        continue
            except Error:
                continue

        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                text = frame.locator("body").inner_text(timeout=2_000)
                found = self._pick_user_email(text)
                if found:
                    return found
            except Error:
                continue

        try:
            text = page.evaluate(
                """() => {
                    const parts = [];
                    for (const f of document.querySelectorAll('iframe')) {
                        try {
                            const d = f.contentDocument || f.contentWindow?.document;
                            if (d?.body) {
                                parts.push(d.body.innerText || '');
                                parts.push(d.body.innerHTML || '');
                            }
                        } catch (_) {}
                    }
                    return parts.join('\\n');
                }"""
            )
            found = self._pick_user_email(str(text or ""))
            if found:
                return found
        except Error:
            pass
        return ""

    def _is_welcome_verified(self, page: Page, require_new: str = "") -> bool:
        """상단 이메일과 환영 메일 본문 이메일이 일치하면 True.
        require_new: 이 주소와 달라야 할 때( New Email 직후 ) 전달."""
        if self._is_verify_modal_blocking(page):
            return False
        displayed = self._read_displayed_email(page)
        if not displayed:
            return False
        if require_new and displayed.lower() == require_new.lower():
            return False
        if not self._has_welcome_mail(page):
            return False
        body_email = self._read_welcome_body_email(page)
        return bool(body_email and body_email.lower() == displayed.lower())

    @staticmethod
    def _has_welcome_mail(page: Page) -> bool:
        """Inbox에 환영 메일(noreply@tempmail.co)이 보이는지."""
        try:
            for text in ("Welcome to TempMail", "noreply@tempmail.co"):
                loc = page.get_by_text(text, exact=False)
                if loc.count() > 0 and loc.first.is_visible():
                    return True
        except Error:
            pass
        return False

    @staticmethod
    def _inbox_message_count(page: Page) -> int:
        """Inbox 헤더의 'N message(s)' 또는 목록 항목 수."""
        try:
            count = page.evaluate(
                """() => {
                    const t = document.body.innerText || '';
                    const m = t.match(/(\\d+)\\s*messages?/i);
                    if (m) return parseInt(m[1], 10);
                    return 0;
                }"""
            )
            if count and int(count) > 0:
                return int(count)
        except Error:
            pass
        if TempMailGenerator._has_welcome_mail(page):
            return 1
        return 0

    def _wait_for_welcome_match(self, page: Page, require_new: str = "") -> None:
        """상단 이메일 = 환영 메일 본문 이메일 일치할 때까지 대기."""
        self.log("상단 이메일과 환영 메일 본문 일치를 확인하는 중...", "info")
        for attempt in range(40):
            if self._stop.is_set():
                raise PostingError("사용자가 작업을 중단했습니다.")
            self._sleep(_jitter(*DELAY_INBOX_POLL))
            self._human_wiggle(page)

            if self._is_welcome_verified(page, require_new=require_new):
                email = self._read_displayed_email(page)
                self.log(f"이메일 일치 확인: {email} → Save address 진행.", "success")
                self._sleep(_jitter(*DELAY_BEFORE_SAVE))
                return

            displayed = self._read_displayed_email(page)
            body = self._read_welcome_body_email(page)
            if attempt % 4 == 3:
                msg = f"대기 중... 상단={displayed or '?'}"
                if body:
                    msg += f", 본문={body}"
                self.log(msg, "info")

        raise PostingError(
            "환영 메일 본문과 상단 이메일이 일치하지 않습니다. "
            "Inbox에 Welcome 메일이 보이는지 확인해 주세요."
        )

    def _save_and_copy(self, page: Page, current_email: str) -> tuple[str, str]:
        """Save address → Copy Link → URL 반환."""
        save_btn = page.get_by_role("link", name="Save address")
        if save_btn.count() == 0 or not save_btn.first.is_visible():
            save_btn = page.get_by_text("Save address", exact=False)
        if save_btn.count() == 0:
            save_btn = page.locator("button:has-text('Save address')")
        save_btn.first.wait_for(state="visible", timeout=15_000)
        self._sleep(_jitter(1.0, 0.5))
        save_btn.first.click(timeout=15_000)
        self._sleep(_jitter(*DELAY_AFTER_SAVE))

        inbox_url = self._read_saved_link(page)
        if not inbox_url:
            raise PostingError("저장 링크를 찾지 못했습니다.")

        email, _ = parse_tempmail_address(inbox_url)
        if email.lower() != current_email.lower():
            raise PostingError(
                f"복사된 URL({email})이 현재 이메일({current_email})과 다릅니다."
            )

        copy_btn = page.get_by_role("button", name="Copy Link")
        if copy_btn.count() == 0:
            copy_btn = page.locator("button:has-text('Copy Link')")
        if copy_btn.count() > 0 and copy_btn.first.is_visible():
            self._sleep(_jitter(0.8, 0.4))
            copy_btn.first.click(timeout=10_000)
            self._sleep(_jitter(*DELAY_AFTER_COPY))

        self._close_modal(page)
        self._sleep(_jitter(*DELAY_AFTER_CLOSE))
        return email, inbox_url

    # -- 이메일 생성 1회 ---------------------------------------------------
    def _generate_one(self, pw) -> tuple[str, str]:
        page = self._active_page(pw)
        displayed = self._read_displayed_email(page) or ""

        # 이미 생성+환영메일까지 완료 → New Email 누르지 않고 Save address
        if (
            self._is_welcome_verified(page)
            and displayed.lower() != self._last_completed_email.lower()
        ):
            self.log(f"이미 생성된 이메일 확인 → New Email 건너뜀: {displayed}", "success")
            result = self._save_and_copy(page, displayed)
            self._last_completed_email = result[0]
            return result

        # Inbox에 환영 메일이 보이면 New Email 금지 — 본문 일치까지 대기
        if (
            displayed
            and self._has_welcome_mail(page)
            and displayed.lower() != self._last_completed_email.lower()
        ):
            self.log(
                f"Inbox 환영 메일 확인 → New Email 건너뜀, 일치 대기: {displayed}",
                "info",
            )
            self._wait_for_welcome_match(page)
            current = self._read_displayed_email(page) or displayed
            result = self._save_and_copy(page, current)
            self._last_completed_email = result[0]
            return result

        previous = displayed
        self._human_wiggle(page)
        self._sleep(_jitter(*DELAY_BEFORE_NEW))

        new_btn = page.get_by_role("button", name="New Email")
        if new_btn.count() == 0 or not new_btn.first.is_visible():
            new_btn = page.locator("button:has-text('New Email')")
        new_btn.first.wait_for(state="visible", timeout=15_000)
        self._sleep(_jitter(0.8, 0.5))
        new_btn.first.click(timeout=15_000)

        self._wait_after_new_email(pw, previous)
        page = self._active_page(pw)
        self._human_pause(page, "새 이메일 주소 확인 중...", _jitter(*DELAY_AFTER_NEW))

        if self._is_welcome_verified(page, require_new=previous):
            current = self._read_displayed_email(page)
            self.log(f"생성 완료 확인 → Save address 진행: {current}", "success")
            result = self._save_and_copy(page, current)
            self._last_completed_email = result[0]
            return result

        self._wait_for_welcome_match(page, require_new=previous)

        current_email = self._read_displayed_email(page) or ""
        if not current_email:
            raise PostingError("화면에서 새 이메일 주소를 읽지 못했습니다.")

        result = self._save_and_copy(page, current_email)
        self._last_completed_email = result[0]
        return result

    @staticmethod
    def _read_displayed_email(page: Page) -> str:
        """화면 상단에 표시된 현재 임시 이메일 (Inbox 발신자 제외)."""
        try:
            email = page.evaluate(
                """() => {
                    const skip = (e) => {
                        const l = e.toLowerCase();
                        return l.includes('noreply') || l.includes('tempmail.co');
                    };
                    const pick = (text) => {
                        const all = text.match(/[\\w.+-]+@[\\w.-]+\\.[a-zA-Z]{2,}/g) || [];
                        for (const e of all) {
                            if (!skip(e)) return e;
                        }
                        return '';
                    };
                    // Copy 버튼 근처 / 상단 영역 우선
                    const copyBtn = [...document.querySelectorAll('button, a')].find(
                        el => /copy/i.test(el.textContent || '')
                    );
                    if (copyBtn) {
                        let node = copyBtn.parentElement;
                        for (let i = 0; i < 4 && node; i++, node = node.parentElement) {
                            const found = pick(node.innerText || '');
                            if (found) return found;
                        }
                    }
                    return pick(document.body.innerText || '');
                }"""
            )
            if email and "@" in str(email):
                return str(email).strip()
        except Error:
            pass
        return ""

    @staticmethod
    def _read_saved_link(page: Page) -> str:
        """Save address 모달 안의 메일함 URL."""
        selectors = (
            "input[value*='tempmail.co/address/']",
            "input[value*='tempmail.co']",
            "textarea",
        )
        for sel in selectors:
            try:
                loc = page.locator(sel)
                for i in range(loc.count()):
                    val = loc.nth(i).input_value()
                    if val and "tempmail.co/address/" in val:
                        return normalize_tempmail_url(val.strip())
            except Error:
                continue

        try:
            html = page.content()
            m = re.search(r"https?://(?:www\.)?tempmail\.co/address/[^\s\"'<>]+", html)
            if m:
                return normalize_tempmail_url(unquote(m.group(0)))
        except Error:
            pass
        return ""

    @staticmethod
    def _close_modal(page: Page) -> None:
        for name in ("Close", "닫기"):
            try:
                btn = page.get_by_role("button", name=name)
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click(timeout=3_000)
                    return
            except Error:
                pass
        try:
            page.keyboard.press("Escape")
        except Error:
            pass


def normalize_tempmail_url(value: str) -> str:
    value = value.strip()
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    if "tempmail.co" not in parsed.netloc.lower():
        raise PostingError("TempMail 주소가 아닙니다.")
    return value
