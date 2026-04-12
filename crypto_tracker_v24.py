###############################################################################
# CRYPTO SEC FILING TRACKER v24
#
# ROOT CAUSE FIX: extract_risk scored by RISK words only. In multi-fund
# 485APOS filings, bond fund risk sections have identical risk-word density
# to crypto fund sections. v24 scores by CRYPTO * RISK — the section that
# mentions "bitcoin"/"digital asset" alongside "risk"/"volatile" wins.
# Same fix in dl_text document selection and extract_risk_quick.
# Summary sub-heading detection tightened (must contain crypto OR be in
# a section that does).
#
# Cell 1: !rm -f /content/crypto_filings.db
# Cell 2: !pip install edgartools requests pandas tqdm beautifulsoup4 lxml --quiet
# Cell 3: Paste this
# Cell 4: from google.colab import files; files.download('/content/crypto_filings_report.html')
###############################################################################

import pandas as pd, requests, re, time, os, sqlite3, unicodedata, threading, math
import html as html_mod, json
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm.auto import tqdm
from bs4 import BeautifulSoup, NavigableString
import warnings; warnings.filterwarnings("ignore")
from edgar import set_identity, search_filings
set_identity("PenroseHayek crypto.hedger23@gmail.com")

print("="*70)
print("  CRYPTO SEC FILING TRACKER v24")
print("  CRYPTO*RISK scoring | tightened summaries | 8 threads")
print("="*70)

START_DATE = "2019-01-01"
END_DATE = datetime.now().strftime("%Y-%m-%d")
NUM_THREADS = 8
MAX_PER_KW = 20
HTTP_TO = 30

KWS = ["cryptocurrency","blockchain","digital asset","bitcoin","ethereum",
       "XRP","stablecoin","crypto ETF","crypto fund","crypto token","DeFi"]
FORMS = ["S-1","S-1/A","N-1A","485APOS","485BPOS","10-K","10-Q","8-K","D"]
T1F_ROOTS = {"485APOS","485BPOS","N-1A","S-1","D"}
HH = {"User-Agent":"PenroseHayek crypto.hedger23@gmail.com","Accept":"text/html"}

CRE = re.compile(r"\b(cryptocurrency|cryptocurrencies|blockchain|digital asset|digital assets|bitcoin|ethereum|XRP|stablecoin|stablecoins|crypto|DeFi|NFT|mining|proof.of.work|proof.of.stake|decentralized finance|virtual currency|virtual currencies|digital currency|crypto.?asset|crypto.?assets)\b", re.I)
RRE = re.compile(r"\b(risk|loss|adverse|volatile|volatility|uncertain|decline|fail|failure|liability|penalty|harm|impair|no assurance|could result|may not|subject to|negatively|negative impact|material adverse|you could lose|speculative|cybersecurity|hack|theft|fraud|manipulation|regulatory|compliance|forfeiture|prohibit|restrict|enforce|sanctions)\b", re.I)
BRE = re.compile(r"(table of contents|date of this prospectus|criminal offense|approved or disapproved|bookrunner|delivery of the securities|incorporation by reference|exhibits? \d|EX-\d|pursuant to|filed herewith|XBRL|iXBRL|X0708 D|LIVE \d{10}|EDGAR Online)", re.I)

RH = ["RISK FACTORS","Risk Factors","PRINCIPAL INVESTMENT RISKS","Principal Investment Risks",
      "PRINCIPAL RISKS","Principal Risks","INVESTMENT RISKS","Investment Risks",
      "KEY RISK FACTORS","Key Risk Factors","Risks Related to","RISKS RELATED TO",
      "Risks of Investing in the Fund","Risks of Investing in the Trust",
      "SUMMARY OF PRINCIPAL RISKS","Principal Investment Risks of an Investment",
      "Summary of Principal Risks","RISKS OF INVESTING"]
SE = re.compile(r"\n\s*(?:"
    # === 10-K / S-1 standard sections ===
    r"Item\s*\d|ITEM\s*\d"
    r"|PART\s*(?:II|III|IV)|Part\s*(?:II|III|IV)"
    r"|USE OF PROCEEDS|DESCRIPTION OF (?:THE |CAPITAL)"
    r"|MANAGEMENT.S DISCUSSION|SECURITY OWNERSHIP"
    r"|SELLING|PLAN OF DISTRIBUTION"
    r"|EXPERTS|LEGAL MATTERS|INDEX TO FINANCIAL"
    r"|UNAUDITED|CAUTIONARY NOTE"
    # === N-1A / 485 / Fund-specific sections (THE MISSING ONES) ===
    r"|PURCHASE AND (?:SALE|REDEMPTION) OF (?:FUND |TRUST )?SHARES"
    r"|TAX INFORMATION|TAX CONSEQUENCES"
    r"|PAYMENTS TO BROKER.DEALERS|PAYMENTS TO FINANCIAL INTERMEDIARIES"
    r"|FINANCIAL HIGHLIGHTS|FEE TABLE|FEES AND EXPENSES"
    r"|SHAREHOLDER INFORMATION|SHAREHOLDER SERVICES"
    r"|HOW TO (?:PURCHASE|BUY|SELL|REDEEM)"
    r"|INVESTMENT OBJECTIVE|INVESTMENT STRATEG"
    r"|FUND SUMMARY|FUND PERFORMANCE|PERFORMANCE INFORMATION"
    r"|ADDITIONAL INFORMATION (?:ABOUT|REGARDING)"
    r"|DISTRIBUTION AND SERVICING|DISTRIBUTION PLAN"
    r"|PORTFOLIO HOLDINGS|PORTFOLIO TURNOVER"
    r"|CREATION AND REDEMPTION|CREATION UNITS"
    r"|MANAGEMENT OF THE (?:FUND|TRUST)"
    r"|ORGANIZATION OF THE (?:FUND|TRUST)"
    r"|DETERMINATION OF NET ASSET VALUE"
    r"|DIVIDENDS.? DISTRIBUTIONS"
    r"|ABOUT THE (?:FUND|TRUST|INDEX)"
    r"|INVESTMENT (?:POLICIES|RESTRICTIONS|LIMITATIONS)"
    r"|DESCRIPTION OF THE (?:INDEX|BENCHMARK)"
    r"|OTHER (?:INFORMATION|RISKS|CONSIDERATIONS)"
    # === General prospectus sections ===
    r"|SIGNATURES|EXHIBIT INDEX|EXHIBITS?"
    r"|REPORT OF INDEPENDENT|FINANCIAL STATEMENTS"
    r"|NOTES TO (?:THE )?(?:CONSOLIDATED )?FINANCIAL"
    r"|SELECTED FINANCIAL DATA"
    r"|PROPERTIES|BUSINESS OVERVIEW"
    r")", re.I)

FTM = {"10-K":"Annual Report","10-Q":"Quarterly Report","8-K":"Current Report",
       "485APOS":"ETF/Fund Amendment","485BPOS":"ETF/Fund Amendment",
       "N-1A":"ETF/Fund Registration","N-1A/A":"ETF/Fund Reg. (Amend.)",
       "S-1":"Security Offering Reg.","S-1/A":"Security Offering Reg. (Amend.)",
       "D":"Exempt Offering","D/A":"Exempt Offering (Amend.)","8-K/A":"Current Report (Amend.)"}

DB = "/content/crypto_filings.db"

