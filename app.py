import re
import html
import json
from io import BytesIO, StringIO
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, unquote, parse_qs, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
import streamlit as st
from pypdf import PdfReader

try:
    import fitz  # PyMuPDF
except Exception:
    fitz = None

st.set_page_config(
    page_title="ASX Institutional Money Flow Radar",
    page_icon="📡",
    layout="wide",
)

ASX_BASE = "https://www.asx.com.au"
TODAY_URL = f"{ASX_BASE}/asx/v2/statistics/todayAnns.do"
PREV_URL = f"{ASX_BASE}/asx/v2/statistics/prevBusDayAnns.do"
WIKI_API = (
    "https://en.wikipedia.org/w/api.php"
    "?action=parse&page=S%26P%2FASX_200&prop=text&format=json&origin=*"
)
WIKI_HTML = "https://en.wikipedia.org/wiki/S%26P/ASX_200"

TARGET_HEADLINES = (
    "becoming a substantial holder",
    "change in substantial holding",
    "ceasing to be a substantial holder",
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0 Mobile Safari/537.36 "
        "ASX-Institutional-Radar/5.0"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-AU,en;q=0.9",
    "Referer": "https://www.asx.com.au/",
}


# ============================================================
# Announcement discovery
# ============================================================

class ASXTableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_tr = False
        self.in_cell = False
        self.current_row = []
        self.current_cell = []
        self.current_link = None
        self.rows = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr":
            self.in_tr = True
            self.current_row = []
        elif tag in ("td", "th") and self.in_tr:
            self.in_cell = True
            self.current_cell = []
            self.current_link = None
        elif tag == "a" and self.in_cell:
            self.current_link = attrs.get("href")

    def handle_data(self, data):
        if self.in_cell:
            self.current_cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.in_cell:
            text = " ".join("".join(self.current_cell).split())
            self.current_row.append(
                {"text": html.unescape(text), "href": self.current_link}
            )
            self.in_cell = False
        elif tag == "tr" and self.in_tr:
            if self.current_row:
                self.rows.append(self.current_row)
            self.in_tr = False


@st.cache_data(ttl=120, show_spinner=False)
def fetch_page(url):
    r = requests.get(url, headers=HEADERS, timeout=35, allow_redirects=True)
    r.raise_for_status()
    return r.text


def clean_headline(text):
    return re.sub(
        r"\s+\d+\s+pages?\s+[\d.]+\s*(?:KB|MB)\s*$",
        "",
        text,
        flags=re.I,
    ).strip()


def basic_signal(headline):
    h = headline.lower()
    if "becoming a substantial holder" in h:
        return "CROSSED 5%+"
    if "ceasing to be a substantial holder" in h:
        return "FELL BELOW 5%"
    if "change in substantial holding" in h:
        return "CHANGE"
    return "OTHER"


def holder_from_headline(headline):
    patterns = [
        r"\bfrom\s+(.+)$",
        r"\bby\s+(.+)$",
        r"\s[-–—]\s*([A-Z][A-Za-z0-9&.,()' /-]{2,120})$",
    ]
    for p in patterns:
        m = re.search(p, headline, re.I)
        if m:
            val = " ".join(m.group(1).split()).strip(" -:")
            if len(val) >= 2:
                return val
    return None


def ids_id(url):
    try:
        return parse_qs(urlparse(url).query).get("idsId", [None])[0]
    except Exception:
        return None


def crossref_target(headline):
    # ASX sometimes shows one physical filing twice:
    #   DOW: "Becoming a substantial holder from MQG"
    #   MQG: "Becoming a substantial holder for DOW"
    m = re.search(r"\bfor\s+([A-Z0-9]{3})\s*$", headline, re.I)
    return m.group(1).upper() if m else None


