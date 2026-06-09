#!/usr/bin/env python3
"""
verify_citations.py - Cross-index citation existence checker for .bib files.

A standalone reference tool inspired by the ARS (academic-research-skills)
"cross-index triangulation" design. Standard-library ONLY - no pip install,
runs on Windows local or the Linux server venv unchanged.

------------------------------------------------------------------------------
WHAT IT DOES
------------------------------------------------------------------------------
Reads a BibTeX (.bib) file and, for each entry, checks whether the cited work
actually exists by querying public academic indexes:

    Crossref           https://api.crossref.org                 (DOI registry of record)
    OpenAlex           https://api.openalex.org                 (broad / OA coverage)
    arXiv              http://export.arxiv.org/api/query        (preprints; ID-gated)  [--arxiv]
    Semantic Scholar   https://api.semanticscholar.org/graph/v1                        [--s2]

It applies four "honesty primitives" (the load-bearing ideas from ARS):

  1. DOI_MISMATCH    A DOI that resolves but whose returned title disagrees with
                     the .bib title (similarity < 0.70) is REJECTED, not accepted.
                     This catches a fabricated/wrong DOI that happens to resolve
                     to a real-but-unrelated paper.
  2. Title cross-check   Even an exact DOI/ID hit must pass a 0.70 title-similarity
                     gate before it counts as a match.
  3. absent != false An index that errors (network / 5xx / timeout) has its signal
                     DROPPED, never recorded as "not found". An outage is not
                     evidence of fabrication.
  4. Triangulation   A reference is only flagged UNMATCHED when it misses in >= k
                     indexes that actually answered (default k = 2).

------------------------------------------------------------------------------
PRIVACY
------------------------------------------------------------------------------
Only the DOI / title / arXiv-id of each reference leaves your machine, sent to
the public APIs above. No file contents, no claim text. Providing --email puts
you in the Crossref / OpenAlex "polite pool" (10 req/s) and sends that email
with each request. arXiv uses plain HTTP (its API has no HTTPS endpoint).

------------------------------------------------------------------------------
VERDICTS (per reference)
------------------------------------------------------------------------------
  VERIFIED       matched in every index that answered
  PARTIAL        matched in >=1 index, missed in another (coverage gap, usually fine)
  DOI_MISMATCH   a DOI/ID resolved but the title disagrees           <- investigate
  UNMATCHED      missed in >= k answering indexes                     <- investigate
  INCONCLUSIVE   too few indexes answered (errors / skips) to decide

This is ADVISORY tooling. A clean run is not proof of correctness, and an
UNMATCHED is not proof of fabrication - the work may simply be unindexed
(books, very recent, non-English, grey literature). Always confirm by hand.

------------------------------------------------------------------------------
USAGE
------------------------------------------------------------------------------
  python verify_citations.py refs.bib
  python verify_citations.py refs.bib --email you@example.edu --csv report.csv
  python verify_citations.py refs.bib --arxiv --s2 --json report.json
  python verify_citations.py refs.bib -k 2 --strict --limit 20
  python verify_citations.py --selftest          # offline parser/similarity test

  Semantic Scholar key (optional, for 10 req/s instead of 1): set env S2_API_KEY.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from difflib import SequenceMatcher

# --------------------------------------------------------------------------- #
# Shared constants (the exact ARS values).                                     #
# --------------------------------------------------------------------------- #
MAX_RETRIES = 3          # retries on HTTP 429
BACKOFF_SECONDS = 2.0    # sleep between 429 retries
TIMEOUT = 30             # per-request socket timeout (s)
THRESHOLD = 0.70         # title-similarity acceptance threshold
USER_AGENT = "verify-citations/1.0 (+stdlib reference tool)"

ATOM_NS = "{http://www.w3.org/2005/Atom}"

# Per-source verdicts.
MATCH, MISS, MISMATCH, ERROR, SKIP = "MATCH", "MISS", "DOI_MISMATCH", "ERROR", "SKIP"


# --------------------------------------------------------------------------- #
# Title similarity (ARS uses difflib.SequenceMatcher, NOT true Levenshtein).  #
# --------------------------------------------------------------------------- #
_PUNCT = str.maketrans({c: " " for c in r"""!"#$%&'()*+,-./:;<=>?@[\]^_`{|}~"""})