# ═══════════════════════════ DATABASE ════════════════════════════════════
def init_db():
    c = sqlite3.connect(DB, check_same_thread=False)
    c.execute("""CREATE TABLE IF NOT EXISTS filings (
        accession_no TEXT PRIMARY KEY, cik TEXT, company_name TEXT, ticker TEXT,
        form_type TEXT, root_form TEXT, filing_date TEXT, filing_category TEXT,
        tier INT, text_length INT, risk_summary TEXT, risk_section TEXT,
        crypto_connection TEXT, sec_url TEXT, search_keyword TEXT,
        n_risk_paras INT, processed_at TEXT)""")
    c.commit(); return c

def get_existing(c):
    return set(r[0] for r in c.execute("SELECT accession_no FROM filings").fetchall())

# ═══════════════════════════ HTML DOWNLOAD — PARAGRAPH-PRESERVING ══════
BLOCK_TAGS = {'p','div','h1','h2','h3','h4','h5','h6','li','tr','th','td',
              'blockquote','section','article','header','footer','dt','dd',
              'figcaption','caption','pre','address','ol','ul','table'}

def html_to_structured_text(raw_html):
    """Convert HTML to text while preserving paragraph structure.
    Instead of get_text() which destroys everything, this inserts
    proper paragraph breaks at block boundaries."""
    soup = BeautifulSoup(raw_html, 'html.parser')
    # Remove non-content tags
    for tag in soup.find_all(['script','style','head','meta','link','noscript']):
        tag.decompose()

    # Insert paragraph markers before/after block elements
    for tag in soup.find_all(BLOCK_TAGS):
        tag.insert_before('\n\n')
        tag.append('\n')
    # Handle <br> as single newline
    for br in soup.find_all('br'):
        br.replace_with('\n')

    text = soup.get_text()

    # Clean up whitespace while preserving paragraph breaks
    # First normalize horizontal whitespace (spaces/tabs) within lines
    text = re.sub(r'[^\S\n]+', ' ', text)
    # Remove leading/trailing spaces on each line
    text = re.sub(r' *\n *', '\n', text)
    # Collapse 3+ newlines into exactly 2 (paragraph break)
    text = re.sub(r'\n{3,}', '\n\n', text)
    # Remove page numbers on their own line
    text = re.sub(r'\n\d{1,3}\n', '\n', text)
    # Remove "Table of Contents" lines
    text = re.sub(r'\nTable of Contents\n', '\n', text, flags=re.I)

    return text.strip()


def dl_url(url):
    """Download a single document and convert to structured text."""
    try:
        r = requests.get(url, headers=HH, timeout=HTTP_TO)
        if r.status_code == 200 and len(r.text) > 500:
            return html_to_structured_text(r.text)
    except: pass
    return None


def get_idx_urls(fu):
    """Get all document URLs from a filing index page."""
    urls = []
    try:
        r = requests.get(fu, headers=HH, timeout=15)
        if r.status_code != 200: return urls
        for row in BeautifulSoup(r.text, 'html.parser').find_all('tr'):
            a = row.find('a')
            if a:
                h = a.get('href','')
                if h.endswith(('.htm','.html')) and 'index' not in h.lower() and 'R1' not in h:
                    urls.append(f"https://www.sec.gov{h}" if h.startswith('/') else h)
    except: pass
    return urls


def dl_text(cik, acc):
    """CRYPTO*RISK document selection: score each document by how many
    crypto keywords appear in its risk section. Bond fund docs score 0."""
    ck = cik.lstrip('0')
    ac = acc.replace('-', '')
    base = f"https://www.sec.gov/Archives/edgar/data/{ck}/{ac}"
    fu = f"{base}/{acc}-index.htm"

    urls = get_idx_urls(fu)
    if not urls: return None

    best_text = None
    best_score = 0
    all_texts = []

    for url in urls[:8]:
        t = dl_url(url)
        if not t or len(t) < 300: continue
        all_texts.append(t)

        risk_sec = extract_risk_quick(t)
        if risk_sec and len(risk_sec) > 800:
            sample = risk_sec[:15000].lower()
            cw = len(CRE.findall(sample))  # crypto keywords
            rw = len(RRE.findall(sample))  # risk keywords
            score = cw * max(rw, 1)  # CRYPTO * RISK product

            if score > best_score:
                best_score = score
                best_text = t

        time.sleep(0.05)

    if not all_texts: return None
    if best_text and best_score > 5:
        return best_text
    return "\n\n".join(all_texts)


def extract_risk_quick(text):
    """Quick CRYPTO*RISK section extraction for document scoring.
    Returns the section with highest crypto*risk keyword product."""
    if not text or len(text) < 500: return ""
    cands = []
    for hdr in RH:
        p = 0
        while True:
            p = text.find(hdr, p)
            if p == -1: break
            cs = p + len(hdr)
            ctx = text[max(0,p-80):p].lower()
            if 'see ' in ctx or 'refer to' in ctx:
                p = cs; continue
            ctx_after = text[cs:cs+120].lower()
            if 'beginning on page' in ctx_after or 'on page' in ctx_after:
                p = cs; continue
            m = SE.search(text, cs + 300)
            ce = m.start() if m else min(cs + 80000, len(text))
            sec = text[cs:ce].strip()
            p = cs
            if len(sec) < 800: continue

            sample = sec[:12000].lower()
            cw = len(CRE.findall(sample))
            rw = len(RRE.findall(sample))
            score = cw * max(rw, 1)
            if score > 3:
                cands.append((score, sec))
    if cands:
        cands.sort(key=lambda x: -x[0])
        return cands[0][1]
    return ""


# ═══════════════════════════ RISK EXTRACTION ═══════════════════════════
def extract_risk(text):
    """CRYPTO*RISK density scoring. In multi-fund filings, a bond fund's risk
    section has the same risk-word density as the crypto fund's section. The
    difference is CRYPTO keywords. Score = crypto_hits * risk_hits. A section
    about 'interest rate risk' with zero crypto words scores 0. A section about
    'bitcoin volatility risk' scores high."""
    if not text: return ""
    cands = []
    for hdr in RH:
        p = 0
        while True:
            p = text.find(hdr, p)
            if p == -1: break
            cs = p + len(hdr)
            context_before = text[max(0,p-80):p].lower()
            if 'see ' in context_before or 'refer to' in context_before:
                p = cs; continue
            context_after = text[cs:cs+120].lower()
            if 'beginning on page' in context_after or 'on page' in context_after:
                p = cs; continue

            m = SE.search(text, cs + 300)
            ce = m.start() if m else min(cs + 80000, len(text))
            sec = text[cs:ce].strip()
            p = cs
            if len(sec) < 800: continue

            # Score by CRYPTO * RISK density
            sample = sec[:15000].lower()
            cw = len(CRE.findall(sample))  # crypto keyword hits
            rw = len(RRE.findall(sample))  # risk keyword hits
            bw = len(re.findall(r'\b(revenue|product|customer|strategy|founded|'
                r'incorporated|headquartered|employees|overview|bookrunner|'
                r'date of this prospectus|table of contents|criminal offense|'
                r'underwriter|delivery of the securities)\b', sample))

            # CRYPTO * RISK product — sections with zero crypto words score 0
            crypto_risk_score = cw * max(rw - bw * 2, 1)
            if crypto_risk_score <= 0: continue

            # Small length bonus (diminishing)
            length_bonus = min(math.log(len(sec) + 1), 12)
            sc = crypto_risk_score + length_bonus
            cands.append((sc, sec))

    if cands:
        cands.sort(key=lambda x: -x[0])
        best = cands[0][1]
        if len(best) > 500: return best

    # FALLBACK: paragraph-level scan
    return extract_crypto_paragraphs_as_text(text)