def parse_asx_announcements(page_html, source_label):
    parser = ASXTableParser()
    parser.feed(page_html)
    records = []

    for row in parser.rows:
        texts = [c["text"] for c in row]
        joined = " | ".join(texts)

        ticker = None
        for txt in texts[:4]:
            candidate = txt.strip().upper()
            if re.fullmatch(r"[A-Z0-9]{3}", candidate):
                ticker = candidate
                break

        headline_cell = next(
            (
                c for c in row
                if any(term in c["text"].lower() for term in TARGET_HEADLINES)
            ),
            None,
        )
        if not ticker or not headline_cell:
            continue

        m = re.search(
            r"(\d{2}/\d{2}/\d{4})(?:\s+(\d{1,2}:\d{2}\s*(?:am|pm)))?",
            joined,
            re.I,
        )
        date_text = m.group(1) if m else ""
        time_text = m.group(2) if m and m.group(2) else ""

        raw_link = headline_cell.get("href")
        link = urljoin(ASX_BASE, raw_link) if raw_link else (
            f"{ASX_BASE}/asx/v2/statistics/announcements.do"
            f"?by=asxCode&asxCode={ticker}&timeframe=D&period=M6"
        )

        headline = clean_headline(headline_cell["text"])

        records.append(
            {
                "Ticker": ticker,
                "Date": date_text,
                "Time": time_text,
                "Disclosure": basic_signal(headline),
                "Headline": headline,
                "Headline holder": holder_from_headline(headline),
                "Crossref target": crossref_target(headline),
                "Announcement ID": ids_id(link),
                "ASX filing": link,
                "Raw href": raw_link or "",
                "Source": source_label,
            }
        )
    return records


def announcement_preference(row):
    # Prefer issuer-side row over holder-side cross-reference.
    # Example: DOW "from MQG" beats MQG "for DOW".
    score = 0
    if row.get("Crossref target"):
        score -= 10
    if row.get("Headline holder"):
        score += 2
    if " from " in row.get("Headline", "").lower():
        score += 3
    return score


def dedupe_cross_references(rows):
    by_key = {}
    no_id = []

    for r in rows:
        ann_id = r.get("Announcement ID")
        if not ann_id:
            no_id.append(r)
            continue

        old = by_key.get(ann_id)
        if old is None or announcement_preference(r) > announcement_preference(old):
            by_key[ann_id] = r

    combined = list(by_key.values()) + no_id

    seen = set()
    out = []
    for r in combined:
        key = (
            r["Ticker"],
            r["Date"],
            r["Time"],
            r["Headline"],
            r.get("Announcement ID"),
        )
        if key not in seen:
            seen.add(key)
            out.append(r)

    return out


def get_disclosures():
    all_records = []
    diagnostics = []

    for label, url in [
        ("Today", TODAY_URL),
        ("Previous trading day", PREV_URL),
    ]:
        try:
            rows = parse_asx_announcements(fetch_page(url), label)
            all_records.extend(rows)
            diagnostics.append((label, "OK", len(rows)))
        except Exception as e:
            diagnostics.append(
                (label, f"ERROR: {type(e).__name__}: {str(e)[:100]}", 0)
            )

    raw_count = len(all_records)
    deduped = dedupe_cross_references(all_records)
    return deduped, diagnostics, raw_count


# ============================================================
# ASX 200 universe
# ============================================================

def codes_from_tables(tables):
    for table in tables:
        cols = [str(c).strip().lower() for c in table.columns]
        candidates = [
            i for i, c in enumerate(cols)
            if ("asx" in c and "code" in c) or c in ("code", "ticker", "symbol")
        ]
        for i in candidates:
            vals = {
                str(x).strip().upper()
                for x in table[table.columns[i]].dropna()
                if re.fullmatch(r"[A-Z0-9]{3}", str(x).strip().upper())
            }
            if 180 <= len(vals) <= 220:
                return vals
    return set()


@st.cache_data(ttl=3600, show_spinner=False)
def get_asx200_codes():
    diagnostics = []

    try:
        r = requests.get(WIKI_API, headers=HEADERS, timeout=25)
        r.raise_for_status()
        page_html = r.json()["parse"]["text"]["*"]
        vals = codes_from_tables(pd.read_html(StringIO(page_html)))
        diagnostics.append(("Wikipedia API", "OK", len(vals)))
        if vals:
            return vals, diagnostics
    except Exception as e:
        diagnostics.append(
            ("Wikipedia API", f"ERROR: {type(e).__name__}: {str(e)[:70]}", 0)
        )

    try:
        r = requests.get(WIKI_HTML, headers=HEADERS, timeout=25)
        r.raise_for_status()
        vals = codes_from_tables(pd.read_html(StringIO(r.text)))
        diagnostics.append(("Wikipedia HTML", "OK", len(vals)))
        if vals:
            return vals, diagnostics
    except Exception as e:
        diagnostics.append(
            ("Wikipedia HTML", f"ERROR: {type(e).__name__}: {str(e)[:70]}", 0)
        )

    return set(), diagnostics


# ============================================================
# ASX PDF resolver
# ============================================================

