# Raw sources

This directory is the optional raw layer behind `knowledge/sources/*.yaml`.
It is deliberately outside the retrieval index.

## Rules

1. **Do not commit copyrighted full text** unless the licence explicitly
   allows redistribution. This is why `sources/papers/`, `sources/rfcs/`,
   `sources/code/`, and `sources/reports/` ship with README placeholders only.
2. `knowledge/sources/src-XXX.yaml` is the authoritative metadata record: title,
   authors, venue, year, DOI/URL, and the locators used by evidence entries.
   A raw file is optional and never replaces that record.
3. Locators must remain resolvable without the raw file. Prefer
   `"RFC 5681, Section 3.1"` or `"paper: Section 4.2"` over a page number in a
   local PDF, because page numbers shift between versions.
4. If you need a local copy for extraction, keep it in the matching
   subdirectory and add it to `.gitignore`; the Docker image does not copy
   `sources/raw/`.

## Layout

```
sources/
  README.md
  papers/    # paper PDFs or local notes (not redistributed)
  rfcs/      # RFC text downloads
  code/      # snapshots of kernel or implementation files
  reports/   # measurement reports, vendor documentation
```

## Adding a source

1. Assign the next `src-NNN` id.
2. Create `knowledge/sources/src-NNN.yaml` following `schemas/source.schema.json`.
3. Add at least one locator describing where each supported claim lives.
4. Cite it from the `evidence` list of at least one experience, otherwise
   `cca-kb quality` warns that the source is unused.
5. Run `cca-kb validate --strict && cca-kb quality`.

The extraction procedure is documented in
[`docs/data-processing.md`](../docs/data-processing.md).
