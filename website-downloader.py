#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import os
import queue
import sys
import threading
import time
from hashlib import sha256
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
import json
from urllib.parse import parse_qsl
from requests.cookies import create_cookie
from http.cookiejar import MozillaCookieJar
import re
from datetime import datetime
from typing import List

# ---------------------------------------------------------------------------
# Config / constants
# ---------------------------------------------------------------------------

LOG_FMT = "%(asctime)s | %(levelname)-8s | %(threadName)s | %(message)s"

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
    "Gecko/20100101 Firefox/128.0"
}

TIMEOUT = 15  # seconds
CHUNK_SIZE = 8192  # bytes

# Conservative margins under common OS limits (~255–260 bytes)
MAX_PATH_LEN = 240
MAX_SEG_LEN = 120


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    filename="web_scraper.log",
    level=logging.DEBUG,
    format=LOG_FMT,
    datefmt="%H:%M:%S",
    force=True,
)
_console = logging.StreamHandler(sys.stdout)
_console.setLevel(logging.INFO)
_console.setFormatter(logging.Formatter(LOG_FMT, datefmt="%H:%M:%S"))
logging.getLogger().addHandler(_console)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTTP session (retry, timeouts, custom UA)
# ---------------------------------------------------------------------------

SESSION = requests.Session()
RETRY_STRAT = Retry(
    total=5,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "HEAD"],
)
SESSION.mount("http://", HTTPAdapter(max_retries=RETRY_STRAT))
SESSION.mount("https://", HTTPAdapter(max_retries=RETRY_STRAT))
SESSION.headers.update(DEFAULT_HEADERS)

_COOKIE_ATTR_RE = re.compile(r"\s*([^=;]+)(?:=([^;]*))?\s*")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def create_dir(path: Path) -> None:
    """Create path (and parents) if it does not already exist."""
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        log.debug("Created directory %s", path)


def sanitize(url_fragment: str) -> str:
    """Strip back-references and Windows backslashes."""
    return url_fragment.replace("\\", "/").replace("..", "").strip()


NON_FETCHABLE_SCHEMES = {"mailto", "tel", "sms", "javascript", "data", "geo", "blob"}


def is_httpish(u: str) -> bool:
    """True iff the URL is http(s) or relative (no scheme)."""
    p = urlparse(u)
    return (p.scheme in ("http", "https")) or (p.scheme == "")


def is_non_fetchable(u: str) -> bool:
    """True iff the URL clearly shouldn't be fetched (mailto:, tel:, data:, ...)."""
    p = urlparse(u)
    return p.scheme in NON_FETCHABLE_SCHEMES


def is_internal(link: str, root_netloc: str) -> bool:
    """Return True if link belongs to root_netloc (or is protocol-relative)."""
    parsed = urlparse(link)
    return not parsed.netloc or parsed.netloc == root_netloc


def _shorten_segment(segment: str, limit: int = MAX_SEG_LEN) -> str:
    """
    Shorten a single path segment if over limit.
    Preserve extension; append a short hash to keep it unique.
    """
    if len(segment) <= limit:
        return segment
    p = Path(segment)
    stem, suffix = p.stem, p.suffix
    h = sha256(segment.encode("utf-8")).hexdigest()[:12]
    # leave room for '-' + hash + suffix
    keep = max(0, limit - len(suffix) - 13)
    return f"{stem[:keep]}-{h}{suffix}"


def to_local_path(parsed: urlparse, site_root: Path) -> Path:
    """
    Map an internal URL to a local file path under site_root.

    - Adds 'index.html' where appropriate.
    - Converts extensionless paths to '.html'.
    - Appends a short query-hash when ?query is present to avoid collisions.
    - Enforces per-segment and overall path length limits. If still too long,
      hashes the leaf name.
    """
    rel = parsed.path.lstrip("/")
    if not rel:
        rel = "index.html"
    elif rel.endswith("/"):
        rel += "index.html"
    elif not Path(rel).suffix:
        rel += ".html"

    if parsed.query:
        qh = sha256(parsed.query.encode("utf-8")).hexdigest()[:10]
        p = Path(rel)
        rel = str(p.with_name(f"{p.stem}-q{qh}{p.suffix}"))

    # Shorten individual segments
    parts = Path(rel).parts
    parts = tuple(_shorten_segment(seg, MAX_SEG_LEN) for seg in parts)
    local_path = site_root / Path(*parts)

    # If full path is still too long, hash the leaf
    if len(str(local_path)) > MAX_PATH_LEN:
        p = local_path
        h = sha256(parsed.geturl().encode("utf-8")).hexdigest()[:16]
        leaf = _shorten_segment(f"{p.stem}-{h}{p.suffix}", MAX_SEG_LEN)
        local_path = p.with_name(leaf)

    return local_path