def clean_embedded_url(value):
    if not value:
        return None
    value = html.unescape(value)
    value = value.replace("\\/", "/").replace("\\u0026", "&")
    value = value.strip(" '\"\t\r\n")
    try:
        value = unquote(value)
    except Exception:
        pass
    return value


def extract_candidate_urls(page_text, base_url):
    found = []

    patterns = [
        r'(?:href|src|data)\s*=\s*["\']([^"\']+)["\']',
        r'(?:window\.)?location(?:\.href)?\s*=\s*["\']([^"\']+)["\']',
        r'window\.open\(\s*["\']([^"\']+)["\']',
        r'content\s*=\s*["\'][^"\']*url\s*=\s*([^"\'>\s]+)',
        r'["\'](https?://[^"\']+)["\']',
        r'["\']([^"\']*asxpdf[^"\']*)["\']',
        r'["\']([^"\']*displayAnnouncement[^"\']*)["\']',
    ]

    for p in patterns:
        for raw in re.findall(p, page_text, flags=re.I):
            u = clean_embedded_url(raw)
            if not u or u.lower().startswith(("javascript:", "mailto:", "#")):
                continue
            full = urljoin(base_url, u)
            if full not in found:
                found.append(full)

    found.sort(
        key=lambda u: (
            0 if "asxpdf" in u.lower() else
            1 if ".pdf" in u.lower() else
            2 if "displayannouncement" in u.lower() else
            3
        )
    )
    return found[:40]


@st.cache_data(ttl=86400, show_spinner=False)
def resolve_pdf(url):
    queue = [(url, 0)]
    visited = set()
    trace = []

    while queue:
        current, depth = queue.pop(0)
        if current in visited or depth > 2:
            continue
        visited.add(current)

        try:
            r = requests.get(
                current,
                headers=HEADERS,
                timeout=45,
                allow_redirects=True,
            )
            trace.append(
                {
                    "url": current,
                    "final": r.url,
                    "status": r.status_code,
                    "type": r.headers.get("content-type", ""),
                    "bytes": len(r.content),
                }
            )
            r.raise_for_status()

            if r.content[:5] == b"%PDF-":
                return r.url, r.content, trace

            ctype = r.headers.get("content-type", "").lower()
            if "html" in ctype or b"<html" in r.content[:5000].lower():
                for candidate in extract_candidate_urls(r.text, r.url):
                    if candidate not in visited:
                        queue.append((candidate, depth + 1))

                for frag in re.findall(
                    r'(/asxpdf/[A-Za-z0-9_./?=&%-]+)',
                    r.text,
                    flags=re.I,
                ):
                    candidate = urljoin(ASX_BASE, clean_embedded_url(frag))
                    if candidate not in visited:
                        queue.append((candidate, depth + 1))

        except Exception as e:
            trace.append(
                {
                    "url": current,
                    "final": "",
                    "status": "ERR",
                    "type": type(e).__name__,
                    "bytes": 0,
                }
            )

    err = ValueError("No PDF found after crawling announcement wrapper(s)")
    err.trace = trace
    raise err


# ============================================================
# PDF extraction / parser
# ============================================================

def extract_text_pymupdf(pdf_bytes, max_pages=14):
    if fitz is None:
        return ""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    parts = []
    for i in range(min(len(doc), max_pages)):
        try:
            parts.append(doc[i].get_text("text"))
        except Exception:
            pass
    return "\n".join(parts)


def extract_text_pypdf(pdf_bytes, max_pages=14):
    reader = PdfReader(BytesIO(pdf_bytes))
    parts = []
    for page in reader.pages[:max_pages]:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            pass
    return "\n".join(parts)


def choose_best_text(pdf_bytes):
    a = extract_text_pymupdf(pdf_bytes)
    b = extract_text_pypdf(pdf_bytes)
    a_score = len(re.sub(r"\s+", "", a))
    b_score = len(re.sub(r"\s+", "", b))
    return (a, "PyMuPDF") if a_score >= b_score else (b, "pypdf")


def normalize_text(text):
    text = text.replace("\x00", " ")
    text = text.replace("â€™", "'").replace("â€˜", "'")
    text = text.replace("â€“", "-").replace("â€”", "-")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


FORM_LABELS = {
    "acn/arsn",
    "abn/acn/arsn",
    "acn/arsn (if applicable)",
    "abn/acn/arsn (if applicable)",
    "company name/scheme",
    "name",
    "details of substantial holder",
    "holder of relevant interest",
    "registered holder of securities",
    "person entitled to be registered as holder",
    "class of securities",
    "number of securities",
    "person's votes",
    "voting power",
}