def extract_crypto_paragraphs_as_text(text, max_chars=80000):
    """Scan entire document for paragraphs containing crypto+risk language.
    Returns them as flowing text preserving paragraph structure."""
    raw = re.split(r'\n\s*\n', text)
    if len(raw) < 10:
        sents = re.split(r'(?<=[.!?])\s+', text)
        raw = []
        chunk = ""
        for s in sents:
            chunk += s + " "
            if len(chunk) > 400:
                raw.append(chunk.strip())
                chunk = ""
        if chunk.strip(): raw.append(chunk.strip())

    good = []
    total = 0
    for p in raw:
        p = p.strip()
        if len(p) < 50: continue
        if BRE.search(p): continue
        c = len(CRE.findall(p))
        r = len(RRE.findall(p))
        if c > 0 and r > 0:
            score = c * r
            good.append((score, p))
            total += len(p)
        if total > max_chars: break

    good.sort(key=lambda x: -x[0])
    if not good: return ""
    return "\n\n".join(p for _, p in good)


def format_risk_for_html(text):
    """Format risk section text into proper HTML paragraphs that mirror
    the actual filing's structure. Preserves headers, indentation, and flow."""
    if not text: return ""
    # Remove page numbers
    text = re.sub(r'\n\s*\d{1,3}\s*\n', '\n', text)
    text = re.sub(r'\nTable of Contents\n', '\n', text, flags=re.I)

    # Split into paragraphs on double newlines
    paras = re.split(r'\n\s*\n', text)
    html_parts = []
    for p in paras:
        p = p.strip()
        if not p or len(p) < 5: continue

        # Detect if this looks like a sub-header (short, possibly bold/caps in original)
        is_header = (
            len(p) < 120 and
            not p.endswith('.') and
            not p.endswith(',') and
            re.search(r'[A-Z]', p) and
            not BRE.search(p)
        )

        escaped = html_mod.escape(p)
        # Preserve single newlines within a paragraph as line breaks for lists/bullets
        # But collapse them for flowing prose
        lines = escaped.split('\n')
        if len(lines) > 1:
            # Check if these look like bullet points or numbered items
            has_bullets = sum(1 for l in lines if re.match(r'^\s*[\u2022\u25CF\-\*\(\d]', l.strip())) > len(lines) * 0.3
            if has_bullets:
                escaped = '<br>'.join(l.strip() for l in lines if l.strip())
            else:
                escaped = ' '.join(l.strip() for l in lines if l.strip())

        if is_header:
            html_parts.append(
                f'<div style="font-weight:700;font-size:11.5px;color:#e2e8f0;'
                f'margin:14px 0 4px 0;padding-bottom:2px;'
                f'border-bottom:1px solid #1e293b;">{escaped}</div>'
            )
        else:
            html_parts.append(
                f'<p style="margin:6px 0;text-indent:0;line-height:1.75;">{escaped}</p>'
            )

    return "".join(html_parts)


def build_summary(text):
    """Extract actual risk SUB-HEADINGS from the filing's risk section.
    SEC risk sub-headers look like: "Bitcoin Volatility Risk", "Custody Risk",
    "Regulatory Uncertainty". They are short noun phrases, often ending in "Risk",
    followed by a paragraph body. NOT sentence fragments like "than bonds rated"."""
    if not text or len(text) < 200: return ""

    # ── STEP 1: Find actual sub-headings ──
    lines = text.split('\n')
    sub_headings = []
    for i, line in enumerate(lines):
        line = line.strip()
        if not line: continue
        words = line.split()
        nw = len(words)

        # Hard filters for sub-heading shape
        if nw < 2 or nw > 15: continue           # 2-15 words
        if len(line) < 10 or len(line) > 140: continue
        if line.endswith('.'): continue           # Headings don't end with period
        if line.endswith(','): continue           # Not a sentence fragment
        if BRE.search(line): continue
        if not line[0].isupper(): continue        # Must start with capital

        # Reject common sentence starters (these are body text, not headings)
        first = words[0].lower()
        if first in ('the','a','an','in','it','if','as','we','to','for','on',
                      'at','by','or','and','but','this','that','these','those',
                      'there','they','you','your','its','such','each','any',
                      'no','not','than','with','from','however','because',
                      'although','while','since','when','where','which'):
            continue

        # Must contain a risk-related OR crypto-related word
        has_risk_word = bool(re.search(
            r'\b(Risk|Risks|Volatility|Volatile|Uncertainty|Uncertain|Loss|'
            r'Fraud|Hack|Theft|Custody|Regulatory|Compliance|Manipulation|'
            r'Cybersecurity|Liquidity|Concentration|Litigation|Fork|Tax|'
            r'Speculative|Leverage|Counterparty|Credit|Default|Impairment)\b', line))
        has_crypto_word = bool(CRE.search(line))
        if not has_risk_word and not has_crypto_word: continue

        # Next non-empty line should be substantially longer (it's the paragraph body)
        next_para_len = 0
        for j in range(i+1, min(i+5, len(lines))):
            nl = lines[j].strip()
            if nl and len(nl) > 30:
                next_para_len = len(nl)
                break
        if next_para_len < len(line) * 1.3 and next_para_len < 80:
            continue

        # Clean and deduplicate
        clean = re.sub(r'^[\u2022\u25CF\-\*]+\s*', '', line).strip()
        clean = re.sub(r'[:\.]$', '', clean).strip()
        if clean and len(clean) > 8:
            is_dup = any(e.lower()[:20] == clean.lower()[:20] for e in sub_headings)
            if not is_dup:
                sub_headings.append(clean)
        if len(sub_headings) >= 12: break

    # ── STEP 2: Thematic categories as fallback ──
    THEMES = [
        ("Price volatility risk", r"(price volatility|volatile|price.{0,15}fluctuat|extreme.{0,10}fluctuat|speculative)"),
        ("Regulatory/compliance uncertainty", r"(regulatory.{0,15}(?:uncertain|risk|change|evolv)|SEC.{0,15}(?:regulat|enforce)|CFTC|securities law)"),
        ("Custody & storage risk", r"(custody|custod|safekeeping|private key|wallet|cold storage)"),
        ("Cybersecurity & hacking risk", r"(hack|cyber.?security|cyber.?attack|security breach|unauthorized access)"),
        ("Tax treatment uncertainty", r"(tax.{0,15}(?:treatment|uncertain|consequence|implication)|IRS|capital gain)"),
        ("Liquidity risk", r"(liquidity.{0,10}risk|illiquid|trading volume.{0,10}(?:low|limit))"),
        ("Market manipulation risk", r"(manipulation|manipulat|wash trading|spoofing)"),
        ("Fork & protocol risk", r"(fork|hard fork|protocol.{0,10}(?:change|upgrade|risk))"),
        ("Concentration risk", r"(concentration|concentrated|single.{0,10}(?:asset|invest|exposure)|undiversified)"),
        ("Operational/technology risk", r"(operational.{0,10}risk|service.{0,10}(?:disrupt|interrupt)|system.{0,10}fail)"),
    ]
    txt_lower = text[:60000].lower()
    theme_names = [name for name, pattern in THEMES if re.search(pattern, txt_lower)]

    # ── STEP 3: Get 2-3 specific, non-boilerplate sentences ──
    specific_sents = _get_specific_sentences(text, count=3)

    # ── STEP 4: Assemble summary (6-10 lines) ──
    parts = []
    if sub_headings:
        n = len(sub_headings)
        display = sub_headings[:8]
        parts.append(f"Filing identifies {n} risk area{'s' if n>1 else ''}: "
                     + "; ".join(display)
                     + (f" (and {n-8} more)" if n > 8 else "") + ".")
    elif theme_names:
        parts.append(f"Key risk categories ({len(theme_names)}): "
                     + "; ".join(theme_names[:6])
                     + (f" (and {len(theme_names)-6} more)" if len(theme_names)>6 else "") + ".")

    for s in specific_sents:
        parts.append(s)

    if not parts:
        return _fallback_sentence_summary(text)

    return " ".join(parts)


