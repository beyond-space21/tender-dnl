"""Download tender packets, solving captchas with local OCR or 2Captcha."""

import base64
import hashlib
import json
import os
import re
import sys
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

LIST_URL = (
    "https://tntenders.gov.in/nicgep/app"
    "?page=FrontEndListTendersbyDate&service=page"
)
TENDER_ID = re.compile(r"\d{4}_[A-Za-z0-9]+_\d+_\d+")
ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT
TEMP_DIR = ROOT / "temp"
TENDERS_DIR = ROOT / "TENDERS"
CAPTCHA_PATH = OUT_DIR / "current_captcha.png"
MAX_CAPTCHA_ATTEMPTS = 10
LOCAL_CAPTCHA_ATTEMPTS = 20
IST = ZoneInfo("Asia/Kolkata")
_LOCAL_OCR = None
_TWOCAPTCHA = None


class CaptchaRejected(Exception):
    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message


class SavedFile(Exception):
    def __init__(self, path: Path) -> None:
        super().__init__(str(path))
        self.path = path


class JobStopped(Exception):
    """Raised when the dashboard Stop button interrupts a long-running scrape step."""


def captcha_src(page) -> str:
    image = page.locator("#captchaImage")
    image.wait_for(state="attached", timeout=30_000)
    return image.get_attribute("src") or ""


def captcha_bytes(page) -> bytes:
    image = page.locator("#captchaImage")
    image.wait_for(state="visible", timeout=30_000)
    return image.screenshot()