def clean_holder(value):
    if not value:
        return None

    value = " ".join(str(value).split()).strip(" :-|;")
    low = value.lower()

    if low in FORM_LABELS:
        return None
    if low.startswith(("acn/arsn", "abn/acn/arsn")):
        return None
    if "if applicable" in low:
        return None
    if re.fullmatch(r"[\d ]{6,}", value):
        return None
    if len(value) < 2 or len(value) > 200:
        return None

    # Clean common continuations while retaining the lead institution.
    value = re.sub(
        r";?\s+and\s+its\s+controlled\s+bodies.*$",
        "",
        value,
        flags=re.I,
    )
    value = re.sub(
        r";?\s+and\s*$",
        "",
        value,
        flags=re.I,
    )
    value = re.sub(
        r"\s+(ABN|ACN|ARSN)\b.*$",
        "",
        value,
        flags=re.I,
    )

    return value.strip(" :-|;")


def extract_holder_name(text, headline_holder=None):
    lines = [x.strip() for x in text.splitlines() if x.strip()]

    # Highest-confidence path: locate section 1 and the Name label.
    for i, line in enumerate(lines[:250]):
        if re.search(r"details of substantial holder", line, re.I):
            window = lines[i:i + 35]
            for j, item in enumerate(window):
                if re.fullmatch(r"name\s*:?", item, re.I):
                    for candidate in window[j + 1:j + 8]:
                        cleaned = clean_holder(candidate)
                        if cleaned:
                            return cleaned

    # Macquarie / custom template path: Name then actual holder.
    for i, line in enumerate(lines[:220]):
        if re.fullmatch(r"name\s*:?", line, re.I):
            for candidate in lines[i + 1:i + 7]:
                cleaned = clean_holder(candidate)
                if cleaned:
                    return cleaned

    # Regex fallback.
    patterns = [
        r"Name of substantial holder\s*[:\-]?\s*([^\n]{2,180})",
        r"Substantial holder\s*[:\-]?\s*([^\n]{2,180})",
    ]
    for p in patterns:
        m = re.search(p, text[:25000], re.I)
        if m:
            cleaned = clean_holder(m.group(1))
            if cleaned:
                return cleaned

    # Headline shorthand remains useful if PDF field extraction fails.
    return clean_holder(headline_holder)


def form_type(text):
    m = re.search(r"\bForm\s+(603|604|605)\b", text[:12000], re.I)
    return f"Form {m.group(1)}" if m else None


def to_float(value):
    if value is None:
        return None
    value = str(value).replace(",", "")
    m = re.search(r"-?\d+(?:\.\d+)?", value)
    return float(m.group(0)) if m else None


def to_int(value):
    n = to_float(value)
    return int(n) if n is not None else None


def plausible_share_numbers(segment):
    out = []
    for m in re.finditer(r"(?<![\d.])(\d{1,3}(?:,\d{3})+|\d{5,})(?![\d.])", segment):
        value = to_int(m.group(1))
        if value is not None and 1000 <= value <= 50_000_000_000:
            out.append((m.start(), value))
    return out


def plausible_percentages(segment):
    out = []

    # Explicit percentages first.
    for m in re.finditer(r"(?<!\d)(\d{1,2}(?:\.\d{1,5})?)\s*%", segment):
        v = to_float(m.group(1))
        if v is not None and 0 < v < 100:
            out.append((m.start(), v))

    # Some extracted forms lose the percent sign. Only use decimal values
    # in the relevant voting-power segment, and only if no explicit % found.
    if not out:
        for m in re.finditer(r"(?<![\d.])(\d{1,2}\.\d{2,5})(?![\d.])", segment):
            v = to_float(m.group(1))
            if v is not None and 0 < v < 100:
                out.append((m.start(), v))

    return out


def voting_segment(text):
    low = text.lower()

    start_candidates = [
        low.find("previous and present voting power"),
        low.find("details of voting power"),
        low.find("voting power"),
    ]
    start_candidates = [x for x in start_candidates if x >= 0]
    start = min(start_candidates) if start_candidates else 0

    end_candidates = []
    for marker in [
        "changes in relevant interests",
        "details of relevant interests",
        "changes in association",
        "addresses",
        "signature",
    ]:
        pos = low.find(marker, start + 100)
        if pos >= 0:
            end_candidates.append(pos)

    end = min(end_candidates) if end_candidates else min(len(text), start + 6000)
    return text[start:end]


