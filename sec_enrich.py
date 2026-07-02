"""SEC filing enrichment — fetches & parses Form 4, 8-K, SC 13D/G, and 13F-HR
filings to extract the structured details that turn an "X filed Y" alert into
an actionable summary.

Stdlib-only (urllib + xml.etree.ElementTree + html.parser). All network calls
take a caller-provided `http_get` function so tests/heartbeat can mock the SEC.

Each public `enrich_*` function returns a dict suitable for embedding in a
Discord alert, or None if the filing can't be parsed (we never raise — we
fall back to the simple alert format).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import Callable, Optional


# -------------------- 8-K item codes --------------------

ITEM_8K_LABELS = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "1.04": "Mine Safety — Reporting of Shutdowns and Patterns of Violations",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Direct Financial Obligation",
    "2.04": "Triggering Events That Accelerate or Increase a Direct Financial Obligation",
    "2.05": "Costs Associated with Exit or Disposal Activities",
    "2.06": "Material Impairments",
    "3.01": "Notice of Delisting or Failure to Satisfy a Continued Listing Rule",
    "3.02": "Unregistered Sales of Equity Securities",
    "3.03": "Material Modification to Rights of Security Holders",
    "4.01": "Changes in Registrant's Certifying Accountant",
    "4.02": "Non-Reliance on Previously Issued Financial Statements",
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure / Election / Appointment of Directors or Officers",
    "5.03": "Amendments to Articles of Incorporation or Bylaws",
    "5.04": "Temporary Suspension of Trading Under Employee Benefit Plans",
    "5.05": "Amendments to the Registrant's Code of Ethics",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "5.08": "Shareholder Director Nominations",
    "6.01": "ABS Informational and Computational Material",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}


def parse_8k_items(items_str: str) -> list[dict]:
    """`items_str` comes from EDGAR submissions API recent.items[i] —
    comma-separated like "2.02,9.01". Returns [{code, label}, ...]."""
    if not items_str:
        return []
    parsed = []
    for code in (c.strip() for c in items_str.split(",")):
        if not code:
            continue
        parsed.append({"code": code, "label": ITEM_8K_LABELS.get(code, "Unknown item")})
    return parsed


# -------------------- 8-K deep analysis --------------------

# Materiality bands per item code. Drives the alert colour, the events feed,
# and which 8-Ks count toward the confluence "corporate" signal. "low" items
# (Reg FD decks, exhibit-only filings, code-of-ethics edits) are routine
# housekeeping — they still alert, but they should not move a trading signal.
ITEM_8K_MATERIALITY = {
    "1.03": "critical",  # bankruptcy / receivership
    "2.04": "critical",  # debt acceleration triggered
    "3.01": "critical",  # delisting notice
    "4.02": "critical",  # non-reliance on prior financials (restatement)
    "5.01": "critical",  # change in control
    "1.01": "high",      # material definitive agreement
    "1.02": "high",      # termination of material agreement
    "2.01": "high",      # M&A completed
    "2.02": "high",      # results of operations (earnings)
    "2.03": "high",      # new direct financial obligation
    "2.05": "high",      # exit / disposal costs (restructuring)
    "2.06": "high",      # material impairment
    "4.01": "high",      # auditor change
    "5.02": "high",      # officer/director departure or appointment
    "3.02": "medium",    # unregistered equity sales
    "3.03": "medium",    # modification to security holders' rights
    "5.03": "medium",    # charter / bylaw amendments
    "5.07": "medium",    # shareholder vote results
    "8.01": "medium",    # other events (catch-all — often real news)
    "1.04": "low",
    "5.04": "low",
    "5.05": "low",
    "5.08": "low",
    "6.01": "low",
    "7.01": "low",       # Reg FD disclosure (usually a furnished deck)
    "9.01": "low",       # financial statements and exhibits (boilerplate)
}

_MATERIALITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def assess_8k_materiality(codes: list[str]) -> tuple[str, list[str]]:
    """Overall materiality for an 8-K = the highest-ranked item present.
    Returns (level, drivers) where drivers name the items that set the level.
    Unknown codes count as "medium" (novel item codes deserve eyes). Pure."""
    if not codes:
        return "low", []
    best, drivers = "low", []
    for code in codes:
        level = ITEM_8K_MATERIALITY.get(code, "medium")
        if _MATERIALITY_RANK[level] > _MATERIALITY_RANK[best]:
            best = level
            drivers = [code]
        elif level == best and code not in drivers:
            drivers.append(code)
    labels = [f"{c} {ITEM_8K_LABELS.get(c, 'Unknown item')}" for c in drivers]
    return best, labels


class _TextExtractor(HTMLParser):
    """HTML → plain text, inserting newlines at block boundaries so item
    headings stay line-anchored. Skips script/style; ignores inline XBRL tags."""
    _BLOCK_TAGS = {"p", "div", "tr", "br", "li", "table", "h1", "h2", "h3",
                   "h4", "h5", "h6", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            # Source-line wraps inside a tag aren't real breaks — only block
            # tags produce newlines, so sentences survive intact.
            self._chunks.append(data.replace("\n", " ").replace("\r", " "))

    def text(self):
        raw = "".join(self._chunks)
        lines = [" ".join(line.split()) for line in raw.split("\n")]
        return "\n".join(line for line in lines if line)


def html_to_text(html: str) -> str:
    """Best-effort plain text from an EDGAR HTML document. Never raises."""
    if not html:
        return ""
    try:
        p = _TextExtractor()
        p.feed(html)
        p.close()
        return p.text()
    except Exception:
        # Fallback: crude tag strip.
        return " ".join(re.sub(r"<[^>]+>", " ", html).split())


_ITEM_HEADING_RE = re.compile(r"^\s*item\s+(\d\.\d{2})\b[.:]?\s*", re.IGNORECASE)


def split_8k_items(text: str) -> dict[str, str]:
    """Split 8-K body text into {item_code: section_text} using line-anchored
    "Item X.XX" headings. Inline references mid-sentence don't split. Pure."""
    sections = {}
    current, buf = None, []
    for line in (text or "").split("\n"):
        m = _ITEM_HEADING_RE.match(line)
        if m:
            if current:
                sections[current] = "\n".join(buf).strip()
            current = m.group(1)
            # Keep any content after the heading label on the same line.
            rest = _ITEM_HEADING_RE.sub("", line, count=1)
            label = ITEM_8K_LABELS.get(current, "")
            if label and rest.lower().startswith(label.lower()):
                rest = rest[len(label):].lstrip(" .:")
            buf = [rest] if rest else []
        elif current:
            buf.append(line)
    if current:
        sections[current] = "\n".join(buf).strip()
    return sections


