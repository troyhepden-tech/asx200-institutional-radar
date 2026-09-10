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
    """Extract rows/cells/links from the old-style ASX announcement tables."""
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
    r = requests.get(url, headers=HEADERS, timeout=25)
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
            (
                cell
                for cell in row
                if any(term in cell["text"].lower() for term in TARGET_HEADLINES)
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

        link = headline_cell.get("href")
        if link:
            link = urljoin(ASX_BASE, link)
        else:
            link = (
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
                "ASX filing": link,
                "Source": source_label,
            }
        )
    return records


@st.cache_data(ttl=900, show_spinner=False)
def get_asx200_codes():
    """Dynamic constituent check. If Wikipedia is unavailable, return empty set."""
    try:
        r = requests.get(WIKI_ASX200, headers=HEADERS, timeout=20)
        r.raise_for_status()
        tables = pd.read_html(StringIO(r.text))
        for table in tables:
            cols = [str(c).lower() for c in table.columns]
            if any("asx" in c and "code" in c for c in cols):
                code_col = table.columns[
                    next(i for i, c in enumerate(cols) if "asx" in c and "code" in c)
                ]
                codes = {
                    str(x).strip().upper()
                    for x in table[code_col].dropna()
                    if re.fullmatch(r"[A-Z0-9]{3}", str(x).strip().upper())
                }
                if len(codes) >= 150:
                    return codes
    except Exception:
        pass
    return set()


def get_disclosures():
    all_records = []
    diagnostics = []
    for label, url in [
        ("Today", TODAY_URL),
        ("Previous trading day", PREV_URL),
    ]:
        try:
            page = fetch_page(url)
            rows = parse_asx_announcements(page, label)
            all_records.extend(rows)
            diagnostics.append((label, "OK", len(rows)))
        except Exception as e:
            diagnostics.append(
                (label, f"ERROR: {type(e).__name__}: {e}", 0)
            )

    seen = set()
    deduped = []
    for r in all_records:
        key = (r["Ticker"], r["Date"], r["Time"], r["Headline"])
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    return deduped, diagnostics


def _clean_num(s):
    if s is None:
        return None
    s = re.sub(r"[^\d.\-]", "", str(s))
    try:
        return float(s) if s else None
    except Exception:
        return None


def _clean_votes(s):
    n = _clean_num(s)
    return int(n) if n is not None else None


def _extract_percent(s):
    if s is None:
        return None
    m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", s)
    return float(m.group(1)) if m else None


def normalize_pdf_text(text):
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_holder_name(text):
    # Forms 603/604/605 commonly put "Name" directly under
    # "Details of substantial holder".
    patterns = [
        r"Details of substantial holder.*?\bName\s+([^\n]{2,180})",
        r"\bName\s+([A-Z][^\n]{2,180}?)(?=\n(?:ACN|ARSN|The holder|There was|Details|Date)\b)",
    ]
    for p in patterns:
        m = re.search(p, text[:12000], flags=re.I | re.S)
        if m:
            name = " ".join(m.group(1).split())
            name = re.sub(r"\s+(ACN|ARSN)\b.*$", "", name, flags=re.I)
            if 2 <= len(name) <= 180:
                return name.strip(" :-")
    return None