def _get_specific_sentences(text, count=2):
    """Find concrete, specific sentences (not boilerplate) from the risk text."""
    sents = re.split(r'(?<=[.!?])\s+', text[:50000])
    GENERIC = re.compile(r'(you could lose|no assurance|past performance|'
        r'an investment in the|please read|as with all investments|'
        r'you should consider|before investing|there is no guarantee|'
        r'may not be suitable|carefully consider|the following)', re.I)
    scored = []
    for s in sents:
        s = s.strip()
        if len(s) < 70 or len(s) > 400: continue
        if GENERIC.search(s): continue
        if BRE.search(s): continue
        c = len(CRE.findall(s)); r = len(RRE.findall(s))
        if c == 0 or r == 0: continue
        specificity = len(re.findall(r'\b(SEC|CFTC|IRS|FINRA|bitcoin|ethereum|'
            r'XRP|Coinbase|CBOE|NYSE|Nasdaq|CME|Bitfinex|Binance|'
            r'\d+%|\$[\d,.]+|billion|million)\b', s, re.I))
        generic_penalty = len(re.findall(r'\b(may|could|might|would|should|'
            r'possible|potential|certain)\b', s, re.I)) * 0.5
        score = c * 2 + r + specificity * 4 - generic_penalty
        scored.append((score, s))
    scored.sort(key=lambda x: -x[0])
    selected = []
    for sc, s in scored:
        sw = set(s.lower().split())
        if any(len(sw & set(p.lower().split())) / max(len(sw),1) > 0.4 for _, p in selected):
            continue
        selected.append((sc, s))
        if len(selected) >= count: break
    return [s for _, s in selected]


def _fallback_sentence_summary(text):
    """Last resort: extract top sentences by keyword density."""
    sents = re.split(r'(?<=[.!?])\s+', text[:40000])
    scored = []
    for s in sents:
        s = s.strip()
        if len(s) < 50 or len(s) > 500: continue
        c = len(CRE.findall(s)); r = len(RRE.findall(s))
        if c == 0 or r == 0: continue
        if BRE.search(s): continue
        scored.append((c*3 + r*2, s))
    scored.sort(key=lambda x: -x[0])
    sel = []
    for sc, s in scored:
        sw = set(s.lower().split())
        if any(len(sw & set(p.lower().split())) / max(len(sw),1) > 0.5 for _, p in sel): continue
        sel.append((sc, s))
        if len(sel) >= 3: break
    return " ".join(s for _, s in sel) if sel else ""


# ═══════════════════════════ PROCESS ONE ═══════════════════════════════
def process_one(item):
    acc, cik, cn, tk, fa, rf, filed, tier, kw = item
    try:
        text = dl_text(cik, acc)
        if not text or len(text) < 300: return None

        rs = extract_risk(text)
        src = rs if rs else text
        sm = build_summary(src)
        if not sm: return None

        # GUARANTEE dropdown content — triple fallback
        if not rs or len(rs) < 150:
            rs = extract_crypto_paragraphs_as_text(text)
        if not rs or len(rs) < 80:
            # Last resort: grab a chunk of text around the first crypto keyword match
            m = CRE.search(text)
            if m:
                start = max(0, m.start() - 500)
                rs = text[start:start+10000]
            else:
                rs = text[:10000]

        ct = list(set(t.lower() for t in CRE.findall(text[:3000])))[:5]
        if rf in T1F_ROOTS:
            cc = f"Proposes {' or '.join(ct)} ETF, fund, or security offering." if ct else "Crypto-related offering."
        elif fa in ["10-K","10-Q"]:
            cc = f"Reports {' or '.join(ct)} operations with risk disclosures." if ct else "Crypto risk disclosures."
        else:
            cc = f"Discloses {' or '.join(ct)} material event." if ct else "Crypto-related."

        su = f"https://www.sec.gov/Archives/edgar/data/{cik.lstrip('0')}/{acc.replace('-','')}/{acc}-index.htm"
        cat = "ETF/Fund" if rf in T1F_ROOTS else "Operating Co."

        return {"accession_no":acc,"cik":cik,"company_name":cn,"ticker":tk,
                "form_type":fa,"root_form":rf,"filing_date":filed,
                "filing_category":cat,"tier":tier,"text_length":len(text),
                "risk_summary":sm,"risk_section":rs[:60000],
                "crypto_connection":cc,"sec_url":su,"search_keyword":kw,
                "n_risk_paras":0,"processed_at":datetime.now().isoformat()}
    except:
        return None


# ═══════════════════════════ STEP 1: SEARCH ════════════════════════════
conn = init_db()
existing = get_existing(conn)
print(f"\n  DB: {len(existing)} filings already processed")
print(f"\n  STEP 1: Search | {START_DATE} -> {END_DATE}")
print(f"  NO get_filing() calls — search metadata only\n")

todo = []
seen = set(existing)
form_new = {}

for ft in FORMS:
    fc = 0
    for kw in KWS:
        if fc >= MAX_PER_KW * len(KWS): break
        try:
            res = search_filings(kw, forms=[ft], start_date=START_DATE, end_date=END_DATE)
            ad = 0
            for r in res:
                if ad >= MAX_PER_KW: break
                a = r.accession_number
                if a in seen: continue
                seen.add(a)

                rn = r.company or "Unknown"
                tk = ""
                tm = re.search(r'\(([A-Z]{1,5})\)', rn)
                if tm and tm.group(1) not in ["CIK","DE","NV","CA","NY","FL","TX"]:
                    tk = tm.group(1)
                cn = re.sub(r'\s*\([A-Z]{1,5}\)\s*', ' ', rn)
                cn = re.sub(r'\s*\(CIK \d+\)\s*', '', cn).strip()
                fa = r.form or ft
                rf = re.sub(r'/A$', '', fa)
                tier = 1 if rf in T1F_ROOTS else 2
                ck = str(r.cik or "").lstrip("0")
                fd = str(r.filed) if r.filed else ""

                todo.append((a, ck, cn, tk, fa, rf, fd, tier, kw))
                ad += 1; fc += 1
        except: pass
        time.sleep(0.12)
    form_new[ft] = fc
    print(f"    {ft:8s}: {fc} new")

