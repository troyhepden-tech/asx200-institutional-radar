import re
import html
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin

import pandas as pd
import requests
import streamlit as st

st.set_page_config(
    page_title="ASX Institutional Money Flow Radar",
    page_icon="📡",
    layout="wide",
)

ASX_BASE = "https://www.asx.com.au"
TODAY_URL = f"{ASX_BASE}/asx/v2/statistics/todayAnns.do"
PREV_URL = f"{ASX_BASE}/asx/v2/statistics/prevBusDayAnns.do"

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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
    "Referer": "https://www.asx.com.au/",
}


class ASXTableParser(HTMLParser):
    """Small stdlib HTML parser so we don't need extra packages."""
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
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.text


def clean_headline(text):
    # ASX table cell often appends "N pages 123KB"
    text = re.sub(r"\s+\d+\s+pages?\s+[\d.]+\s*(?:KB|MB)\s*$", "", text, flags=re.I)
    return text.strip()


def classify(headline):
    h = headline.lower()
    if "becoming a substantial holder" in h:
        return "NEW / ACCUMULATION"
    if "ceasing to be a substantial holder" in h:
        return "EXIT / REDUCTION"
    if "change in substantial holding" in h:
        return "CHANGE"
    return "OTHER"


def signal_score(kind):
    if kind == "NEW / ACCUMULATION":
        return 3
    if kind == "EXIT / REDUCTION":
        return -3
    return 0


def parse_asx_announcements(page_html, source_label):
    parser = ASXTableParser()
    parser.feed(page_html)

    records = []

    for row in parser.rows:
        texts = [c["text"] for c in row]
        joined = " | ".join(texts)

        # Find the ticker. ASX codes are normally 3 chars, sometimes digit-bearing.
        ticker = None
        for txt in texts[:2]:
            candidate = txt.strip().upper()
            if re.fullmatch(r"[A-Z0-9]{3}", candidate):
                ticker = candidate
                break

        # Find a target headline cell.
        headline_cell = None
        for cell in row:
            lower = cell["text"].lower()
            if any(term in lower for term in TARGET_HEADLINES):
                headline_cell = cell
                break

        if not ticker or not headline_cell:
            continue

        headline = clean_headline(headline_cell["text"])
        kind = classify(headline)

        # Find date/time from row text.
        date_match = re.search(
            r"(\d{2}/\d{2}/\d{4})(?:\s+(\d{1,2}:\d{2}\s*(?:am|pm)))?",
            joined,
            flags=re.I,
        )
        date_text = date_match.group(1) if date_match else ""
        time_text = date_match.group(2) if date_match and date_match.group(2) else ""

        link = headline_cell["href"]
        if link:
            link = urljoin(ASX_BASE, link)
        else:
            # Fallback takes user to that company's announcement history.
            link = (
                f"{ASX_BASE}/asx/v2/statistics/announcements.do"
                f"?by=asxCode&asxCode={ticker}&timeframe=D&period=M6"
            )

        records.append(
            {
                "Ticker": ticker,
                "Date": date_text,
                "Time": time_text,
                "Signal": kind,
                "Score": signal_score(kind),
                "Headline": headline,
                "Source": source_label,
                "ASX link": link,
            }
        )

    return records


def get_disclosures():
    all_records = []
    diagnostics = []

    endpoints = [
        ("Today", TODAY_URL),
        ("Previous trading day", PREV_URL),
    ]

    for label, url in endpoints:
        try:
            page = fetch_page(url)
            rows = parse_asx_announcements(page, label)
            all_records.extend(rows)
            diagnostics.append((label, "OK", len(rows)))
        except Exception as e:
            diagnostics.append((label, f"ERROR: {type(e).__name__}: {e}", 0))

    # Remove duplicates if the two pages overlap.
    seen = set()
    deduped = []
    for r in all_records:
        key = (r["Ticker"], r["Date"], r["Time"], r["Headline"])
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    return deduped, diagnostics


st.title("📡 ASX Institutional Money Flow Radar")
st.caption(
    "Direct from ASX public company announcements — substantial-holder notices only."
)

with st.sidebar:
    st.header("Radar controls")
    refresh = st.button("🔄 Fetch latest disclosures", use_container_width=True)
    st.caption("Checks ASX Today + Previous Trading Day.")
    ticker_filter = st.text_input("Ticker filter", placeholder="e.g. ZIP")
    show_changes = st.checkbox("Include 'Change in substantial holding'", value=True)
    st.divider()
    st.caption(
        "Signal guide: NEW = holder crossed the substantial-holder threshold; "
        "EXIT = holder ceased being substantial; CHANGE requires the underlying "
        "filing to determine exact buying/selling."
    )

if refresh:
    fetch_page.clear()

with st.spinner("Checking ASX announcements…"):
    records, diagnostics = get_disclosures()

df = pd.DataFrame(records)

if not df.empty:
    if not show_changes:
        df = df[df["Signal"] != "CHANGE"]

    if ticker_filter.strip():
        df = df[df["Ticker"].str.contains(ticker_filter.strip().upper(), regex=False)]

st.subheader("Latest substantial-holder disclosures")

if df.empty:
    st.warning(
        "No qualifying disclosures found in ASX Today or Previous Trading Day. "
        "This can happen outside market hours or when neither page contains a "
        "substantial-holder filing."
    )
else:
    buys = int((df["Signal"] == "NEW / ACCUMULATION").sum())
    changes = int((df["Signal"] == "CHANGE").sum())
    exits = int((df["Signal"] == "EXIT / REDUCTION").sum())

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Disclosures", len(df))
    c2.metric("New substantial", buys)
    c3.metric("Changes", changes)
    c4.metric("Ceasing", exits)

    # Most useful ordering first, then date/time as supplied by ASX.
    order = {
        "NEW / ACCUMULATION": 0,
        "CHANGE": 1,
        "EXIT / REDUCTION": 2,
    }
    df["_order"] = df["Signal"].map(order).fillna(9)
    df = df.sort_values(["_order", "Date", "Time"], ascending=[True, False, False])
    df = df.drop(columns=["_order"])

    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "ASX link": st.column_config.LinkColumn("ASX filing", display_text="Open ↗"),
            "Score": st.column_config.NumberColumn("Radar score"),
        },
    )

    st.download_button(
        "⬇️ Download current results (CSV)",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name="asx_substantial_holder_radar.csv",
        mime="text/csv",
        use_container_width=True,
    )

with st.expander("Diagnostics"):
    st.write("If the radar ever looks empty, this tells us whether ASX was reachable.")
    for label, status, count in diagnostics:
        st.write(f"**{label}:** {status} — {count} qualifying disclosure(s)")
    st.write(f"Last app refresh: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

st.divider()
st.caption(
    "Research tool only. A substantial-holder notice is not automatically a clean "
    "institutional buy/sell signal. Open the underlying ASX filing before acting."
)
