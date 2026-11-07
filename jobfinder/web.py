"""web.py - text helpers, URL import, SSRF protection and the polite HTTP fetcher."""
from __future__ import annotations

import importlib.util
import ipaddress
import re
import socket
import threading
import time
import unicodedata
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse

import warnings

import requests
from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning, XMLParsedAsHTMLWarning

# lxml parses large career pages many times faster than Python's own parser
PARSER = "lxml" if importlib.util.find_spec("lxml") else "html.parser"

warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

# --------------------------------------------------------------------------- text helpers
def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower().replace("\u2019", "'").strip()


def _wb(word: str, text: str) -> bool:
    return bool(word) and re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", text) is not None


def html_to_text(html: str) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(html or "", PARSER).get_text(" ")).strip()


# --------------------------------------------------------------------------- URL import / SSRF
def normalize_url(url: str) -> str:
    p = urlparse(url.strip())
    host = (p.hostname or "").lower()
    try:
        port = p.port
    except ValueError:
        port = None
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
         if not k.lower().startswith("utm_") and k.lower() not in ("gclid", "fbclid")]
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunparse((p.scheme.lower(), netloc, path, "", urlencode(q), ""))


def syntactic_ok(url: str) -> bool:
    """Offline URL screening (no DNS): http(s) only, no localhost / private IP literals."""
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        if p.scheme not in ("http", "https") or not host or p.username or p.password:
            return False
        if host == "localhost" or host.endswith((".localhost", ".internal", ".local")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return "." in host
    except ValueError:
        return False


def check_url(url: str):
    """Full SSRF check including DNS resolution. Returns (ok, reason)."""
    if not syntactic_ok(url):
        return False, "blocked scheme/host"
    p = urlparse(url)
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False, "dns resolution failed"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            return False, f"resolves to non-public address {ip}"
    return True, ""


def _unescape(s: str) -> str:
    s = s.replace("\\/", "/")
    s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)
    s = s.replace("\\x26", "&").replace("&amp;", "&")
    return s.replace("\\", "").rstrip(".,;)")


def extract_urls(text: str) -> list[str]:
    """Extract http(s) URLs from JS arrays / quoted strings / hrefs. Never executes JavaScript."""
    found = []
    for m in re.finditer(r"""["'`](https?:\\?/\\?/[^"'`\s<>]+)["'`]""", text or "", re.I):
        found.append(_unescape(m.group(1)))
    try:
        for a in BeautifulSoup(text or "", PARSER).find_all("a", href=True):
            if a["href"].lower().startswith(("http://", "https://")):
                found.append(a["href"].strip())
    except Exception:  # malformed markup must never crash the import
        pass
    seen, out = set(), []
    for u in found:
        if not syntactic_ok(u):
            continue
        n = normalize_url(u)
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


# --------------------------------------------------------------------------- fetcher
class AccessError(Exception):
    pass


class RobotsRefused(AccessError):
    """robots.txt closes this address. For a website's own page it is a block; for one offer's page or PDF it only means
    that document is not opened (the offer is kept with what its list shows)."""


BOT = "JobSearchAssistant"  # the name robots.txt rules are checked against
# A browser-compatible User-Agent: many career sites serve an empty page to unknown clients. The bot name stays in it.
UA = f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36 {BOT}/2.0"
_ANTIBOT = re.compile(r"cf-challenge|are you a robot|verify you are human|checking your browser|perfdrive|captcha-delivery", re.I)


