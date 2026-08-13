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
    _human_delay,
    _jitter,
    find_chrome,
    parse_tempmail_address,
)


LogCallback = Callable[[str, str], None]
CreatedCallback = Callable[[str, str], None]  # (email, inbox_url)

TEMPMAIL_HOME = "https://www.tempmail.co/"

# 단계별 기본 대기(초) — 실제 대기는 _random_delay 로 흔들린다
DELAY_BEFORE_NEW = (2.0, 1.5)
DELAY_AFTER_NEW = (5.5, 2.5)
DELAY_INBOX_POLL = (3.5, 2.0)
DELAY_BEFORE_SAVE = (3.5, 1.8)
DELAY_AFTER_SAVE = (3.0, 1.5)
DELAY_AFTER_COPY = (2.0, 1.0)
DELAY_AFTER_CLOSE = (4.0, 2.0)
DELAY_BETWEEN_BATCH = (14.0, 8.0)


def _random_delay(base: float, spread: float, *, lo_scale: float = 0.7, hi_scale: float = 2.6) -> float:
    """같은 패턴이 반복되지 않도록 넓게 흔든 대기 시간."""
    value = _jitter(base, spread) * random.uniform(lo_scale, hi_scale)
    return max(0.6, value)


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

    def run_batch(self, count: int, *, loop_until_stop: bool = False) -> list[tuple[str, str]]:
        """임시 메일을 생성한다. loop_until_stop=True 이면 중단할 때까지 반복."""
        if not loop_until_stop and count < 1:
            raise PostingError("생성할 개수는 1 이상이어야 합니다.")

        chrome = find_chrome()
        results: list[tuple[str, str]] = []
        self.log(f"Chrome 확인: {chrome.name}", "info")

        with sync_playwright() as pw:
            self._launch(pw, chrome)
            page = self._first_page()
            self._inject_stealth(page)
            self._goto(page, TEMPMAIL_HOME)
            self._human_pause(page, "TempMail 접속 중...", _random_delay(3, 1))
            self._wait_if_cloudflare(pw, page)
            page = self._first_page()

            index = 0
            while True:
                if self._stop.is_set():
                    break
                index += 1
                if not loop_until_stop and index > count:
                    break
                label = f"#{index}" if loop_until_stop else f"[{index}/{count}]"
                self.log(f"{label} 새 임시 메일 생성 중...", "info")
                try:
                    email, url = self._generate_one(pw)
                    results.append((email, url))
                    self.log(f"{label} 생성 완료: {email}", "success")
                    if self.on_created is not None:
                        self.on_created(email, url)
                except PostingError as exc:
                    self.log(f"{label} {exc}", "error")
                except (Error, PWTimeoutError) as exc:
                    if self._stop.is_set():
                        break
                    self.log(f"{label} 브라우저 오류: {exc}", "error")

                if self._stop.is_set():
                    break
                if not loop_until_stop and index >= count:
                    break
                wait_s = _human_delay(12.0, 48.0)
                self.log(f"다음 생성까지 {wait_s:.1f}초 대기...", "info")
                self._sleep(wait_s)

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
            f"--window-size=1500,1000",
            f"--window-position={random.randint(20, 80)},{random.randint(20, 60)}",
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
        del page
        self.log(message, "info")
        self._sleep(seconds)

    def _click_like_human(self, page: Page, locator, *, timeout: int = 15_000) -> None:
        """스크롤 후 Playwright 클릭."""
        del page
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
        try:
            target.click(timeout=timeout)
        except Error:
            target.click(timeout=timeout, force=True)

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

    def _cf_page_ready(self, page: Page, previous_email: str = "") -> bool:
        """캡차가 지나갔고 메일 화면을 쓸 수 있는지. CDP로 체크박스에 손대지 않는다."""
        if self._is_verify_modal_blocking(page) or self._is_interstitial(page):
            return False
        if self._is_welcome_verified(page, require_new=previous_email):
            return True
        if self._is_tempmail_ready(page, previous_email):
            return True
        return bool(self._read_displayed_email(page))

    def _wait_if_cloudflare(self, pw, page: Page) -> None:
        """벨로그와 동일: 연결을 끊고 캡차가 스스로 통과되길 기다린다."""
        if self._is_tempmail_ready(page):
            self.log("TempMail 화면이 이미 준비되어 있습니다.", "info")
            return
        if not self._is_verify_modal_blocking(page) and not self._is_interstitial(page):
            return

        self.log("Cloudflare 감지 → 연결을 끊어 깨끗한 상태에서 통과를 기다립니다...", "info")
        if not self._endpoint:
            return
        self._disconnect_only()

        for _ in range(60):
            if self._stop.is_set():
                raise PostingError("사용자가 작업을 중단했습니다.")
            self._sleep(4)

            browser = None
            passed = False
            try:
                browser = pw.chromium.connect_over_cdp(self._endpoint, timeout=8_000)
                found = self._find_tempmail_page(browser)
                if found is not None and self._cf_page_ready(found):
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
                self.log("Cloudflare 통과 확인 → 작업을 계속합니다.", "success")
                self._ensure_connected(pw)
                return

        self._ensure_connected(pw)
        page = self._active_page(pw)
        if self._cf_page_ready(page):
            self.log("대기 종료 후 이메일 화면 확인 → 계속 진행.", "success")
            return
        raise PostingError("봇 인증을 시간 내에 통과하지 못했습니다.")

    def _wait_after_new_email(self, pw, previous_email: str) -> None:
        """New Email 클릭 직후 연결을 끊고, 새 주소·캡차 통과만 감시한다."""
        if not self._endpoint:
            raise PostingError("브라우저 연결이 끊어졌습니다.")

        self.log("새 메일 생성 및 Cloudflare 통과를 기다리는 중...", "info")
        self._disconnect_only()

        for _ in range(90):
            if self._stop.is_set():
                raise PostingError("사용자가 작업을 중단했습니다.")
            self._sleep(4)

            browser = None
            passed = False
            label = ""
            try:
                browser = pw.chromium.connect_over_cdp(self._endpoint, timeout=8_000)
                page = self._find_tempmail_page(browser)
                if page is not None and self._cf_page_ready(page, previous_email):
                    passed = True
                    if self._is_welcome_verified(page, require_new=previous_email):
                        label = f"새 이메일+환영메일 확인: {self._read_displayed_email(page)}"
                    else:
                        label = f"새 이메일 확인: {self._read_displayed_email(page)}"
            except (Error, PWTimeoutError):
                pass
            finally:
                if browser is not None:
                    try:
                        browser.close()
                    except Error:
                        pass
            if passed:
                self.log(label or "새 이메일 확인", "success")
                self._ensure_connected(pw)
                return

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
                            self._click_like_human(page, item, timeout=5_000)
                            self._sleep(_human_delay(1.0, 2.2))
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
        """상단 이메일 = 환영 메일 본문 이메일 일치할 때까지 대기.
        불일치가 계속되면 30초마다 페이지를 새로고침한다."""
        self.log("상단 이메일과 환영 메일 본문 일치를 확인하는 중...", "info")
        last_refresh = time.monotonic()
        for attempt in range(40):
            if self._stop.is_set():
                raise PostingError("사용자가 작업을 중단했습니다.")
            self._sleep(_random_delay(*DELAY_INBOX_POLL))

            if self._is_welcome_verified(page, require_new=require_new):
                email = self._read_displayed_email(page)
                self.log(f"이메일 일치 확인: {email} → Save address 진행.", "success")
                self._sleep(_random_delay(*DELAY_BEFORE_SAVE))
                return

            displayed = self._read_displayed_email(page)
            body = self._read_welcome_body_email(page)
            if attempt % 4 == 3:
                msg = f"대기 중... 상단={displayed or '?'}"
                if body:
                    msg += f", 본문={body}"
                self.log(msg, "info")

            if time.monotonic() - last_refresh >= 30:
                self.log("일치 대기 중 — 페이지를 새로고침합니다...", "info")
                try:
                    page.reload(wait_until="domcontentloaded", timeout=30_000)
                    self._sleep(_jitter(2.5, 1.0))
                except Error as exc:
                    self.log(f"새로고침 실패(계속 대기): {exc}", "info")
                last_refresh = time.monotonic()

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
        self._sleep(_human_delay(0.8, 2.2))
        self._click_like_human(page, save_btn.first)
        self._sleep(_random_delay(*DELAY_AFTER_SAVE))

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
            self._sleep(_human_delay(0.6, 1.6))
            self._click_like_human(page, copy_btn.first, timeout=10_000)
            self._sleep(_random_delay(*DELAY_AFTER_COPY))

        self._close_modal(page)
        self._sleep(_random_delay(*DELAY_AFTER_CLOSE))
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
        self._sleep(_random_delay(*DELAY_BEFORE_NEW))

        new_btn = page.get_by_role("button", name="New Email")
        if new_btn.count() == 0 or not new_btn.first.is_visible():
            new_btn = page.locator("button:has-text('New Email')")
        new_btn.first.wait_for(state="visible", timeout=15_000)
        self._sleep(_human_delay(0.6, 1.8))
        self._click_like_human(page, new_btn.first)

        self._wait_after_new_email(pw, previous)
        page = self._active_page(pw)
        self._human_pause(page, "새 이메일 주소 확인 중...", _random_delay(*DELAY_AFTER_NEW))

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