def normalize_title(s: str) -> str:
    """Lowercase, turn punctuation into spaces, collapse whitespace."""
    return " ".join(s.lower().translate(_PUNCT).split())


def similar(a: str, b: str) -> float:
    """Ratcliff-Obershelp ratio in [0,1] on normalized titles."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()


# --------------------------------------------------------------------------- #
# Minimal BibTeX parser (brace-balanced; stdlib only).                        #
# --------------------------------------------------------------------------- #
@dataclass
class Entry:
    key: str
    etype: str
    title: str = ""
    doi: str = ""
    year: int | None = None
    arxiv_id: str | None = None
    results: dict = field(default_factory=dict)
    verdict: str = ""


def _read_braced(s: str, k: int) -> tuple[str, int]:
    """s[k] == '{'. Return (inner_text, index_after_close)."""
    depth, i, n = 0, k, len(s)
    while i < n:
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return s[k + 1:i], i + 1
        i += 1
    return s[k + 1:], n


def _read_quoted(s: str, k: int) -> tuple[str, int]:
    """s[k] == '\"'. Return (inner_text, index_after_close). Honors brace depth."""
    i, n, depth = k + 1, len(s), 0
    while i < n:
        c = s[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == '"' and depth == 0:
            return s[k + 1:i], i + 1
        i += 1
    return s[k + 1:], n


def _parse_fields(body: str) -> dict[str, str]:
    """Parse 'name = value, name = value, ...' (value is {..}, \"..\", or bare)."""
    fields: dict[str, str] = {}
    i, n = 0, len(body)
    while i < n:
        while i < n and not body[i].isalpha():        # find a field-name start
            i += 1
        j = i
        while j < n and (body[j].isalnum() or body[j] in "_-"):
            j += 1
        name = body[i:j].strip().lower()
        eq = body.find("=", j)
        if not name or eq < 0:
            break
        k = eq + 1
        while k < n and body[k] in " \t\r\n":
            k += 1
        if k >= n:
            break
        if body[k] == "{":
            val, k = _read_braced(body, k)
        elif body[k] == '"':
            val, k = _read_quoted(body, k)
        else:                                          # bare value -> until comma
            m = k
            while m < n and body[m] != ",":
                m += 1
            val, k = body[k:m].strip(), m
        fields[name] = val
        nxt = body.find(",", k)
        if nxt < 0:
            break
        i = nxt + 1
    return fields


def _clean(v: str) -> str:
    return " ".join(re.sub(r"[{}]", "", v).replace("\\&", "&").split())


def _clean_doi(v: str) -> str:
    v = _clean(v)
    v = re.sub(r"(?i)^https?://(dx\.)?doi\.org/", "", v)
    v = re.sub(r"(?i)^doi:\s*", "", v)
    return v.strip()


def _parse_year(v: str) -> int | None:
    m = re.search(r"\b(\d{4})\b", v or "")
    return int(m.group(1)) if m else None


def _parse_arxiv(fields: dict[str, str]) -> str | None:
    ep = _clean(fields.get("eprint", "")).strip()
    ap = _clean(fields.get("archiveprefix", "") or fields.get("eprinttype", "")).lower()
    if ep and ("arxiv" in ap or ap == ""):
        ep = re.sub(r"(?i)^arxiv:", "", ep).strip()
        if ep:
            return ep
    m = re.search(r"(?i)10\.48550/arxiv\.(.+)$", _clean_doi(fields.get("doi", "")))
    return m.group(1) if m else None


def parse_bibtex(text: str) -> list[Entry]:
    entries: list[Entry] = []
    pos, n = 0, len(text)
    while True:
        at = text.find("@", pos)
        if at < 0:
            break
        ob = text.find("{", at)
        if ob < 0:
            break
        etype = text[at + 1:ob].strip().lower()
        _, close = _read_braced(text, ob)            # close = index after '}'
        body = text[ob + 1:close - 1]
        pos = close
        if etype in ("comment", "preamble", "string"):
            continue
        comma = body.find(",")
        if comma < 0:
            continue
        key = body[:comma].strip()
        f = _parse_fields(body[comma + 1:])
        if not key:
            continue
        entries.append(Entry(
            key=key, etype=etype,
            title=_clean(f.get("title", "")),
            doi=_clean_doi(f.get("doi", "")),
            year=_parse_year(f.get("year", "") or f.get("date", "")),
            arxiv_id=_parse_arxiv(f),
        ))
    return entries


# --------------------------------------------------------------------------- #
# HTTP base: throttle + 429 backoff + 404->miss + degradation = Unavailable.  #
# --------------------------------------------------------------------------- #
class IndexUnavailable(Exception):
    """The index could not answer (network / 5xx / timeout). Signal is DROPPED."""


class _Client:
    name = "?"

    def __init__(self, min_interval: float, headers: dict[str, str] | None = None):
        self._min_interval = min_interval
        self._last: float | None = None
        self._headers = {"User-Agent": USER_AGENT, **(headers or {})}

    def _throttle(self) -> None:
        if self._last is None:
            return
        elapsed = time.monotonic() - self._last     # monotonic: immune to clock jumps
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)

    def _fetch(self, url: str) -> bytes | None:
        """Return body bytes, or None on HTTP 404 (a clean miss). Raise on degradation."""
        self._throttle()
        self._last = time.monotonic()
        req = urllib.request.Request(url, headers=self._headers)
        for attempt in range(MAX_RETRIES + 1):
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    return resp.read()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code == 429 and attempt < MAX_RETRIES:
                    time.sleep(BACKOFF_SECONDS)
                    self._last = time.monotonic()
                    continue
                raise IndexUnavailable(f"{self.name} HTTP {e.code}") from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                raise IndexUnavailable(f"{self.name} network error: {e}") from e
        raise IndexUnavailable(f"{self.name} retries exhausted")

    def _json(self, url: str) -> dict | None:
        raw = self._fetch(url)
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise IndexUnavailable(f"{self.name} parse error: {e}") from e

    # Each subclass implements lookup(entry) -> one of MATCH / MISS / MISMATCH / SKIP.
    def lookup(self, e: Entry) -> str:                # pragma: no cover - abstract
        raise NotImplementedError