class Robots:
    """robots.txt as RFC 9309 reads it: the group for our bot name (else '*'), the LONGEST matching rule wins, Allow wins
    a tie, '*' and '$' wildcards. (urllib.robotparser takes the first rule: 'Disallow: /' then 'Allow: /careers' would
    wrongly refuse /careers.)"""

    def __init__(self, text=""):
        groups, agents, in_rules = {}, [], False  # two groups for the same agent are merged
        self.sitemaps = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", text)
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            k, v = (x.strip() for x in line.split(":", 1))
            k = k.lower()
            if k == "user-agent":
                if in_rules:
                    agents, in_rules = [], False
                agents.append(v.lower())
                groups.setdefault(v.lower(), [])
            elif k in ("allow", "disallow") and agents:
                in_rules = True
                for a in agents:
                    if v:
                        groups[a].append((k == "allow", v))
        bot = BOT.lower()
        self.rules = next((r for a, r in groups.items() if a != "*" and a in bot), groups.get("*", []))

    @staticmethod
    def _match(pattern, path):
        rx = re.escape(pattern).replace(r"\*", ".*")
        rx = rx[:-2] + "$" if rx.endswith(r"\$") else rx
        return re.match(rx, path) is not None

    def allowed(self, url) -> bool:
        p = urlparse(url)
        path = (p.path or "/") + (f"?{p.query}" if p.query else "")
        best = (-1, True)
        for allow, pat in self.rules:
            if self._match(pat, path) and (len(pat), allow) > best:
                best = (len(pat), allow)
        return best[1]


# The pause between two requests is kept PER SERVER for the whole application, not per website: websites read in parallel
# often share one platform's servers (every Workday tenant of a cluster, SmartRecruiters' API for Alten, Devoteam, EPSA),
# which would otherwise receive several requests a second and start refusing them (Workday: HTTP 403 for hours).
_PACE, _PACE_LOCK = {}, threading.Lock()
_SHARED_SERVERS = re.compile(r"(?:^|\.)(wd\d+\.myworkdayjobs\.com|wd\d+\.myworkdaysite\.com)$")


def _server(host: str) -> str:
    m = _SHARED_SERVERS.search(host or "")
    return m[1] if m else (host or "")


class Fetcher:
    def __init__(self, cfg, note=None):
        self.cfg, self.note = cfg, note or (lambda m: None)
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"})
        self.robots = {}

    def _wait(self, host):
        key, delay = _server(host), self.cfg["delay"]
        if delay <= 0:
            return
        with _PACE_LOCK:  # book the next free slot of this server, then wait for it outside the lock
            now = time.time()
            slot = max(now, _PACE.get(key, 0.0) + delay)
            _PACE[key] = slot
        if slot > now:
            time.sleep(slot - now)

    def robots_ok(self, url) -> bool:
        if not self.cfg["respect_robots"]:
            return True
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self.robots:
            try:
                r = self.request("GET", origin + "/robots.txt", _robots=True)
                self.robots[origin] = Robots(r.text if r.status_code == 200 else "")
            except Exception:
                self.robots[origin] = Robots()
        return self.robots[origin].allowed(url)

    def sitemaps(self, url) -> list:
        """The sitemaps a website declares in its robots.txt (else /sitemap.xml)."""
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        self.robots_ok(origin + "/")
        return self.robots[origin].sitemaps if origin in self.robots and self.robots[origin].sitemaps else [origin + "/sitemap.xml"]

    def get(self, url, **kw) -> requests.Response:
        return self.request("GET", url, **kw)

    def post_json(self, url, payload) -> requests.Response:
        return self.request("POST", url, json=payload, headers={"Accept": "application/json"})

    def request(self, method, url, _robots=False, **kw) -> requests.Response:
        if not _robots and not self.robots_ok(url):
            raise RobotsRefused("access_restricted: disallowed by robots.txt")
        for attempt in range(self.cfg["retries"] + 1):
            cur = url
            try:
                for _ in range(6):  # manual redirects, each hop re-validated (SSRF)
                    ok, why = check_url(cur)
                    if not ok and why == "dns resolution failed":  # network / DNS trouble: retried, never 'blocked'
                        raise requests.ConnectionError("the website's name could not be resolved (network or DNS problem)")
                    if not ok:
                        raise AccessError(f"access_restricted: {why}")
                    self._wait(urlparse(cur).hostname)
                    r = self.s.request(method, cur, timeout=self.cfg["timeout"], allow_redirects=False, **kw)
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                        cur, method = urljoin(cur, r.headers["Location"]), ("GET" if r.status_code == 303 else method)
                        continue
                    break
                else:
                    raise AccessError("too many redirects")
                # an anti-bot page is small; a normal page that merely loads a captcha script for its forms is not one
                challenge = r.status_code == 200 and len(r.text) < 30000 and _ANTIBOT.search(r.text[:30000])
                # a redirect to an error page ('/403.html', '/access-denied') is a refusal sent with status 200
                refused = cur != url and re.search(r"/(40[13]|forbidden|access[-_]?denied)(\.html?)?$", urlparse(cur).path, re.I)
                # 406 'Not Acceptable' on a plain page request is a firewall refusing automated visitors (inetum.com);
                # 999 is the non-standard 'request denied' of Workday and LinkedIn when they limit a reader
                if r.status_code in (401, 403, 406, 999) or challenge or refused:
                    raise AccessError(f"access_restricted: HTTP {r.status_code} / anti-bot page - inspect manually")
                if r.status_code in (429, 500, 502, 503, 504) and attempt < self.cfg["retries"]:
                    time.sleep(min(2 ** attempt * 2, 20))
                    continue
                if r.status_code >= 400:
                    raise ConnectionError(f"HTTP {r.status_code}")
                if "charset" not in (r.headers.get("content-type") or "").lower() and r.encoding in (None, "ISO-8859-1"):
                    r.encoding = r.apparent_encoding or "utf-8"  # no charset sent: guess from the bytes, not latin-1
                return r
            except requests.exceptions.SSLError as e:
                raise ConnectionError("source_unreachable: invalid HTTPS certificate on the website") from e
            except (requests.Timeout, requests.ConnectionError) as e:
                if attempt >= self.cfg["retries"]:
                    why = str(e) if "could not be resolved" in str(e) else type(e).__name__
                    raise ConnectionError(f"source_unreachable: {why}") from e
                time.sleep(min(2 ** attempt * 2, 20))
        raise ConnectionError("source_unreachable")