print(f"\n  {len(todo)} new filings to process ({len(existing)} in DB skipped)")
t1_count = sum(1 for t in todo if t[7] == 1)
t2_count = sum(1 for t in todo if t[7] == 2)
print(f"  ETF/Fund: {t1_count} | Operating Co: {t2_count}")

if not todo and not existing:
    print("No filings found."); exit()

# ═══════════════════════════ STEP 2: PARALLEL DOWNLOAD ═════════════════
if todo:
    est = len(todo) * 5 // NUM_THREADS // 60
    print(f"\n  STEP 2: Download + analyze ({len(todo)} filings, {NUM_THREADS} threads)")
    print(f"  Pure HTTP — ~{max(est,1)}-{max(est*2,2)} min estimated\n")

    saved = 0; failed = 0
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=NUM_THREADS) as ex:
        futs = {ex.submit(process_one, i): i for i in todo}
        with tqdm(total=len(futs), desc="Processing", unit="f",
                  bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]') as pb:
            for f in as_completed(futs):
                try:
                    r = f.result(timeout=120)
                except:
                    r = None; failed += 1
                if r:
                    with lock:
                        conn.execute("""INSERT OR REPLACE INTO filings VALUES
                            (:accession_no,:cik,:company_name,:ticker,:form_type,
                             :root_form,:filing_date,:filing_category,:tier,
                             :text_length,:risk_summary,:risk_section,
                             :crypto_connection,:sec_url,:search_keyword,
                             :n_risk_paras,:processed_at)""", r)
                        saved += 1
                        if saved % 50 == 0: conn.commit()
                pb.update(1)
                pb.set_postfix(saved=saved, fail=failed)

    conn.commit()
    print(f"\n  {saved} saved, {failed} failed/timed out")
    print(f"  {len(todo) - saved - failed} had no crypto risk content (dropped)")
else:
    print(f"\n  No new filings — using {len(existing)} from DB")

# ═══════════════════════════ STEP 3: LOAD + HTML ═══════════════════════
print(f"\n  STEP 3: Report from database...")
df = pd.read_sql("SELECT * FROM filings WHERE risk_summary != '' ORDER BY filing_date DESC", conn)
conn.close()
if df.empty: print("No filings with risk content."); exit()

# Dedup operating companies
df['fdd'] = pd.to_datetime(df['filing_date'], errors='coerce')
t2 = df[df['tier'] == 2].copy()
t1 = df[df['tier'] == 1].copy()
if not t2.empty:
    b = len(t2)
    t2 = t2.sort_values('fdd', ascending=False).drop_duplicates(
        subset=['company_name','root_form'], keep='first')
    print(f"  Dedup T2: {b} -> {len(t2)}")
df = pd.concat([t1, t2], ignore_index=True).sort_values('fdd', ascending=False).reset_index(drop=True)

def cln(t):
    if pd.isna(t): return t
    return re.sub(r'\s+', ' ', ''.join(c for c in unicodedata.normalize('NFKD', str(t))
        if ord(c) < 128 or c in ['"',"'",'-'])).strip()
for c in ['crypto_connection','risk_summary','company_name']:
    if c in df.columns: df[c] = df[c].apply(cln)

df['ftd'] = df['form_type'].map(FTM).fillna(df['form_type'])
df.to_csv(f"/content/edgar_crypto_filings_cleaned.csv", index=False)
df['filing_date'] = pd.to_datetime(df['filing_date'], errors='coerce')
df['fm'] = df['filing_date'].dt.to_period('M')
mo = df.groupby('fm').size().reset_index(name='c'); mo['fm'] = mo['fm'].astype(str)
ftc = df.groupby(['filing_category','form_type']).size().reset_index(name='c')
cc = df['filing_category'].value_counts().reset_index(); cc.columns = ['cat','c']
coc = df.groupby('company_name').size().reset_index(name='c').sort_values('c', ascending=False)
tc = coc.head(15)
kwc = df['search_keyword'].value_counts().reset_index(); kwc.columns = ['kw','c']
st = {'T':len(df), 'C':df['company_name'].nunique(),
      'E':len(df[df['filing_category']=='ETF/Fund']),
      'O':len(df[df['filing_category']=='Operating Co.'])}

print(f"\n  FINAL: {st['T']} filings | {st['C']} companies | {st['E']} ETF | {st['O']} Oper")
print(f"  Forms: {df['form_type'].value_counts().to_dict()}")

# ═══════════════════════════ HTML GENERATION ═══════════════════════════
print(f"\n  Generating HTML...")

# --- Build filing table rows with data attributes ---
fr = ""
for _, r in df.iterrows():
    u = r.get('sec_url','#')
    bg = "10K" if "10-K" in str(r['form_type']) else "10Q" if "10-Q" in str(r['form_type']) else "8K" if "8-K" in str(r['form_type']) else "S1" if "S-1" in str(r['form_type']) else "ETF" if "485" in str(r['form_type']) or "N-1A" in str(r['form_type']) else "ot"
    rs = str(r.get('risk_summary',''))
    month = str(r['filing_date'])[:7] if pd.notna(r['filing_date']) else ""
    comp = str(r['company_name'])
    form = str(r['form_type'])
    cat = str(r.get('filing_category',''))
    kwd = str(r.get('search_keyword',''))

    fr += (f'<tr data-month="{html_mod.escape(month)}" '
           f'data-company="{html_mod.escape(comp)}" '
           f'data-form="{html_mod.escape(form)}" '
           f'data-category="{html_mod.escape(cat)}" '
           f'data-keyword="{html_mod.escape(kwd)}">'
           f'<td class="p bdr"><a href="{u}" target="_blank" class="lk">'
           f'{html_mod.escape(comp)}</a></td>'
           f'<td class="p bdr">{r.get("ticker","")}</td>'
           f'<td class="p bdr"><span class="b b-{bg}">{form}</span></td>'
           f'<td class="p bdr">{r.get("ftd","")}</td>'
           f'<td class="p bdr">{str(r["filing_date"])[:10]}</td>'
           f'<td class="p bdr t1" style="color:#fca5a5;">'
           f'{html_mod.escape(rs[:140])+"..." if len(rs)>140 else html_mod.escape(rs)}</td>'
           f'<td class="p bdr"><a href="{u}" target="_blank" class="lk t1">SEC&rarr;</a></td>'
           f'</tr>\n')