def safe_write_text(path: Path, text: str, encoding: str = "utf-8") -> Path:
    """
    Write text to path, falling back to a hashed filename if OS rejects it
    (e.g., filename too long). Returns the final path used.
    """
    try:
        path.write_text(text, encoding=encoding)
        return path
    except OSError as exc:
        log.warning("Write failed for %s: %s. Falling back to hashed leaf.", path, exc)
        p = path
        h = sha256(str(p).encode("utf-8")).hexdigest()[:16]
        fallback = p.with_name(_shorten_segment(f"{p.stem}-{h}{p.suffix}", MAX_SEG_LEN))
        create_dir(fallback.parent)
        fallback.write_text(text, encoding=encoding)
        return fallback


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------


def fetch_html(url: str) -> Optional[BeautifulSoup]:
    """Download url and return a BeautifulSoup tree (or None on error)."""
    try:
        resp = SESSION.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:  # noqa: BLE001
        log.warning("HTTP error for %s – %s", url, exc)
        return None


def fetch_binary(url: str, dest: Path) -> None:
    """Stream url to dest unless it already exists. Safe against long paths."""
    if dest.exists():
        return
    try:
        resp = SESSION.get(url, timeout=TIMEOUT, stream=True)
        resp.raise_for_status()
        create_dir(dest.parent)
        try:
            with dest.open("wb") as fh:
                for chunk in resp.iter_content(CHUNK_SIZE):
                    fh.write(chunk)
            log.debug("Saved resource -> %s", dest)
        except OSError as exc:
            # Fallback to hashed leaf if OS rejects path
            log.warning("Binary write failed for %s: %s. Using fallback.", dest, exc)
            p = dest
            h = sha256(str(p).encode("utf-8")).hexdigest()[:16]
            fallback = p.with_name(
                _shorten_segment(f"{p.stem}-{h}{p.suffix}", MAX_SEG_LEN)
            )
            create_dir(fallback.parent)
            with fallback.open("wb") as fh:
                for chunk in resp.iter_content(CHUNK_SIZE):
                    fh.write(chunk)
            log.debug("Saved resource (fallback) -> %s", fallback)
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to save %s – %s", url, exc)


# ---------------------------------------------------------------------------
# Link rewriting
# ---------------------------------------------------------------------------


def rewrite_links(
    soup: BeautifulSoup,
    page_url: str,
    site_root: Path,
    page_dir: Path,
    allow_hosts: Optional[set[str]] = None,
    exclude_patterns: Optional[list[str]] = None,
) -> None:
    """
    Rewrite links to local relative paths for:
      - internal URLs
      - assets on allow-listed external hosts (e.g., Wistia)
    Also rewrites srcset (img/picture/source) and video poster.
    Respects exclude_patterns: matching URLs are left as-is.
    """
    allow_hosts = allow_hosts or set()
    exclude_patterns = exclude_patterns or []
    root_netloc = urlparse(page_url).netloc

    def excluded(u: str) -> bool:
        u_l = u.lower()
        for pat in exclude_patterns:
            if pat.lower() in u_l:
                return True
        return False

    def rewrite_single(u: str) -> Optional[str]:
        """Return rewritten relative path if URL should be localized; else None."""
        if not u or u.startswith("#") or is_non_fetchable(u) or not is_httpish(u):
            return None
        abs_u = urljoin(page_url, u)
        if excluded(abs_u):
            return None
        if not is_allowed_url(abs_u, root_netloc, allow_hosts):
            return None  # leave truly-external (non-allowed) as-is

        lp = to_local_path(urlparse(abs_u), site_root)
        try:
            return os.path.relpath(lp, page_dir)
        except ValueError:
            return str(lp)

    # tags/attributes we should localize
    TAG_ATTRS = {
        "a": "href",
        "link": "href",
        "img": "src",
        "script": "src",
        "source": "src",      # <source src="..."> (audio/video)
        "audio": "src",
        "video": "src",
        "track": "src",
    }

    for tag in soup.find_all(list(TAG_ATTRS.keys()) + ["picture"]):
        # 1) poster on <video>
        if tag.name == "video" and tag.has_attr("poster"):
            new_poster = rewrite_single(sanitize(tag["poster"]))
            if new_poster:
                tag["poster"] = new_poster

        # 2) main src/href on standard tags
        attr = TAG_ATTRS.get(tag.name)
        if attr and tag.has_attr(attr):
            original = sanitize(tag[attr])
            new_val = rewrite_single(original)
            if new_val:
                tag[attr] = new_val

        # 3) srcset on <img>, <source> (inside <picture>), etc.
        if tag.has_attr("srcset"):
            entries = []
            for url, desc in parse_srcset(tag["srcset"]):
                url = sanitize(url)
                new_url = rewrite_single(url)
                if new_url:
                    entries.append(f"{new_url} {desc}".strip())
                else:
                    # keep original entry (external or excluded)
                    entries.append(f"{url} {desc}".strip())
            if entries:
                tag["srcset"] = ", ".join(entries)