class Crossref(_Client):
    name = "crossref"

    def __init__(self, email: str | None = None):
        ua = USER_AGENT + (f" (mailto:{email})" if email else "")
        super().__init__(0.1 if email else 0.2, {"User-Agent": ua})

    def lookup(self, e: Entry) -> str:
        if e.doi:
            data = self._json("https://api.crossref.org/works/"
                              + urllib.parse.quote(e.doi, safe=""))
            if data:
                title = (data.get("message", {}).get("title") or [""])[0]
                if similar(title, e.title) >= THRESHOLD:
                    return MATCH
                return MISMATCH                        # DOI resolved to a wrong title
        if e.title and self._title_search(e.title):
            return MATCH
        return MISS

    def _title_search(self, title: str) -> bool:
        data = self._json("https://api.crossref.org/works?"
                          + urllib.parse.urlencode({"query.title": title, "rows": "5"}))
        for it in (data or {}).get("message", {}).get("items", []):
            if similar((it.get("title") or [""])[0], title) >= THRESHOLD:
                return True
        return False


class OpenAlex(_Client):
    name = "openalex"
    FIELDS = "id,title,publication_year,doi"

    def __init__(self, email: str | None = None):
        super().__init__(0.1 if email else 1.0)
        self._email = email

    def _q(self, params: dict[str, str]) -> dict:
        if self._email:
            params["mailto"] = self._email
        return params

    def lookup(self, e: Entry) -> str:
        if e.doi:
            data = self._json("https://api.openalex.org/works/doi:"
                              + urllib.parse.quote(e.doi, safe="")
                              + "?" + urllib.parse.urlencode(self._q({"select": self.FIELDS})))
            if data:
                if similar(data.get("title") or "", e.title) >= THRESHOLD:
                    return MATCH
                return MISMATCH
        if e.title and self._title_search(e.title):
            return MATCH
        return MISS

    def _title_search(self, title: str) -> bool:
        data = self._json("https://api.openalex.org/works?"
                          + urllib.parse.urlencode(self._q(
                              {"search": title, "per-page": "5", "select": self.FIELDS})))
        for c in (data or {}).get("results", []):
            if similar(c.get("title") or "", title) >= THRESHOLD:
                return True
        return False


