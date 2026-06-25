#!/usr/bin/env python3
"""EDGAR fetcher — pull SEC filings and XBRL facts with zero auth.

Stdlib only. SEC requires a descriptive User-Agent (name + email) on every
request or it returns HTTP 403. Set FLOWLY_SEC_UA to override the default.

Usage:
    edgar.py cik AAPL
    edgar.py filings AAPL --type 10-K -n 5
    edgar.py latest AAPL --type 10-Q
    edgar.py facts AAPL --tag Revenues [--unit USD]
    edgar.py search "going concern" [--type 10-K] [--ticker AAPL] [-n 10]
    edgar.py get <document-url-or-accession>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

UA = None  # resolved lazily

SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
COMPANYFACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
COMPANYCONCEPT = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{tag}.json"
TICKERS = "https://www.sec.gov/files/company_tickers.json"
FTS = "https://efts.sec.gov/LATEST/search-index?q={q}"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/"


def _ua() -> str:
    global UA
    if UA is None:
        import os
        UA = os.environ.get("FLOWLY_SEC_UA", "Flowly Research research@flowly.ai")
    return UA


def _get(url: str, *, raw: bool = False):
    req = urllib.request.Request(url, headers={"User-Agent": _ua(), "Accept-Encoding": "gzip, deflate"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                import gzip
                data = gzip.decompress(data)
            return data if raw else data.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} fetching {url}\n(SEC needs a real User-Agent; set FLOWLY_SEC_UA=\"Name email\")")
    except urllib.error.URLError as e:
        sys.exit(f"network error: {e}")


_tick_cache: dict[str, int] | None = None


def resolve_cik(ticker_or_cik: str) -> int:
    if ticker_or_cik.isdigit():
        return int(ticker_or_cik)
    global _tick_cache
    if _tick_cache is None:
        data = json.loads(_get(TICKERS))
        _tick_cache = {row["ticker"].upper(): int(row["cik_str"]) for row in data.values()}
    cik = _tick_cache.get(ticker_or_cik.upper())
    if cik is None:
        sys.exit(f"unknown ticker: {ticker_or_cik}")
    return cik


def cmd_cik(args):
    print(f"{args.ticker.upper()} -> CIK {resolve_cik(args.ticker):010d}")


def _recent_filings(cik: int):
    sub = json.loads(_get(SUBMISSIONS.format(cik=cik)))
    name = sub.get("name", "?")
    r = sub["filings"]["recent"]
    rows = []
    for i in range(len(r["accessionNumber"])):
        rows.append({
            "form": r["form"][i],
            "filed": r["filingDate"][i],
            "period": r["reportDate"][i],
            "accession": r["accessionNumber"][i],
            "primaryDoc": r["primaryDocument"][i],
            "desc": r["primaryDocDescription"][i],
        })
    return name, rows


def cmd_filings(args):
    cik = resolve_cik(args.ticker)
    name, rows = _recent_filings(cik)
    if args.type:
        want = args.type.upper()
        rows = [x for x in rows if x["form"].upper() == want]
    rows = rows[: args.n]
    print(f"{name} (CIK {cik:010d})")
    for x in rows:
        print(f"  {x['form']:<8} filed {x['filed']}  period {x['period']}  {x['accession']}")
        print(f"           {_doc_url(cik, x['accession'], x['primaryDoc'])}")


def _doc_url(cik: int, accession: str, doc: str) -> str:
    acc_nodash = accession.replace("-", "")
    return ARCHIVE.format(cik=cik, acc_nodash=acc_nodash) + doc


def cmd_latest(args):
    cik = resolve_cik(args.ticker)
    name, rows = _recent_filings(cik)
    if args.type:
        want = args.type.upper()
        rows = [x for x in rows if x["form"].upper() == want]
    if not rows:
        sys.exit(f"no {args.type or 'matching'} filings for {args.ticker}")
    x = rows[0]
    url = _doc_url(cik, x["accession"], x["primaryDoc"])
    print(f"{name} — {x['form']} filed {x['filed']} (period {x['period']})")
    print(f"Primary document: {url}")
    print(f"Filing index:     {ARCHIVE.format(cik=cik, acc_nodash=x['accession'].replace('-', ''))}")


def cmd_facts(args):
    cik = resolve_cik(args.ticker)
    tag = args.tag
    try:
        data = json.loads(_get(COMPANYCONCEPT.format(cik=cik, tag=tag)))
    except SystemExit:
        # fall back to scanning full companyfacts for a fuzzy tag match
        facts = json.loads(_get(COMPANYFACTS.format(cik=cik)))
        gaap = facts.get("facts", {}).get("us-gaap", {})
        match = [k for k in gaap if tag.lower() in k.lower()]
        if not match:
            sys.exit(f"no us-gaap tag matching '{tag}'. Try one of: {', '.join(list(gaap)[:20])} ...")
        print(f"matched tag: {match[0]} (candidates: {', '.join(match[:8])})")
        data = {"units": gaap[match[0]]["units"], "label": gaap[match[0]].get("label", match[0])}
    units = data["units"]
    unit_key = args.unit if args.unit in units else next(iter(units))
    rows = units[unit_key]
    # annual (FY) figures, most recent last
    fy = [r for r in rows if r.get("fp") == "FY" and r.get("form", "").startswith("10-K")]
    fy = fy or rows
    print(f"{data.get('label', tag)} [{unit_key}]")
    for r in fy[-8:]:
        period = r.get("end", "?")
        val = r.get("val")
        print(f"  {period}  {val:>18,}  ({r.get('form','?')} {r.get('fy','')}{r.get('fp','')})")


def cmd_search(args):
    params = {"q": f'"{args.query}"', "forms": args.type or "", "hits": str(args.n)}
    if args.ticker:
        params["entityName"] = args.ticker
    url = "https://efts.sec.gov/LATEST/search-index?" + urllib.parse.urlencode({k: v for k, v in params.items() if v})
    data = json.loads(_get(url))
    hits = data.get("hits", {}).get("hits", [])
    if not hits:
        print("no matches")
        return
    for h in hits:
        s = h.get("_source", {})
        cik = (s.get("ciks") or ["?"])[0]
        acc = h.get("_id", "").split(":")[0]
        print(f"  {s.get('form','?'):<8} {s.get('file_date','?')}  {(s.get('display_names') or ['?'])[0]}")
        print(f"           acc {acc}  cik {cik}")


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag in ("p", "div", "tr", "br", "li", "h1", "h2", "h3", "table"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data)


def cmd_get(args):
    target = args.url
    html = _get(target)
    if "<" in html and ">" in html:
        p = _TextExtractor()
        p.feed(html)
        text = "".join(p.parts)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    else:
        text = html
    if args.max and len(text) > args.max:
        text = text[: args.max] + f"\n\n... [truncated at {args.max} chars; use --max 0 for full]"
    print(text.strip())


def main():
    ap = argparse.ArgumentParser(description="EDGAR fetcher (SEC filings, no auth)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("cik"); p.add_argument("ticker"); p.set_defaults(fn=cmd_cik)

    p = sub.add_parser("filings"); p.add_argument("ticker"); p.add_argument("--type", default="")
    p.add_argument("-n", type=int, default=10); p.set_defaults(fn=cmd_filings)

    p = sub.add_parser("latest"); p.add_argument("ticker"); p.add_argument("--type", default="")
    p.set_defaults(fn=cmd_latest)

    p = sub.add_parser("facts"); p.add_argument("ticker"); p.add_argument("--tag", required=True)
    p.add_argument("--unit", default="USD"); p.set_defaults(fn=cmd_facts)

    p = sub.add_parser("search"); p.add_argument("query"); p.add_argument("--type", default="")
    p.add_argument("--ticker", default=""); p.add_argument("-n", type=int, default=10); p.set_defaults(fn=cmd_search)

    p = sub.add_parser("get"); p.add_argument("url"); p.add_argument("--max", type=int, default=200000)
    p.set_defaults(fn=cmd_get)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
