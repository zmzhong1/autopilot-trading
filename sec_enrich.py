"""SEC filing enrichment — fetches & parses Form 4, 8-K, SC 13D/G, and 13F-HR
filings to extract the structured details that turn an "X filed Y" alert into
an actionable summary.

Stdlib-only (urllib + xml.etree.ElementTree). All network calls take a
caller-provided `http_get` function so tests/heartbeat can mock the SEC.

Each public `enrich_*` function returns a dict suitable for embedding in a
Discord alert, or None if the filing can't be parsed (we never raise — we
fall back to the simple alert format).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
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
        side = "sell" if side != "neutral" else "sell"
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

    holdings = []
    for it in root.findall(".//infoTable"):
        cusip = _find_text(it, "cusip") or ""
        # Old 13F values are reported in $thousands; normalize to dollars.
        value_raw = _to_float(_find_text(it, "value")) or 0
        # Heuristic: schema 2022+ uses dollars; older uses thousands.
        # Most filings use thousands. Multiply by 1000.
        value_usd = value_raw * 1000
        shares = _to_float(_find_text(it, "shrsOrPrnAmt/sshPrnamt")) or 0
        issuer = _find_text(it, "nameOfIssuer") or ""
        if cusip:
            holdings.append({
                "cusip": cusip,
                "issuer": issuer,
                "value_usd": value_usd,
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