def parse_voting_power(text):
    """
    Heuristic parser for the standard 604 table:
    Previous notice | person's votes | voting power
    Present notice  | person's votes | voting power
    """
    t = text[:18000]

    previous_votes = previous_pct = present_votes = present_pct = None

    # Best case: both rows survive PDF text extraction in readable order.
    prev = re.search(
        r"Previous notice.{0,220}?([\d,]{4,})\s+(\d{1,3}(?:\.\d+)?)\s*%",
        t,
        flags=re.I | re.S,
    )
    pres = re.search(
        r"Present notice.{0,220}?([\d,]{4,})\s+(\d{1,3}(?:\.\d+)?)\s*%",
        t,
        flags=re.I | re.S,
    )

    if prev:
        previous_votes = _clean_votes(prev.group(1))
        previous_pct = _clean_num(prev.group(2))
    if pres:
        present_votes = _clean_votes(pres.group(1))
        present_pct = _clean_num(pres.group(2))

    # Some PDFs put percentages before votes. Try a second shape.
    if previous_pct is None or previous_votes is None:
        prev2 = re.search(
            r"Previous notice.{0,220}?(\d{1,3}(?:\.\d+)?)\s*%.{0,120}?([\d,]{4,})",
            t,
            flags=re.I | re.S,
        )
        if prev2:
            previous_pct = previous_pct or _clean_num(prev2.group(1))
            previous_votes = previous_votes or _clean_votes(prev2.group(2))

    if present_pct is None or present_votes is None:
        pres2 = re.search(
            r"Present notice.{0,220}?(\d{1,3}(?:\.\d+)?)\s*%.{0,120}?([\d,]{4,})",
            t,
            flags=re.I | re.S,
        )
        if pres2:
            present_pct = present_pct or _clean_num(pres2.group(1))
            present_votes = present_votes or _clean_votes(pres2.group(2))

    # Form 603 only has a current voting-power row. Capture a plausible
    # shares + percent pair near the "Voting power" heading.
    if present_pct is None:
        vp = re.search(
            r"Voting power.{0,700}?([\d,]{4,})\s+(\d{1,3}(?:\.\d+)?)\s*%",
            t,
            flags=re.I | re.S,
        )
        if vp:
            present_votes = present_votes or _clean_votes(vp.group(1))
            present_pct = _clean_num(vp.group(2))

    return previous_votes, previous_pct, present_votes, present_pct


def infer_transaction_date(text):
    patterns = [
        r"(?:date of change|became a substantial holder on|ceased to be a substantial holder on)\s+(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
    ]
    for p in patterns:
        m = re.search(p, text[:12000], flags=re.I)
        if m:
            return m.group(1)
    return None


@st.cache_data(ttl=86400, show_spinner=False)
def parse_filing(url):
    result = {
        "Holder": None,
        "Previous votes": None,
        "Previous %": None,
        "Current votes": None,
        "Current %": None,
        "Net votes": None,
        "% point change": None,
        "Transaction date": None,
        "Parse status": "Not parsed",
    }

    try:
        r = requests.get(url, headers=HEADERS, timeout=35)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "").lower()

        if "pdf" not in ctype and not r.content.startswith(b"%PDF"):
            result["Parse status"] = "Link is not a PDF"
            return result

        reader = PdfReader(BytesIO(r.content))
        texts = []
        # Most standard Form 603/604/605 fields are on the first few pages.
        for page in reader.pages[:6]:
            try:
                texts.append(page.extract_text() or "")
            except Exception:
                pass
        text = normalize_pdf_text("\n".join(texts))

        if len(text) < 80:
            result["Parse status"] = "PDF text unavailable"
            return result

        holder = extract_holder_name(text)
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

        extracted = sum(
            x is not None for x in [holder, pv, pp, cv, cp]
        )
        result["Parse status"] = (
            "Parsed" if extracted >= 3 else "Partial parse" if extracted else "No fields found"
        )
        return result

    except Exception as e:
        result["Parse status"] = f"ERROR: {type(e).__name__}"
        return result


@st.cache_data(ttl=900, show_spinner=False)
def latest_price_aud(ticker):
    """Approximate latest market price from Yahoo's public chart endpoint."""
    try:
        url = (
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{ticker}.AX?range=5d&interval=1d"
        )
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        payload = r.json()["chart"]["result"][0]
        closes = payload["indicators"]["quote"][0]["close"]
        valid = [x for x in closes if x is not None]
        return float(valid[-1]) if valid else None
    except Exception:
        return None


def direction_for(row):
    net = row.get("Net votes")
    disclosure = row.get("Disclosure")

    if pd.notna(net):
        if net > 0:
            return "🟢 INCREASED"
        if net < 0:
            return "🔴 DECREASED"
        return "⚪ UNCHANGED"

    if disclosure == "CROSSED 5%+":
        return "🟢 CROSSED 5%+"
    if disclosure == "FELL BELOW 5%":
        return "🔴 FELL BELOW 5%"
    return "🟡 VERIFY FILING"