# Sentence boundary: punctuation + space + capital, but never after common
# abbreviations (Ms. Kress, Mr. Doe, Acme Inc. announced, …).
_SENTENCE_SPLIT_RE = re.compile(
    r"(?<=[.!?])(?<!\bMr\.)(?<!\bMs\.)(?<!Mrs\.)(?<!\bDr\.)(?<!\bJr\.)(?<!\bSr\.)"
    r"(?<!Inc\.)(?<!Ltd\.)(?<!orp\.)(?<!\bCo\.)(?<!\bNo\.)(?<!\bvs\.)(?<!U\.S\.)"
    r"\s+(?=[A-Z“\"(])")
# Signature blocks / boilerplate we never want in a summary.
_BOILERPLATE_RE = re.compile(
    r"pursuant to the requirements of the securities exchange act"
    r"|forward-looking statements|safe harbor|signature", re.IGNORECASE)


def _sentences(text: str) -> list[str]:
    out = []
    for para in (text or "").split("\n"):
        out.extend(s.strip() for s in _SENTENCE_SPLIT_RE.split(para) if s.strip())
    return out


def _is_headingish(s: str) -> bool:
    """True for item-heading remnants ('Departure of Directors; Election of
    Directors; …') — nearly every significant word Title-Cased, no real prose."""
    words = [w for w in re.findall(r"[A-Za-z]{3,}", s)]
    if len(words) < 4:
        return False
    capped = sum(1 for w in words if w[0].isupper())
    return capped / len(words) >= 0.75