def pair_nearest_numbers_and_percentages(segment):
    nums = plausible_share_numbers(segment)
    pcts = plausible_percentages(segment)

    pairs = []
    used_nums = set()

    for ppos, pct in pcts:
        candidates = [
            (abs(npos - ppos), idx, npos, n)
            for idx, (npos, n) in enumerate(nums)
            if idx not in used_nums and abs(npos - ppos) <= 800
        ]
        if not candidates:
            continue

        _, idx, npos, number = min(candidates)
        used_nums.add(idx)
        pairs.append((min(npos, ppos), number, pct))

    pairs.sort(key=lambda x: x[0])
    return [(number, pct) for _, number, pct in pairs]


def parse_voting_power(text, ftype):
    segment = voting_segment(text)
    pairs = pair_nearest_numbers_and_percentages(segment)

    previous_votes = previous_pct = current_votes = current_pct = None

    if ftype == "Form 604":
        # Standard 604 should have Previous notice then Present notice.
        if len(pairs) >= 2:
            previous_votes, previous_pct = pairs[0]
            current_votes, current_pct = pairs[1]
        else:
            # Label-aware fallback.
            prev_match = re.search(
                r"Previous notice(.{0,1400})Present notice",
                segment,
                re.I | re.S,
            )
            pres_match = re.search(
                r"Present notice(.{0,1800})",
                segment,
                re.I | re.S,
            )
            if prev_match:
                p = pair_nearest_numbers_and_percentages(prev_match.group(1))
                if p:
                    previous_votes, previous_pct = p[0]
            if pres_match:
                p = pair_nearest_numbers_and_percentages(pres_match.group(1))
                if p:
                    current_votes, current_pct = p[0]

    elif ftype == "Form 603":
        # Initial substantial-holder notice has only the current position.
        substantial = [(n, p) for n, p in pairs if p >= 5]
        chosen = substantial[0] if substantial else (pairs[0] if pairs else None)
        if chosen:
            current_votes, current_pct = chosen

    elif ftype == "Form 605":
        # Ceasing notice may not include a clean present-voting-power table.
        # Preserve any last disclosed pair as previous position only.
        if pairs:
            previous_votes, previous_pct = pairs[0]

    else:
        if len(pairs) >= 2:
            previous_votes, previous_pct = pairs[0]
            current_votes, current_pct = pairs[1]
        elif len(pairs) == 1:
            current_votes, current_pct = pairs[0]

    return previous_votes, previous_pct, current_votes, current_pct


def infer_transaction_date(text):
    patterns = [
        r"became a substantial holder on\s*[:\-]?\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
        r"ceased to be a substantial holder on\s*[:\-]?\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
        r"date of change\s*[:\-]?\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
    ]
    for p in patterns:
        m = re.search(p, text[:22000], re.I)
        if m:
            return m.group(1)
    return None


def parser_confidence(holder, pv, pp, cv, cp, ftype):
    points = 0
    if holder:
        points += 2
    if ftype:
        points += 1
    if cv is not None:
        points += 1
    if cp is not None:
        points += 1
    if pv is not None:
        points += 1
    if pp is not None:
        points += 1

    if points >= 6:
        return "HIGH"
    if points >= 4:
        return "MEDIUM"
    if points >= 2:
        return "LOW"
    return "NONE"


@st.cache_data(ttl=86400, show_spinner=False)
def parse_filing(url, headline_holder=None):
    result = {
        "Form": None,
        "Holder": clean_holder(headline_holder),
        "Previous votes": None,
        "Previous %": None,
        "Current votes": None,
        "Current %": None,
        "Net votes": None,
        "% point change": None,
        "Transaction date": None,
        "Confidence": "NONE",
        "Parse status": "Not parsed",
        "Resolved PDF": None,
        "Text engine": None,
        "Text chars": 0,
        "Resolver trace": "",
        "Debug preview": "",
    }

    try:
        pdf_url, pdf_bytes, trace = resolve_pdf(url)
        result["Resolved PDF"] = pdf_url
        result["Resolver trace"] = json.dumps(trace, indent=2)[:7000]

        raw_text, engine = choose_best_text(pdf_bytes)
        text = normalize_text(raw_text)

        result["Text engine"] = engine
        result["Text chars"] = len(text)
        result["Debug preview"] = text[:3000]

        if len(text) < 80:
            result["Parse status"] = "Image/scanned PDF"
            return result

        ftype = form_type(text)
        holder = extract_holder_name(text, headline_holder)
        pv, pp, cv, cp = parse_voting_power(text, ftype)

        result["Form"] = ftype
        result["Holder"] = holder
        result["Previous votes"] = pv
        result["Previous %"] = pp
        result["Current votes"] = cv
        result["Current %"] = cp
        result["Transaction date"] = infer_transaction_date(text)

        if pv is not None and cv is not None:
            result["Net votes"] = cv - pv
        if pp is not None and cp is not None:
            result["% point change"] = round(cp - pp, 5)

        result["Confidence"] = parser_confidence(
            holder, pv, pp, cv, cp, ftype
        )

        populated = sum(
            x is not None for x in [holder, ftype, pv, pp, cv, cp]
        )

        if result["Confidence"] in ("HIGH", "MEDIUM"):
            result["Parse status"] = "Parsed"
        elif populated:
            result["Parse status"] = "Partial parse"
        else:
            result["Parse status"] = "No fields found"

    except Exception as e:
        trace = getattr(e, "trace", None)
        if trace:
            result["Resolver trace"] = json.dumps(trace, indent=2)[:7000]
        result["Parse status"] = (
            f"ERROR: {type(e).__name__}: {str(e)[:110]}"
        )

    return result