def radar_score(row):
    score = 0.0
    net = row.get("Net votes")
    pp = row.get("% point change")
    disclosure = row.get("Disclosure")
    is200 = row.get("ASX 200")

    if pd.notna(net):
        score += 4 if net > 0 else -4 if net < 0 else 0
    elif disclosure == "CROSSED 5%+":
        score += 2
    elif disclosure == "FELL BELOW 5%":
        score -= 2

    if pd.notna(pp):
        score += max(-3, min(3, float(pp)))

    if is200 is True:
        score += 0.5 if score > 0 else -0.5 if score < 0 else 0

    return round(score, 2)


st.title("📡 ASX Institutional Money Flow Radar")
st.caption(
    "Direct ASX substantial-holder disclosures + deep filing analysis."
)

with st.sidebar:
    st.header("Radar controls")
    refresh = st.button("🔄 Refresh ASX announcements", use_container_width=True)
    deep_scan = st.button("🧠 Deep-analyse filings", use_container_width=True)

    max_deep = st.select_slider(
        "Filings to deep-analyse",
        options=[10, 20, 30, 50],
        value=30,
    )
    asx200_only = st.checkbox("ASX 200 only", value=False)
    ticker_filter = st.text_input("Ticker filter", placeholder="e.g. ZIP")
    show_changes = st.checkbox("Include change notices", value=True)

    st.divider()
    st.caption(
        "Deep analysis opens each ASX PDF and attempts to extract the holder, "
        "old/new voting power and share-count movement."
    )

if refresh:
    fetch_page.clear()
    get_asx200_codes.clear()

records, diagnostics = get_disclosures()
df = pd.DataFrame(records)

asx200_codes = get_asx200_codes()
if not df.empty:
    if asx200_codes:
        df["ASX 200"] = df["Ticker"].isin(asx200_codes)
    else:
        df["ASX 200"] = None

if deep_scan and not df.empty:
    targets = df.head(max_deep).copy()
    parsed_by_index = {}

    progress = st.progress(0, text="Opening ASX filings…")
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(parse_filing, row["ASX filing"]): idx
            for idx, row in targets.iterrows()
        }
        done = 0
        for future in as_completed(futures):
            idx = futures[future]
            try:
                parsed_by_index[idx] = future.result()
            except Exception as e:
                parsed_by_index[idx] = {"Parse status": f"ERROR: {type(e).__name__}"}
            done += 1
            progress.progress(done / len(futures), text=f"Analysed {done}/{len(futures)} filings")

    progress.empty()
    st.session_state["parsed_filings"] = parsed_by_index

parsed_by_index = st.session_state.get("parsed_filings", {})
if not df.empty and parsed_by_index:
    parsed_df = pd.DataFrame.from_dict(parsed_by_index, orient="index")
    for col in parsed_df.columns:
        df.loc[parsed_df.index, col] = parsed_df[col]

    # Pull prices only where a share-count move was actually extracted.
    price_tickers = sorted(
        set(
            df.loc[df["Net votes"].notna(), "Ticker"].tolist()
            if "Net votes" in df.columns
            else []
        )
    )
    prices = {}
    if price_tickers:
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(latest_price_aud, t): t for t in price_tickers}
            for f in as_completed(futures):
                prices[futures[f]] = f.result()

    df["Approx price A$"] = df["Ticker"].map(prices)
    if "Net votes" in df.columns:
        df["Approx net value A$"] = df.apply(
            lambda r: (
                r["Net votes"] * r["Approx price A$"]
                if pd.notna(r.get("Net votes")) and pd.notna(r.get("Approx price A$"))
                else None
            ),
            axis=1,
        )

    df["Direction"] = df.apply(direction_for, axis=1)
    df["Radar score"] = df.apply(radar_score, axis=1)