def summarize_section(text: str, max_sentences: int = 2, max_chars: int = 320) -> str:
    """First substantive sentences of an item section — the filing's own words,
    minus boilerplate. Pure."""
    picked = []
    for s in _sentences(text):
        if len(s) < 25 or _BOILERPLATE_RE.search(s) or _is_headingish(s):
            continue
        picked.append(s)
        if len(picked) >= max_sentences:
            break
    summary = " ".join(picked)
    if len(summary) > max_chars:
        summary = summary[:max_chars - 1].rsplit(" ", 1)[0] + "…"
    return summary


_MONEY_OR_PCT_RE = re.compile(
    r"\$\s?[\d,]+(?:\.\d+)?\s*(?:billion|million|trillion)?|\d+(?:\.\d+)?\s?%",
    re.IGNORECASE)
_FINANCIAL_KEYWORDS_RE = re.compile(
    r"\brevenue|net income|net loss|earnings per (?:\w+ )?share|\bEPS\b"
    r"|gross margin|operating (?:income|margin|expenses)|guidance|outlook"
    r"|dividend|repurchase|buyback|free cash flow|record\b", re.IGNORECASE)
_GUIDANCE_RE = re.compile(r"\bexpect|guidance|outlook|forecast", re.IGNORECASE)


def extract_financial_highlights(text: str, max_items: int = 6) -> list[str]:
    """Sentences that carry the numbers — revenue/EPS/margin/guidance lines
    from an earnings 8-K or its press-release exhibit. Pure."""
    highlights, seen = [], set()
    for s in _sentences(text):
        if len(s) < 20 or _BOILERPLATE_RE.search(s):
            continue
        if not (_FINANCIAL_KEYWORDS_RE.search(s) and _MONEY_OR_PCT_RE.search(s)):
            continue
        line = s if len(s) <= 240 else s[:239].rsplit(" ", 1)[0] + "…"
        key = line.lower()[:80]
        if key in seen:
            continue
        seen.add(key)
        if _GUIDANCE_RE.search(s):
            line = "🔭 " + line
        highlights.append(line)
        if len(highlights) >= max_items:
            break
    return highlights


_PERSONNEL_RE = re.compile(
    r"\bappoint|\bresign|retire|terminat|\belect|\bnamed\b|promot|step(?:ping|ped)? down"
    r"|will serve as|departure|succeed", re.IGNORECASE)
_ROLE_RE = re.compile(
    r"chief executive|chief financial|chief operating|chief technology|chairman"
    r"|\bCEO\b|\bCFO\b|\bCOO\b|\bCTO\b|president|director|officer", re.IGNORECASE)


def extract_personnel_changes(text: str, max_items: int = 4) -> list[str]:
    """Sentences describing officer/director departures or appointments
    (Item 5.02). Pure."""
    out = []
    for s in _sentences(text):
        if len(s) < 25 or _BOILERPLATE_RE.search(s) or _is_headingish(s):
            continue
        if _PERSONNEL_RE.search(s) and _ROLE_RE.search(s):
            out.append(s if len(s) <= 240 else s[:239].rsplit(" ", 1)[0] + "…")
            if len(out) >= max_items:
                break
    return out


def _press_release_title(html: str, text: str) -> Optional[str]:
    """Headline of a press-release exhibit: <title> if real, else the first
    substantial line of text."""
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.IGNORECASE | re.DOTALL)
    if m:
        title = " ".join(m.group(1).split())
        if 15 <= len(title) <= 200 and not re.match(r"^(ex|exhibit)[-_ ]?\d", title, re.I):
            return title
    for line in (text or "").split("\n"):
        if 20 <= len(line) <= 200 and not line.lower().startswith("exhibit"):
            return line
    return None