# ============================================================
# Price / scoring
# ============================================================

@st.cache_data(ttl=900, show_spinner=False)
def latest_price_aud(ticker):
    try:
        url = (
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{ticker}.AX?range=5d&interval=1d"
        )
        r = requests.get(url, headers=HEADERS, timeout=18)
        r.raise_for_status()
        data = r.json()["chart"]["result"][0]
        closes = data["indicators"]["quote"][0]["close"]
        valid = [x for x in closes if x is not None]
        return float(valid[-1]) if valid else None
    except Exception:
        return None


def direction(row):
    net = row.get("Net votes")
    disc = row.get("Disclosure")

    if pd.notna(net):
        if net > 0:
            return "🟢 INCREASED"
        if net < 0:
            return "🔴 DECREASED"
        return "⚪ UNCHANGED"

    if disc == "CROSSED 5%+":
        return "🟢 CROSSED 5%+"
    if disc == "FELL BELOW 5%":
        return "🔴 FELL BELOW 5%"
    return "🟡 VERIFY FILING"


def radar_score(row):
    score = 0.0
    net = row.get("Net votes")
    pp = row.get("% point change")
    confidence = row.get("Confidence")

    if pd.notna(net):
        score += 5 if net > 0 else -5 if net < 0 else 0

        if pd.notna(pp):
            score += max(-4.0, min(4.0, float(pp) * 1.5))

        if confidence == "HIGH":
            score *= 1.15
        elif confidence == "LOW":
            score *= 0.65

    elif row.get("Disclosure") == "CROSSED 5%+":
        score += 1
    elif row.get("Disclosure") == "FELL BELOW 5%":
        score -= 1

    if row.get("ASX 200") is True:
        score += 0.5 if score > 0 else -0.5 if score < 0 else 0

    return round(score, 2)


# ============================================================
# UI
# ============================================================

st.title("📡 ASX Institutional Money Flow Radar")
st.caption(
    "V5 — tuned against real ASX 603/604/605 exports from the live app."
)

with st.sidebar:
    st.header("Radar controls")

    refresh = st.button("🔄 Refresh announcements", use_container_width=True)
    deep_scan = st.button("🧠 Deep-analyse filings", use_container_width=True)

    max_deep = st.select_slider(
        "Filings to analyse",
        options=[5, 10, 20, 30, 50],
        value=10,
    )
    asx200_only = st.checkbox("ASX 200 only", value=False)
    ticker_filter = st.text_input("Ticker filter", placeholder="e.g. WGX")
    show_changes = st.checkbox("Include change notices", value=True)

    st.divider()
    st.caption(
        "V5 removes duplicate ASX cross-reference rows, rejects form labels as "
        "holder names, and treats Forms 603/604/605 differently."
    )

if refresh:
    fetch_page.clear()
    get_asx200_codes.clear()
    resolve_pdf.clear()
    parse_filing.clear()

records, announcement_diag, raw_announcement_count = get_disclosures()
df = pd.DataFrame(records)

asx200, asx200_diag = get_asx200_codes()

if not df.empty:
    df["ASX 200"] = df["Ticker"].isin(asx200) if asx200 else None