# ---------------------------------------------------------------------------
# Crawl coordinator
# ---------------------------------------------------------------------------


def crawl_site(start_url: str, root: Path, max_pages: int, threads: int, max_depth: int = None, allow_hosts: Optional[list[str]] = None, crawl_external_html: bool = False) -> None:
    q_pages: queue.Queue[tuple[str, int]] = queue.Queue()
    q_pages.put((start_url, 0))
    seen_pages: set[str] = set()
    download_q: queue.Queue[tuple[str, Path]] = queue.Queue()
    allow_hosts_set: set[str] = set(allow_hosts or [])

    def worker() -> None:
        while True:
            try:
                url, dest = download_q.get(timeout=3)
            except queue.Empty:
                return
            if is_non_fetchable(url) or not is_httpish(url):
                log.debug("Skip non-fetchable: %s", url)
                download_q.task_done()
                continue
            fetch_binary(url, dest)
            download_q.task_done()

    workers: list[threading.Thread] = []
    for i in range(max(1, threads)):
        t = threading.Thread(target=worker, name=f"DL-{i+1}", daemon=True)
        t.start()
        workers.append(t)

    start_time = time.time()
    root_netloc = urlparse(start_url).netloc

    while not q_pages.empty() and len(seen_pages) < max_pages:
        page_url, depth = q_pages.get()
        if page_url in seen_pages:
            continue
        seen_pages.add(page_url)
        log.info("[%s/%s] %s", len(seen_pages), max_pages, page_url)

        soup = fetch_html(page_url)
        if soup is None:
            continue

        # Gather links & assets
        for tag in soup.find_all(["img", "script", "link", "a"]):
            link = tag.get("src") or tag.get("href")
            if tag.has_attr("srcset"):
                for u in parse_srcset(tag["srcset"]):
                    u = sanitize(u)
                    if not u or u.startswith("#") or is_non_fetchable(u) or not is_httpish(u):
                        continue
                    abs_u = urljoin(page_url, u)
                    if not is_allowed_url(abs_u, root_netloc, allow_hosts_set):
                        continue
                    parsed_u = urlparse(abs_u)
                    dest_u = to_local_path(parsed_u, root)
                    download_q.put((abs_u, dest_u))
            if not link:
                continue
            link = sanitize(link)
            if link.startswith("#") or is_non_fetchable(link) or not is_httpish(link):
                continue
            abs_url = urljoin(page_url, link)
            parsed = urlparse(abs_url)
            if not is_allowed_url(abs_url, root_netloc, allow_hosts_set):
                continue

            # Exclude unwanted URL patterns
            if should_exclude(abs_url, args.exclude):
                log.info("Skipping excluded URL: %s", abs_url)
                continue

            dest_path = to_local_path(parsed, root)
            # HTML?
            is_html_like = parsed.path.endswith("/") or not Path(parsed.path).suffix
            if is_html_like:
                # Only crawl HTML on same host, unless explicitly asked to crawl external HTML
                if (urlparse(abs_url).netloc == root_netloc) or crawl_external_html:
                    if (max_depth is None or depth < max_depth):
                        if abs_url not in seen_pages and abs_url not in [u for (u, _) in list(q_pages.queue)]:  # type: ignore
                            q_pages.put((abs_url, depth + 1))
            else:
                if should_exclude(abs_url, args.exclude):
                    log.info("Skipping excluded asset: %s", abs_url)
                else:
                    download_q.put((abs_url, dest_path))
        # Save current page
        local_path = to_local_path(urlparse(page_url), root)
        create_dir(local_path.parent)
        rewrite_links(soup, page_url, root, local_path.parent)
        html = soup.prettify()
        final_path = safe_write_text(local_path, html, encoding="utf-8")
        log.debug("Saved page %s", final_path)

    download_q.join()
    elapsed = time.time() - start_time
    if seen_pages:
        log.info(
            "Crawl finished: %s pages in %.2fs (%.2fs avg)",
            len(seen_pages),
            elapsed,
            elapsed / len(seen_pages),
        )
    else:
        log.warning("Nothing downloaded – check URL or connectivity")