_DATA_REQUESTS = ("document", "xhr", "fetch")


def browser_guard(f):
    """Playwright route handler for every request a rendered page makes: public addresses only (SSRF), and the page's own
    DATA requests (documents, frames, XHR / fetch) are refused where robots.txt closes them, exactly as the app's own
    requests are. A job widget whose API host closes itself to robots (TalentLink: 'Disallow: /') therefore stays empty
    instead of being read through the browser. Images, styles, fonts and scripts are not data and are not checked."""
    def guard(route):
        req = route.request
        u = req.url
        if u.startswith(("data:", "blob:")):
            return route.continue_()
        if not syntactic_ok(u):
            return route.abort()
        if f is not None and req.resource_type in _DATA_REQUESTS:
            try:
                allowed = f.robots_ok(u)
            except Exception:  # robots.txt unreadable: treated as allowed, like the fetcher does
                allowed = True
            if not allowed:
                f.note(f"robots.txt closes {u[:100]}: not loaded by the browser")
                return route.abort()
        return route.continue_()
    return guard


_SYNTHETIC_PARAM = re.compile(r"([?&])(?:jbcard|jbid|jboffer)=[^&#]*&?")


def offer_link(url, title="", apply_url="") -> str:
    """The address that opens THIS offer. An offer with its own page: that page. An offer the app had to tell apart with an
    invented marker ('?jboffer=…': written as text on a lab page, a panel opened by a click, an id without address):
    its job-platform page when the data gave one, else the list page with a text anchor ('#:~:text=…') that current
    browsers open scrolled to the offer's title, highlighted."""
    url, apply_url = url or "", apply_url or ""
    if not _SYNTHETIC_PARAM.search(url):
        return url or apply_url
    if apply_url.startswith(("http://", "https://")) and not _SYNTHETIC_PARAM.search(apply_url):
        return apply_url
    base = _SYNTHETIC_PARAM.sub(lambda m: m[1], url).rstrip("?&").replace("?&", "?").split("#")[0]
    words = " ".join(re.sub(r"\s+", " ", title or "").strip().split()[:8])
    # in a text anchor '-', ',' and '&' have a meaning: they are always escaped
    return f"{base}#:~:text={quote(words, safe='').replace('-', '%2D')}" if words else base