if deep_scan and not df.empty:
    st.session_state["parsed_filings_v5"] = {}
    targets = df.head(max_deep).copy()
    parsed = {}

    progress = st.progress(0, text="Resolving and parsing ASX filings…")

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(
                parse_filing,
                row["ASX filing"],
                row.get("Headline holder"),
            ): idx
            for idx, row in targets.iterrows()
        }

        total = len(futures)
        done = 0

        for f in as_completed(futures):
            idx = futures[f]
            try:
                parsed[idx] = f.result()
            except Exception as e:
                parsed[idx] = {
                    "Parse status": f"ERROR: {type(e).__name__}: {str(e)[:90]}"
                }

            done += 1
            progress.progress(
                done / total,
                text=f"Deep-analysed {done}/{total} filings",
            )

    progress.empty()
    st.session_state["parsed_filings_v5"] = parsed

parsed = st.session_state.get("parsed_filings_v5", {})

if not df.empty and parsed:
    parsed_df = pd.DataFrame.from_dict(parsed, orient="index")
    for col in parsed_df.columns:
        df.loc[parsed_df.index, col] = parsed_df[col]

    price_tickers = (
        sorted(set(df.loc[df["Net votes"].notna(), "Ticker"]))
        if "Net votes" in df.columns
        else []
    )

    prices = {}
    if price_tickers:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(latest_price_aud, ticker): ticker
                for ticker in price_tickers
            }
            for future in as_completed(futures):
                prices[futures[future]] = future.result()

    df["Approx price A$"] = df["Ticker"].map(prices)

    if "Net votes" in df.columns:
        df["Approx net value A$"] = df.apply(
            lambda r: (
                r["Net votes"] * r["Approx price A$"]
                if pd.notna(r.get("Net votes"))
                and pd.notna(r.get("Approx price A$"))
                else None
            ),
            axis=1,
        )

if not df.empty:
    df["Direction"] = df.apply(direction, axis=1)
    df["Radar score"] = df.apply(radar_score, axis=1)

    if not show_changes:
        df = df[df["Disclosure"] != "CHANGE"]

    if asx200_only:
        df = df[df["ASX 200"] == True]

    if ticker_filter.strip():
        needle = ticker_filter.strip().upper()
        df = df[df["Ticker"].str.contains(needle, regex=False)]

st.subheader("Institutional activity")

if df.empty:
    st.warning("No qualifying disclosures match the current filters.")