# ---------------------------------------------------------------------------
# Helper function for output folder
# ---------------------------------------------------------------------------


def make_root(url: str, custom: Optional[str]) -> Path:
    """Derive output folder from URL if custom not supplied."""
    return Path(custom) if custom else Path(urlparse(url).netloc.replace(".", "_"))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Recursively mirror a website for offline use.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--url",
        required=True,
        help="Starting URL to crawl (e.g., https://example.com/).",
    )
    p.add_argument(
        "--destination",
        default=None,
        help="Output folder (defaults to a folder derived from the URL).",
    )
    p.add_argument(
        "--max-pages",
        type=int,
        default=50,
        help="Maximum number of HTML pages to crawl.",
    )
    p.add_argument(
        "--threads",
        type=int,
        default=6,
        help="Number of concurrent download workers.",
    )
    p.add_argument("--header", action="append", default=[], help="Extra header, e.g. 'Accept-Language=en-CA' (repeatable)")
    p.add_argument("--cookie", action="append", default=[], help="Cookie 'k=v' (repeatable)")
    p.add_argument("--cookie-file", help="Netscape/Mozilla cookie jar file")

    p.add_argument("--bearer-token", help="Bearer/JWT token")

    p.add_argument("--basic-user", help="HTTP Basic username")
    p.add_argument("--basic-pass", help="HTTP Basic password")

    p.add_argument("--login-url", help="Form login URL (POST by default)")
    p.add_argument("--login-method", choices=["POST", "GET"], default="POST", help="Login HTTP method")
    p.add_argument("--login-data", help="URL-encoded form data: 'u=alice&p=secret'")
    p.add_argument("--login-json", help="JSON body for login: '{\"u\":\"alice\",\"p\":\"secret\"}'")

    p.add_argument("--csrf-get", help="URL to GET for CSRF extraction (defaults to --login-url)")
    p.add_argument("--csrf-selector", help="CSS selector for CSRF element (reads .value or text)")
    p.add_argument("--csrf-field", help="Form field name to place CSRF token into; defaults to selected element's name")

    p.add_argument("--probe-url", help="URL to verify access after auth (defaults to --url)")
    p.add_argument("--probe-status", type=int, default=200, help="Expected status for probe")
    p.add_argument("--probe-contains", help="Substring expected in probe response body")
    p.add_argument("--max-depth", type=int, default=None, help="Max crawl depth (0=start page only)")
    p.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Exclude URLs containing these substrings (repeatable)"
    )
    p.add_argument(
        "--allow-host",
        action="append",
        default=[],
        help="Additional hostnames allowed for asset downloads (repeatable)"
    )
    p.add_argument(
        "--crawl-external-html",
        action="store_true",
        help="Also crawl HTML pages on allow-listed hosts (off by default; assets only)"
    )
    return p.parse_args()

def is_allowed_url(abs_url: str, root_netloc: str, allow_hosts: set[str]) -> bool:
    """Return True if URL is same-host or in allow-listed hosts."""
    p = urlparse(abs_url)
    if not p.netloc:
        return True  # relative -> same host
    return (p.netloc == root_netloc) or (p.netloc in allow_hosts)

