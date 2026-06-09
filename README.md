# citation-verifier

[![DOI](https://img.shields.io/badge/DOI-10.5281%2Fzenodo.20602827-blue)](https://doi.org/10.5281/zenodo.20602827) ![license](https://img.shields.io/badge/license-MIT-blue) ![python](https://img.shields.io/badge/python-3.8%2B-blue) ![dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)

A tiny, dependency-free tool that checks whether the references in a BibTeX
(`.bib`) file **actually exist** — by cross-checking each one against public
scholarly indexes (Crossref, OpenAlex, arXiv, Semantic Scholar).

It is built to catch, *before submission*, the three things that slip past a
human eye in a 150-entry bibliography:

1. **Fabricated references** — a growing problem with AI-assisted writing.
2. **Wrong / mistyped DOIs** that point to a different paper.
3. **Wrong titles, authors, or years** that no index can resolve.

> **Why this matters.** Zhao et al. (2026, *LLM hallucinations in the wild:
> Large-scale evidence from non-existent citations*, arXiv:2605.07723) audited
> 111 million references across 2.5 million papers and estimated **~146,932
> hallucinated citations in 2025 alone** — concentrated among early-career
> researchers and AI-assisted manuscripts. This tool is a small, transparent
> defense you can run yourself.

This is **advisory** tooling. A clean run is not proof of correctness, and an
`UNMATCHED` flag is not proof of fabrication — always confirm flagged entries
by hand.

---

## What it does *not* do

- It does **not** check whether the *claims* in your paper are supported by the
  cited sources — only whether the references **exist**.
- It does **not** edit your `.bib` — it only reads and reports.
- It does **not** judge citation style/format (APA, IEEE, …).

---

## How it works — four "honesty primitives"

For every reference it asks each index *"does this work exist?"*, then combines
the answers. The logic is deliberately conservative:

1. **DOI cross-check** — a DOI that resolves but whose returned title disagrees
   with your `.bib` title (similarity < 0.70) is **rejected** as `DOI_MISMATCH`,
   not accepted. This catches a wrong DOI that happens to resolve to a real but
   unrelated paper.
2. **Title gate** — even an exact DOI/ID hit must pass a 0.70 title-similarity
   check (case-insensitive, punctuation-normalized).
3. **Outage ≠ fabrication** — if an index errors (network / 5xx / timeout) its
   signal is **dropped**, never recorded as "not found".
4. **Triangulation** — a reference is only flagged `UNMATCHED` when it misses in
   **≥ k indexes that actually answered** (default `k = 2`).

---

## Indexes queried

| Index | Default | Auth needed | Notes |
|---|---|---|---|
| [Crossref](https://api.crossref.org) | on | none (email optional) | DOI registry of record; best for journal articles |
| [OpenAlex](https://api.openalex.org) | on | none (email optional) | broad / open-access coverage |
| [arXiv](http://export.arxiv.org) | `--arxiv` | none | preprints; only checked for entries with an arXiv id |
| [Semantic Scholar](https://www.semanticscholar.org/product/api) | `--s2` | key optional | extra coverage; see key section below |

Only the **DOI / title / arXiv-id** of each reference leaves your machine. No
file contents, no paper text.

---

## Install

No dependencies — Python 3.8+ standard library only. Just get the single file:

```bash
git clone https://github.com/tvquynh/citation-verifier.git
cd citation-verifier
```

(or download `verify_citations.py` on its own — it is fully self-contained.)

---

## Quick start

```bash
# 1. Confirm it works (offline, no network):
python verify_citations.py --selftest

# 2. Try the bundled sample (queries Crossref + OpenAlex):
python verify_citations.py examples/sample.bib

# 3. Run on your own bibliography:
python verify_citations.py path/to/refs.bib
```

Expected output on the sample:

```
  KEY                        YEAR DOI crossref  openalex   VERDICT
------------------------------------------------------------------
  he2016resnet               2016   Y    ok        ok      VERIFIED
  vaswani2017attention       2017   -    ok        ok      VERIFIED
! wrongdoi_demo              2020   Y MISMATCH  MISMATCH   DOI_MISMATCH
! fabricated_demo            2031   -   miss      miss     UNMATCHED
```

Lines marked `!` need a manual look.

---

## Usage

```
python verify_citations.py BIBFILE [options]

  --email EMAIL     contact email for the Crossref/OpenAlex "polite pool"
                    (~10 req/s, much faster on large files; no registration)
  --arxiv           also query arXiv (only for entries carrying an arXiv id)
  --s2              also query Semantic Scholar (reads S2_API_KEY env if set)
  -k, --min-miss K  flag UNMATCHED only when missed in >= K answering indexes
                    (default 2)
  --limit N         check only the first N entries (handy for a quick test)
  --csv PATH        write a CSV report
  --json PATH       write a JSON report
  --strict          exit non-zero if any DOI_MISMATCH or UNMATCHED is found
                    (useful in CI)
  --selftest        run the offline self-test and exit
```

### Verdicts

| Verdict | Meaning |
|---|---|
| `VERIFIED` | matched in every index that answered |
| `PARTIAL` | matched in ≥1 index, missed in another (usually a coverage gap) |
| `DOI_MISMATCH` | a DOI/ID resolved but the title disagrees — **fix the DOI** |
| `UNMATCHED` | missed in ≥ k answering indexes — **investigate** |
| `INCONCLUSIVE` | too few indexes answered (errors/skips) to decide |

### Reading the CSV

```bash
python verify_citations.py refs.bib --email you@example.edu --csv report.csv
```

Then, to list just the problems (PowerShell example):

```powershell
Import-Csv report.csv | Where-Object { $_.verdict -in 'DOI_MISMATCH','UNMATCHED' } | Format-Table key,verdict,doi,title
```

---

## Faster, friendlier: the `--email` polite pool

Crossref and OpenAlex offer a "polite pool" with higher, more stable rate
limits (about **10 requests/second**) to clients that identify themselves with a
contact email. **No registration** — just pass your email:

```bash
python verify_citations.py refs.bib --email you@example.edu
```

Without `--email`, these two run at the slower anonymous rate. For a large
bibliography (100+ references) the email typically makes the run several times
faster.

---

## Optional: a Semantic Scholar API key

The tool works **without any key**. A Semantic Scholar (S2) key is optional and
only matters when you pass `--s2`.

- **Without a key**, S2 requests use the shared *unauthenticated* pool
  (1000 req/s shared across **all** users worldwide, and throttled hard during
  busy periods).
- **With a key**, you get a guaranteed introductory rate of **1 request/second**
  on all endpoints. The key buys *reliability*, not raw speed.

### How to request a key

1. Go to the Semantic Scholar API page:
   **https://www.semanticscholar.org/product/api**
2. Find the **"Request an API Key"** form on that page (the `#api-key` section).
3. Submit your details and a short description of your intended use.
4. You will receive your **private key by email**. This is not always instant —
   it can take some time, so request it ahead of need. **Keep the key private;
   never commit it to a repository.**

Official API documentation: https://api.semanticscholar.org/api-docs/

### How to use the key

Set it as an environment variable before running with `--s2`:

```bash
# Linux / macOS
export S2_API_KEY="your-key-here"

# Windows — PowerShell (current session only)
$env:S2_API_KEY = "your-key-here"

# Windows — persist across sessions
setx S2_API_KEY "your-key-here"
```

Then:

```bash
python verify_citations.py refs.bib --s2 --arxiv --email you@example.edu --csv report.csv
```

---

## Privacy

Running the tool sends each reference's **DOI / title / arXiv-id** to the public
APIs above (this is published bibliographic metadata, not your manuscript). With
`--email`, your contact email is sent to Crossref/OpenAlex per their polite-pool
convention. arXiv uses plain HTTP because its API has no HTTPS endpoint.

---

## Limitations

- `UNMATCHED` ≠ fabricated. A real work can be unindexed: books, very recent
  papers, non-English sources, grey literature, some conference proceedings.
- Title matching uses `difflib.SequenceMatcher` (Ratcliff–Obershelp ratio), not
  Levenshtein edit distance; the 0.70 threshold is calibrated to that ratio.
- Coverage differs by index and discipline. Using several indexes together
  (the default Crossref + OpenAlex, plus `--arxiv` / `--s2`) reduces both false
  positives and false negatives.

---

## Acknowledgements

The design — cross-index triangulation, the DOI-mismatch check, and the
"outage ≠ fabrication" rule — is inspired by the integrity ideas in the
[Academic Research Skills](https://github.com/Imbad0202/academic-research-skills)
project. This tool is an **independent, clean-room implementation** written
directly from the public API documentation of Crossref, OpenAlex, arXiv, and
Semantic Scholar. It is **not affiliated with or endorsed by** that project.

## Development note

This tool was developed with AI assistance. The author reviewed and verified all
outputs and remains fully responsible for the code.

## License

[MIT](LICENSE) © 2026 Van-Quynh Trinh, Posts and Telecommunications Institute of
Technology (PTIT).

## Citing

If this tool is useful in your work, please cite it. See [`CITATION.cff`](CITATION.cff), or use:

> Trinh, V.-Q. (2026). *citation-verifier* (Version 1.0.0) [Computer software].
> https://doi.org/10.5281/zenodo.20602827

- **Concept DOI** (always resolves to the latest version): [10.5281/zenodo.20602827](https://doi.org/10.5281/zenodo.20602827)
- **This version (1.0.0)**: [10.5281/zenodo.20602828](https://doi.org/10.5281/zenodo.20602828)