class SemanticScholar(_Client):
    name = "s2"
    BASE = "https://api.semanticscholar.org/graph/v1"
    FIELDS = "title,year,externalIds"

    def __init__(self, api_key: str | None = None):
        # Semantic Scholar's introductory API-key limit is 1 req/s on all
        # endpoints; the shared unauthenticated pool (1000 req/s globally) is
        # throttled hard when busy. A key buys reliability (a guaranteed ~1 req/s),
        # not raw speed - so pace both at 1 req/s and let the 429 backoff below
        # absorb any residual throttling.
        super().__init__(1.0, {"x-api-key": api_key} if api_key else None)

    def lookup(self, e: Entry) -> str:
        if e.doi:
            data = self._json(f"{self.BASE}/paper/DOI:"
                              + urllib.parse.quote(e.doi, safe="")
                              + f"?fields={self.FIELDS}")
            if data and data.get("paperId"):
                if similar(data.get("title") or "", e.title) >= THRESHOLD:
                    return MATCH
                return MISMATCH
        if e.title and self._title_search(e.title):
            return MATCH
        return MISS

    def _title_search(self, title: str) -> bool:
        data = self._json(f"{self.BASE}/paper/search?"
                          + urllib.parse.urlencode({"query": title, "limit": "5"})
                          + f"&fields={self.FIELDS}")
        for c in (data or {}).get("data", []):
            if similar(c.get("title") or "", title) >= THRESHOLD:
                return True
        return False


class Arxiv(_Client):
    name = "arxiv"
    BASE = "http://export.arxiv.org/api/query"          # arXiv has no HTTPS endpoint

    def __init__(self) -> None:
        super().__init__(3.0)                            # arXiv asks ~3s between calls

    def _entries(self, query: dict[str, str]) -> list[ET.Element]:
        raw = self._fetch(self.BASE + "?" + urllib.parse.urlencode(query))
        if raw is None:
            return []
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as e:
            raise IndexUnavailable(f"arxiv parse error: {e}") from e
        if root.tag != f"{ATOM_NS}feed":                 # HTML error page served as 200
            raise IndexUnavailable("arxiv returned a non-Atom body")
        return root.findall(f"{ATOM_NS}entry")

    @staticmethod
    def _title(entry: ET.Element) -> str:
        node = entry.find(f"{ATOM_NS}title")
        return " ".join(node.text.split()) if node is not None and node.text else ""

    def lookup(self, e: Entry) -> str:
        if not e.arxiv_id:
            return SKIP                                  # ID-gated: no spurious signal
        entries = self._entries({"id_list": e.arxiv_id})
        if entries:
            if similar(self._title(entries[0]), e.title) >= THRESHOLD:
                return MATCH
            return MISMATCH
        if e.title:
            for c in self._entries({"search_query": f'ti:"{e.title}"', "max_results": "5"}):
                if similar(self._title(c), e.title) >= THRESHOLD:
                    return MATCH
        return MISS