def parse_srcset(val: str) -> list[tuple[str, str]]:
    """
    Returns list of (url, descriptor) pairs.
    'https://a/1.webp 320w, https://a/2.webp 640w' ->
      [('https://a/1.webp','320w'), ('https://a/2.webp','640w')]
    """
    out = []
    for part in (val or "").split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split()
        url = bits[0]
        desc = " ".join(bits[1:]) if len(bits) > 1 else ""
        out.append((url, desc))
    return out

def should_exclude(url: str, patterns: list[str]) -> bool:
    url_lower = url.lower()
    for pat in patterns:
        if pat.lower() in url_lower:
            return True
    return False

def apply_headers(headers: list[str]) -> None:
    for kv in headers or []:
        if "=" not in kv:
            log.warning("Ignoring header without '=': %s", kv)
            continue
        k, v = kv.split("=", 1)
        SESSION.headers[k.strip()] = v.strip()

def apply_cookies_inline(items: list[str]) -> None:
    for kv in items or []:
        if "=" not in kv:
            log.warning("Ignoring cookie without '=': %s", kv)
            continue
        k, v = kv.split("=", 1)
        SESSION.cookies.set(k.strip(), v.strip())


def _extract_csrf_from_html(url: str, css_selector: str) -> tuple[str, Optional[str]]:
    """
    Returns (value, name) where name may be None if not present on element.
    """
    soup = fetch_html(url)
    if not soup:
        raise RuntimeError(f"CSRF GET failed for {url}")
    el = soup.select_one(css_selector)
    if not el:
        raise RuntimeError(f"CSRF selector '{css_selector}' not found at {url}")
    val = el.get("value") or el.text.strip()
    if not val:
        raise RuntimeError("CSRF element found but empty")
    name = el.get("name")
    return val, name

def do_form_login(
    login_url: str,
    method: str,
    data_str: Optional[str],
    json_str: Optional[str],
    csrf_get: Optional[str],
    csrf_selector: Optional[str],
    csrf_field: Optional[str],
) -> None:
    payload = None
    json_payload = None

    if data_str and json_str:
        raise ValueError("--login-data and --login-json are mutually exclusive")

    if data_str:
        payload = dict(parse_qsl(data_str, keep_blank_values=True))
    if json_str:
        json_payload = json.loads(json_str)

    if csrf_selector:
        if not csrf_get:
            csrf_get = login_url
        token, name_from_el = _extract_csrf_from_html(csrf_get, csrf_selector)
        target_field = csrf_field or name_from_el
        if not target_field:
            raise RuntimeError("--csrf-field required when the selected element has no name")
        if json_payload is not None:
            json_payload[target_field] = token
        else:
            payload = payload or {}
            payload[target_field] = token

    try:
        if method.upper() == "POST":
            resp = SESSION.post(login_url, data=payload, json=json_payload, timeout=TIMEOUT)
        else:
            resp = SESSION.get(login_url, params=payload, timeout=TIMEOUT)
        resp.raise_for_status()
        log.info("Login %s %s -> %s", method.upper(), login_url, resp.status_code)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Login request failed: {exc}") from exc

def probe_access(url: str, expect_status: int, expect_contains: Optional[str]) -> None:
    try:
        r = SESSION.get(url, timeout=TIMEOUT)
        ok = (r.status_code == expect_status)
        if ok and expect_contains:
            ok = (expect_contains in r.text)
        if not ok:
            # Provide context
            snippet = ""
            try:
                snippet = r.text[:200].replace("\n", " ")
            except Exception:
                pass
            raise RuntimeError(
                f"Auth probe failed: got {r.status_code}, expected {expect_status}. Snippet: {snippet}"
            )
        log.info("Auth probe OK: %s", url)
    except Exception as exc:  # noqa: BLE001
        log.error("Auth probe error – %s", exc)
        sys.exit(3)