# --- Build risk cards with data attributes and formatted dropdowns ---
rc = ""; ci = 0
for _, r in df.iterrows():
    ci += 1
    u = r.get('sec_url','#')
    rs = str(r.get('risk_summary',''))
    rd = str(r.get('risk_section',''))
    bg = "b-S1" if "S-1" in str(r['form_type']) else "b-10K" if "10-K" in str(r['form_type']) else "b-10Q" if "10-Q" in str(r['form_type']) else "b-8K" if "8-K" in str(r['form_type']) else "b-ETF" if "485" in str(r['form_type']) or "N-1A" in str(r['form_type']) else "b-ot"
    tl = r.get('text_length', 0)
    trl = "ETF/Fund" if r.get('tier',1) == 1 else "Oper.Co."
    month = str(r['filing_date'])[:7] if pd.notna(r['filing_date']) else ""
    comp = str(r['company_name'])
    form = str(r['form_type'])
    cat = str(r.get('filing_category',''))
    kwd = str(r.get('search_keyword',''))

    # Summary block
    sh = (f'<div style="font-size:10px;color:#64748b;font-weight:700;margin-top:8px;'
          f'margin-bottom:4px;letter-spacing:.05em;text-transform:uppercase;">'
          f'Risk Factor Section Summary</div>'
          f'<div style="padding:10px 14px;background:#ef444408;border-left:3px solid #ef4444;'
          f'border-radius:0 6px 6px 0;font-size:11px;line-height:1.85;color:#e2e8f0;">'
          f'{html_mod.escape(rs)}</div>')

    # Dropdown — formatted to mirror the actual filing
    col = ''
    if rd and len(rd) > 40:
        rds = format_risk_for_html(rd)
        if not rds:
            rds = f'<p style="margin:6px 0;">{html_mod.escape(rd[:40000])}</p>'
        col = (f'<div style="margin-top:10px;">'
               f'<button onclick="var e=document.getElementById(\'rd{ci}\');'
               f'e.style.display=e.style.display===\'none\'?\'block\':\'none\';'
               f'this.textContent=e.style.display===\'none\'?\'\\u25B6 Show Full Risk Section\':\'\\u25BC Hide Risk Section\'"'
               f' style="background:#1e293b;border:1px solid #334155;color:#94a3b8;'
               f'padding:6px 16px;border-radius:5px;cursor:pointer;font-family:inherit;'
               f'font-size:10px;font-weight:600;transition:all .15s;"'
               f' onmouseover="this.style.borderColor=\'#6366f1\';this.style.color=\'#c7d2fe\'"'
               f' onmouseout="this.style.borderColor=\'#334155\';this.style.color=\'#94a3b8\'"'
               f'>&#9654; Show Full Risk Section</button>'
               f'<div id="rd{ci}" style="display:none;margin-top:10px;padding:16px 20px;'
               f'background:#0f172a;border-radius:8px;border:1px solid #1e293b;'
               f'max-height:700px;overflow-y:auto;font-size:10.5px;line-height:1.8;'
               f'color:#cbd5e1;">{rds}</div></div>')

    rc += (f'<div class="rcard card" style="margin-bottom:14px;background:#ef444406;'
           f'border-color:#ef444418;" '
           f'data-month="{html_mod.escape(month)}" '
           f'data-company="{html_mod.escape(comp)}" '
           f'data-form="{html_mod.escape(form)}" '
           f'data-category="{html_mod.escape(cat)}" '
           f'data-keyword="{html_mod.escape(kwd)}">'
           # Header row
           f'<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px;">'
           f'<div><a href="{u}" target="_blank" class="lk" style="font-weight:700;font-size:13px;">'
           f'{html_mod.escape(comp)}</a>'
           f'{f" <span style=color:#a5b4fc;font-size:10px>{r.get(chr(116)+chr(105)+chr(99)+chr(107)+chr(101)+chr(114),chr(32))}</span>" if r.get("ticker") else ""}'
           f' <span class="b {bg}" style="margin-left:4px;">{form}</span>'
           f' <span style="color:#64748b;font-size:9px;margin-left:6px;">'
           f'{str(r["filing_date"])[:10]} &middot; {trl} &middot; {int(tl):,}ch</span></div>'
           f'<a href="{u}" target="_blank" class="lk t1" style="white-space:nowrap;">SEC.gov &rarr;</a></div>'
           # Crypto connection
           f'<div style="font-size:10px;color:#a5b4fc;margin-bottom:4px;">'
           f'<b>Crypto:</b> {html_mod.escape(str(r.get("crypto_connection",""))[:200])}</div>'
           # Summary + dropdown
           f'{sh}{col}</div>\n')

# --- Chart data ---
ml = mo['fm'].tolist(); mv = mo['c'].tolist()
cll = cc['cat'].tolist(); cv = cc['c'].tolist()
col_ = tc['company_name'].tolist()[:12]; cov = tc['c'].tolist()[:12]
kwl = kwc['kw'].tolist()[:10]; kwv = kwc['c'].tolist()[:10]

# Form type breakdown table — clickable rows
ftr = ""
for _, r in ftc.iterrows():
    ft_val = str(r['form_type'])
    ftr += (f'<tr style="cursor:pointer;transition:background .15s;" '
            f'onmouseover="this.style.background=\'#1e293b\'" '
            f'onmouseout="this.style.background=\'transparent\'" '
            f'onclick="applyFilter(\'form\',\'{html_mod.escape(ft_val)}\','
            f'\'Form: {html_mod.escape(ft_val)}\')">'
            f'<td class="p bdr">{r["filing_category"]}</td>'
            f'<td class="p bdr"><span class="b b-{("10K" if "10-K" in ft_val else "10Q" if "10-Q" in ft_val else "8K" if "8-K" in ft_val else "S1" if "S-1" in ft_val else "ETF" if "485" in ft_val or "N-1A" in ft_val else "ot")}">{ft_val}</span></td>'
            f'<td class="p bdr">{FTM.get(r["form_type"],r["form_type"])}</td>'
            f'<td class="p bdr" style="text-align:right;font-weight:700;">{r["c"]}</td></tr>')