# --------------------------------------------------------------------------- #
# Triangulation (absent != false): ERROR/SKIP excluded from the decision.     #
# --------------------------------------------------------------------------- #
def triangulate(results: dict[str, str], k: int) -> str:
    verdicts = list(results.values())
    if MISMATCH in verdicts:
        return MISMATCH
    definitive = [v for v in verdicts if v in (MATCH, MISS)]
    matches = [v for v in definitive if v == MATCH]
    misses = [v for v in definitive if v == MISS]
    if matches and not misses:
        return "VERIFIED"
    if matches and misses:
        return "PARTIAL"
    if len(misses) >= k:
        return "UNMATCHED"
    return "INCONCLUSIVE"


# --------------------------------------------------------------------------- #
# Reporting.                                                                   #
# --------------------------------------------------------------------------- #
_CELL = {MATCH: "ok", MISS: "miss", MISMATCH: "MISMATCH", ERROR: "err", SKIP: "-"}
_FLAG = {"DOI_MISMATCH", "UNMATCHED"}


def print_table(entries: list[Entry], sources: list[str]) -> None:
    head = f"{'':1} {'KEY':<26} {'YEAR':>4} {'DOI':>3} " + \
        " ".join(f"{s[:8]:^9}" for s in sources) + "  VERDICT"
    print(head)
    print("-" * len(head))
    for e in entries:
        cells = " ".join(f"{_CELL.get(e.results.get(s, ERROR), '?'):^9}" for s in sources)
        mark = "!" if e.verdict in _FLAG else " "
        key = (e.key[:25] + "+") if len(e.key) > 26 else e.key
        print(f"{mark} {key:<26} {str(e.year or '-'):>4} "
              f"{('Y' if e.doi else '-'):>3} {cells}  {e.verdict}")


def print_summary(entries: list[Entry]) -> None:
    counts: dict[str, int] = {}
    for e in entries:
        counts[e.verdict] = counts.get(e.verdict, 0) + 1
    print("\nSummary:")
    for v in ("VERIFIED", "PARTIAL", "INCONCLUSIVE", "DOI_MISMATCH", "UNMATCHED"):
        if counts.get(v):
            print(f"  {v:<13} {counts[v]}")
    flagged = [e for e in entries if e.verdict in _FLAG]
    if flagged:
        print(f"\n{len(flagged)} reference(s) need a manual look:")
        for e in flagged:
            print(f"  [{e.verdict}] {e.key}"
                  + (f"  doi={e.doi}" if e.doi else "")
                  + (f'  "{e.title[:70]}"' if e.title else ""))


def write_csv(path: str, entries: list[Entry], sources: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["key", "type", "year", "doi", "arxiv_id", *sources, "verdict", "title"])
        for e in entries:
            w.writerow([e.key, e.etype, e.year or "", e.doi, e.arxiv_id or "",
                        *[e.results.get(s, "") for s in sources], e.verdict, e.title])
    print(f"\nCSV written: {path}")


def write_json(path: str, entries: list[Entry], sources: list[str]) -> None:
    payload = [{"key": e.key, "type": e.etype, "year": e.year, "doi": e.doi,
                "arxiv_id": e.arxiv_id, "title": e.title,
                "per_source": {s: e.results.get(s) for s in sources},
                "verdict": e.verdict} for e in entries]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(f"JSON written: {path}")