def enrich_8k(cik: str, accession: str, primary_doc: str, items_str: str,
              http_get_text: Callable, http_get_json: Callable) -> Optional[dict]:
    """Fetch and analyze an 8-K: per-item summaries in the filing's own words,
    financial highlights (from the press-release exhibit when present),
    personnel changes, and a materiality assessment.

    Returns a dict (see keys below) or None when there is nothing to say —
    callers fall back to the plain item-code card. Never raises."""
    items = parse_8k_items(items_str or "")
    codes = [i["code"] for i in items]

    base = _filing_dir(cik, accession)
    primary_text, primary_html = "", ""
    if primary_doc and primary_doc.lower().endswith((".htm", ".html")):
        try:
            primary_html = http_get_text(f"{base}/{primary_doc}")
        except Exception:
            primary_html = ""
    idx = fetch_filing_index(cik, accession, http_get_json)
    if not primary_html and idx:
        name = find_file_in_index(idx, r"8-?k.*\.htm") or find_file_in_index(idx, r"\.htm$")
        if name:
            try:
                primary_html = http_get_text(f"{base}/{name}")
            except Exception:
                primary_html = ""
    primary_text = html_to_text(primary_html)

    # Press-release / earnings exhibit (EX-99.*) — that's where the numbers live.
    exhibit_html, exhibit_text, exhibit_url = "", "", None
    if idx:
        ex_name = (find_file_in_index(idx, r"ex[-_.]?99.*\.htm")
                   or find_file_in_index(idx, r"press.*\.htm"))
        if ex_name:
            exhibit_url = f"{base}/{ex_name}"
            try:
                exhibit_html = http_get_text(exhibit_url)
            except Exception:
                exhibit_html = ""
            exhibit_text = html_to_text(exhibit_html)

    sections = split_8k_items(primary_text)
    # The submissions API sometimes omits item codes; recover them from the body.
    if not codes and sections:
        codes = sorted(sections.keys())
        items = [{"code": c, "label": ITEM_8K_LABELS.get(c, "Unknown item")}
                 for c in codes]
    if not items and not primary_text and not exhibit_text:
        return None

    item_details = []
    for it in items:
        summary = summarize_section(sections.get(it["code"], ""))
        item_details.append({**it, "summary": summary})

    # Numbers: prefer the exhibit (earnings PR), fall back to the 8-K body.
    highlights = extract_financial_highlights(exhibit_text or primary_text)
    personnel = []
    if "5.02" in codes:
        personnel = extract_personnel_changes(
            sections.get("5.02", "") or primary_text)

    level, drivers = assess_8k_materiality(codes)
    press_title = (_press_release_title(exhibit_html, exhibit_text)
                   if exhibit_text else None)

    return {
        "items": item_details,
        "codes": codes,
        "materiality": level,
        "materiality_drivers": drivers,
        "financial_highlights": highlights,
        "personnel": personnel,
        "press_release_title": press_title,
        "press_release_url": exhibit_url,
    }


# -------------------- Filing index discovery --------------------

def _filing_dir(cik: str, accession: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}"


def fetch_filing_index(cik: str, accession: str, http_get: Callable) -> Optional[dict]:
    """Returns the directory's index.json or None on failure."""
    try:
        return http_get(f"{_filing_dir(cik, accession)}/index.json")
    except Exception:
        return None


def find_file_in_index(index_json: dict, name_pattern: str) -> Optional[str]:
    """Find first file in directory matching regex (case-insensitive)."""
    items = (index_json or {}).get("directory", {}).get("item", [])
    rx = re.compile(name_pattern, re.IGNORECASE)
    for item in items:
        name = item.get("name", "")
        if rx.search(name):
            return name
    return None


# -------------------- XML helpers --------------------

def _strip_ns(elem: ET.Element) -> None:
    """Remove namespace prefixes from tags so XPath stays simple."""
    for e in elem.iter():
        if "}" in e.tag:
            e.tag = e.tag.split("}", 1)[1]


def _find_text(root: ET.Element, path: str) -> Optional[str]:
    el = root.find(path)
    if el is None or el.text is None:
        return None
    return el.text.strip() or None


# -------------------- Form 4 --------------------