def setup_auth(args: argparse.Namespace) -> None:
    # Arbitrary headers
    apply_headers(args.header)

    # Cookies inline / file
    apply_cookie_strings(args.cookie)
    if args.cookie_file:
        load_cookie_file(args.cookie_file)

    # Bearer
    if args.bearer_token:
        SESSION.headers["Authorization"] = f"Bearer {args.bearer_token}"

    # Basic
    if args.basic_user or args.basic_pass:
        if not (args.basic_user and args.basic_pass):
            log.error("Both --basic-user and --basic-pass are required for Basic auth")
            sys.exit(2)
        SESSION.auth = (args.basic_user, args.basic_pass)

    # Form login
    if args.login_url:
        do_form_login(
            args.login_url,
            args.login_method,
            args.login_data,
            args.login_json,
            args.csrf_get,
            args.csrf_selector,
            args.csrf_field,
        )

    # Probe
    probe_url = args.probe_url or args.url
    probe_access(probe_url, args.probe_status, args.probe_contains)

def _parse_cookie_attr_pairs(s: str) -> dict:
    """
    Parse a cookie string that may include attributes:
    Examples accepted:
      "name=value"
      "name=value; domain=.nozominetworks.com; path=/; secure; httponly; expires=2026-12-15T16:11:29Z"
      "name=value; Domain=.example.com; Path=/foo"
    Returns dict with keys: name, value, domain, path, secure (bool), httponly (bool), expires (int unix ts)
    """
    parts = [p.strip() for p in s.split(";") if p.strip() != ""]
    if not parts:
        raise ValueError("Empty cookie string")
    # first part is name=value
    m = _COOKIE_ATTR_RE.match(parts[0])
    if not m:
        raise ValueError("Bad cookie name/value")
    name = m.group(1)
    value = m.group(2) or ""
    out = {"name": name, "value": value, "domain": None, "path": "/", "secure": False, "httponly": False, "expires": None}
    for attr in parts[1:]:
        attr_m = _COOKIE_ATTR_RE.match(attr)
        if not attr_m:
            continue
        k = attr_m.group(1).lower()
        v = attr_m.group(2)
        if k == "domain":
            out["domain"] = v
        elif k == "path":
            out["path"] = v or "/"
        elif k == "secure":
            out["secure"] = True
        elif k in ("httponly", "http_only"):
            out["httponly"] = True
        elif k == "expires":
            # try several formats, fall back to parse as unix or iso
            try:
                # accept ISO-ish string
                dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
                out["expires"] = int(dt.timestamp())
            except Exception:
                try:
                    out["expires"] = int(v)
                except Exception:
                    out["expires"] = None
        else:
            # unknown attribute - ignore
            pass
    return out

def apply_cookie_strings(items: List[str]) -> None:
    """Accepts list of cookie strings (name=value or name=value; domain=...; path=...; secure; httponly)."""
    for s in items or []:
        try:
            c = _parse_cookie_attr_pairs(s)
            cookie = create_cookie(
                name=c["name"],
                value=c["value"],
                domain=c["domain"],
                path=c["path"],
                secure=c["secure"],
                rest={"HttpOnly": c["httponly"]},
                expires=c["expires"]
            )
            SESSION.cookies.set_cookie(cookie)
            log.info("Added cookie %s (domain=%s path=%s)", c["name"], c["domain"], c["path"])
        except Exception as exc:
            log.warning("Failed to parse cookie '%s' – %s", s, exc)

def load_cookie_file(path: str) -> None:
    """Load Netscape/Mozilla cookie jar into SESSION (existing function upgraded)."""
    jar = MozillaCookieJar()
    try:
        jar.load(path, ignore_discard=True, ignore_expires=False)
        for c in jar:
            # requests expects cookies as http.cookiejar.Cookie objects; set_cookie accepts them
            SESSION.cookies.set_cookie(create_cookie(
                name=c.name,
                value=c.value,
                domain=c.domain,
                path=c.path,
                secure=c.secure,
                rest={"HttpOnly": getattr(c, "rest", {}).get("HttpOnly", False)},
                expires=getattr(c, "expires", None)
            ))
        log.info("Loaded %d cookies from %s", len(jar), path)
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to load cookie file %s – %s", path, exc)

if __name__ == "__main__":
    args = parse_args()
    if args.max_pages < 1:
        log.error("--max-pages must be >= 1"); sys.exit(2)
    if args.threads < 1:
        log.error("--threads must be >= 1"); sys.exit(2)

    # NEW: configure authentication + probe access
    setup_auth(args)

    host = args.url
    root = make_root(args.url, args.destination)
    crawl_site(host, root, args.max_pages, args.threads, max_depth=args.max_depth)
    input("Press Enter to exit...")
