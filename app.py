import re
import html
import json
from io import BytesIO, StringIO
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, unquote
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
        "ASX-Institutional-Radar/4.0"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-AU,en;q=0.9",
    "Referer": "https://www.asx.com.au/",
}


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
            self.current_cell = []
            self.current_link = None
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
        m = re.search(p, headline, flags=re.I)
        if m:
            candidate = " ".join(m.group(1).split()).strip(" -:")
            if len(candidate) >= 2:
                return candidate
    return None


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
                "ASX filing": link,
                "Raw href": raw_link or "",
                "Source": source_label,
            }
        )
    return records


def get_disclosures():
    all_records, diagnostics = [], []
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

    seen, out = set(), []
    for r in all_records:
        key = (r["Ticker"], r["Date"], r["Time"], r["Headline"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out, diagnostics


def codes_from_tables(tables):
    for table in tables:
        cols = [str(c).strip().lower() for c in table.columns]
        candidate_indexes = [
            i for i, c in enumerate(cols)
            if ("asx" in c and "code" in c) or c in ("code", "ticker", "symbol")
        ]
        for i in candidate_indexes:
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
        tables = pd.read_html(StringIO(page_html))
        vals = codes_from_tables(tables)
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
        tables = pd.read_html(StringIO(r.text))
        vals = codes_from_tables(tables)
        diagnostics.append(("Wikipedia HTML", "OK", len(vals)))
        if vals:
            return vals, diagnostics
    except Exception as e:
        diagnostics.append(
            ("Wikipedia HTML", f"ERROR: {type(e).__name__}: {str(e)[:70]}", 0)
        )

    return set(), diagnostics


def looks_like_pdf(response):
    return response.content[:5] == b"%PDF-"


def clean_embedded_url(value):
    if not value:
        return None
    value = html.unescape(value)
    value = value.replace("\\/", "/")
    value = value.replace("\\u0026", "&")
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
            if not u:
                continue
            if u.lower().startswith(("javascript:", "mailto:", "#")):
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

            if looks_like_pdf(r):
                return r.url, r.content, trace

            ctype = r.headers.get("content-type", "").lower()
            if "html" in ctype or b"<html" in r.content[:5000].lower():
                text = r.text

                for candidate in extract_candidate_urls(text, r.url):
                    if candidate not in visited:
                        queue.append((candidate, depth + 1))

                fragments = re.findall(
                    r'(/asxpdf/[A-Za-z0-9_./?=&%-]+)',
                    text,
                    flags=re.I,
                )
                for frag in fragments:
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


def extract_text_pymupdf(pdf_bytes, max_pages=12):
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


def extract_text_pypdf(pdf_bytes, max_pages=12):
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
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_holder(s):
    if not s:
        return None
    s = " ".join(s.split())
    s = re.sub(r"\s+(ACN|ARSN|ABN)\b.*$", "", s, flags=re.I)
    s = re.sub(
        r"\s+(The holder|There was|Details of|Date of)\b.*$",
        "",
        s,
        flags=re.I,
    )
    s = s.strip(" :-|")
    if len(s) < 2 or len(s) > 180:
        return None
    return s


def extract_holder_name(text, headline_holder=None):
    patterns = [
        r"Details of substantial holder.{0,700}?\bName\s*[:\-]?\s*([^\n]{2,180})",
        r"Name of substantial holder\s*[:\-]?\s*([^\n]{2,180})",
        r"Substantial holder\s*[:\-]?\s*([^\n]{2,180})",
        r"\bName\s*[:\-]?\s*([A-Z][A-Za-z0-9&.,()'\/ \-]{2,160})(?=\n)",
    ]
    for p in patterns:
        m = re.search(p, text[:25000], flags=re.I | re.S)
        if m:
            name = clean_holder(m.group(1))
            if name and "substantial holder" not in name.lower():
                return name
    return clean_holder(headline_holder)


def num(s):
    if s is None:
        return None
    s = str(s).replace(",", "")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def votes(s):
    n = num(s)
    return int(n) if n is not None else None


def parse_voting_power(text):
    t = text[:35000]
    pv = pp = cv = cp = None

    prev_patterns = [
        r"Previous notice.{0,450}?([\d,]{3,})\s+(\d{1,3}(?:\.\d+)?)\s*%",
        r"Previous notice.{0,450}?(\d{1,3}(?:\.\d+)?)\s*%.{0,180}?([\d,]{3,})",
    ]
    pres_patterns = [
        r"Present notice.{0,450}?([\d,]{3,})\s+(\d{1,3}(?:\.\d+)?)\s*%",
        r"Present notice.{0,450}?(\d{1,3}(?:\.\d+)?)\s*%.{0,180}?([\d,]{3,})",
    ]

    for i, p in enumerate(prev_patterns):
        m = re.search(p, t, flags=re.I | re.S)
        if m:
            if i == 0:
                pv, pp = votes(m.group(1)), num(m.group(2))
            else:
                pp, pv = num(m.group(1)), votes(m.group(2))
            break

    for i, p in enumerate(pres_patterns):
        m = re.search(p, t, flags=re.I | re.S)
        if m:
            if i == 0:
                cv, cp = votes(m.group(1)), num(m.group(2))
            else:
                cp, cv = num(m.group(1)), votes(m.group(2))
            break

    if pp is None:
        m = re.search(
            r"Previous notice.{0,550}?(\d{1,3}(?:\.\d+)?)\s*%",
            t,
            re.I | re.S,
        )
        if m:
            pp = num(m.group(1))

    if cp is None:
        m = re.search(
            r"Present notice.{0,550}?(\d{1,3}(?:\.\d+)?)\s*%",
            t,
            re.I | re.S,
        )
        if m:
            cp = num(m.group(1))

    if cp is None:
        pairs = re.findall(
            r"([\d,]{4,})\s+(\d{1,3}(?:\.\d+)?)\s*%",
            t[:18000],
            flags=re.I,
        )
        plausible = [
            (votes(a), num(b))
            for a, b in pairs
            if 0 < (num(b) or 0) < 100
        ]
        substantial = [
            (a, b) for a, b in plausible
            if b is not None and b >= 5
        ]
        if substantial:
            cv, cp = substantial[0]
        elif plausible:
            cv, cp = plausible[0]

    return pv, pp, cv, cp


def infer_transaction_date(text):
    patterns = [
        r"became a substantial holder on\s*[:\-]?\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
        r"ceased to be a substantial holder on\s*[:\-]?\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
        r"date of change\s*[:\-]?\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
    ]
    for p in patterns:
        m = re.search(p, text[:20000], re.I)
        if m:
            return m.group(1)
    return None


@st.cache_data(ttl=86400, show_spinner=False)
def parse_filing(url, headline_holder=None):
    result = {
        "Holder": headline_holder,
        "Previous votes": None,
        "Previous %": None,
        "Current votes": None,
        "Current %": None,
        "Net votes": None,
        "% point change": None,
        "Transaction date": None,
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
        result["Debug preview"] = text[:2200]

        if len(text) < 80:
            result["Parse status"] = "Image/scanned PDF"
            return result

        holder = extract_holder_name(text, headline_holder)
        pv, pp, cv, cp = parse_voting_power(text)

        result["Holder"] = holder
        result["Previous votes"] = pv
        result["Previous %"] = pp
        result["Current votes"] = cv
        result["Current %"] = cp
        result["Transaction date"] = infer_transaction_date(text)

        if pv is not None and cv is not None:
            result["Net votes"] = cv - pv
        if pp is not None and cp is not None:
            result["% point change"] = round(cp - pp, 3)

        extracted = sum(x is not None for x in [holder, pv, pp, cv, cp])
        if extracted >= 4:
            result["Parse status"] = "Parsed"
        elif extracted >= 1:
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


def score(row):
    s = 0.0
    net = row.get("Net votes")
    pp = row.get("% point change")

    if pd.notna(net):
        s += 4 if net > 0 else -4 if net < 0 else 0
        if pd.notna(pp):
            s += max(-3.0, min(3.0, float(pp)))
    elif row.get("Disclosure") == "CROSSED 5%+":
        s += 1
    elif row.get("Disclosure") == "FELL BELOW 5%":
        s -= 1

    if row.get("ASX 200") is True:
        s += 0.5 if s > 0 else -0.5 if s < 0 else 0

    return round(s, 2)


st.title("📡 ASX Institutional Money Flow Radar")
st.caption("V4 — robust ASX document resolver + deep filing parser.")

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
    ticker_filter = st.text_input("Ticker filter", placeholder="e.g. MQG")
    show_changes = st.checkbox("Include change notices", value=True)

    st.divider()
    st.caption(
        "V4 crawls ASX wrapper pages instead of assuming the announcement link "
        "is already a direct PDF. Start with 5–10 filings while testing."
    )

if refresh:
    fetch_page.clear()
    get_asx200_codes.clear()
    resolve_pdf.clear()

records, announcement_diag = get_disclosures()
df = pd.DataFrame(records)

asx200, asx200_diag = get_asx200_codes()
if not df.empty:
    df["ASX 200"] = df["Ticker"].isin(asx200) if asx200 else None

if deep_scan and not df.empty:
    st.session_state["parsed_filings_v4"] = {}
    targets = df.head(max_deep).copy()
    parsed = {}

    progress = st.progress(0, text="Resolving ASX documents…")
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(
                parse_filing,
                row["ASX filing"],
                row.get("Headline holder"),
            ): idx
            for idx, row in targets.iterrows()
        }

        done = 0
        total = len(futures)
        for future in as_completed(futures):
            idx = futures[future]
            try:
                parsed[idx] = future.result()
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
    st.session_state["parsed_filings_v4"] = parsed

parsed = st.session_state.get("parsed_filings_v4", {})

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
                pool.submit(latest_price_aud, t): t
                for t in price_tickers
            }
            for f in as_completed(futures):
                prices[futures[f]] = f.result()

    df["Approx price A$"] = df["Ticker"].map(prices)
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
    df["Radar score"] = df.apply(score, axis=1)

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
    parsed_ok = (
        int(df["Parse status"].isin(["Parsed", "Partial parse"]).sum())
        if "Parse status" in df.columns
        else 0
    )

    inc = int(df["Direction"].eq("🟢 INCREASED").sum())
    dec = int(df["Direction"].eq("🔴 DECREASED").sum())

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Disclosures", len(df))
    c2.metric("Confirmed increases", inc)
    c3.metric("Confirmed decreases", dec)
    c4.metric("Deep parsed", parsed_ok)

    display_cols = [
        "Ticker",
        "ASX 200",
        "Holder",
        "Direction",
        "Previous %",
        "Current %",
        "% point change",
        "Net votes",
        "Approx price A$",
        "Approx net value A$",
        "Radar score",
        "Date",
        "Time",
        "ASX filing",
    ]
    display_cols = [c for c in display_cols if c in df.columns]

    st.dataframe(
        df.sort_values(
            ["Radar score", "Date", "Time"],
            ascending=[False, False, False],
        )[display_cols],
        use_container_width=True,
        hide_index=True,
        column_config={
            "ASX filing": st.column_config.LinkColumn(
                "ASX filing", display_text="Open ↗"
            ),
            "Previous %": st.column_config.NumberColumn(format="%.2f%%"),
            "Current %": st.column_config.NumberColumn(format="%.2f%%"),
            "% point change": st.column_config.NumberColumn(format="%.3f"),
            "Approx price A$": st.column_config.NumberColumn(format="$%.3f"),
            "Approx net value A$": st.column_config.NumberColumn(format="$%,.0f"),
            "Radar score": st.column_config.NumberColumn(format="%.2f"),
        },
    )

    if parsed:
        st.subheader("🔥 Confirmed accumulation")
        confirmed_up = df[df["Direction"] == "🟢 INCREASED"].sort_values(
            ["Radar score", "Approx net value A$"],
            ascending=[False, False],
        )
        if confirmed_up.empty:
            st.caption("No confirmed increases parsed yet.")
        else:
            st.dataframe(
                confirmed_up[
                    [c for c in [
                        "Ticker", "ASX 200", "Holder",
                        "Previous %", "Current %",
                        "% point change", "Net votes",
                        "Approx net value A$", "Radar score",
                        "ASX filing"
                    ] if c in confirmed_up.columns]
                ],
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
        confirmed_down = df[df["Direction"] == "🔴 DECREASED"].sort_values(
            ["Radar score", "Approx net value A$"],
            ascending=[True, True],
        )
        if confirmed_down.empty:
            st.caption("No confirmed reductions parsed yet.")
        else:
            st.dataframe(
                confirmed_down[
                    [c for c in [
                        "Ticker", "ASX 200", "Holder",
                        "Previous %", "Current %",
                        "% point change", "Net votes",
                        "Approx net value A$", "Radar score",
                        "ASX filing"
                    ] if c in confirmed_down.columns]
                ],
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
        threshold = df[
            df["Direction"].isin(
                ["🟢 CROSSED 5%+", "🔴 FELL BELOW 5%"]
            )
        ]
        st.caption(
            "Threshold events stay separate until the filing yields an actual old/new holding."
        )
        if not threshold.empty:
            st.dataframe(
                threshold[
                    [c for c in [
                        "Ticker", "ASX 200", "Holder",
                        "Direction", "Date", "Time", "ASX filing"
                    ] if c in threshold.columns]
                ],
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
        data=df.to_csv(index=False).encode("utf-8"),
        file_name="asx_institutional_money_flow_radar.csv",
        mime="text/csv",
        use_container_width=True,
    )


with st.expander("🛠 Diagnostics / resolver health"):
    st.write("### Announcement feeds")
    for label, status, count in announcement_diag:
        st.write(f"**{label}:** {status} — {count} notice(s)")

    st.write("### ASX 200 universe")
    st.write(f"**Codes loaded:** {len(asx200)}")
    for label, status, count in asx200_diag:
        st.write(f"**{label}:** {status} — {count} code(s)")

    if parsed and not df.empty and "Parse status" in df.columns:
        st.write("### Deep parser")
        diag_cols = [
            "Ticker",
            "Holder",
            "Parse status",
            "Text engine",
            "Text chars",
            "Resolved PDF",
            "ASX filing",
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
                ),
                "ASX filing": st.column_config.LinkColumn(
                    "ASX filing", display_text="ASX ↗"
                ),
            },
        )

        available_debug = [i for i in df.index if i in parsed]

        if available_debug:
            chosen = st.selectbox(
                "Inspect one resolver/parser trace",
                available_debug,
                format_func=lambda i: (
                    f"{df.loc[i, 'Ticker']} — {df.loc[i, 'Headline']}"
                ),
            )

            p = parsed.get(chosen, {})
            st.write("**Resolver trace**")
            st.code(
                p.get("Resolver trace") or "No resolver trace captured",
                language="json",
            )

            st.write("**Extracted PDF text preview**")
            st.code(
                p.get("Debug preview") or "No extractable PDF text",
                language=None,
            )

    st.caption(
        f"App refresh: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )


st.divider()
st.caption(
    "Research tool only. A substantial-holder threshold event is not automatically "
    "a same-day buy or sell. Confirmed direction requires parsed old/new holdings. "
    "Estimated dollar flow uses net voting-share change × a recent market price."
)
