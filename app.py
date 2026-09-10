import io, re, sqlite3
from pathlib import Path
from datetime import datetime
import feedparser, pandas as pd, requests, streamlit as st
from pypdf import PdfReader

st.set_page_config(page_title="ASX 200 Institutional Radar", page_icon="📡", layout="wide")
FEED_URL="https://finance.mooh.org/rss.php"
DB_PATH=Path("radar.db")
KEYWORDS=["substantial holder","change in substantial holding","becoming a substantial holder","ceasing to be a substantial holder"]

@st.cache_resource
def db():
    con=sqlite3.connect(DB_PATH,check_same_thread=False)
    con.execute("""CREATE TABLE IF NOT EXISTS announcements (
    guid TEXT PRIMARY KEY,ticker TEXT,headline TEXT,published_at TEXT,url TEXT,institution TEXT,
    holder_before REAL,holder_after REAL,shares_before REAL,shares_after REAL,direction TEXT,raw_text TEXT)""")
    con.commit(); return con

def num(s):
    try:return float(str(s).replace(",","").replace("$","").strip())
    except:return None

def first(patterns,text):
    for p in patterns:
        m=re.search(p,text or "",re.I|re.S)
        if m:return m.group(1).strip()
    return None

def parse_pdf(url):
    if not url:return {}
    try:
        r=requests.get(url,timeout=25,headers={"User-Agent":"ASX200-Institutional-Radar/1.0"});r.raise_for_status()
        text="\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(r.content)).pages)
    except:return {}
    institution=first([r"Name of substantial holder\s*[:\-]?\s*(.+?)(?:\n|2\.)",r"Name\s*[:\-]?\s*(.+?)(?:\n|ACN|ABN)"],text)
    bp=num(first([r"previously.*?relevant interest.*?(\d+(?:\.\d+)?)\s*%",r"previous.*?(\d+(?:\.\d+)?)\s*%"],text))
    ap=num(first([r"current.*?relevant interest.*?(\d+(?:\.\d+)?)\s*%",r"current.*?(\d+(?:\.\d+)?)\s*%"],text))
    bs=num(first([r"previously.*?number of securities.*?([\d,]+)",r"previous.*?([\d,]+)\s*(?:ordinary shares|securities)"],text))
    ass=num(first([r"current.*?number of securities.*?([\d,]+)",r"current.*?([\d,]+)\s*(?:ordinary shares|securities)"],text))
    direction=None
    if ap is not None and bp is not None: direction="BUYING" if ap>bp else "SELLING" if ap<bp else "FLAT"
    elif ass is not None and bs is not None: direction="BUYING" if ass>bs else "SELLING" if ass<bs else "FLAT"
    return dict(institution=institution,holder_before=bp,holder_after=ap,shares_before=bs,shares_after=ass,direction=direction,raw_text=text[:100000])

def ticker_from_title(title):
    m=re.search(r"\b([A-Z0-9]{2,4})\b\s*$",title or "")
    return m.group(1) if m else None

def fetch_and_process():
    feed=feedparser.parse(FEED_URL);con=db();added=0
    for e in feed.entries:
        title=e.get("title","")
        if not any(k in title.lower() for k in KEYWORDS):continue
        guid=e.get("id") or e.get("link") or title
        d=parse_pdf(e.get("link"))
        cur=con.execute("""INSERT OR IGNORE INTO announcements VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",(
            guid,ticker_from_title(title),title,e.get("published") or e.get("updated"),e.get("link"),d.get("institution"),d.get("holder_before"),d.get("holder_after"),d.get("shares_before"),d.get("shares_after"),d.get("direction"),d.get("raw_text")))
        added+=cur.rowcount
    con.commit();return added

def load_data():return pd.read_sql_query("SELECT * FROM announcements ORDER BY published_at DESC",db())

def score_data(df,days):
    cols=["ticker","score","buy_events","sell_events","institutions_buying","institutions_selling"]
    if df.empty:return pd.DataFrame(columns=cols)
    x=df.copy();x["published_at"]=pd.to_datetime(x["published_at"],errors="coerce",utc=True)
    x=x[x["published_at"]>=pd.Timestamp.now(tz="UTC")-pd.Timedelta(days=days)].copy()
    if x.empty:return pd.DataFrame(columns=cols)
    now=pd.Timestamp.now(tz="UTC")
    def es(r):
        base={"BUYING":3,"SELLING":-3,"FLAT":0}.get(r.get("direction"),0);move=0
        if pd.notna(r.get("holder_before")) and pd.notna(r.get("holder_after")):move=min(abs(r["holder_after"]-r["holder_before"])*2,8)
        age=max(.25,1-max(0,(now-r["published_at"]).days)/max(days,1));return base*(1+move/10)*age
    x["event_score"]=x.apply(es,axis=1)
    rows=[]
    for ticker,g in x.groupby("ticker",dropna=False):
        rows.append({"ticker":ticker,"score":g.event_score.sum(),"buy_events":(g.direction=="BUYING").sum(),"sell_events":(g.direction=="SELLING").sum(),"institutions_buying":g.loc[g.direction=="BUYING","institution"].nunique(),"institutions_selling":g.loc[g.direction=="SELLING","institution"].nunique()})
    return pd.DataFrame(rows).sort_values("score",ascending=False)

st.title("📡 ASX 200 Institutional Money Flow Radar")
st.caption("Publicly disclosed substantial-holder activity — NOT every institutional trade.")
with st.sidebar:
    st.header("Controls");days=st.slider("Lookback (days)",7,365,90);ticker_filter=st.text_input("Ticker (optional)").upper().strip()
    if st.button("🔄 Fetch latest disclosures",use_container_width=True):
        with st.spinner("Checking public disclosures…"):
            try:st.success(f"Done — {fetch_and_process()} new disclosures added.")
            except Exception as ex:st.error(f"Feed error: {ex}")
ann=load_data()
if ticker_filter:ann=ann[ann.ticker==ticker_filter]
if ann.empty:st.info("No disclosures stored yet. Tap **Fetch latest disclosures** in the sidebar.")
else:
    st.subheader("Institutional accumulation / selling ranking");st.dataframe(score_data(ann,days).head(50),use_container_width=True,hide_index=True)
    st.subheader("Latest disclosed activity");cols=["published_at","ticker","institution","direction","holder_before","holder_after","shares_before","shares_after","headline","url"]
    st.dataframe(ann[[c for c in cols if c in ann.columns]].head(100),use_container_width=True,hide_index=True)
    st.caption(f"Stored disclosures: {len(ann):,} | Dashboard refresh: {datetime.now().strftime('%d %b %Y %H:%M:%S')}")
st.divider();st.caption("Research tool only. Verify the underlying ASX announcement before acting on a signal.")
