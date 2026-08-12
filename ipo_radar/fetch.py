"""HTTP fetching for 38.co.kr.

38.co.kr serves EUC-KR encoded, server-rendered HTML with a permissive
robots.txt (``Disallow:`` empty). Stdlib only, so the scheduled job has no
dependencies that can drift out from under it.
"""

import gzip
import time
import urllib.error
import urllib.request
from typing import Optional

BASE = "https://www.38.co.kr/html/fund/"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Be a good citizen: 38.co.kr is a small site run for retail investors.
POLITE_DELAY_SEC = 1.0

_last_request_at = 0.0


class FetchError(Exception):
    pass


def _sleep_polite() -> None:
    global _last_request_at
    elapsed = time.time() - _last_request_at
    if elapsed < POLITE_DELAY_SEC:
        time.sleep(POLITE_DELAY_SEC - elapsed)
    _last_request_at = time.time()


def fetch(url: str, retries: int = 3, timeout: int = 20) -> str:
    """GET *url* and decode as EUC-KR. Retries with backoff on transient errors."""
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        if attempt:
            time.sleep(2 ** attempt)
        _sleep_polite()
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Encoding": "gzip",
                "Accept-Language": "ko-KR,ko;q=0.9",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
        except (urllib.error.URLError, OSError) as exc:
            last_err = exc
            continue
        # errors="replace" keeps a single bad byte from killing the whole run.
        return raw.decode("euc-kr", errors="replace")
    raise FetchError("failed to fetch %s after %d attempts: %s" % (url, retries, last_err))


def fetch_schedule_page() -> str:
    """공모청약일정 — upcoming subscription windows."""
    return fetch(BASE + "?o=k")


def fetch_demand_results_page() -> str:
    """수요예측결과 — institutional demand-forecast results."""
    return fetch(BASE + "?o=r1")


def fetch_detail_page(no: str) -> str:
    """Per-stock detail page, keyed by 38.co.kr's internal ``no``."""
    return fetch(BASE + "?o=v&no=%s" % no)


def detail_url(no: str) -> str:
    return BASE + "?o=v&no=%s" % no