# ═══════════════════════════ FULL HTML ═════════════════════════════════
html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Crypto Filing Tracker v24</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&display=swap');
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{font-family:'JetBrains Mono',monospace;background:#0a0e17;color:#e2e8f0;font-size:13px;}}
::-webkit-scrollbar{{width:5px;}}
::-webkit-scrollbar-thumb{{background:#1e293b;border-radius:3px;}}
.card{{background:#111827;border:1px solid #1e293b;border-radius:10px;padding:14px;}}
.st{{border-top:3px solid;}}
.bdr{{border-bottom:1px solid #1e293b;}}
.p{{padding:5px 8px;}}
.lk{{color:#818cf8;text-decoration:none;}}
.lk:hover{{color:#a5b4fc;text-decoration:underline;}}
.t1{{font-size:11px;}}
table{{width:100%;border-collapse:collapse;}}
th{{padding:5px 8px;text-align:left;color:#64748b;font-size:9px;letter-spacing:.08em;
    text-transform:uppercase;background:#0f1420;position:sticky;top:0;z-index:1;}}
.b{{padding:2px 6px;border-radius:4px;font-size:9px;font-weight:700;display:inline-block;}}
.b-10K{{background:#6366f120;color:#a5b4fc;border:1px solid #6366f140;}}
.b-10Q{{background:#8b5cf620;color:#c4b5fd;border:1px solid #8b5cf640;}}
.b-8K{{background:#f59e0b20;color:#fcd34d;border:1px solid #f59e0b40;}}
.b-S1{{background:#22c55e20;color:#86efac;border:1px solid #22c55e40;}}
.b-ETF{{background:#ec489920;color:#f9a8d4;border:1px solid #ec489940;}}
.b-ot{{background:#64748b20;color:#94a3b8;border:1px solid #64748b40;}}
.tb{{padding:8px 16px;background:transparent;border:none;color:#64748b;cursor:pointer;
    font-family:inherit;font-size:12px;font-weight:600;text-transform:uppercase;
    letter-spacing:.05em;border-bottom:2px solid transparent;transition:all .15s;}}
.tb:hover{{color:#94a3b8;}}
.tb.on{{color:#f1f5f9;border-bottom-color:#6366f1;}}
.tab{{display:none;}}
.tab.on{{display:block;}}
input{{font-family:inherit;background:#0a0e17;border:1px solid #334155;border-radius:6px;
      padding:7px 11px;color:#f1f5f9;font-size:12px;outline:none;transition:border .15s;}}
input:focus{{border-color:#6366f1;}}
#filter-bar{{display:none;align-items:center;gap:10px;margin-bottom:10px;padding:8px 14px;
    background:#6366f115;border:1px solid #6366f140;border-radius:8px;}}
#filter-bar .filter-label{{font-size:10px;color:#a5b4fc;font-weight:600;letter-spacing:.04em;}}
#filter-bar .filter-value{{font-size:11px;color:#f1f5f9;font-weight:700;}}
#filter-bar .clear-btn{{background:#ef444420;border:1px solid #ef444460;color:#fca5a5;
    padding:3px 10px;border-radius:4px;cursor:pointer;font-family:inherit;font-size:9px;
    font-weight:600;letter-spacing:.04em;transition:all .15s;}}
#filter-bar .clear-btn:hover{{background:#ef444440;color:#fff;}}
canvas{{cursor:pointer;}}
</style>
</head>
<body>
<div style="max-width:1200px;margin:0 auto;padding:16px;">

<!-- HEADER -->
<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;">
<div style="display:flex;align-items:center;gap:10px;">
<span style="font-size:24px;">&#9878;</span>
<div>
<div style="font-weight:700;font-size:16px;">CRYPTO SEC FILING TRACKER</div>
<div style="font-size:9px;color:#64748b;">FOR CRYPTO LAWYERS | {START_DATE} to {END_DATE} | v24 | SQLite | Interactive</div>
</div></div>
<div style="text-align:right;font-size:9px;color:#64748b;">
{datetime.now().strftime('%Y-%m-%d %H:%M')}<br>{st['T']} filings | {st['C']} companies
</div></div>

<!-- STAT CARDS -->
<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:12px;">
{''.join(f'<div class="card st" style="border-top-color:{c};"><div style="font-size:8px;color:#64748b;letter-spacing:.1em;margin-bottom:2px;">{l}</div><div style="font-size:18px;font-weight:700;color:{c};">{v}</div></div>' for l,v,c in [("FILINGS",st['T'],"#a5b4fc"),("COMPANIES",st['C'],"#86efac"),("ETF/FUND",st['E'],"#f9a8d4"),("OPER.CO.",st['O'],"#fcd34d")])}
</div>

<!-- FILTER BAR (hidden until chart click) -->
<div id="filter-bar">
<span class="filter-label">FILTERED BY:</span>
<span class="filter-value" id="filter-text"></span>
<button class="clear-btn" onclick="clearFilter()">&#10005; CLEAR</button>
</div>

<!-- TABS -->
<div style="display:flex;border-bottom:1px solid #1e293b;margin-bottom:12px;">
<button class="tb on" onclick="switchTab('ch',this)">Charts ({len(df)})</button>
<button class="tb" onclick="switchTab('ri',this)">Risk Summaries ({len(df)})</button>
<button class="tb" onclick="switchTab('sr',this)">Sources</button>
</div>

<!-- TAB: CHARTS -->
<div id="t-ch" class="tab on">
<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px;">
<div class="card">
<div style="font-weight:600;font-size:10px;margin-bottom:4px;">
MONTHLY FILINGS <span style="color:#64748b;font-size:8px;">(click a bar to filter)</span></div>
<canvas id="c1" height="160"></canvas></div>
<div class="card">
<div style="font-weight:600;font-size:10px;margin-bottom:4px;">
ETF vs OPERATING CO. <span style="color:#64748b;font-size:8px;">(click to filter)</span></div>
<canvas id="c2" height="160"></canvas></div>
</div>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px;">
<div class="card">
<div style="font-weight:600;font-size:10px;margin-bottom:4px;">
TOP COMPANIES <span style="color:#64748b;font-size:8px;">(click to filter)</span></div>
<canvas id="c3" height="200"></canvas></div>
<div class="card">
<div style="font-weight:600;font-size:10px;margin-bottom:4px;">
KEYWORDS <span style="color:#64748b;font-size:8px;">(click to filter)</span></div>
<canvas id="c4" height="200"></canvas></div>
</div>

<div class="card" style="margin-bottom:10px;">
<div style="font-weight:600;font-size:10px;margin-bottom:4px;">
FORM TYPES <span style="color:#64748b;font-size:8px;">(click a row to filter)</span></div>
<table><thead><tr><th>Category</th><th>Form</th><th>Description</th>
<th style="text-align:right;">Count</th></tr></thead><tbody>{ftr}</tbody></table></div>

<div class="card">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
<div style="font-weight:600;font-size:10px;" id="ft-label">ALL FILINGS ({len(df)})</div>
<input id="ff" placeholder="Search filings..." style="width:240px;" oninput="textFilter('ft','ff')">
</div>
<div style="overflow:auto;max-height:600px;">
<table id="ft"><thead><tr>
<th>Company</th><th>Tkr</th><th>Form</th><th>Type</th><th>Filed</th><th>Risk Summary</th><th>Link</th>
</tr></thead><tbody>{fr}</tbody></table></div></div>
</div>

<!-- TAB: RISK SUMMARIES -->
<div id="t-ri" class="tab">
<div style="display:flex;justify-content:space-between;margin-bottom:8px;">
<div>
<div style="font-weight:600;font-size:12px;">RISK FACTOR SUMMARIES</div>
<div style="font-size:9px;color:#64748b;">Sub-heading extraction + key sentences | Expand for full risk section as filed</div>
</div>
<input id="rf" placeholder="Search risks..." style="width:220px;" oninput="textFilter('rc','rf')">
</div>
<div id="rc">{rc}</div>
</div>

<!-- TAB: SOURCES -->
<div id="t-sr" class="tab"><div class="card">
<div style="font-weight:600;font-size:12px;margin-bottom:8px;">Methodology &amp; Sources</div>
<div style="font-size:10px;color:#94a3b8;line-height:1.7;">
<p style="margin-bottom:8px;"><b>v24</b>: CRYPTO*RISK scoring — sections scored by crypto_keywords * risk_keywords product,
so bond fund risk sections (zero crypto words) score 0 even in multi-fund filings.
Sub-heading detection tightened (rejects sentence fragments, requires noun-phrase shape).
Expanded section boundaries. edgartools search + pure HTTP download + SQLite. 8 threads.
{START_DATE} to present. DB: {DB}</p>
<p style="margin-bottom:8px;"><b>Data source:</b> SEC EDGAR Full-Text Search System (EFTS) via edgartools.
Filing documents downloaded directly from sec.gov.</p>
<p style="margin-bottom:4px;"><b>SEC Statements on Crypto:</b></p>
<ul style="margin-left:16px;line-height:2;">
<li><a class="lk" href="https://www.sec.gov/digital-assets" target="_blank">SEC Digital Assets Landing Page</a></li>
<li><a class="lk" href="https://www.sec.gov/litigation/litreleases.htm" target="_blank">SEC Litigation Releases</a></li>
<li><a class="lk" href="https://www.sec.gov/news/pressreleases" target="_blank">SEC Press Releases</a></li>
<li><a class="lk" href="https://www.sec.gov/news/statements" target="_blank">SEC Speeches &amp; Statements</a></li>
<li><a class="lk" href="https://www.sec.gov/investor/alerts" target="_blank">SEC Investor Alerts</a></li>
</ul>
</div></div></div>

</div><!-- /max-width container -->

<script>
// ═══════════════ TAB SWITCHING ═══════════════
function switchTab(n,b) {{
    document.querySelectorAll('.tab').forEach(t=>t.classList.remove('on'));
    document.getElementById('t-'+n).classList.add('on');
    document.querySelectorAll('.tb').forEach(x=>x.classList.remove('on'));
    b.classList.add('on');
}}

// ═══════════════ TEXT FILTER (search box) ═══════════════
function textFilter(tid,iid) {{
    const q = document.getElementById(iid).value.toLowerCase();
    const container = document.getElementById(tid);
    container.querySelectorAll('tr, .rcard').forEach(el => {{
        if (el.tagName === 'TR' && el.parentElement.tagName === 'THEAD') return;
        el.style.display = el.textContent.toLowerCase().includes(q) ? '' : 'none';
    }});
}}

// ═══════════════ CHART CLICK FILTER ═══════════════
let activeFilterType = null;
let activeFilterValue = null;

function applyFilter(type, value, label) {{
    activeFilterType = type;
    activeFilterValue = value;
    const bar = document.getElementById('filter-bar');
    bar.style.display = 'flex';
    document.getElementById('filter-text').textContent = label;

    // Filter filing table rows (Charts tab)
    let visibleCount = 0;
    document.querySelectorAll('#ft tbody tr').forEach(tr => {{
        const attr = tr.getAttribute('data-' + type) || '';
        const match = type === 'company'
            ? attr.toLowerCase().includes(value.toLowerCase())
            : attr === value;
        tr.style.display = match ? '' : 'none';
        if (match) visibleCount++;
    }});
    document.getElementById('ft-label').textContent =
        'FILTERED FILINGS (' + visibleCount + ')';

    // Filter risk cards (Risk Summaries tab)
    document.querySelectorAll('#rc .rcard').forEach(card => {{
        const attr = card.getAttribute('data-' + type) || '';
        const match = type === 'company'
            ? attr.toLowerCase().includes(value.toLowerCase())
            : attr === value;
        card.style.display = match ? '' : 'none';
    }});
}}

function clearFilter() {{
    activeFilterType = null;
    activeFilterValue = null;
    document.getElementById('filter-bar').style.display = 'none';
    document.getElementById('ft-label').textContent = 'ALL FILINGS ({len(df)})';
    document.querySelectorAll('#ft tbody tr').forEach(tr => tr.style.display = '');
    document.querySelectorAll('#rc .rcard').forEach(c => c.style.display = '');
    // Clear text search boxes too
    document.getElementById('ff').value = '';
    document.getElementById('rf').value = '';
}}

// ═══════════════ CHART SETUP ═══════════════
Chart.defaults.color = '#94a3b8';
Chart.defaults.borderColor = '#1e293b';

// Monthly filings chart — click a bar to filter by that month
const c1 = new Chart(document.getElementById('c1'), {{
    type: 'bar',
    data: {{
        labels: {json.dumps(ml)},
        datasets: [{{
            data: {json.dumps(mv)},
            backgroundColor: '#6366f160',
            hoverBackgroundColor: '#6366f1',
            borderColor: '#6366f1',
            borderWidth: 1,
            borderRadius: 3
        }}]
    }},
    options: {{
        responsive: true,
        plugins: {{ legend: {{ display: false }} }},
        scales: {{ x: {{ ticks: {{ font: {{ size: 7 }}, maxRotation: 45 }} }} }},
        onClick: (evt, elements) => {{
            if (elements.length > 0) {{
                const idx = elements[0].index;
                const month = {json.dumps(ml)}[idx];
                applyFilter('month', month, 'Month: ' + month);
            }}
        }}
    }}
}});

// Category doughnut — click a slice to filter by ETF/Fund or Operating Co.
const c2 = new Chart(document.getElementById('c2'), {{
    type: 'doughnut',
    data: {{
        labels: {json.dumps(cll)},
        datasets: [{{
            data: {json.dumps(cv)},
            backgroundColor: ['#ec4899','#f59e0b','#64748b'],
            hoverBackgroundColor: ['#f472b6','#fbbf24','#94a3b8'],
            borderWidth: 0
        }}]
    }},
    options: {{
        responsive: true,
        plugins: {{ legend: {{ position: 'bottom' }} }},
        onClick: (evt, elements) => {{
            if (elements.length > 0) {{
                const idx = elements[0].index;
                const cat = {json.dumps(cll)}[idx];
                applyFilter('category', cat, 'Category: ' + cat);
            }}
        }}
    }}
}});

// Top companies bar — click a bar to filter by company
const c3Labels = {json.dumps([c[:30] for c in col_])};
const c3Full = {json.dumps(col_)};
const c3 = new Chart(document.getElementById('c3'), {{
    type: 'bar',
    data: {{
        labels: c3Labels,
        datasets: [{{
            data: {json.dumps(cov)},
            backgroundColor: '#8b5cf660',
            hoverBackgroundColor: '#8b5cf6',
            borderColor: '#8b5cf6',
            borderWidth: 1,
            borderRadius: 3
        }}]
    }},
    options: {{
        responsive: true,
        indexAxis: 'y',
        plugins: {{ legend: {{ display: false }} }},
        scales: {{ y: {{ ticks: {{ font: {{ size: 7 }} }} }} }},
        onClick: (evt, elements) => {{
            if (elements.length > 0) {{
                const idx = elements[0].index;
                const company = c3Full[idx];
                applyFilter('company', company, 'Company: ' + company);
            }}
        }}
    }}
}});

// Keywords bar — click to filter by keyword
const c4 = new Chart(document.getElementById('c4'), {{
    type: 'bar',
    data: {{
        labels: {json.dumps(kwl)},
        datasets: [{{
            data: {json.dumps(kwv)},
            backgroundColor: ['#6366f180','#22c55e80','#f59e0b80','#ef444480',
                              '#8b5cf680','#ec489980','#06b6d480','#64748b80',
                              '#a78bfa80','#fbbf2480'],
            hoverBackgroundColor: ['#6366f1','#22c55e','#f59e0b','#ef4444',
                                   '#8b5cf6','#ec4899','#06b6d4','#64748b',
                                   '#a78bfa','#fbbf24'],
            borderRadius: 3
        }}]
    }},
    options: {{
        responsive: true,
        plugins: {{ legend: {{ display: false }} }},
        onClick: (evt, elements) => {{
            if (elements.length > 0) {{
                const idx = elements[0].index;
                const kw = {json.dumps(kwl)}[idx];
                applyFilter('keyword', kw, 'Keyword: ' + kw);
            }}
        }}
    }}
}});
</script>
</body>
</html>"""

with open("/content/crypto_filings_report.html", "w") as f:
    f.write(html)
sz = os.path.getsize('/content/crypto_filings_report.html')
print(f"\n  Done: /content/crypto_filings_report.html ({sz/1024:.0f}KB)")
print(f"  DB: {DB} ({os.path.getsize(DB)/1024/1024:.1f}MB) — re-run for NEW filings only")
print(f"\n  INTERACTIVE FEATURES:")
print(f"    Click any bar/slice on Monthly, Company, Category, or Keyword charts")
print(f"    -> Filing table AND Risk cards filter instantly")
print(f"    -> Purple filter bar appears with clear button")
print(f"    -> Filter persists across Charts and Risk Summaries tabs")
