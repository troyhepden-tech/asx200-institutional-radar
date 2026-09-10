import re
import html
from io import BytesIO, StringIO
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin
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
WIKI_ASX200 = "https://en.wikipedia.org/wiki/S%26P/ASX_200"

TARGET_HEADLINES = (
    "becoming a substantial holder",
    "change in substantial holding",
    "ceasing to be a substantial holder",
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0 Mobile Safari/537.36"
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
            self.current_row.append({"text": html.unescape(text), "href": self.current_link})
            self.in_cell = False
            self.current_cell = []
            self.current_link = None
        elif tag == "tr" and self.in_tr:
            if self.current_row:
                self.rows.append(self.current_row)
            self.in_tr = False


@st.cache_data(ttl=120, show_spinner=False)
def fetch_page(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
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
    m = re.search(r"\bfrom\s+(.+)$", headline, flags=re.I)
    return m.group(1).strip() if m else None


def parse_asx_announcements(page_html, source_label):
    parser = ASXTableParser()
    parser.feed(page_html)
    records = []

    for row in parser.rows:
        texts = [c["text"] for c in row]
        joined = " | ".join(texts)

        ticker = None
        for txt in texts[:3]:
            candidate = txt.strip().upper()
            if re.fullmatch(r"[A-Z0-9]{3}", candidate):
                ticker = candidate
                break

        headline_cell = next(
            (c for c in row if any(term in c["text"].lower() for term in TARGET_HEADLINES)),
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

        link = headline_cell.get("href")
        link = urljoin(ASX_BASE, link) if link else (
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
                "Source": source_label,
            }
        )
    return records


@st.cache_data(ttl=900, show_spinner=False)
def get_asx200_codes():
    try:
        r = requests.get(WIKI_ASX200, headers=HEADERS, timeout=20)
        r.raise_for_status()
        tables = pd.read_html(StringIO(r.text))
        for table in tables:
            cols = [str(c).lower() for c in table.columns]
            for i, c in enumerate(cols):
                if "asx" in c and "code" in c:
                    vals = {
                        str(x).strip().upper()
                        for x in table[table.columns[i]].dropna()
                        if re.fullmatch(r"[A-Z0-9]{3}", str(x).strip().upper())
                    }
                    if len(vals) >= 150:
                        return vals
    except Exception:
        pass
    return set()


def get_disclosures():
    all_records, diagnostics = [], []
    for label, url in [("Today", TODAY_URL), ("Previous trading day", PREV_URL)]:
        try:
            rows = parse_asx_announcements(fetch_page(url), label)
            all_records.extend(rows)
            diagnostics.append((label, "OK", len(rows)))
        except Exception as e:
            diagnostics.append((label, f"ERROR: {type(e).__name__}: {e}", 0))

    seen, out = set(), []
    for r in all_records:
        key = (r["Ticker"], r["Date"], r["Time"], r["Headline"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out, diagnostics


def resolve_pdf(url):
    r = requests.get(url, headers=HEADERS, timeout=40, allow_redirects=True)
    r.raise_for_status()

    ctype = r.headers.get("content-type", "").lower()
    if "pdf" in ctype or r.content.startswith(b"%PDF"):
        return r.url, r.content

    text = r.text
    candidates = re.findall(
        r'href=["\']([^"\']+(?:\.pdf|asxpdf[^"\']*))["\']',
        text,
        flags=re.I,
    )
    for href in candidates:
        pdf_url = urljoin(r.url, html.unescape(href))
        try:
            pr = requests.get(pdf_url, headers=HEADERS, timeout=40, allow_redirects=True)
            pr.raise_for_status()
            pct = pr.headers.get("content-type", "").lower()
            if "pdf" in pct or pr.content.startswith(b"%PDF"):
                return pr.url, pr.content
        except Exception:
            continue
    raise ValueError("No PDF found behind announcement link")


def extract_text_pymupdf(pdf_bytes, max_pages=10):
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


def extract_text_pypdf(pdf_bytes, max_pages=10):
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
    return a if len(re.sub(r"\s+", "", a)) >= len(re.sub(r"\s+", "", b)) else b


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
    s = re.sub(r"\s+(The holder|There was|Details of|Date of)\b.*$", "", s, flags=re.I)
    s = s.strip(" :-|")
    if len(s) < 2 or len(s) > 180:
        return None
    return s


def extract_holder_name(text, headline_holder=None):
    patterns = [
        r"Details of substantial holder.{0,600}?\bName\s*[:\-]?\s*([^\n]{2,180})",
        r"Name of substantial holder\s*[:\-]?\s*([^\n]{2,180})",
        r"Substantial holder\s*[:\-]?\s*([^\n]{2,180})",
        r"\bName\s*[:\-]?\s*([A-Z][A-Za-z0-9&.,()'\/ \-]{2,160})(?=\n)",
    ]
    for p in patterns:
        m = re.search(p, text[:20000], flags=re.I | re.S)
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
    t = text[:30000]

    pv = pp = cv = cp = None

    patterns_prev = [
        r"Previous notice.{0,350}?([\d,]{3,})\s+(\d{1,3}(?:\.\d+)?)\s*%",
        r"Previous notice.{0,350}?(\d{1,3}(?:\.\d+)?)\s*%.{0,160}?([\d,]{3,})",
    ]
    patterns_pres = [
        r"Present notice.{0,350}?([\d,]{3,})\s+(\d{1,3}(?:\.\d+)?)\s*%",
        r"Present notice.{0,350}?(\d{1,3}(?:\.\d+)?)\s*%.{0,160}?([\d,]{3,})",
    ]

    for i, p in enumerate(patterns_prev):
        m = re.search(p, t, flags=re.I | re.S)
        if m:
            if i == 0:
                pv, pp = votes(m.group(1)), num(m.group(2))
            else:
                pp, pv = num(m.group(1)), votes(m.group(2))
            break

    for i, p in enumerate(patterns_pres):
        m = re.search(p, t, flags=re.I | re.S)
        if m:
            if i == 0:
                cv, cp = votes(m.group(1)), num(m.group(2))
            else:
                cp, cv = num(m.group(1)), votes(m.group(2))
            break

    if pp is None:
        m = re.search(r"Previous notice.{0,450}?(\d{1,3}(?:\.\d+)?)\s*%", t, re.I | re.S)
        if m:
            pp = num(m.group(1))
    if cp is None:
        m = re.search(r"Present notice.{0,450}?(\d{1,3}(?:\.\d+)?)\s*%", t, re.I | re.S)
        if m:
            cp = num(m.group(1))

    if cp is None:
        candidates = re.findall(
            r"([\d,]{4,})\s+(\d{1,3}(?:\.\d+)?)\s*%",
            t[:16000],
            flags=re.I,
        )
        plausible = [(votes(a), num(b)) for a, b in candidates if 0 < (num(b) or 0) < 100]
        strong = [(a, b) for a, b in plausible if b is not None and b >= 5]
        if strong:
            cv, cp = strong[0]
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
        m = re.search(p, text[:18000], re.I)
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
        "Text chars": 0,
        "Debug preview": "",
    }

    try:
        pdf_url, pdf_bytes = resolve_pdf(url)
        result["Resolved PDF"] = pdf_url

        text = normalize_text(choose_best_text(pdf_bytes))
        result["Text chars"] = len(text)
        result["Debug preview"] = text[:1800]

        if len(text) < 80:
            result["Parse status"] = "Likely scanned/image PDF"
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
        return result

    except Exception as e:
        result["Parse status"] = f"ERROR: {type(e).__name__}: {str(e)[:90]}"
        return result


@st.cache_data(ttl=900, show_spinner=False)
def latest_price_aud(ticker):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}.AX?range=5d&interval=1d"
        r = requests.get(url, headers=HEADERS, timeout=15)
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
    disc = row.get("Disclosure")

    if pd.notna(net):
        s += 4 if net > 0 else -4 if net < 0 else 0
    elif disc == "CROSSED 5%+":
        s += 2
    elif disc == "FELL BELOW 5%":
        s -= 2

    if pd.notna(pp):
        s += max(-3.0, min(3.0, float(pp)))

    if row.get("ASX 200") is True:
        s += 0.5 if s > 0 else -0.5 if s < 0 else 0
    return round(s, 2)


st.title("📡 ASX Institutional Money Flow Radar")
st.caption("Official ASX announcements + substantial-holder filing analysis.")

with st.sidebar:
    st.header("Radar controls")
    refresh = st.button("🔄 Refresh announcements", use_container_width=True)
    deep_scan = st.button("🧠 Deep-analyse filings", use_container_width=True)

    max_deep = st.select_slider(
        "Filings to analyse", options=[10, 20, 30, 50], value=20
    )
    asx200_only = st.checkbox("ASX 200 only", value=False)
    ticker_filter = st.text_input("Ticker filter", placeholder="e.g. ZIP")
    show_changes = st.checkbox("Include change notices", value=True)

    st.divider()
    st.caption(
        "V3 uses two PDF text engines and follows ASX redirects/intermediary pages. "
        "Diagnostics below show exactly what the parser can and cannot read."
    )

if refresh:
    fetch_page.clear()
    get_asx200_codes.clear()

records, diagnostics = get_disclosures()
df = pd.DataFrame(records)
asx200 = get_asx200_codes()

if not df.empty:
    df["ASX 200"] = df["Ticker"].isin(asx200) if asx200 else None

if deep_scan and not df.empty:
    st.session_state["parsed_filings_v3"] = {}
    targets = df.head(max_deep).copy()
    parsed = {}

    progress = st.progress(0, text="Opening ASX PDFs…")
    with ThreadPoolExecutor(max_workers=5) as pool:
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
                parsed[idx] = {"Parse status": f"ERROR: {type(e).__name__}"}
            done += 1
            progress.progress(done / total, text=f"Analysed {done}/{total} filings")

    progress.empty()
    st.session_state["parsed_filings_v3"] = parsed

parsed = st.session_state.get("parsed_filings_v3", {})

if not df.empty and parsed:
    pdf_df = pd.DataFrame.from_dict(parsed, orient="index")
    for col in pdf_df.columns:
        df.loc[pdf_df.index, col] = pdf_df[col]

    price_tickers = (
        sorted(set(df.loc[df["Net votes"].notna(), "Ticker"]))
        if "Net votes" in df.columns
        else []
    )
    prices = {}
    if price_tickers:
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(latest_price_aud, t): t for t in price_tickers}
            for f in as_completed(futures):
                prices[futures[f]] = f.result()

    df["Approx price A$"] = df["Ticker"].map(prices)
    df["Approx net value A$"] = df.apply(
        lambda r: (
            r["Net votes"] * r["Approx price A$"]
            if pd.notna(r.get("Net votes")) and pd.notna(r.get("Approx price A$"))
            else None
        ),
        axis=1,
    )

df["Direction"] = df.apply(direction, axis=1) if not df.empty else None
df["Radar score"] = df.apply(score, axis=1) if not df.empty else None

if not df.empty:
    if not show_changes:
        df = df[df["Disclosure"] != "CHANGE"]
    if asx200_only:
        df = df[df["ASX 200"] == True]
    if ticker_filter.strip():
        df = df[df["Ticker"].str.contains(ticker_filter.strip().upper(), regex=False)]

st.subheader("Institutional activity")

if df.empty:
    st.warning("No qualifying disclosures match the current filters.")
else:
    deep_ok = (
        int(df["Parse status"].isin(["Parsed", "Partial parse"]).sum())
        if "Parse status" in df.columns
        else 0
    )
    scanned = (
        int(df["Parse status"].eq("Likely scanned/image PDF").sum())
        if "Parse status" in df.columns
        else 0
    )
    increased = int(df["Direction"].str.contains("INCREASED|CROSSED", regex=True).sum())
    decreased = int(df["Direction"].str.contains("DECREASED|FELL", regex=True).sum())

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Disclosures", len(df))
    c2.metric("Bullish / crossed", increased)
    c3.metric("Bearish / fell", decreased)
    c4.metric("Deep parsed", deep_ok)

    if scanned:
        st.info(
            f"{scanned} filing(s) appear to be scanned/image PDFs. "
            "Those need OCR before holder/voting-power fields can be extracted."
        )

    df = df.sort_values(["Radar score", "Date", "Time"], ascending=[False, False, False])

    cols = [
        "Ticker", "ASX 200", "Holder", "Direction",
        "Previous %", "Current %", "% point change",
        "Net votes", "Approx price A$", "Approx net value A$",
        "Radar score", "Date", "Time", "ASX filing",
    ]
    cols = [c for c in cols if c in df.columns]

    st.dataframe(
        df[cols],
        use_container_width=True,
        hide_index=True,
        column_config={
            "ASX filing": st.column_config.LinkColumn("ASX filing", display_text="Open ↗"),
            "Previous %": st.column_config.NumberColumn(format="%.2f%%"),
            "Current %": st.column_config.NumberColumn(format="%.2f%%"),
            "% point change": st.column_config.NumberColumn(format="%.3f"),
            "Approx price A$": st.column_config.NumberColumn(format="$%.3f"),
            "Approx net value A$": st.column_config.NumberColumn(format="$%,.0f"),
            "Radar score": st.column_config.NumberColumn(format="%.2f"),
        },
    )

    if parsed:
        st.subheader("🔥 Strongest accumulation signals")
        pos = df[df["Radar score"] > 0].head(10)
        if not pos.empty:
            st.dataframe(
                pos[[c for c in [
                    "Ticker", "ASX 200", "Holder", "Direction",
                    "Previous %", "Current %", "% point change",
                    "Net votes", "Approx net value A$", "Radar score", "ASX filing"
                ] if c in pos.columns]],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "ASX filing": st.column_config.LinkColumn("ASX filing", display_text="Open ↗"),
                    "Approx net value A$": st.column_config.NumberColumn(format="$%,.0f"),
                },
            )

        st.subheader("🧊 Strongest reduction signals")
        neg = df[df["Radar score"] < 0].sort_values("Radar score").head(10)
        if not neg.empty:
            st.dataframe(
                neg[[c for c in [
                    "Ticker", "ASX 200", "Holder", "Direction",
                    "Previous %", "Current %", "% point change",
                    "Net votes", "Approx net value A$", "Radar score", "ASX filing"
                ] if c in neg.columns]],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "ASX filing": st.column_config.LinkColumn("ASX filing", display_text="Open ↗"),
                    "Approx net value A$": st.column_config.NumberColumn(format="$%,.0f"),
                },
            )

    st.download_button(
        "⬇️ Download current radar CSV",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name="asx_institutional_money_flow_radar.csv",
        mime="text/csv",
        use_container_width=True,
    )