def save_captcha(data: bytes, path: Path = CAPTCHA_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".png.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def load_local_env() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.is_file():
        return
    # .env wins over stale process env for captcha settings (pm2 often pins old values).
    override = {
        "CAPTCHA_PROVIDER",
        "CAPTCHA_ATTEMPTS",
        "TWOCAPTCHA_APIKEY",
        "APIKEY_2CAPTCHA",
        "TWO_CAPTCHA_API_KEY",
        "PLAYWRIGHT_BROWSERS_PATH",
        "MAX_PARALLEL",
        "MAX_PARALLEL_MIN",
        "DOWNLOAD_ADAPTIVE",
        "DOWNLOAD_BACKOFF_BASE",
        "DOWNLOAD_BACKOFF_CAP",
        "DOWNLOAD_GAP_MS",
    }
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        key = name.strip()
        cleaned = value.strip().strip("'\"")
        if key in override:
            if cleaned:
                os.environ[key] = cleaned
        else:
            os.environ.setdefault(key, cleaned)


load_local_env()


def captcha_provider() -> str:
    """Always use 2Captcha for download captchas."""
    load_local_env()
    return "2captcha"


def captcha_attempts(provider: str | None = None) -> int:
    load_local_env()
    mode = provider or captcha_provider()
    override = os.environ.get("CAPTCHA_ATTEMPTS", "").strip()
    if override.isdigit() and int(override) > 0:
        return int(override)
    if mode == "local":
        return LOCAL_CAPTCHA_ATTEMPTS
    return MAX_CAPTCHA_ATTEMPTS


def twocaptcha_api_key() -> str:
    load_local_env()
    apikey = (
        os.environ.get("TWOCAPTCHA_APIKEY", "").strip()
        or os.environ.get("APIKEY_2CAPTCHA", "").strip()
        or os.environ.get("TWO_CAPTCHA_API_KEY", "").strip()
    )
    if apikey:
        return apikey
    raise RuntimeError("Add TWOCAPTCHA_APIKEY to .env.")


def captcha_credentials(provider: str | None = None) -> tuple[str, str]:
    """Return (unused_userid, apikey) for the active captcha provider."""
    mode = provider or captcha_provider()
    if mode == "2captcha":
        return "", twocaptcha_api_key()
    return "", ""


def twocaptcha_client(apikey: str = ""):
    global _TWOCAPTCHA
    key = apikey or twocaptcha_api_key()
    if _TWOCAPTCHA is None or getattr(_TWOCAPTCHA, "API_KEY", None) != key:
        from twocaptcha import TwoCaptcha

        _TWOCAPTCHA = TwoCaptcha(
            key,
            defaultTimeout=120,
            pollingInterval=5,
        )
    return _TWOCAPTCHA


def local_ocr(beta: bool = False):
    global _LOCAL_OCR
    key = "beta" if beta else "default"
    if _LOCAL_OCR is None:
        _LOCAL_OCR = {}
    if key not in _LOCAL_OCR:
        import ddddocr

        _LOCAL_OCR[key] = ddddocr.DdddOcr(show_ad=False, beta=beta)
    return _LOCAL_OCR[key]


def _normalize_answer(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(raw or ""))


def _denoise_black_ink(image: bytes) -> bytes | None:
    """Keep near-black ink and drop coloured salt-and-pepper speckles."""
    try:
        import cv2
        import numpy as np

        arr = np.frombuffer(image, dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None:
            return None
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        # Characters are dark; blue/grey speckles have higher value or chroma.
        mask = cv2.inRange(hsv, (0, 0, 0), (180, 80, 110))
        mask = cv2.medianBlur(mask, 3)
        out = np.full_like(bgr, 255)
        out[mask > 0] = (0, 0, 0)
        out = cv2.resize(out, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)
        ok, encoded = cv2.imencode(".png", out)
        return encoded.tobytes() if ok else None
    except Exception:
        return None


def _case_variants(answer: str) -> list[str]:
    """Expand ambiguous letter cases without flooding the submit loop."""
    swaps = {
        "c": "C",
        "C": "c",
        "o": "O",
        "O": "o",
        "p": "P",
        "P": "p",
        "s": "S",
        "S": "s",
        "u": "U",
        "U": "u",
        "v": "V",
        "V": "v",
        "w": "W",
        "W": "w",
        "x": "X",
        "X": "x",
        "z": "Z",
        "Z": "z",
        "i": "I",
        "I": "i",
        "l": "L",
        "L": "l",
        "b": "B",
        "B": "b",
        "d": "D",
        "D": "d",
    }
    seen = [answer]
    # Single-position flips first (most likely case errors).
    for index, char in enumerate(answer):
        other = swaps.get(char)
        if not other:
            continue
        flipped = answer[:index] + other + answer[index + 1 :]
        if flipped not in seen:
            seen.append(flipped)
    lower = answer.lower()
    upper_first = answer[:1].upper() + answer[1:].lower() if answer else answer
    for extra in (lower, upper_first, answer.upper()):
        if extra and extra not in seen:
            seen.append(extra)
    return seen


def _read_one(image: bytes, beta: bool) -> tuple[str, float]:
    engine = local_ocr(beta=beta)
    try:
        result = engine.classification(image, probability=True)
    except TypeError:
        result = engine.classification(image)
    if isinstance(result, dict):
        text = _normalize_answer(result.get("text") or "")
        confidence = float(result.get("confidence") or 0.0)
        return text, confidence
    return _normalize_answer(result), 0.0


def _ocr_candidates(image: bytes) -> list[str]:
    scored: dict[str, float] = {}

    def add(text: str, score: float) -> None:
        if not (4 <= len(text) <= 8):
            return
        # Prefer exact 6-char reads; TN captchas are almost always length 6.
        score = score + (0.15 if len(text) == 6 else 0.0)
        scored[text] = max(scored.get(text, 0.0), score)

    # Raw screenshot first — beta model is strongest on this site.
    for beta, base in ((True, 1.0), (False, 0.75)):
        text, confidence = _read_one(image, beta=beta)
        add(text, base + confidence)

    cleaned = _denoise_black_ink(image)
    if cleaned:
        for beta, base in ((True, 0.55), (False, 0.4)):
            text, confidence = _read_one(cleaned, beta=beta)
            add(text, base + confidence)

    if not scored:
        return []

    ordered = sorted(
        scored.items(),
        key=lambda item: (-item[1], 0 if len(item[0]) == 6 else 1, item[0]),
    )
    candidates: list[str] = []
    for text, _ in ordered:
        for variant in _case_variants(text):
            if variant not in candidates:
                candidates.append(variant)
    return candidates


def solve_captcha_local(image: bytes, tried: set[str] | None = None) -> str:
    candidates = _ocr_candidates(image)
    if tried is not None:
        candidates = [item for item in candidates if item not in tried]
    if not candidates:
        raise RuntimeError("Local OCR returned no text.")
    answer = candidates[0]
    import db

    db.record_captcha_image()
    return answer


def solve_captcha_2captcha(image: bytes, apikey: str = "") -> str:
    from twocaptcha import ApiException, NetworkException, TimeoutException

    body = base64.b64encode(image).decode("ascii")
    try:
        result = twocaptcha_client(apikey).normal(
            body,
            caseSensitive=1,
            minLen=5,
            maxLen=8,
            phrase=0,
        )
    except TimeoutException as exc:
        raise RuntimeError(f"2Captcha timed out: {exc}") from exc
    except NetworkException as exc:
        raise RuntimeError(f"2Captcha network error: {exc}") from exc
    except ApiException as exc:
        raise RuntimeError(f"2Captcha API error: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"2Captcha failed: {exc}") from exc

    answer = str((result or {}).get("code") or "").strip().replace(" ", "")
    if not answer:
        raise RuntimeError(f"2Captcha returned no text: {result}")
    import db

    db.record_captcha_image()
    return answer


def solve_captcha(
    image: bytes,
    userid: str = "",
    apikey: str = "",
    provider: str | None = None,
    tried: set[str] | None = None,
) -> str:
    mode = provider or captcha_provider()
    if mode == "2captcha":
        return solve_captcha_2captcha(image, apikey=apikey)
    return solve_captcha_local(image, tried=tried)


def wait_for_changed_src(page, previous: str, timeout: int) -> bool:
    try:
        page.wait_for_function(
            """(previous) => {
                const image = document.querySelector('#captchaImage');
                return image && image.getAttribute('src') && image.getAttribute('src') !== previous;
            }""",
            arg=previous,
            timeout=timeout,
        )
    except PlaywrightTimeoutError:
        return False
    return True


def wait_for_new_captcha(page, previous: str) -> str:
    if wait_for_changed_src(page, previous, 5_000):
        return captcha_src(page)

    refresh = page.locator("#captcha")
    if refresh.count():
        refresh.click()
        if wait_for_changed_src(page, previous, 20_000):
            return captcha_src(page)

    zip_link = page.get_by_role("link", name="Download as zip file")
    if zip_link.count():
        try:
            zip_link.click(timeout=10_000)
            page.locator("#captchaImage").wait_for(state="attached", timeout=30_000)
        except (PlaywrightError, PlaywrightTimeoutError):
            pass
        else:
            return captcha_src(page)

    raise PlaywrightTimeoutError("The captcha image did not refresh.")


def filename_from_disposition(header: str) -> str:
    match = re.search(
        r"filename\*=UTF-8''([^;]+)|filename=\"?([^\";]+)\"?",
        header,
        re.I,
    )
    if not match:
        return "tender.zip"
    name = unquote((match.group(1) or match.group(2)).strip())
    return Path(name).name or "tender.zip"


def notice_from_html(html: str) -> str:
    match = re.search(r"Invalid[^<]{0,120}", html)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(0)).strip()


def is_captcha_post(response) -> bool:
    return (
        response.request.method == "POST"
        and response.url.split("?", 1)[0].rstrip("/").endswith("/nicgep/app")
    )


def save_response_file(response) -> Path:
    header = response.headers.get("content-disposition") or ""
    name = filename_from_disposition(header)
    data = response.body()
    if data[:2] == b"PK" and not name.lower().endswith(".zip"):
        name = "tender.zip"
    target = OUT_DIR / name
    target.write_bytes(data)
    return target


def open_first_tender(page) -> tuple[str, str]:
    page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_function(
        "() => window.tapestry && window.tapestry.form",
        timeout=60_000,
    )
    with page.expect_navigation(timeout=60_000):
        page.evaluate("() => document.querySelector('#LinkSubmit_1').click()")
    page.locator("td.pageheader", has_text="Closing within 14 Days").wait_for(
        timeout=60_000
    )

    first = page.locator("#table tr.even, #table tr.odd").first
    tender_id = TENDER_ID.search(first.inner_text())
    title_link = first.locator('a[title="View Tender Information"]')
    title = title_link.inner_text().strip()
    with page.expect_navigation(timeout=60_000):
        title_link.evaluate("el => el.click()")
    return (tender_id.group(0) if tender_id else ""), title


def _persist_download(download, dest: Path | None) -> Path:
    failure = download.failure()
    if failure:
        raise RuntimeError(f"Download failed: {failure}")
    target = dest or (TEMP_DIR / (download.suggested_filename or "tender.zip"))
    target.parent.mkdir(parents=True, exist_ok=True)
    download.save_as(target)
    return target


def _zip_download_link(page):
    direct = page.locator("a#DirectLink_8")
    if direct.count():
        return direct.first
    link = page.get_by_role("link", name="Download as zip file")
    if link.count():
        return link.first
    return page.locator("a", has_text=re.compile(r"Download as zip file", re.I)).first


def _click_zip_for_file_or_captcha(page, dest: Path | None) -> Path | None:
    """Click the zip link. Return a saved path if the file arrives; None if captcha is shown."""
    zip_link = _zip_download_link(page)
    zip_link.wait_for(timeout=60_000)
    captured: list = []
    start_url = page.url

    def on_download(download) -> None:
        captured.append(download)

    page.on("download", on_download)
    try:
        zip_link.click()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if captured:
                return _persist_download(captured[0], dest)
            try:
                if page.locator("#captchaText").count():
                    return None
            except PlaywrightError:
                if captured:
                    return _persist_download(captured[0], dest)
                raise

            navigated = page.url != start_url
            zip_again = _zip_download_link(page)
            # After navigation, a warm session often shows the zip link with no captcha.
            if navigated and zip_again.count() and not page.locator("#captchaText").count():
                page.wait_for_timeout(500)
                if captured:
                    return _persist_download(captured[0], dest)
                if page.locator("#captchaText").count():
                    return None
                try:
                    with page.expect_download(timeout=60_000) as info:
                        zip_again.click()
                    return _persist_download(info.value, dest)
                except PlaywrightTimeoutError:
                    if page.locator("#captchaText").count():
                        return None
                    raise
            page.wait_for_timeout(100)

        if captured:
            return _persist_download(captured[0], dest)
        if page.locator("#captchaText").count():
            return None
        raise PlaywrightTimeoutError("Neither captcha nor download appeared.")
    finally:
        page.remove_listener("download", on_download)


def download_packet(
    page,
    userid: str = "",
    apikey: str = "",
    dest: Path | None = None,
    captcha_path: Path = CAPTCHA_PATH,
    provider: str | None = None,
    timings: dict | None = None,
) -> Path:
    mode = provider or captcha_provider()
    max_attempts = captcha_attempts(mode)
    label = "2Captcha" if mode == "2captcha" else "Local OCR"
    started = time.monotonic()

    def _finish(path: Path, *, captcha: bool) -> Path:
        if timings is not None:
            zip_mb = round(path.stat().st_size / (1024 * 1024), 3) if path.is_file() else 0
            timings["zip"] = {
                "ms": int(round((time.monotonic() - started) * 1000)),
                "captcha": captcha,
                "zip_mb": zip_mb,
            }
        return path

    direct = _click_zip_for_file_or_captcha(page, dest)
    if direct is not None:
        print("Download reused the open session (no captcha).", flush=True)
        return _finish(direct, captcha=False)

    page.locator("#captchaText").wait_for(timeout=60_000)
    shown = ""
    needs_new_image = False
    attempt = 0
    weak_skips = 0
    tried_answers: set[str] = set()
    # For local OCR, try a few ranked guesses on the same image before refresh.
    max_guesses_per_image = 4 if mode == "local" else 1

    while attempt < max_attempts:
        try:
            if needs_new_image:
                shown = wait_for_new_captcha(page, shown)
                needs_new_image = False
                tried_answers.clear()
            else:
                shown = captcha_src(page)
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            print(f"The captcha image did not refresh: {exc}", flush=True)
            return _finish(Path(), captcha=True)
        image = captcha_bytes(page)
        save_captcha(image, captcha_path)
        print(f"Captcha image: {captcha_path}", flush=True)

        guesses_left = max_guesses_per_image
        accepted = False
        notice = ""
        while guesses_left > 0 and attempt < max_attempts:
            guesses_left -= 1
            try:
                answer = solve_captcha(
                    image, userid, apikey, provider=mode, tried=tried_answers
                )
            except RuntimeError as exc:
                print(f"{label}: {exc}. Trying a new image.", flush=True)
                needs_new_image = True
                weak_skips += 1
                break

            if mode == "local" and len(answer) < 5:
                print(f"{label}: weak read {answer!r}, refreshing image.", flush=True)
                needs_new_image = True
                weak_skips += 1
                break

            tried_answers.add(answer)
            attempt += 1
            print(f"{label} ({attempt}/{max_attempts}): {answer}", flush=True)

            page.locator("#captchaText").fill(answer)
            try:
                with page.expect_response(is_captcha_post, timeout=60_000) as response_info:
                    page.locator("#Submit").click()
                response = response_info.value
                notice = notice_from_html(response.body().decode("utf-8", "replace"))
                page.wait_for_function(
                    """() => document.getElementById('captchaImage')
                        || [...document.querySelectorAll('a')].some(link =>
                            (link.textContent || '').includes('Download as zip file'))""",
                    timeout=30_000,
                )
            except (PlaywrightTimeoutError, PlaywrightError) as exc:
                print(f"The submit did not finish: {exc}", flush=True)
                needs_new_image = True
                break

            if page.locator("#captchaImage").count():
                current = captcha_src(page)
                # Site sometimes keeps the same image; try the next OCR guess.
                if (
                    mode == "local"
                    and guesses_left > 0
                    and current
                    and current == shown
                ):
                    print(
                        f"{notice or 'Rejected.'} Trying another OCR guess.",
                        flush=True,
                    )
                    continue
                print(
                    f"{notice or 'The site rejected that captcha.'} Trying a new image.",
                    flush=True,
                )
                needs_new_image = True
                break

            accepted = True
            break

        if weak_skips > max_attempts:
            break
        if needs_new_image or not accepted:
            if not accepted and not needs_new_image:
                needs_new_image = True
            continue

        zip_again = _zip_download_link(page)
        if not zip_again.count():
            print(f"{notice or 'The download page did not come back.'} Trying a new image.", flush=True)
            needs_new_image = True
            continue

        try:
            with page.expect_download(timeout=60_000) as download_info:
                zip_again.click()
            return _finish(_persist_download(download_info.value, dest), captcha=True)
        except PlaywrightTimeoutError:
            if page.locator("#captchaImage").count():
                print("The site asked for the captcha again.", flush=True)
                needs_new_image = False
                shown = ""
                continue
            print("The zip file did not start.", flush=True)
            return _finish(Path(), captcha=True)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return _finish(Path(), captcha=True)

    print(f"{label} did not produce an accepted captcha.", file=sys.stderr)
    return _finish(Path(), captcha=True)


ROW_JS = r"""() => [...document.querySelectorAll('#table tr.even, #table tr.odd')].map(tr => {
  const cells = [...tr.cells].map(td => td.innerText.replace(/\s+/g, ' ').trim());
  const link = tr.querySelector('a[title="View Tender Information"]');
  return {cells, title: link ? link.innerText.trim() : ''};
})"""


def parse_when(text: str) -> datetime | None:
    cleaned = " ".join(text.split())
    if not cleaned or cleaned.upper() == "NA":
        return None
    try:
        return datetime.strptime(cleaned, "%d-%b-%Y %I:%M %p").replace(tzinfo=IST)
    except ValueError:
        return None


def open_closing_list(page) -> None:
    page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_function(
        "() => window.tapestry && window.tapestry.form",
        timeout=60_000,
    )
    with page.expect_navigation(timeout=60_000):
        page.evaluate("() => document.querySelector('#LinkSubmit_1').click()")
    page.locator("td.pageheader", has_text="Closing within 14 Days").wait_for(
        timeout=60_000
    )
    page.locator("#table tr.even, #table tr.odd").first.wait_for(timeout=60_000)


def list_rows(page) -> list[dict]:
    raw = page.evaluate(ROW_JS)
    rows = []
    for item in raw:
        cells = item["cells"]
        if len(cells) < 6:
            continue
        match = TENDER_ID.search(" ".join(cells))
        if not match:
            continue
        rows.append(
            {
                "tender_id": match.group(0),
                "title": item["title"] or cells[4],
                "organisation": cells[5],
                "published_at": parse_when(cells[1]),
                "closing_at": parse_when(cells[2]),
                "opening_at": parse_when(cells[3]),
            }
        )
    return rows


def pager_state(page) -> dict:
    return page.evaluate(
        """() => {
            const root = document.querySelector("span[id^='informal']");
            if (!root) return {current: 1, last: 1, numbers: []};
            const pageOf = (el) => {
                if (!el) return null;
                const values = new URL(el.href, location.origin).searchParams.getAll('sp');
                const number = Number(values[values.length - 1]);
                return Number.isFinite(number) ? number : null;
            };
            const bold = [...root.querySelectorAll('b')]
                .map((node) => node.textContent.trim())
                .find((text) => /^\\d+$/.test(text));
            const numbers = [...root.querySelectorAll('a')]
                .map((node) => ({id: node.id, page: pageOf(node)}))
                .filter((item) => item.id && item.page);
            const last = numbers.find((item) => item.id === 'linkLast');
            return {
                current: bold ? Number(bold) : 1,
                last: last ? last.page : (bold ? Number(bold) : 1),
                numbers,
            };
        }"""
    )


def click_page_number(page, number: int) -> bool:
    target_id = page.evaluate(
        """(number) => {
            const root = document.querySelector("span[id^='informal']");
            if (!root) return null;
            const match = [...root.querySelectorAll('a')].find((node) => {
                const values = new URL(node.href, location.origin).searchParams.getAll('sp');
                return Number(values[values.length - 1]) === number;
            });
            return match ? match.id : null;
        }""",
        number,
    )
    if not target_id:
        return False
    with page.expect_navigation(timeout=60_000):
        page.locator(f"[id='{target_id}']").click()
    page.locator("#table tr.even, #table tr.odd").first.wait_for(timeout=60_000)
    return True


# Tapestry pager pages can be fetched directly by number inside the live session:
# one GET returns the full list-page HTML (rows + detail links), no navigation needed.
LIST_FETCH_JS = r"""async (args) => {
  const {numbers, needle} = args;
  const base = '/nicgep/app?component=%24TablePages.linkPage'
    + '&page=FrontEndListTendersbyDate&service=direct&session=T'
    + '&sp=AFrontEndListTendersbyDate%2Ctable&sp=';
  const out = [];
  for (const n of numbers) {
    const item = {page: n, rows: [], last: null, current: null, error: null};
    try {
      const res = await fetch(base + n, {credentials: 'include'});
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const html = await res.text();
      const doc = new DOMParser().parseFromString(html, 'text/html');
      for (const tr of doc.querySelectorAll('#table tr.even, #table tr.odd')) {
        const cells = [...tr.querySelectorAll('td')]
          .map(td => (td.textContent || '').replace(/\s+/g, ' ').trim());
        const link = tr.querySelector('a[title="View Tender Information"]');
        const m = (tr.textContent || '').match(/\d{4}_[A-Za-z0-9]+_\d+_\d+/);
        item.rows.push({
          id: m ? m[0] : null,
          href: link ? link.getAttribute('href') : null,
          title: link ? (link.textContent || '').replace(/\s+/g, ' ').trim() : '',
          cells,
        });
      }
      const pager = doc.querySelector("span[id^='informal']");
      if (pager) {
        const bold = [...pager.querySelectorAll('b')]
          .map(b => (b.textContent || '').trim())
          .find(t => /^\d+$/.test(t));
        if (bold) item.current = Number(bold);
        const lastLink = pager.querySelector('a#linkLast');
        if (lastLink) {
          const sp = new URL(lastLink.getAttribute('href'), location.origin)
            .searchParams.getAll('sp');
          const v = Number(sp[sp.length - 1]);
          if (Number.isFinite(v)) item.last = v;
        } else if (bold) {
          item.last = Number(bold);
        }
      }
    } catch (e) {
      item.error = String(e);
    }
    out.push(item);
    if (needle && item.rows.some(r => r.id === needle)) break;
  }
  return out;
}"""


def _fetch_list_pages(page, numbers: list[int], needle: str | None = None) -> list[dict]:
    """Fetch list pages by number inside the open session (fast, no navigation)."""
    result = page.evaluate(LIST_FETCH_JS, {"numbers": numbers, "needle": needle})
    return result if isinstance(result, list) else []


def _open_detail_href(page, tender_id: str, href: str) -> None:
    """Open a tender detail page straight from its list-row link href."""
    url = "https://tntenders.gov.in" + href if href.startswith("/") else href
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_function(
        """() => document.querySelector('td.pageheader')
            || [...document.querySelectorAll('a')].some(link =>
                (link.textContent || '').includes('Download as zip file'))""",
        timeout=30_000,
    )


def goto_list_page(page, number: int) -> None:
    if number <= 1:
        return
    for _ in range(number + 5):
        state = pager_state(page)
        if state["current"] == number:
            return
        if click_page_number(page, number):
            continue
        pages = [item["page"] for item in state["numbers"] if item["id"].startswith("linkPage")]
        if not pages:
            break
        if number > state["current"]:
            hop = max(page_no for page_no in pages if page_no < number)
        else:
            lower = [page_no for page_no in pages if page_no > number]
            hop = min(lower) if lower else min(pages)
        if not click_page_number(page, hop):
            break
    if pager_state(page)["current"] != number:
        raise RuntimeError(f"List page {number} was not found.")


def _row_from_fetch(raw: dict) -> dict | None:
    cells = raw.get("cells") or []
    if len(cells) < 6:
        return None
    match = TENDER_ID.search(" ".join(cells))
    if not match:
        return None
    return {
        "tender_id": match.group(0),
        "title": raw.get("title") or cells[4],
        "organisation": cells[5],
        "published_at": parse_when(cells[1]),
        "closing_at": parse_when(cells[2]),
        "opening_at": parse_when(cells[3]),
    }


def _list_env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _list_env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def scrape_catalog(page, stop_event=None, progress=None) -> dict:
    """Walk the closing list and upsert rows.

    Incremental mode (default when the catalog is non-empty): stop after
    LIST_STABLE_PAGES consecutive pages with no DB-new IDs, then probe the
    tail. If the tail has unknowns, resume through the gap. Force a full
    sweep with LIST_FULL=1, empty catalog, page-count growth, or when the
    last full sweep is older than LIST_FULL_EVERY_HOURS.
    """
    import db

    open_closing_list(page)
    known = db.known_tender_ids()
    prev = db.last_list_sweep() or {}
    seen: set[str] = set()
    last = max(1, int(pager_state(page)["last"] or 1))
    print(f"Closing list has {last} pages.", flush=True)

    stable_need = max(1, _list_env_int("LIST_STABLE_PAGES", 3))
    tail_pages = max(0, _list_env_int("LIST_TAIL_PAGES", 5))
    full_every_h = max(0, _list_env_int("LIST_FULL_EVERY_HOURS", 24))
    force_full = _list_env_flag("LIST_FULL", False) or not _list_env_flag(
        "LIST_INCREMENTAL", True
    )
    if not known:
        force_full = True
        print("Listing: empty catalog — full sweep.", flush=True)
    prev_pages = int(prev.get("last_pages") or 0)
    if prev_pages and last > prev_pages + 2:
        force_full = True
        print(
            f"Listing: page count grew {prev_pages} → {last} — full sweep.",
            flush=True,
        )
    if full_every_h and not force_full:
        stamp = prev.get("last_full_at") or prev.get("finished_at")
        if stamp:
            try:
                finished = datetime.fromisoformat(str(stamp))
                if finished.tzinfo is None:
                    finished = finished.replace(tzinfo=IST)
                age_h = (
                    datetime.now(IST) - finished.astimezone(IST)
                ).total_seconds() / 3600
                if age_h >= full_every_h:
                    force_full = True
                    print(
                        f"Listing: last full sweep {age_h:.0f}h ago — full sweep.",
                        flush=True,
                    )
            except (TypeError, ValueError):
                pass

    # Always reach at least where we last saw a brand-new tender.
    min_before_stop = 0
    if not force_full:
        min_before_stop = max(0, int(prev.get("deepest_new_page") or 0))

    scanned_pages: set[int] = set()
    dup_rows = 0
    raw_rows_total = 0
    new_count = 0
    deepest_new_page: int | None = None
    stable_streak = 0
    early_stop = False
    stopped_at: int | None = None

    def ingest(item: dict) -> tuple[int, int]:
        """Upsert one fetched page. Returns (row_count, db_new_count)."""
        nonlocal dup_rows, raw_rows_total, new_count, deepest_new_page, last
        if item.get("last"):
            last = max(1, int(item["last"]))
        current = int(item.get("current") or item["page"])
        scanned_pages.add(current)
        added = 0
        rows = 0
        for raw in item["rows"]:
            raw_rows_total += 1
            row = _row_from_fetch(raw)
            if row is None:
                continue
            rows += 1
            tid = row["tender_id"]
            if tid in seen:
                dup_rows += 1
                continue
            seen.add(tid)
            row["list_page"] = current
            is_new = tid not in known
            db.upsert_seen(row)
            if is_new:
                known.add(tid)
                added += 1
                new_count += 1
                deepest_new_page = current if deepest_new_page is None else max(
                    deepest_new_page, current
                )
        print(
            f"page {current}/{last}: {rows} tenders, {added} new-to-db",
            flush=True,
        )
        if progress:
            progress(current, last, len(seen))
        return rows, added

    def fetch_batch(numbers: list[int]) -> list[dict]:
        wanted = [n for n in numbers if n not in scanned_pages and 1 <= n <= last]
        if not wanted:
            return []
        items = _fetch_list_pages(page, wanted)
        if not items or all(not item["rows"] for item in items):
            open_closing_list(page)
            items = _fetch_list_pages(page, wanted)
            if not items or all(not item["rows"] for item in items):
                raise RuntimeError(
                    f"Could not fetch list pages {wanted[0]}-{wanted[-1]}."
                )
        return items

    # --- forward scan ---
    page_no = 1
    while page_no <= last:
        if stop_event is not None and stop_event.is_set():
            break
        batch = list(range(page_no, min(last, page_no + LOOKUP_BATCH - 1) + 1))
        items = fetch_batch(batch)
        batch_new = 0
        batch_rows = 0
        for item in items:
            rows, added = ingest(item)
            batch_rows += rows
            batch_new += added
            if stop_event is not None and stop_event.is_set():
                break
        page_no = batch[-1] + 1

        if force_full or (stop_event is not None and stop_event.is_set()):
            continue
        # Only count toward early-stop once we are past the adaptive floor.
        if batch[-1] < min_before_stop:
            stable_streak = 0
            continue
        if batch_rows > 0 and batch_new == 0:
            stable_streak += max(1, len(items))
        else:
            stable_streak = 0
        if stable_streak >= stable_need:
            early_stop = True
            stopped_at = batch[-1]
            print(
                f"Listing: early stop after {stable_streak} stable pages "
                f"at page {stopped_at}/{last} ({new_count} new so far).",
                flush=True,
            )
            break

    # --- tail probe (catch late-closing newcomers the front pass may skip) ---
    if (
        early_stop
        and tail_pages > 0
        and (stop_event is None or not stop_event.is_set())
    ):
        tail_start = max(1, last - tail_pages + 1)
        if stopped_at is not None and tail_start > stopped_at + 1:
            print(
                f"Listing: probing tail pages {tail_start}-{last}.",
                flush=True,
            )
            tail_new = 0
            for start in range(tail_start, last + 1, LOOKUP_BATCH):
                if stop_event is not None and stop_event.is_set():
                    break
                items = fetch_batch(list(range(start, min(last, start + LOOKUP_BATCH - 1) + 1)))
                for item in items:
                    _, added = ingest(item)
                    tail_new += added
            if tail_new > 0 and (stop_event is None or not stop_event.is_set()):
                # Gap between front stop and tail may hide more newcomers.
                gap_lo = (stopped_at or 0) + 1
                gap_hi = tail_start - 1
                if gap_lo <= gap_hi:
                    print(
                        f"Listing: tail found {tail_new} new — "
                        f"filling gap {gap_lo}-{gap_hi}.",
                        flush=True,
                    )
                    for start in range(gap_lo, gap_hi + 1, LOOKUP_BATCH):
                        if stop_event is not None and stop_event.is_set():
                            break
                        items = fetch_batch(
                            list(range(start, min(gap_hi, start + LOOKUP_BATCH - 1) + 1))
                        )
                        for item in items:
                            ingest(item)
                early_stop = False  # effectively covered the catalog
                force_full = True

    pages_scanned = len(scanned_pages)
    stats = {
        "unique": len(seen),
        "new": new_count,
        "pages": last,
        "pages_scanned": pages_scanned,
        "rows": raw_rows_total,
        "duplicates": dup_rows,
        "early_stop": early_stop,
        "stopped_at": stopped_at,
        "deepest_new_page": deepest_new_page,
        "full_sweep": (not early_stop) or force_full,
    }
    try:
        db.save_list_sweep(stats)
    except Exception as exc:
        print(f"Listing: could not save sweep meta: {exc}", flush=True)
    mode = "full" if stats["full_sweep"] else f"incremental(stop@{stopped_at})"
    print(
        f"Listing complete ({mode}): {stats['new']} new / {stats['unique']} unique "
        f"from {stats['rows']} row slots; scanned {pages_scanned}/{last} pages "
        f"({stats['duplicates']} duplicate IDs on the site list).",
        flush=True,
    )
    return stats


def _table_has_tender(page, tender_id: str) -> bool:
    try:
        return tender_id in page.inner_text("#table")
    except PlaywrightError:
        return False


def _click_tender_row(page, tender_id: str) -> bool:
    row = page.locator("#table tr.even, #table tr.odd").filter(has_text=tender_id)
    if row.count() == 0:
        return False
    link = row.first.locator('a[title="View Tender Information"]')
    if link.count() == 0:
        return False
    with page.expect_navigation(timeout=60_000):
        link.evaluate("el => el.click()")
    return True


def _goto_list_page_resilient(page, number: int) -> int:
    """Navigate to a list page; reopen the catalog once if the pager hop fails."""
    if number <= 1:
        state = pager_state(page)
        if state["current"] != 1:
            open_closing_list(page)
        return pager_state(page)["current"]
    try:
        goto_list_page(page, number)
    except RuntimeError:
        open_closing_list(page)
        goto_list_page(page, number)
    return pager_state(page)["current"]


def _search_page_order(start: int, last: int, forward: int, backward: int) -> list[int]:
    """Prefer scanning forward: new sooner-closing tenders push rows to higher pages."""
    start = max(1, min(start, last))
    order: list[int] = []
    seen: set[int] = set()

    def add(page_no: int) -> None:
        if 1 <= page_no <= last and page_no not in seen:
            seen.add(page_no)
            order.append(page_no)

    for page_no in range(start, min(last, start + forward) + 1):
        add(page_no)
    low = max(1, start - backward)
    for page_no in range(start - 1, low - 1, -1):
        add(page_no)
    # Late fallback: tenders often sit near the current end after heavy re-pagination.
    fallback = max(0, int(os.environ.get("LIST_SEARCH_FALLBACK", "100")))
    for page_no in range(last, max(0, last - fallback), -1):
        add(page_no)
    return order


def _open_listed_tender_slow(
    page,
    tender_id: str,
    list_page: int,
    stop_event: threading.Event | None = None,
    timings: dict | None = None,
) -> int:
    """Legacy fallback: walk the pager with real navigations (slow but robust)."""
    if stop_event is not None and stop_event.is_set():
        raise JobStopped(f"Stopped while looking up {tender_id}.")
    started = time.monotonic()
    open_closing_list(page)
    last = max(1, int(pager_state(page)["last"] or 1))
    start = max(1, min(int(list_page or 1), last))
    forward = max(6, int(os.environ.get("LIST_SEARCH_FORWARD", "120")))
    backward = max(0, int(os.environ.get("LIST_SEARCH_BACK", "25")))
    order = _search_page_order(start, last, forward, backward)
    print(
        f"lookup {tender_id}: slow walk, start page {start}/{last}, "
        f"search {len(order)} pages (fwd={forward}, back={backward})",
        flush=True,
    )
    walked = 0

    def record(hit: bool, found_page: int | None = None) -> None:
        if timings is None:
            return
        timings["lookup"] = {
            "fetches": walked,
            "ms": int(round((time.monotonic() - started) * 1000)),
            "hit": hit,
            "mode": "slow",
            "anchor_page": start,
            "found_page": found_page,
        }

    for page_no in order:
        if stop_event is not None and stop_event.is_set():
            raise JobStopped(f"Stopped while looking up {tender_id}.")
        try:
            actual = _goto_list_page_resilient(page, page_no)
            walked += 1
        except RuntimeError as exc:
            print(f"lookup {tender_id}: skip page {page_no} ({exc})", flush=True)
            continue
        if not _table_has_tender(page, tender_id):
            continue
        if _click_tender_row(page, tender_id):
            print(f"opened {tender_id} from list page {actual}", flush=True)
            record(True, actual)
            return actual
        raise RuntimeError(
            f"{tender_id} was on list page {actual} but the detail link did not open."
        )

    record(False)
    raise RuntimeError(f"{tender_id} is no longer on the closing list.")


LOOKUP_BATCH = 10


def _item_closing_range(item: dict) -> tuple[datetime | None, datetime | None]:
    """Min and max closing date of the rows on one fetched list page."""
    dates = []
    for row in item.get("rows") or []:
        cells = row.get("cells") or []
        if len(cells) > 2:
            when = parse_when(cells[2])
            if when:
                dates.append(when)
    if not dates:
        return None, None
    return min(dates), max(dates)


def _ring_order(center: int, last: int) -> list[int]:
    """Pages ordered by distance from center (center first)."""
    order = [center]
    step = 1
    while center - step >= 1 or center + step <= last:
        if center + step <= last:
            order.append(center + step)
        if center - step >= 1:
            order.append(center - step)
        step += 1
    return order


def open_listed_tender(
    page,
    tender_id: str,
    list_page: int,
    stop_event: threading.Event | None = None,
    closing_at: datetime | None = None,
    timings: dict | None = None,
    lookup_cache: dict | None = None,
) -> int:
    """Open a tender from the live closing list. Returns the page where it was found.

    Fast path: fetch list pages by number inside the session (one GET per page,
    no navigation). The list is sorted by closing date, so when the tender's
    closing date is known we binary-search for the right region, then scan
    outward. Finally jump straight to the row's detail link.
    """

    def check_stop() -> None:
        if stop_event is not None and stop_event.is_set():
            raise JobStopped(f"Stopped while looking up {tender_id}.")

    check_stop()
    started = time.monotonic()
    start = max(1, int(list_page or 1))
    state = {"fetched": 0, "last": start, "anchor": start}
    cache: dict[int, dict] = {}
    shared = lookup_cache if isinstance(lookup_cache, dict) else None

    def _record_ranges(items: list[dict]) -> None:
        """Persist page->closing-span in the worker-level cache for later lookups."""
        if shared is None:
            return
        ranges = shared.setdefault("ranges", {})
        for item in items:
            page_no = item.get("current") or item.get("page")
            if page_no is None:
                continue
            lo, hi = _item_closing_range(item)
            if lo is not None or hi is not None:
                ranges[int(page_no)] = (lo, hi)
            if item.get("last"):
                shared["last"] = int(item["last"])

    def fetch(numbers: list[int]) -> list[dict]:
        wanted = [n for n in numbers if n not in cache]
        if not wanted:
            return []
        items = _fetch_list_pages(page, wanted, needle=tender_id)
        state["fetched"] += len(items)
        for item in items:
            if item.get("last"):
                state["last"] = int(item["last"])
            cache[int(item.get("current") or item["page"])] = item
        _record_ranges(items)
        return items

    def find_href(items: list[dict]) -> tuple[int, str] | None:
        for item in items:
            for row in item.get("rows") or []:
                if row.get("id") == tender_id and row.get("href"):
                    return int(item.get("current") or item["page"]), row["href"]
        return None

    def record_lookup(hit: bool, found_page: int | None = None, window=None) -> None:
        if timings is None:
            return
        timings["lookup"] = {
            "fetches": state["fetched"],
            "ms": int(round((time.monotonic() - started) * 1000)),
            "hit": hit,
            "mode": "fast",
            "anchor_page": state["anchor"],
            "found_page": found_page,
            "window": list(window) if window else None,
        }

    def finish(hit: tuple[int, str]) -> int:
        found_page, href = hit
        _open_detail_href(page, tender_id, href)
        elapsed = time.monotonic() - started
        print(
            f"opened {tender_id} from list page {found_page} "
            f"({state['fetched']} fetches, {elapsed:.1f}s)",
            flush=True,
        )
        record_lookup(True, found_page)
        return found_page

    # Probe the stored page; an empty result means the session has no list state yet.
    probe = fetch([start])
    if not probe or not probe[0]["rows"]:
        open_closing_list(page)
        cache.clear()
        probe = fetch([start])
        if not probe or not probe[0]["rows"]:
            print(f"lookup {tender_id}: fetch scan unavailable, falling back", flush=True)
            return _open_listed_tender_slow(
                page,
                tender_id,
                list_page,
                stop_event=stop_event,
                timings=timings,
            )

    hit = find_href(probe)
    if hit is not None:
        return finish(hit)

    last = state["last"]
    start = max(1, min(start, last))
    anchor = start

    # The list is sorted by closing date. Prefer the worker-level cache: pages
    # whose closing-date span contains our date are the anchor band — probe a
    # few of them directly and skip the binary search entirely.
    if closing_at is not None:
        ranges = (shared or {}).get("ranges") or {}
        band = sorted(
            page_no
            for page_no, (lo, hi) in ranges.items()
            if lo is not None and hi is not None and lo <= closing_at <= hi
        )
        if band:
            anchor = min(band, key=lambda page_no: abs(page_no - start))
            probe_pages = [page_no for page_no in band if page_no not in cache]
            probe_pages.sort(key=lambda page_no: abs(page_no - anchor))
            if probe_pages:
                hit = find_href(fetch(probe_pages[:4]))
                if hit is not None:
                    return finish(hit)
        else:
            low, high = 1, last
            while low < high:
                check_stop()
                mid = (low + high) // 2
                items = fetch([mid])
                hit = find_href(items)
                if hit is not None:
                    return finish(hit)
                item = cache.get(mid)
                _, max_close = _item_closing_range(item) if item else (None, None)
                if max_close is None:
                    break
                if max_close < closing_at:
                    low = mid + 1
                else:
                    high = mid
            else:
                anchor = low
        state["anchor"] = anchor
        print(
            f"lookup {tender_id}: closing-date anchor page {anchor}/{last} "
            f"(stored {start})",
            flush=True,
        )

    # The catalog is sorted by closing date, so a miss near the anchor means
    # the tender left the list. Do not walk all ~400 pages.
    radius = max(4, int(os.environ.get("LOOKUP_RADIUS", "10")))
    miss_cap = max(4, _env_int("LOOKUP_MISS_MAX_FETCHES", 12))
    lo = max(1, anchor - radius)
    hi = min(last, anchor + radius)
    order = [
        n
        for n in _ring_order(anchor, last)
        if lo <= n <= hi and n not in cache
    ]
    outside = 0
    for index in range(0, len(order), LOOKUP_BATCH):
        check_stop()
        if closing_at is not None and state["fetched"] >= miss_cap:
            break
        items = fetch(order[index : index + LOOKUP_BATCH])
        hit = find_href(items)
        if hit is not None:
            return finish(hit)
        if closing_at is not None:
            for item in items:
                span = _item_closing_range(item)
                if span[0] is None or span[1] is None:
                    continue
                if span[0] <= closing_at <= span[1]:
                    outside = 0
                else:
                    outside += 1
            # Left the closing-date band on several pages: it is not listed.
            if outside >= 4:
                break

    elapsed = time.monotonic() - started
    print(
        f"lookup {tender_id}: not on closing list "
        f"(anchor {anchor}, window {lo}-{hi}, {state['fetched']} fetches, {elapsed:.1f}s)",
        flush=True,
    )
    record_lookup(False, window=(lo, hi))
    raise RuntimeError(f"{tender_id} is no longer on the closing list.")


DETAIL_PARSE_JS = r"""() => {
  // GePNIC detail pages label every field: td.td_caption -> td.td_field.
  const pairs = {};
  for (const tr of document.querySelectorAll('tr')) {
    const cells = [...tr.children].filter(el => el.tagName === 'TD');
    for (let i = 0; i < cells.length - 1; i++) {
      if (!cells[i].classList.contains('td_caption')) continue;
      const label = (cells[i].textContent || '').replace(/\s+/g, ' ').trim();
      if (!label) continue;
      let j = i + 1;
      while (j < cells.length && !cells[j].classList.contains('td_field')) j++;
      if (j >= cells.length) continue;
      const value = (cells[j].textContent || '').replace(/\s+/g, ' ').trim();
      if (!(label in pairs)) pairs[label] = value;
      i = j;
    }
  }
  // Documents tables: NIT (no type column) and Work Item Documents (typed).
  const documents = [];
  for (const table of document.querySelectorAll('table')) {
    const headCells = [...(table.querySelector('tr') || {children: []}).children]
      .map(td => (td.textContent || '').replace(/\s+/g, ' ').trim());
    const hasName = headCells.includes('Document Name');
    const hasSize = headCells.some(h => /Document Size/i.test(h));
    if (!hasName || !hasSize) continue;
    const typed = headCells.includes('Document Type');
    const rows = [...table.querySelectorAll('tr')].slice(1);
    for (const tr of rows) {
      const cells = [...tr.children].map(td => (td.textContent || '').replace(/\s+/g, ' ').trim());
      if (cells.length < 4) continue;
      if (typed && cells.length >= 5) {
        documents.push({
          category: 'work_item', doc_type: cells[1], name: cells[2],
          description: cells[3], size_kb: cells[4],
        });
      } else if (!typed) {
        documents.push({
          category: 'nit', doc_type: 'Tender Notice', name: cells[1],
          description: cells[2], size_kb: cells[3],
        });
      }
    }
  }
  return {pairs, documents};
}"""

DETAIL_FIELD_MAP = {
    "organisation_chain": "Organisation Chain",
    "tender_reference": "Tender Reference Number",
    "tender_type": "Tender Type",
    "form_of_contract": "Form Of Contract",
    "tender_category": "Tender Category",
    "no_of_covers": "No. of Covers",
    "payment_mode": "Payment Mode",
    "withdrawal_allowed": "Withdrawal Allowed",
    "tender_fee_inr": "Tender Fee in ₹",
    "fee_exemption_allowed": "Tender Fee Exemption Allowed",
    "emd_amount_inr": "EMD Amount in ₹",
    "emd_fee_type": "EMD Fee Type",
    "emd_percentage": "EMD Percentage",
    "emd_exemption_allowed": "EMD Exemption Allowed",
    "title": "Title",
    "work_description": "Work Description",
    "pre_qualification": "NDA/Pre Qualification",
    "tender_value_inr": "Tender Value in ₹",
    "product_category": "Product Category",
    "sub_category": "Sub category",
    "contract_type": "Contract Type",
    "bid_validity_days": "Bid Validity(Days)",
    "period_of_work_days": "Period Of Work(Days)",
    "location": "Location",
    "pincode": "Pincode",
    "pre_bid_meeting_place": "Pre Bid Meeting Place",
    "pre_bid_meeting_address": "Pre Bid Meeting Address",
    "pre_bid_meeting_date": "Pre Bid Meeting Date",
    "bid_opening_place": "Bid Opening Place",
    "tenderer_class": "Tenderer Class",
    "published_date": "Published Date",
    "bid_opening_date": "Bid Opening Date",
    "doc_download_start": "Document Download / Sale Start Date",
    "doc_download_end": "Document Download / Sale End Date",
    "clarification_start": "Clarification Start Date",
    "clarification_end": "Clarification End Date",
    "bid_submission_start": "Bid Submission Start Date",
    "bid_submission_end": "Bid Submission End Date",
    "inviting_authority_name": "Name",
    "inviting_authority_address": "Address",
}


def parse_tender_detail(page) -> dict:
    """Extract structured fields and document list from the open tender detail page."""
    raw = page.evaluate(DETAIL_PARSE_JS)
    pairs = raw.get("pairs") or {}
    details: dict = {}
    for key, label in DETAIL_FIELD_MAP.items():
        value = (pairs.get(label) or "").strip()
        details[key] = value if value and value.upper() != "NA" else None
    details["documents"] = raw.get("documents") or []
    return details


CORRIGENDA_LIST_JS = r"""() => {
  const table =
    document.querySelector('#corrigendumDocumenttable') ||
    (() => {
      const header = [...document.querySelectorAll('td.pageheader')]
        .find(h => /Latest Corrigendum List/i.test(h.innerText || ''));
      if (!header) return null;
      let node = header.closest('tr');
      for (let i = 0; i < 12; i++) {
        node = node?.nextElementSibling;
        if (!node) break;
        const t = node.querySelector('table');
        if (t && /Corrigendum Title|S\.No/i.test(t.innerText || '')) return t;
      }
      return null;
    })();
  if (!table) return [];
  const items = [];
  for (const tr of table.querySelectorAll('tr.even, tr.odd')) {
    const cells = [...tr.cells].map(td => td.innerText.replace(/\s+/g, ' ').trim());
    if (!cells.length) continue;
    const view = tr.querySelector(
      'a[title="View Corrigendum History"], a[title="View"], a[title*="Corrigendum" i]'
    ) || tr.querySelector('a');
    items.push({
      serial: cells[0] || '',
      title: cells[1] || '',
      corr_type: cells[2] || '',
      view_href: view ? (view.getAttribute('href') || '') : '',
      view_title: view ? (view.getAttribute('title') || '') : '',
      view_link_id: view ? (view.id || '') : '',
    });
  }
  return items;
}"""

CORRIGENDA_HISTORY_JS = r"""() => {
  // History popup repeats Critical Dates: first block = revised/current, second = original.
  const DATE_RE = /^(Publish Date|Document Download\s*\/\s*Sale Start Date|Document Download\s*\/\s*Sale End Date|Clarification Start Date|Clarification End Date|Bid Submission Start Date|Bid Submission End Date|Bid Opening Date)$/i;
  const canon = (label) => {
    const n = (label || '').replace(/\s+/g, ' ').trim();
    return n.replace(/\s*\/\s*/g, '/');
  };
  const normalize = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const pairs = [];
  for (const tr of document.querySelectorAll('tr')) {
    const cells = [...tr.children].filter(el => el.tagName === 'TD');
    for (let i = 0; i < cells.length - 1; i++) {
      const label = normalize(cells[i].textContent);
      if (!DATE_RE.test(label)) continue;
      let j = i + 1;
      while (j < cells.length && !cells[j].classList.contains('td_field') && cells.length > 2) j++;
      if (j >= cells.length) j = i + 1;
      const value = normalize(cells[j].textContent);
      if (value) pairs.push([canon(label), value]);
      i = j;
    }
  }
  const revised = {};
  const original = {};
  const seen = {};
  for (const [label, value] of pairs) {
    const key = label;
    if (!(key in seen)) {
      revised[key] = value;
      seen[key] = 1;
    } else if (seen[key] === 1) {
      original[key] = value;
      seen[key] = 2;
    }
  }
  // Document Name lives in the history table only. Never take NIT/BOQ links
  // from FrontEndTenderDetails (those match /.pdf$/ on the detail page).
  const docTable = [...document.querySelectorAll('table')].find(t => {
    const head = normalize(t.innerText).slice(0, 400);
    return /Corrigendum Title/i.test(head) && /Document Name/i.test(head);
  });
  let pdf = null;
  let description = null;
  let published = null;
  let doc_size = null;
  if (docTable) {
    const dataRow = [...docTable.querySelectorAll('tr')].find(tr =>
      tr.querySelector('a') || (
        tr.cells && tr.cells.length >= 4 &&
        !/Corrigendum Title/i.test(tr.innerText || '')
      )
    );
    if (dataRow && dataRow.cells) {
      description = normalize(dataRow.cells[1] && dataRow.cells[1].innerText);
      published = normalize(dataRow.cells[2] && dataRow.cells[2].innerText);
      doc_size = normalize(dataRow.cells[4] && dataRow.cells[4].innerText);
      pdf = dataRow.querySelector('a');
    }
  }
  const headers = [...document.querySelectorAll('td.pageheader, .pageheader')]
    .map(el => normalize(el.innerText)).filter(Boolean);
  return {
    headers,
    revised,
    original,
    description: description || null,
    published: published || null,
    doc_size: doc_size || null,
    is_history: Boolean(docTable) || /CorrViewDetailsPrint/i.test(location.href),
    pdf_name: pdf ? normalize(pdf.innerText) : null,
    pdf_href: pdf ? (pdf.getAttribute('href') || '') : null,
    pdf_id: pdf ? (pdf.id || '') : null,
  };
}"""


def list_corrigenda(page) -> list[dict]:
    """Read Latest Corrigendum List rows from the tender detail page."""
    raw = page.evaluate(CORRIGENDA_LIST_JS)
    return raw if isinstance(raw, list) else []


def _corrigendum_slug(text: str) -> str:
    cleaned = re.sub(r"[^\w\-]+", "_", (text or "corrigendum").strip())
    return (cleaned[:60] or "corrigendum").strip("_")


def _corrigendum_table(page):
    by_id = page.locator("#corrigendumDocumenttable")
    if by_id.count():
        return by_id.first
    header = page.locator("td.pageheader", has_text="Latest Corrigendum List")
    if header.count() == 0:
        return None
    table = header.locator("xpath=ancestor::tr[1]/following-sibling::tr[1]//table").first
    return table if table.count() else None


def _on_tender_detail(page) -> bool:
    try:
        return (
            page.locator("#corrigendumDocumenttable").count() > 0
            or page.locator("td.pageheader", has_text="Latest Corrigendum List").count() > 0
            or _zip_download_link(page).count() > 0
        )
    except PlaywrightError:
        return False


def _parse_corrigendum_history(target) -> dict:
    raw = target.evaluate(CORRIGENDA_HISTORY_JS)
    return raw if isinstance(raw, dict) else {}


def _is_pdf_bytes(data: bytes) -> bool:
    return bool(data) and data[:4] == b"%PDF"


def _keep_pdf_download(path: Path) -> Path | None:
    if path.is_file() and _is_pdf_bytes(path.read_bytes()[:8]):
        return path
    if path.is_file():
        path.unlink(missing_ok=True)
    return None


def _download_corrigendum_pdf(target, dest: Path) -> Path | None:
    """Save the history table's Document Name PDF. Skip when the cell is empty."""
    info = _parse_corrigendum_history(target)
    if not info.get("is_history"):
        return None
    if not (info.get("pdf_id") or info.get("pdf_name") or info.get("pdf_href")):
        return None

    link = None
    if info.get("pdf_id"):
        link = target.locator(f"#{info['pdf_id']}").first
    if (link is None or link.count() == 0) and info.get("pdf_name"):
        link = target.locator("a", has_text=info["pdf_name"]).first
    if link is None or link.count() == 0:
        return None

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.expect_download(timeout=45_000) as download_info:
            link.click()
        download = download_info.value
        suggested = download.suggested_filename or dest.name
        final = dest
        if suggested.lower().endswith(".pdf"):
            final = dest.with_name(suggested)
        download.save_as(str(final))
        kept = _keep_pdf_download(final)
        if kept is not None:
            return kept
    except (PlaywrightTimeoutError, PlaywrightError):
        pass

    href = info.get("pdf_href") or ""
    if not href:
        return None
    try:
        abs_url = target.evaluate("(h) => new URL(h, location.href).href", href)
        response = target.request.get(abs_url)
        body = response.body()
        if response.ok and _is_pdf_bytes(body):
            dest.write_bytes(body)
            return dest
    except Exception:
        return None
    return None


def _file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _history_fingerprint(history: dict) -> str:
    """Stable digest of non-PDF corrigendum content (dates / description / doc name)."""
    payload = {
        "description": history.get("description"),
        "published": history.get("published"),
        "doc_size": history.get("doc_size"),
        "pdf_name": history.get("pdf_name"),
        "revised": history.get("revised") or {},
        "original": history.get("original") or {},
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _existing_corr_pdf(sub: Path) -> Path | None:
    direct = sub / "corrigendum.pdf"
    if direct.is_file():
        return direct
    pdfs = sorted(sub.glob("*.pdf"))
    return pdfs[0] if pdfs else None


def _open_corrigendum_history(page, row):
    """Click View on a list row; return (history_page, opened_popup)."""
    view = row.locator(
        'a[title="View Corrigendum History"], a[title="View"], a[title*="Corrigendum" i]'
    )
    if view.count() == 0:
        view = row.locator("a")
    if view.count() == 0:
        return None, False
    try:
        with page.expect_popup(timeout=20_000) as popup_info:
            view.first.click()
        history_page = popup_info.value
        history_page.wait_for_load_state("domcontentloaded")
        history_page.wait_for_timeout(800)
        return history_page, True
    except PlaywrightTimeoutError:
        view.first.click()
        page.wait_for_load_state("domcontentloaded", timeout=30_000)
        page.wait_for_timeout(800)
        return page, False


def _close_corrigendum_history(
    page,
    history_page,
    opened_popup: bool,
    tender_id: str,
    list_page: int,
    stop_event: threading.Event | None = None,
) -> None:
    if opened_popup and history_page is not None:
        try:
            history_page.close()
        except PlaywrightError:
            pass
        return
    if not opened_popup and not _on_tender_detail(page):
        try:
            page.go_back(wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(800)
        except PlaywrightError:
            open_listed_tender(page, tender_id, list_page, stop_event=stop_event)


def _sync_corrigendum_row(
    page,
    tender_id: str,
    item: dict,
    index: int,
    folder: Path,
    list_page: int,
    stop_event: threading.Event | None = None,
    *,
    compare: bool = False,
) -> str:
    """Fetch one corrigendum history (+ PDF).

    Returns: new | updated | unchanged | skipped | error
    When compare=False (initial download), always writes and returns new/updated.
    When compare=True, replaces the PDF only if SHA-256 differs (or history fingerprint
    changes when there is no PDF).
    """
    if not _on_tender_detail(page):
        open_listed_tender(page, tender_id, list_page, stop_event=stop_event)
    table = _corrigendum_table(page)
    if table is None:
        return "skipped"
    rows = table.locator("tr.even, tr.odd")
    if index >= rows.count():
        return "skipped"

    serial = str(item.get("serial") or index + 1)
    title = item.get("title") or f"corrigendum_{serial}"
    corr_root = folder / "corrigendum"
    corr_root.mkdir(parents=True, exist_ok=True)
    sub = corr_root / f"{serial}_{_corrigendum_slug(title)}"
    # Prefer an existing folder for this serial if the title slug changed.
    existing_subs = sorted(corr_root.glob(f"{serial}_*"))
    if existing_subs and not sub.is_dir():
        sub = existing_subs[0]
    sub.mkdir(parents=True, exist_ok=True)

    old_meta: dict = {}
    meta_path = sub / "metadata.json"
    if meta_path.is_file():
        try:
            old_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            old_meta = {}
    old_pdf = _existing_corr_pdf(sub)
    old_sha = old_meta.get("pdf_sha256") or (
        _file_sha256(old_pdf) if old_pdf else None
    )
    old_fp = old_meta.get("history_sha256")
    if not old_fp and (sub / "history.json").is_file():
        try:
            old_hist = json.loads((sub / "history.json").read_text(encoding="utf-8"))
            old_fp = _history_fingerprint(old_hist)
        except Exception:
            old_fp = None
    had_local = bool(old_meta or old_pdf or (sub / "history.json").is_file())

    history_page = None
    opened_popup = False
    try:
        history_page, opened_popup = _open_corrigendum_history(page, rows.nth(index))
        if history_page is None:
            return "skipped"

        (sub / "history.html").write_text(history_page.content(), encoding="utf-8")
        history = _parse_corrigendum_history(history_page)
        if not history.get("is_history"):
            print(
                f"corrigendum {tender_id} {serial}: "
                "not a history popup (skipping detail-page PDFs)",
                flush=True,
            )
            history = {
                "is_history": False,
                "revised": {},
                "original": {},
                "pdf_name": None,
                "pdf_href": None,
                "pdf_id": None,
            }

        new_fp = _history_fingerprint(history)
        pdf_path = None
        new_sha = None
        pdf_changed = False

        if history.get("is_history") and (
            history.get("pdf_id") or history.get("pdf_name") or history.get("pdf_href")
        ):
            if compare and old_pdf is not None:
                probe = sub / f".probe_{serial}.pdf"
                probed = _download_corrigendum_pdf(history_page, probe)
                if probed is not None:
                    new_sha = _file_sha256(probed)
                    if new_sha and old_sha and new_sha == old_sha:
                        probed.unlink(missing_ok=True)
                        pdf_path = old_pdf
                        new_sha = old_sha
                        print(
                            f"corrigendum {tender_id} {serial}: PDF unchanged "
                            f"(sha256={old_sha[:12]}…)",
                            flush=True,
                        )
                    else:
                        # Authenticity mismatch — keep previous as .prev, install new.
                        if old_pdf.is_file():
                            prev = old_pdf.with_suffix(old_pdf.suffix + ".prev")
                            try:
                                if prev.is_file():
                                    prev.unlink()
                                old_pdf.replace(prev)
                            except OSError:
                                pass
                        final = sub / "corrigendum.pdf"
                        if probed.resolve() != final.resolve():
                            if final.is_file():
                                final.unlink(missing_ok=True)
                            probed.replace(final)
                        pdf_path = final
                        pdf_changed = True
                        print(
                            f"corrigendum {tender_id} {serial}: PDF updated "
                            f"({old_sha[:12] if old_sha else 'none'}… → "
                            f"{(new_sha or '')[:12]}…)",
                            flush=True,
                        )
                else:
                    pdf_path = old_pdf
                    new_sha = old_sha
            else:
                pdf_path = _download_corrigendum_pdf(
                    history_page, sub / "corrigendum.pdf"
                )
                if pdf_path is not None:
                    new_sha = _file_sha256(pdf_path)
                    pdf_changed = True
                    print(
                        f"corrigendum {tender_id} {serial}: saved {pdf_path.name} "
                        f"({pdf_path.stat().st_size} bytes)",
                        flush=True,
                    )
        elif history.get("is_history"):
            print(
                f"corrigendum {tender_id} {serial}: history saved, "
                "no document attached",
                flush=True,
            )

        history_changed = bool(old_fp and new_fp and old_fp != new_fp)
        meta = {
            **item,
            "description": history.get("description"),
            "revised_dates": history.get("revised") or {},
            "original_dates": history.get("original") or {},
            "pdf_file": pdf_path.name if pdf_path is not None else None,
            "pdf_name": history.get("pdf_name"),
            "pdf_missing": pdf_path is None,
            "pdf_sha256": new_sha,
            "history_sha256": new_fp,
            "published": history.get("published"),
            "doc_size": history.get("doc_size"),
        }
        (sub / "metadata.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (sub / "history.json").write_text(
            json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        item["description"] = meta.get("description")
        item["revised_dates"] = meta["revised_dates"]
        item["original_dates"] = meta["original_dates"]
        item["pdf_file"] = meta.get("pdf_file")
        item["pdf_missing"] = meta["pdf_missing"]
        item["pdf_sha256"] = new_sha
        item["history_sha256"] = new_fp

        if not had_local:
            return "new"
        if pdf_changed or history_changed:
            return "updated"
        if compare:
            return "unchanged"
        return "updated"
    except JobStopped:
        raise
    except Exception as exc:
        print(f"corrigendum {tender_id} {serial}: {exc}", flush=True)
        (sub / "metadata.json").write_text(
            json.dumps(item, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return "error"
    finally:
        _close_corrigendum_history(
            page, history_page, opened_popup, tender_id, list_page, stop_event
        )


def download_corrigendum_items(
    page,
    tender_id: str,
    items: list[dict],
    folder: Path,
    list_page: int,
    userid: str = "",
    apikey: str = "",
    provider: str | None = None,
    stop_event: threading.Event | None = None,
) -> int:
    """Save each corrigendum's history popup (dates) and corrigendum.pdf under folder/corrigendum/."""
    del userid, apikey, provider  # captcha zip path no longer used for corrigenda
    if not items:
        return 0
    if _corrigendum_table(page) is None:
        print(f"corrigendum {tender_id}: list header found but table missing", flush=True)
        return 0
    saved = 0
    for index, item in enumerate(items):
        if stop_event is not None and stop_event.is_set():
            raise JobStopped(f"Stopped while saving corrigenda for {tender_id}.")
        status = _sync_corrigendum_row(
            page,
            tender_id,
            item,
            index,
            folder,
            list_page,
            stop_event=stop_event,
            compare=False,
        )
        if status in {"new", "updated"} and not item.get("pdf_missing", True):
            saved += 1
        elif status in {"new", "updated"}:
            # History-only corrigendum (Fee type etc.) still counts as saved meta.
            saved += 0 if item.get("pdf_missing") else 1
    # Count PDFs actually present after sync.
    corr_root = folder / "corrigendum"
    if corr_root.is_dir():
        saved = sum(1 for p in corr_root.glob("*/*.pdf") if not p.name.endswith(".prev"))
    return saved


def refresh_corrigenda_one(
    page,
    tender: dict,
    stop_event: threading.Event | None = None,
) -> dict:
    """Re-check a downloaded tender's detail page for new/changed corrigenda.

    - No prior corrigenda: detect whether any were released; fetch if so.
    - Prior corrigenda: re-open each history entry and compare PDF SHA-256
      (or history fingerprint when no PDF) before replacing files.
    """
    import db
    import extract_pipeline

    tender_id = tender["tender_id"]
    folder = TENDERS_DIR / tender_id
    result = {
        "tender_id": tender_id,
        "changed": False,
        "new": 0,
        "updated": 0,
        "unchanged": 0,
        "still_none": False,
        "error": None,
    }
    if not folder.is_dir():
        result["error"] = "no packet folder"
        return result

    closing_at = tender.get("closing_at")
    if isinstance(closing_at, str):
        try:
            closing_at = datetime.fromisoformat(closing_at)
        except ValueError:
            closing_at = None

    stored: list[dict] = []
    corr_json = folder / "corrigenda.json"
    if corr_json.is_file():
        try:
            stored = json.loads(corr_json.read_text(encoding="utf-8"))
            if not isinstance(stored, list):
                stored = []
        except json.JSONDecodeError:
            stored = []
    if not stored:
        stored = list(tender.get("corrigenda") or [])

    try:
        found_page = open_listed_tender(
            page,
            tender_id,
            tender.get("list_page") or 1,
            stop_event=stop_event,
            closing_at=closing_at,
        )
        if found_page != (tender.get("list_page") or 1):
            db.update_list_page(tender_id, found_page)

        live = list_corrigenda(page)
        if not live and not stored:
            result["still_none"] = True
            print(f"corr-refresh {tender_id}: still no corrigendum", flush=True)
            return result

        if not live and stored:
            print(
                f"corr-refresh {tender_id}: portal shows none "
                f"(kept {len(stored)} stored)",
                flush=True,
            )
            return result

        if not stored and live:
            print(
                f"corr-refresh {tender_id}: {len(live)} corrigendum(s) newly released",
                flush=True,
            )

        for index, item in enumerate(live):
            if stop_event is not None and stop_event.is_set():
                raise JobStopped(f"Stopped while refreshing corrigenda for {tender_id}.")
            status = _sync_corrigendum_row(
                page,
                tender_id,
                item,
                index,
                folder,
                found_page,
                stop_event=stop_event,
                compare=bool(stored),
            )
            if status == "new":
                result["new"] += 1
            elif status == "updated":
                result["updated"] += 1
            elif status == "unchanged":
                result["unchanged"] += 1

        db.save_corrigenda(tender_id, live)
        corr_json.write_text(
            json.dumps(live, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        result["changed"] = result["new"] > 0 or result["updated"] > 0

        if result["changed"]:
            try:
                page_details = parse_tender_detail(page)
            except Exception:
                page_details = (tender.get("details") or {}).get("page") or {}
            try:
                details = extract_pipeline.finalize(
                    tender_id, folder, tender, page_details, live
                )
                extract_pipeline.save(tender_id, folder, details)
            except Exception as exc:
                print(f"corr-refresh {tender_id}: re-extract failed: {exc}", flush=True)

        print(
            f"corr-refresh {tender_id}: "
            f"new={result['new']} updated={result['updated']} "
            f"unchanged={result['unchanged']}",
            flush=True,
        )
        return result
    except JobStopped:
        raise
    except Exception as exc:
        result["error"] = str(exc)
        print(f"corr-refresh {tender_id}: {exc}", flush=True)
        return result


def refresh_corrigenda_pending(
    tenders: list[dict],
    stop_event: threading.Event | None = None,
    on_progress=None,
) -> dict:
    """Walk downloaded tenders and refresh corrigenda with one warm browser."""
    stop = stop_event or threading.Event()
    summary = {
        "done": 0,
        "changed": 0,
        "new": 0,
        "updated": 0,
        "still_none": 0,
        "errors": 0,
        "fatal": None,
    }
    if not tenders:
        return summary
    try:
        playwright_cm = sync_playwright()
        playwright = playwright_cm.__enter__()
        browser = playwright.chromium.launch(headless=True)
    except Exception as exc:
        summary["fatal"] = str(exc)
        return summary
    page = browser.new_page(accept_downloads=True)
    try:
        for tender in tenders:
            if stop.is_set():
                break
            try:
                result = refresh_corrigenda_one(page, tender, stop_event=stop)
            except JobStopped:
                break
            summary["done"] += 1
            if result.get("changed"):
                summary["changed"] += 1
            summary["new"] += int(result.get("new") or 0)
            summary["updated"] += int(result.get("updated") or 0)
            if result.get("still_none"):
                summary["still_none"] += 1
            if result.get("error"):
                summary["errors"] += 1
            if on_progress:
                on_progress(summary["done"], result)
    finally:
        try:
            browser.close()
        except Exception:
            pass
        playwright_cm.__exit__(None, None, None)
    return summary


def extract_zip(zip_path: Path, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    root = folder.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            target = (folder / info.filename).resolve()
            if target != root and root not in target.parents:
                continue
            archive.extract(info, folder)


def _log_timings(tender_id: str, timings: dict) -> None:
    parts = []
    lookup = timings.get("lookup") or {}
    if lookup:
        parts.append(
            f"lookup={lookup.get('ms', 0)}ms/{lookup.get('fetches', 0)}f"
            f"{'' if lookup.get('hit', True) else '/miss'}"
        )
    corr = timings.get("corr") or {}
    if corr:
        parts.append(f"corr={corr.get('ms', 0)}ms/{corr.get('count', 0)}")
    zip_info = timings.get("zip") or {}
    if zip_info:
        tag = "captcha" if zip_info.get("captcha") else "reuse"
        parts.append(f"zip={zip_info.get('ms', 0)}ms/{tag}")
    extract = timings.get("extract") or {}
    if extract:
        parts.append(f"extract={extract.get('ms', 0)}ms")
        if extract.get("llm_ms") is not None:
            parts.append(f"llm={extract.get('llm_ms')}ms")
    if timings.get("total_ms") is not None:
        parts.append(f"total={timings['total_ms']}ms")
    if parts:
        print(f"timings {tender_id}: {' '.join(parts)}", flush=True)


def _extract_one(
    tender_id: str,
    folder: Path,
    tender: dict,
    page_details: dict,
    corrigenda: list[dict],
    stop_event: threading.Event | None = None,
) -> dict | None:
    """Run finalize+save for a downloaded packet. Safe to run off-worker."""
    import db
    import extract_pipeline

    if stop_event is not None and stop_event.is_set():
        print(f"extract {tender_id}: skipped (stop requested)", flush=True)
        return None
    try:
        details = extract_pipeline.finalize(
            tender_id,
            folder,
            tender,
            page_details,
            corrigenda,
            stop_event=stop_event,
        )
        extract_pipeline.save(tender_id, folder, details)
        status = details["status"]
        extract_timings = status.get("timings")
        if extract_timings:
            db.merge_timings(tender_id, {"extract": extract_timings})
        print(
            f"{tender_id}: fields extracted "
            f"(page={status['page_ok']} boq={status['boq_ok']} llm={status['llm_ok']})",
            flush=True,
        )
        return details
    except Exception as exc:
        print(f"{tender_id}: field extraction failed: {exc}", flush=True)
        return None


def download_one(
    tender: dict,
    userid: str = "",
    apikey: str = "",
    provider: str | None = None,
    page=None,
    stop_event: threading.Event | None = None,
    extract_pool=None,
    extract_state: dict | None = None,
    lookup_cache: dict | None = None,
) -> str | None:
    """Download one tender. Reuse `page` when provided so the browser session stays warm."""
    import db

    tender_id = tender["tender_id"]
    closing_at = tender.get("closing_at")
    timings: dict = {}
    wall_started = time.monotonic()
    if stop_event is not None and stop_event.is_set():
        return "stopped"
    if not db.claim(tender_id):
        print(f"skip {tender_id}", flush=True)
        return None
    if closing_at is not None and closing_at < datetime.now(IST):
        message = "closing date has passed; tender left the closing list"
        timings["skipped"] = "expired"
        timings["total_ms"] = int(round((time.monotonic() - wall_started) * 1000))
        db.mark_failed(tender_id, message, timings=timings)
        print(f"expired {tender_id}: {message}", flush=True)
        return message
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    mode = provider or captcha_provider()

    def run(active_page) -> None:
        if stop_event is not None and stop_event.is_set():
            raise JobStopped(f"Stopped before {tender_id}.")
        stored_page = tender.get("list_page") or 1
        found_page = open_listed_tender(
            active_page,
            tender_id,
            stored_page,
            stop_event=stop_event,
            closing_at=closing_at,
            timings=timings,
            lookup_cache=lookup_cache,
        )
        if found_page != stored_page:
            db.update_list_page(tender_id, found_page)

        if stop_event is not None and stop_event.is_set():
            raise JobStopped(f"Stopped after opening {tender_id}.")

        folder = TENDERS_DIR / tender_id
        folder.mkdir(parents=True, exist_ok=True)

        import extract_pipeline

        try:
            page_details = parse_tender_detail(active_page)
            extract_pipeline.save(tender_id, folder, {"page": page_details})
        except Exception as exc:
            page_details = {}
            print(f"{tender_id}: detail parse failed: {exc}", flush=True)

        corrigenda = list_corrigenda(active_page)
        db.save_corrigenda(tender_id, corrigenda)
        (folder / "corrigenda.json").write_text(
            json.dumps(corrigenda, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        if corrigenda:
            print(
                f"{tender_id}: {len(corrigenda)} corrigendum(s) on detail page",
                flush=True,
            )
            corr_started = time.monotonic()
            saved_corr = download_corrigendum_items(
                active_page,
                tender_id,
                corrigenda,
                folder,
                found_page,
                userid,
                apikey,
                provider=mode,
                stop_event=stop_event,
            )
            timings["corr"] = {
                "ms": int(round((time.monotonic() - corr_started) * 1000)),
                "count": len(corrigenda),
                "pdf": saved_corr,
            }
            print(f"{tender_id}: saved {saved_corr} corrigendum pdf(s)", flush=True)
            # download_corrigendum_items mutates items with revised/original dates + pdf.
            db.save_corrigenda(tender_id, corrigenda)
            (folder / "corrigenda.json").write_text(
                json.dumps(corrigenda, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            if not _on_tender_detail(active_page):
                open_listed_tender(
                    active_page,
                    tender_id,
                    found_page,
                    stop_event=stop_event,
                    lookup_cache=lookup_cache,
                )
        else:
            timings["corr"] = {"ms": 0, "count": 0, "pdf": 0}

        if stop_event is not None and stop_event.is_set():
            raise JobStopped(f"Stopped before zip for {tender_id}.")

        dest = TEMP_DIR / f"{tender_id}.zip"
        captcha_path = TEMP_DIR / f"{tender_id}.png"
        saved = download_packet(
            active_page,
            userid,
            apikey,
            dest,
            captcha_path,
            provider=mode,
            timings=timings,
        )
        if not saved.is_file() or saved.stat().st_size == 0:
            raise RuntimeError("The zip was not saved.")
        extract_zip(saved, folder)

        timings["total_ms"] = int(round((time.monotonic() - wall_started) * 1000))
        db.mark_downloaded(tender_id, saved.name, str(folder), timings=timings)
        _log_timings(tender_id, timings)
        print(f"downloaded {tender_id} -> {folder}", flush=True)

        if extract_pool is not None:
            # Extraction (Gemini) runs off the browser worker so the session
            # moves on to the next tender immediately.
            def _task() -> None:
                details = _extract_one(
                    tender_id, folder, tender, page_details, corrigenda, stop_event
                )
                if extract_state is not None:
                    with extract_state.get("lock"):
                        if details is not None:
                            extract_state["extracted"] += 1
                        else:
                            extract_state["extract_errors"] += 1

            fut = extract_pool.submit(_task)
            if extract_state is not None:
                extract_state["futs"].append(fut)
        else:
            details = _extract_one(
                tender_id, folder, tender, page_details, corrigenda, stop_event
            )
            if details is not None and details.get("status", {}).get("timings"):
                timings["extract"] = details["status"]["timings"]

    try:
        if page is not None:
            run(page)
            return None
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            fresh = browser.new_page(accept_downloads=True)
            try:
                run(fresh)
                return None
            finally:
                browser.close()
    except JobStopped as exc:
        db.release_claim(tender_id)
        print(f"stopped {tender_id}: {exc}", flush=True)
        return "stopped"
    except Exception as exc:
        timings["total_ms"] = int(round((time.monotonic() - wall_started) * 1000))
        db.mark_failed(tender_id, str(exc), timings=timings)
        _log_timings(tender_id, timings)
        print(f"failed {tender_id}: {exc}", flush=True)
        return str(exc)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)).strip() or default)
    except ValueError:
        return default


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def resolve_download_workers() -> tuple[int, str]:
    """Pick download concurrency from MAX_PARALLEL and recent timings.

    - MAX_PARALLEL: hard ceiling (default 4)
    - MAX_PARALLEL_MIN: floor when adaptive (default 2)
    - DOWNLOAD_ADAPTIVE=1: raise toward ceiling when portal is healthy
    """
    load_local_env()
    ceiling = max(1, _env_int("MAX_PARALLEL", 4))
    floor = max(1, min(ceiling, _env_int("MAX_PARALLEL_MIN", 2)))
    if not _env_flag("DOWNLOAD_ADAPTIVE", True):
        return ceiling, f"fixed MAX_PARALLEL={ceiling}"

    import db

    try:
        stats = db.timing_stats()
    except Exception:
        stats = {}
    samples = int(stats.get("with_lookup") or 0)
    total_ms = stats.get("total_ms_avg")
    reuse = int(stats.get("zip_reuse") or 0)
    captcha = int(stats.get("zip_captcha") or 0)
    reuse_ratio = reuse / max(1, reuse + captcha)

    if samples < 10 or total_ms is None:
        chosen = min(ceiling, max(floor, 3))
        return chosen, f"adaptive warm-up → {chosen} (ceiling {ceiling})"

    if total_ms > 40_000 or (reuse + captcha >= 8 and reuse_ratio < 0.85):
        return (
            floor,
            f"adaptive stress → {floor} (avg {total_ms}ms, reuse {reuse_ratio:.0%})",
        )
    if total_ms < 22_000 and reuse_ratio >= 0.90:
        return (
            ceiling,
            f"adaptive healthy → {ceiling} (avg {total_ms}ms, reuse {reuse_ratio:.0%})",
        )
    mid = (floor + ceiling + 1) // 2
    return mid, f"adaptive mid → {mid} (avg {total_ms}ms, reuse {reuse_ratio:.0%})"


_TRANSIENT_ERROR_TOKENS = (
    "timeout",
    "timed out",
    "429",
    "rate limit",
    "too many",
    "net::",
    "navigation",
    "connection",
    "econnreset",
    "econnrefused",
    "target closed",
    "browser has been closed",
    "did not finish",
    "did not start",
    "captcha image did not refresh",
)


def _is_transient_download_error(error: str | None) -> bool:
    if not error or error in {"stopped", "None"}:
        return False
    err = error.lower()
    if "no longer on the closing list" in err or "closing date has passed" in err:
        return False
    return any(token in err for token in _TRANSIENT_ERROR_TOKENS)


def download_pending(
    tenders: list[dict],
    userid: str = "",
    apikey: str = "",
    provider: str | None = None,
    workers: int = 1,
    stop_event: threading.Event | None = None,
    on_progress=None,
) -> dict:
    """Download with long-lived browser sessions; solve captcha only when asked.

    Workers share an exponential backoff gate: transient portal errors (timeouts,
    429, navigation failures) pause all workers briefly so we do not stampede
    the site when raising MAX_PARALLEL.
    """
    stop = stop_event or threading.Event()
    workers = max(1, workers)
    work: Queue = Queue()
    for tender in tenders:
        work.put(tender)

    from concurrent.futures import (
        CancelledError,
        ThreadPoolExecutor,
        TimeoutError as FuturesTimeout,
    )

    lock = threading.Lock()
    state = {
        "done": 0,
        "quota_hit": False,
        "last_error": None,
        "fatal": None,
        "workers": workers,
        "backoff_events": 0,
        "extracted": 0,
        "extract_errors": 0,
        "extract_workers": 0,
    }
    extract_workers = max(1, _env_int("EXTRACT_WORKERS", 4))
    state["extract_workers"] = extract_workers
    extract_state = {"lock": lock, "extracted": 0, "extract_errors": 0, "futs": []}
    extract_pool = ThreadPoolExecutor(
        max_workers=extract_workers, thread_name_prefix="extract"
    )
    extract_futs: list = []
    pace = {"streak": 0, "sleep_until": 0.0}
    backoff_base = max(1.0, float(os.environ.get("DOWNLOAD_BACKOFF_BASE", "2")))
    backoff_cap = max(backoff_base, float(os.environ.get("DOWNLOAD_BACKOFF_CAP", "60")))
    gap_ms = max(0, _env_int("DOWNLOAD_GAP_MS", 150 if workers >= 3 else 0))

    def wait_for_pace(worker_id: int) -> None:
        while not stop.is_set():
            with lock:
                until = pace["sleep_until"]
            delay = until - time.monotonic()
            if delay <= 0:
                return
            print(
                f"download worker {worker_id}: pacing {delay:.1f}s (shared backoff)",
                flush=True,
            )
            time.sleep(min(delay, 1.0))

    def note_result(error: str | None) -> None:
        with lock:
            if error is None:
                pace["streak"] = max(0, pace["streak"] - 1)
                return
            if not _is_transient_download_error(error):
                return
            pace["streak"] += 1
            delay = min(backoff_cap, backoff_base ** min(pace["streak"], 5))
            pace["sleep_until"] = max(pace["sleep_until"], time.monotonic() + delay)
            state["backoff_events"] += 1
            print(
                f"download pace: backoff {delay:.1f}s "
                f"(streak={pace['streak']}) after: {error[:120]}",
                flush=True,
            )

    def worker(worker_id: int) -> None:
        mode = provider or captcha_provider()
        print(f"download worker {worker_id}: opening browser session", flush=True)
        try:
            playwright_cm = sync_playwright()
            playwright = playwright_cm.__enter__()
        except Exception as exc:
            with lock:
                state["fatal"] = str(exc)
            stop.set()
            print(f"download worker {worker_id}: browser failed: {exc}", flush=True)
            return
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:
            with lock:
                state["fatal"] = str(exc)
            stop.set()
            print(f"download worker {worker_id}: browser failed: {exc}", flush=True)
            playwright_cm.__exit__(None, None, None)
            return
        page = browser.new_page(accept_downloads=True)
        lookup_cache: dict = {"ranges": {}, "last": None}
        try:
            while not stop.is_set():
                wait_for_pace(worker_id)
                if stop.is_set():
                    break
                try:
                    tender = work.get_nowait()
                except Empty:
                    break
                error = download_one(
                    tender,
                    userid,
                    apikey,
                    provider=mode,
                    page=page,
                    stop_event=stop,
                    extract_pool=extract_pool,
                    extract_state=extract_state,
                    lookup_cache=lookup_cache,
                )
                with lock:
                    state["done"] += 1
                    state["last_error"] = error
                    done = state["done"]
                if on_progress:
                    on_progress(done, error)
                if error == "stopped" or stop.is_set():
                    break
                note_result(error)
                err = (error or "").lower()
                if any(
                    token in err
                    for token in (
                        "error_zero_balance",
                        "zero balance",
                        "above free usage limit",
                    )
                ):
                    with lock:
                        state["quota_hit"] = True
                    stop.set()
                    break
                if gap_ms and error is None and not stop.is_set():
                    time.sleep(gap_ms / 1000.0)
        finally:
            try:
                browser.close()
            except Exception:
                pass
            playwright_cm.__exit__(None, None, None)
            print(f"download worker {worker_id}: closed browser session", flush=True)

    threads = [
        threading.Thread(target=worker, args=(index + 1,), daemon=True)
        for index in range(min(workers, max(1, len(tenders))))
    ]
    if not tenders:
        extract_pool.shutdown(wait=False)
        return state
    print(
        f"download: starting {len(threads)} worker(s) "
        f"for {len(tenders)} tenders (gap={gap_ms}ms, "
        f"extract={extract_workers} pool threads)",
        flush=True,
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # Drain pending extractions so the job only ends when details are written.
    # Bounded: each in-flight Gemini call is capped by GEMINI_TIMEOUT_S, and the
    # overall wait by EXTRACT_DRAIN_TIMEOUT so Stop never hangs the job phase.
    if extract_pool is not None:
        drain_timeout = max(30, _env_int("EXTRACT_DRAIN_TIMEOUT", 180))
        deadline = time.monotonic() + drain_timeout
        futs = list(extract_state.get("futs") or [])
        pending = len(futs)
        abandoned = 0
        print(
            f"download: waiting for extraction pool to drain "
            f"({pending} task(s), up to {drain_timeout}s)",
            flush=True,
        )
        for fut in futs:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                abandoned += 1
                continue
            try:
                fut.result(timeout=remaining)
            except FuturesTimeout:
                abandoned += 1
            except CancelledError:
                pass
            except Exception:
                abandoned += 0  # task already recorded its own error
        if abandoned:
            print(
                f"download: extract drain gave up on {abandoned} task(s) "
                f"after {drain_timeout}s (they finish or die in the background)",
                flush=True,
            )
        extract_pool.shutdown(wait=False, cancel_futures=True)
    state["extracted"] = extract_state["extracted"]
    state["extract_errors"] = extract_state["extract_errors"]
    return state


def main() -> None:
    from dashboard import main as serve

    serve()


if __name__ == "__main__":
    raise SystemExit(main())