# Common transaction codes (full set in SEC Form 4 instructions, Table II).
FORM4_TX_CODES = {
    "P": ("Open-market purchase", "buy"),
    "S": ("Open-market sale", "sell"),
    "A": ("Grant / award", "grant"),
    "M": ("Option exercise", "exercise"),
    "F": ("Tax withholding", "neutral"),
    "G": ("Gift", "neutral"),
    "X": ("Option exercise (in-the-money)", "exercise"),
    "D": ("Disposition to issuer", "sell"),
    "C": ("Conversion of derivative", "neutral"),
    "V": ("Voluntary reported transaction", "neutral"),
    "J": ("Other (described in remarks)", "neutral"),
    "K": ("Equity swap", "neutral"),
    "I": ("Discretionary transaction", "neutral"),
    "U": ("Disposition pursuant to tender offer", "sell"),
}


def enrich_form4(cik: str, accession: str, primary_doc: str,
                 http_get_text: Callable, http_get_json: Callable) -> Optional[dict]:
    """Fetch Form 4 XML and return summary dict, or None on failure.

    Summary fields: insider, role, ticker, transactions[{code, label, side,
    shares, price, value, post_holdings, security}], total_value, dominant_side.
    """
    xml_text = _fetch_form4_xml(cik, accession, primary_doc, http_get_text, http_get_json)
    if not xml_text:
        return None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    _strip_ns(root)

    issuer_name = _find_text(root, "issuer/issuerName")
    ticker = _find_text(root, "issuer/issuerTradingSymbol")

    owner = root.find("reportingOwner")
    if owner is not None:
        insider_name = _find_text(owner, "reportingOwnerId/rptOwnerName")
        rel = owner.find("reportingOwnerRelationship")
        role_parts = []
        if rel is not None:
            if (_find_text(rel, "isDirector") or "").lower() in ("1", "true"):
                role_parts.append("Director")
            if (_find_text(rel, "isOfficer") or "").lower() in ("1", "true"):
                title = _find_text(rel, "officerTitle") or "Officer"
                role_parts.append(title)
            if (_find_text(rel, "isTenPercentOwner") or "").lower() in ("1", "true"):
                role_parts.append("10%+ owner")
        role = ", ".join(role_parts) or None
    else:
        insider_name, role = None, None

    transactions = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        transactions.append(_parse_form4_tx(tx, derivative=False))
    for tx in root.findall("derivativeTable/derivativeTransaction"):
        transactions.append(_parse_form4_tx(tx, derivative=True))
    transactions = [t for t in transactions if t]

    if not transactions:
        return None

    total_value = sum(t["value"] for t in transactions if t.get("value"))
    side_totals = {"buy": 0.0, "sell": 0.0, "neutral": 0.0,
                   "grant": 0.0, "exercise": 0.0}
    for t in transactions:
        side_totals[t.get("side", "neutral")] += t.get("value") or 0.0
    dominant_side = max(side_totals, key=side_totals.get) if any(side_totals.values()) else "neutral"

    return {
        "issuer_name": issuer_name,
        "ticker": ticker,
        "insider": insider_name,
        "role": role,
        "transactions": transactions,
        "total_value": total_value,
        "dominant_side": dominant_side,
    }


