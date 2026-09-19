# Local Papers

Research papers stored as local PDFs in `$OV/papers/` (L3 externally certified knowledge: peer-reviewed or high-citation). Used for deep reading sessions when a paper is not available via URL or when the workflow needs the canonical PDF on disk.

## Directory Structure

```
$OV/papers/
├── <firstauthor>_<venue>_<year>_<name>.pdf        ← raw PDF (flat, agent-only)
├── <firstauthor>_<venue>_<year>_<name>-notes.md   ← optional notes/artifacts
└── ...

$OV/cache/<paper-slug>/
├── paper.txt                                      ← reusable L1 extraction
├── index.md                                       ← source pointer + cache contract
└── source.json                                    ← freshness signature
```

## How to Use

Papers are referenced by filename. Keep the canonical PDF in `<paths.papers>/`
or `<paths.preprints>/`, then materialize reusable text before dispatching a
Reader or Scholar:

```bash
python3 scripts/paper_cache.py "$OV/papers/<file>.pdf"
```

The helper returns `<paths.cache>/<paper-slug>/`, reuses a fresh extraction,
and rebuilds it when the source PDF changes. Pass that directory as
`cache_path`; Readers consume `paper.txt` and `index.md` without re-extracting
the PDF. The text is an L1 derivative, not L3 evidence, so it does not sit next
to the canonical PDF.

Page images and other one-session renderings follow the shared scratch rule in
`AGENTS.md`: create them under `mktemp -d`. Promote an image into `$OV/` only
when it becomes a durable reading artifact.

Use a stable, descriptive slug (kebab-case title or `<venue>-<id>`) as the
basename.

### Naming convention
`<firstauthor>_<venue>_<year>_<name>.pdf` (flat store, agent-only). Example: `nvidia_techreport_2026_cosmos3.pdf`. `<firstauthor>` is the first-author surname or the corporate author (e.g. `nvidia`); `<venue>` is the publication venue, or `arxiv` / `preprint` / `techreport` when there is none; `<year>` is the 4-digit year; `<name>` is a short lower-case stem.

Legacy `<slugified-title>.pdf` and `<source>-<id>.pdf` files are tolerated on read. The cache-hit check in `read.md` (Local cache check) globs the store on author or distinctive keyword, so either grammar resolves; new files should use the structured grammar above so that glob stays reliable.

## Local extraction

`scripts/paper_cache.py` is the single extraction entrypoint. It is stdlib-only
and invokes Poppler's `pdftotext -layout`; no network API is involved. It
refuses PDFs outside `<paths.papers>/` and `<paths.preprints>/` so a web-fetched
paper must first be placed in its canonical L3 store.