with st.expander("🛠 Diagnostics / parser health"):
    for label, status, count in diagnostics:
        st.write(f"**{label}:** {status} — {count} substantial-holder notice(s)")

    st.write(f"**ASX 200 codes loaded:** {len(asx200)}")

    if parsed and not df.empty and "Parse status" in df.columns:
        diag_cols = [
            "Ticker", "Holder", "Parse status", "Text chars",
            "Resolved PDF", "ASX filing"
        ]
        diag_cols = [c for c in diag_cols if c in df.columns]
        st.dataframe(
            df[diag_cols],
            use_container_width=True,
            hide_index=True,
            column_config={
                "Resolved PDF": st.column_config.LinkColumn("Resolved PDF", display_text="PDF ↗"),
                "ASX filing": st.column_config.LinkColumn("ASX filing", display_text="ASX ↗"),
            },
        )

        debug_choices = [
            str(i) for i in df.index
            if i in parsed and parsed[i].get("Debug preview")
        ]
        if debug_choices:
            chosen = st.selectbox(
                "Show extracted-text preview for parser tuning",
                debug_choices,
                format_func=lambda i: f"{df.loc[int(i), 'Ticker']} — row {i}",
            )
            preview = parsed[int(chosen)].get("Debug preview", "")
            st.code(preview or "No extractable text", language=None)

    st.caption(f"App refresh: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

st.divider()
st.caption(
    "Research tool only. Threshold notices are not automatically same-day buys/sells. "
    "Dollar movement is an estimate based on extracted net voting shares × recent market price. "
    "Always verify the underlying ASX filing."
)