def _parse_form4_tx(tx: ET.Element, derivative: bool) -> Optional[dict]:
    code = _find_text(tx, "transactionCoding/transactionCode")
    if not code:
        return None
    label, side = FORM4_TX_CODES.get(code, (f"Code {code}", "neutral"))
    shares = _to_float(_find_text(tx, "transactionAmounts/transactionShares/value"))
    price = _to_float(_find_text(tx, "transactionAmounts/transactionPricePerShare/value"))
    ad = _find_text(tx, "transactionAmounts/transactionAcquiredDisposedCode/value")
    # If the SEC tags it as a disposition, override side toward sell.
    if ad == "D" and side not in ("exercise",):
        side = "sell"
    elif ad == "A" and side == "neutral":
        side = "buy"
    post = _to_float(_find_text(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value"))
    security = _find_text(tx, "securityTitle/value")
    value = (shares or 0) * (price or 0) if shares and price else None
    return {
        "code": code,
        "label": label,
        "side": side,
        "shares": shares,
        "price": price,
        "value": value,
        "post_holdings": post,
        "security": security,
        "derivative": derivative,
    }


def _to_float(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except (ValueError, TypeError):
        return None


def _fetch_form4_xml(cik: str, accession: str, primary_doc: str,
                     http_get_text: Callable, http_get_json: Callable) -> Optional[str]:
    base = _filing_dir(cik, accession)
    if primary_doc and primary_doc.lower().endswith(".xml"):
        try:
            return http_get_text(f"{base}/{primary_doc}")
        except Exception:
            pass
    idx = fetch_filing_index(cik, accession, http_get_json)
    if not idx:
        return None
    name = find_file_in_index(idx, r"^(?:wf-)?form4.*\.xml$") or find_file_in_index(idx, r"\.xml$")
    if not name:
        return None
    try:
        return http_get_text(f"{base}/{name}")
    except Exception:
        return None


# -------------------- SC 13D/G --------------------

def enrich_sc13(cik: str, accession: str, primary_doc: str,
                http_get_text: Callable, http_get_json: Callable) -> Optional[dict]:
    """Best-effort parse of SC 13D/G structured XML (post-2024 mandate).

    Returns {issuer_name, issuer_cusip, percent_of_class, aggregate_amount} or
    None if no XML or it doesn't match the schema.
    """
    base = _filing_dir(cik, accession)
    xml_text = None
    if primary_doc and primary_doc.lower().endswith(".xml"):
        try:
            xml_text = http_get_text(f"{base}/{primary_doc}")
        except Exception:
            pass
    if not xml_text:
        idx = fetch_filing_index(cik, accession, http_get_json)
        if idx:
            name = find_file_in_index(idx, r"primary_doc\.xml$") or find_file_in_index(idx, r"\.xml$")
            if name:
                try:
                    xml_text = http_get_text(f"{base}/{name}")
                except Exception:
                    return None
    if not xml_text:
        return None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    _strip_ns(root)

    issuer_name = (_find_text(root, ".//issuerName")
                   or _find_text(root, ".//nameOfIssuer"))
    cusip = _find_text(root, ".//issuerCusip") or _find_text(root, ".//cusip")
    percent = _to_float(_find_text(root, ".//percentOfClass"))
    aggregate = _to_float(_find_text(root, ".//aggregateAmountOwned"))

    if not (issuer_name or percent or aggregate):
        return None
    return {
        "issuer_name": issuer_name,
        "issuer_cusip": cusip,
        "percent_of_class": percent,
        "aggregate_amount": aggregate,
    }


# -------------------- 13F-HR --------------------

def _thirteenf_value_scale(raw_values) -> int:
    """Scale factor to turn 13F infotable `value` fields into whole dollars.

    The SEC's pre-2023 13F schema reports `value` in $thousands; the 2023+ schema
    reports whole dollars. The old code multiplied by 1000 unconditionally, which
    inflated every modern (dollar-denominated) filing 1000x. A 13F filer must hold
    >= $100M in 13(f) securities, so if a filing's raw value total is below that
    floor the values must still be in thousands (scale 1000); otherwise they are
    already dollars (scale 1).
    """
    raw_total = sum(v for v in raw_values if v)
    return 1000 if 0 < raw_total < 100_000_000 else 1


def fetch_13f_holdings(cik: str, accession: str,
                       http_get_text: Callable, http_get_json: Callable) -> Optional[list[dict]]:
    """Returns list of {cusip, issuer, value_usd, shares} or None."""
    idx = fetch_filing_index(cik, accession, http_get_json)
    if not idx:
        return None
    name = (find_file_in_index(idx, r"infotable.*\.xml$")
            or find_file_in_index(idx, r"informationtable\.xml$"))
    if not name:
        return None
    try:
        xml_text = http_get_text(f"{_filing_dir(cik, accession)}/{name}")
    except Exception:
        return None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    _strip_ns(root)

    # Determine the value unit (thousands vs dollars) once per filing from the
    # aggregate magnitude, then scale every holding consistently.
    info_tables = root.findall(".//infoTable")
    raw_values = [_to_float(_find_text(it, "value")) or 0 for it in info_tables]
    scale = _thirteenf_value_scale(raw_values)

    holdings = []
    for it, value_raw in zip(info_tables, raw_values):
        cusip = _find_text(it, "cusip") or ""
        shares = _to_float(_find_text(it, "shrsOrPrnAmt/sshPrnamt")) or 0
        issuer = _find_text(it, "nameOfIssuer") or ""
        if cusip:
            holdings.append({
                "cusip": cusip,
                "issuer": issuer,
                "value_usd": value_raw * scale,
                "shares": shares,
            })
    return holdings or None


def find_prior_13fhr_accession(cik: str, current_accession: str,
                               http_get_json: Callable) -> Optional[str]:
    """Look up the most recent 13F-HR (non-amendment) before current_accession."""
    try:
        data = http_get_json(f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json")
    except Exception:
        return None
    recent = data.get("filings", {}).get("recent", {})
    accs = recent.get("accessionNumber", [])
    forms = recent.get("form", [])
    found_current = False
    for i, acc in enumerate(accs):
        form = forms[i] if i < len(forms) else ""
        if acc == current_accession:
            found_current = True
            continue
        if not found_current:
            continue
        if form == "13F-HR":
            return acc
    return None


def enrich_13fhr(cik: str, accession: str,
                 http_get_text: Callable, http_get_json: Callable,
                 max_changes_per_bucket: int = 5) -> Optional[dict]:
    """Diff current 13F-HR vs previous 13F-HR for the same CIK.

    Returns {position_count, total_value_usd, new_positions[], exited[],
    increased[], decreased[], prior_accession} or None.
    """
    current = fetch_13f_holdings(cik, accession, http_get_text, http_get_json)
    if not current:
        return None
    total_value = sum(h["value_usd"] for h in current)
    by_cusip = {h["cusip"]: h for h in current}

    prior_acc = find_prior_13fhr_accession(cik, accession, http_get_json)
    summary = {
        "position_count": len(current),
        "total_value_usd": total_value,
        "new_positions": [],
        "exited": [],
        "increased": [],
        "decreased": [],
        "prior_accession": prior_acc,
    }
    if not prior_acc:
        return summary
    prior = fetch_13f_holdings(cik, prior_acc, http_get_text, http_get_json)
    if not prior:
        return summary
    prior_by_cusip = {h["cusip"]: h for h in prior}

    new_keys = set(by_cusip) - set(prior_by_cusip)
    exited_keys = set(prior_by_cusip) - set(by_cusip)
    common_keys = set(by_cusip) & set(prior_by_cusip)

    new_pos = sorted((by_cusip[k] for k in new_keys),
                     key=lambda h: -h["value_usd"])[:max_changes_per_bucket]
    exited_pos = sorted((prior_by_cusip[k] for k in exited_keys),
                        key=lambda h: -h["value_usd"])[:max_changes_per_bucket]

    increased, decreased = [], []
    for k in common_keys:
        cur = by_cusip[k]
        old = prior_by_cusip[k]
        delta_shares = cur["shares"] - old["shares"]
        if abs(delta_shares) < 1 or old["shares"] == 0:
            continue
        pct = delta_shares / old["shares"] if old["shares"] else 0
        if abs(pct) < 0.05:  # ignore <5% noise
            continue
        record = {**cur, "delta_shares": delta_shares, "pct_change": pct}
        if delta_shares > 0:
            increased.append(record)
        else:
            decreased.append(record)
    increased.sort(key=lambda h: -h["value_usd"])
    decreased.sort(key=lambda h: -h["value_usd"])

    summary["new_positions"] = new_pos
    summary["exited"] = exited_pos
    summary["increased"] = increased[:max_changes_per_bucket]
    summary["decreased"] = decreased[:max_changes_per_bucket]
    return summary


# -------------------- Money formatting --------------------

def fmt_money(n: Optional[float]) -> str:
    if n is None:
        return "?"
    n = float(n)
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 1_000_000_000:
        return f"{sign}${n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{sign}${n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{sign}${n / 1_000:.1f}K"
    return f"{sign}${n:,.0f}"


def fmt_shares(n: Optional[float]) -> str:
    if n is None:
        return "?"
    n = float(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:,.0f}"