# --------------------------------------------------------------------------- #
# Offline self-test (no network).                                             #
# --------------------------------------------------------------------------- #
def selftest() -> int:
    sample = r"""
@article{vaswani2017,
  title   = {Attention Is All You Need},
  author  = {Vaswani, Ashish and others},
  year    = {2017},
  eprint  = {1706.03762},
  archivePrefix = {arXiv},
  doi     = {10.48550/arXiv.1706.03762}
}
@inproceedings{he2016deep,
  title = {Deep Residual Learning for {Image} Recognition},
  year  = "2016",
  doi   = {https://doi.org/10.1109/CVPR.2016.90}
}
"""
    ents = parse_bibtex(sample)
    assert len(ents) == 2, f"expected 2 entries, got {len(ents)}"
    a, b = ents
    assert a.key == "vaswani2017" and a.year == 2017, a
    assert a.arxiv_id == "1706.03762", a.arxiv_id
    assert b.title == "Deep Residual Learning for Image Recognition", repr(b.title)
    assert b.doi == "10.1109/CVPR.2016.90", b.doi          # url + prefix stripped
    assert similar("Attention Is All You Need", "attention is all you need!") > 0.95
    assert similar("R.A.G.", "RAG") >= 0.70                 # punctuation normalization
    assert similar("Deep Residual Learning", "A Totally Different Title") < 0.70
    assert triangulate({"crossref": MATCH, "openalex": MATCH}, 2) == "VERIFIED"
    assert triangulate({"crossref": MATCH, "openalex": MISS}, 2) == "PARTIAL"
    assert triangulate({"crossref": MISS, "openalex": MISS}, 2) == "UNMATCHED"
    assert triangulate({"crossref": MISS, "openalex": ERROR}, 2) == "INCONCLUSIVE"
    assert triangulate({"crossref": MISMATCH, "openalex": MATCH}, 2) == "DOI_MISMATCH"
    print("selftest OK - parser, similarity, and triangulation all pass.")
    return 0


# --------------------------------------------------------------------------- #
# CLI.                                                                         #
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Cross-index citation existence checker for .bib files.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("bibfile", nargs="?", help="path to a .bib file")
    p.add_argument("--email", help="email for Crossref/OpenAlex polite pool (10 req/s)")
    p.add_argument("--arxiv", action="store_true", help="also query arXiv (ID-gated)")
    p.add_argument("--s2", action="store_true",
                   help="also query Semantic Scholar (reads S2_API_KEY env if set)")
    p.add_argument("-k", "--min-miss", type=int, default=2, metavar="K",
                   help="flag UNMATCHED only when missed in >= K answering indexes (default 2)")
    p.add_argument("--limit", type=int, default=0, help="check only the first N entries")
    p.add_argument("--csv", metavar="PATH", help="write a CSV report")
    p.add_argument("--json", metavar="PATH", help="write a JSON report")
    p.add_argument("--strict", action="store_true",
                   help="exit non-zero if any DOI_MISMATCH or UNMATCHED is found")
    p.add_argument("--selftest", action="store_true", help="run offline self-test and exit")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.selftest:
        return selftest()
    if not args.bibfile:
        print("error: a .bib file is required (or use --selftest).", file=sys.stderr)
        return 2
    if not os.path.isfile(args.bibfile):
        print(f"error: no such file: {args.bibfile}", file=sys.stderr)
        return 2

    with open(args.bibfile, encoding="utf-8", errors="replace") as fh:
        entries = parse_bibtex(fh.read())
    if args.limit > 0:
        entries = entries[:args.limit]
    if not entries:
        print("No BibTeX entries found.", file=sys.stderr)
        return 1

    clients: list[_Client] = [Crossref(args.email), OpenAlex(args.email)]
    if args.s2:
        clients.append(SemanticScholar(os.environ.get("S2_API_KEY")))
    if args.arxiv:
        clients.append(Arxiv())
    sources = [c.name for c in clients]

    if not args.email:
        print("[note] no --email: Crossref/OpenAlex run at the slower anonymous rate.",
              file=sys.stderr)
    print(f"Checking {len(entries)} reference(s) against: {', '.join(sources)}\n",
          file=sys.stderr)

    for i, e in enumerate(entries, 1):
        for c in clients:
            try:
                e.results[c.name] = c.lookup(e)
            except IndexUnavailable as ex:
                e.results[c.name] = ERROR
                print(f"  [{i}/{len(entries)}] {c.name}: {ex}", file=sys.stderr)
        e.verdict = triangulate(e.results, args.min_miss)
        print(f"  [{i}/{len(entries)}] {e.key}: {e.verdict}", file=sys.stderr)

    print()
    print_table(entries, sources)
    print_summary(entries)
    if args.csv:
        write_csv(args.csv, entries, sources)
    if args.json:
        write_json(args.json, entries, sources)

    if args.strict and any(e.verdict in _FLAG for e in entries):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