else:
    confirmed_up = int(df["Direction"].eq("🟢 INCREASED").sum())
    confirmed_down = int(df["Direction"].eq("🔴 DECREASED").sum())

    parsed_ok = (
        int(df["Parse status"].eq("Parsed").sum())
        if "Parse status" in df.columns
        else 0
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Unique disclosures", len(df))
    c2.metric("Confirmed increases", confirmed_up)
    c3.metric("Confirmed decreases", confirmed_down)
    c4.metric("Parsed", parsed_ok)

    display_cols = [
        "Ticker",
        "ASX 200",
        "Form",
        "Holder",
        "Direction",
        "Previous %",
        "Current %",
        "% point change",
        "Net votes",
        "Approx price A$",
        "Approx net value A$",
        "Confidence",
        "Radar score",
        "Date",
        "Time",
        "ASX filing",
    ]
    display_cols = [c for c in display_cols if c in df.columns]

    ranked = df.sort_values(
        ["Radar score", "Date", "Time"],
        ascending=[False, False, False],
    )

    st.dataframe(
        ranked[display_cols],
        use_container_width=True,
        hide_index=True,
        column_config={
            "ASX filing": st.column_config.LinkColumn(
                "ASX filing", display_text="Open ↗"
            ),
            "Previous %": st.column_config.NumberColumn(format="%.4f%%"),
            "Current %": st.column_config.NumberColumn(format="%.4f%%"),
            "% point change": st.column_config.NumberColumn(format="%.4f"),
            "Approx price A$": st.column_config.NumberColumn(format="$%.3f"),
            "Approx net value A$": st.column_config.NumberColumn(format="$%,.0f"),
            "Radar score": st.column_config.NumberColumn(format="%.2f"),
        },
    )

    if parsed:
        st.subheader("🔥 Confirmed accumulation")
        ups = ranked[ranked["Direction"] == "🟢 INCREASED"]

        if ups.empty:
            st.caption("No confirmed increases parsed in this batch yet.")
        else:
            up_cols = [
                "Ticker",
                "ASX 200",
                "Holder",
                "Previous %",
                "Current %",
                "% point change",
                "Net votes",
                "Approx net value A$",
                "Confidence",
                "Radar score",
                "ASX filing",
            ]
            up_cols = [c for c in up_cols if c in ups.columns]

            st.dataframe(
                ups[up_cols],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "ASX filing": st.column_config.LinkColumn(
                        "ASX filing", display_text="Open ↗"
                    ),
                    "Approx net value A$": st.column_config.NumberColumn(
                        format="$%,.0f"
                    ),
                },
            )

        st.subheader("🧊 Confirmed reductions")
        downs = ranked[ranked["Direction"] == "🔴 DECREASED"].sort_values(
            "Radar score"
        )

        if downs.empty:
            st.caption("No confirmed decreases parsed in this batch yet.")
        else:
            down_cols = [
                "Ticker",
                "ASX 200",
                "Holder",
                "Previous %",
                "Current %",
                "% point change",
                "Net votes",
                "Approx net value A$",
                "Confidence",
                "Radar score",
                "ASX filing",
            ]
            down_cols = [c for c in down_cols if c in downs.columns]

            st.dataframe(
                downs[down_cols],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "ASX filing": st.column_config.LinkColumn(
                        "ASX filing", display_text="Open ↗"
                    ),
                    "Approx net value A$": st.column_config.NumberColumn(
                        format="$%,.0f"
                    ),
                },
            )

        st.subheader("👀 Threshold watchlist")
        threshold = ranked[
            ranked["Direction"].isin(
                ["🟢 CROSSED 5%+", "🔴 FELL BELOW 5%"]
            )
        ]

        st.caption(
            "These remain threshold signals until the filing gives us enough "
            "old/new holding data to calculate a confirmed net move."
        )

        if not threshold.empty:
            threshold_cols = [
                "Ticker",
                "ASX 200",
                "Form",
                "Holder",
                "Direction",
                "Current %",
                "Current votes",
                "Confidence",
                "Date",
                "Time",
                "ASX filing",
            ]
            threshold_cols = [c for c in threshold_cols if c in threshold.columns]

            st.dataframe(
                threshold[threshold_cols],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "ASX filing": st.column_config.LinkColumn(
                        "ASX filing", display_text="Open ↗"
                    )
                },
            )

    st.download_button(
        "⬇️ Download current radar CSV",
        data=ranked.to_csv(index=False).encode("utf-8"),
        file_name="asx_institutional_money_flow_radar.csv",
        mime="text/csv",
        use_container_width=True,
    )


with st.expander("🛠 Diagnostics / parser health"):
    st.write("### Announcement feed")
    st.write(f"**Raw substantial-holder rows:** {raw_announcement_count}")
    st.write(f"**Unique physical filings after cross-reference dedupe:** {len(records)}")

    for label, status, count in announcement_diag:
        st.write(f"**{label}:** {status} — {count} row(s)")

    st.write("### ASX 200 universe")
    st.write(f"**Codes loaded:** {len(asx200)}")

    for label, status, count in asx200_diag:
        st.write(f"**{label}:** {status} — {count} code(s)")

    if parsed and not df.empty and "Parse status" in df.columns:
        st.write("### Deep parser")

        diag_cols = [
            "Ticker",
            "Form",
            "Holder",
            "Parse status",
            "Confidence",
            "Text engine",
            "Text chars",
            "Resolved PDF",
            "Announcement ID",
            "Raw href",
        ]
        diag_cols = [c for c in diag_cols if c in df.columns]

        st.dataframe(
            df[diag_cols],
            use_container_width=True,
            hide_index=True,
            column_config={
                "Resolved PDF": st.column_config.LinkColumn(
                    "Resolved PDF", display_text="PDF ↗"
                )
            },
        )

        debug_rows = [i for i in df.index if i in parsed]

        if debug_rows:
            chosen = st.selectbox(
                "Inspect one parsed filing",
                debug_rows,
                format_func=lambda i: (
                    f"{df.loc[i, 'Ticker']} — {df.loc[i, 'Headline']}"
                ),
            )

            payload = parsed.get(chosen, {})

            st.write("**Resolver trace**")
            st.code(
                payload.get("Resolver trace") or "No resolver trace captured",
                language="json",
            )

            st.write("**Extracted text preview**")
            st.code(
                payload.get("Debug preview") or "No extractable PDF text",
                language=None,
            )

    st.caption(
        f"App refresh: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )


st.divider()
st.caption(
    "Research tool only. A substantial-holder threshold event is not automatically "
    "a same-day buy/sell. Confirmed direction requires parsable previous/current "
    "holdings. Dollar flow is approximate: net voting-share change × recent price."
)