else:
    if not df.empty:
        df["Direction"] = df.apply(direction_for, axis=1)
        df["Radar score"] = df.apply(radar_score, axis=1)

if not df.empty:
    if not show_changes:
        df = df[df["Disclosure"] != "CHANGE"]
    if asx200_only:
        df = df[df["ASX 200"] == True]
    if ticker_filter.strip():
        df = df[
            df["Ticker"].str.contains(ticker_filter.strip().upper(), regex=False)
        ]

st.subheader("Institutional activity")

if df.empty:
    st.warning(
        "No qualifying disclosures match the current filters. "
        "Try turning off ASX 200 only or refreshing the announcements."
    )
else:
    parsed_count = (
        int(df["Parse status"].isin(["Parsed", "Partial parse"]).sum())
        if "Parse status" in df.columns
        else 0
    )
    increased = int(df["Direction"].str.contains("INCREASED|CROSSED", regex=True).sum())
    decreased = int(df["Direction"].str.contains("DECREASED|FELL", regex=True).sum())

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Disclosures", len(df))
    c2.metric("Bullish / crossed", increased)
    c3.metric("Bearish / fell", decreased)
    c4.metric("Deep parsed", parsed_count)

    if "Radar score" in df.columns:
        df = df.sort_values(
            ["Radar score", "Date", "Time"],
            ascending=[False, False, False],
        )

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
        df[display_cols],
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

    if parsed_by_index:
        st.subheader("🔥 Strongest accumulation signals")
        positive = df[df["Radar score"] > 0].head(10)
        if not positive.empty:
            st.dataframe(
                positive[[c for c in [
                    "Ticker", "ASX 200", "Holder", "Direction",
                    "% point change", "Net votes",
                    "Approx net value A$", "Radar score", "ASX filing"
                ] if c in positive.columns]],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "ASX filing": st.column_config.LinkColumn(
                        "ASX filing", display_text="Open ↗"
                    ),
                    "Approx net value A$": st.column_config.NumberColumn(format="$%,.0f"),
                },
            )

        st.subheader("🧊 Strongest reduction signals")
        negative = df[df["Radar score"] < 0].sort_values("Radar score").head(10)
        if not negative.empty:
            st.dataframe(
                negative[[c for c in [
                    "Ticker", "ASX 200", "Holder", "Direction",
                    "% point change", "Net votes",
                    "Approx net value A$", "Radar score", "ASX filing"
                ] if c in negative.columns]],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "ASX filing": st.column_config.LinkColumn(
                        "ASX filing", display_text="Open ↗"
                    ),
                    "Approx net value A$": st.column_config.NumberColumn(format="$%,.0f"),
                },
            )

    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "⬇️ Download radar results",
        data=csv,
        file_name="asx_institutional_money_flow_radar.csv",
        mime="text/csv",
        use_container_width=True,
    )

with st.expander("Diagnostics / parser health"):
    for label, status, count in diagnostics:
        st.write(f"**{label}:** {status} — {count} qualifying disclosure(s)")
    st.write(
        f"ASX 200 constituent source: "
        f"{len(asx200_codes)} codes loaded"
        if asx200_codes
        else "ASX 200 constituent source unavailable — ASX 200 filter disabled effectively."
    )

    if "Parse status" in df.columns and not df.empty:
        st.write("**Deep-parser results:**")
        st.dataframe(
            df[["Ticker", "Holder", "Parse status", "ASX filing"]]
            if "Holder" in df.columns
            else df[["Ticker", "Parse status", "ASX filing"]],
            use_container_width=True,
            hide_index=True,
            column_config={
                "ASX filing": st.column_config.LinkColumn(
                    "ASX filing", display_text="Open ↗"
                )
            },
        )

    st.caption(f"App refresh: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

st.divider()
st.caption(
    "Important: 'crossed 5%' and 'fell below 5%' are disclosure-threshold events, "
    "not proof that the entire position was bought or sold that day. Estimated dollar "
    "movement uses extracted net voting shares × a recent market price and is approximate. "
    "Always verify the ASX filing before acting."
)
