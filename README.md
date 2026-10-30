# PhD Zotero agent

Turn a DOI and a PDF into a Zotero record with authors, journal, publication date,
DOI, abstract, the PDF attachment, one category under **PhD**, and **five generated
scientific keyword tags**. Includes single-paper import and an automatic inbox.

## Install on your Mac

Requires Python 3.10 or newer and an internet connection. Unzip the package, open
Terminal, and change into the extracted `zotero-agent` directory:

```bash
cd /path/to/zotero-agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

Open `.env` in a text editor and set:

| Setting | Value |
| --- | --- |
| `ZOTERO_LIBRARY_ID` | Your numeric Zotero user ID, shown on the API keys page |
| `ZOTERO_LIBRARY_TYPE` | `user` for your personal library; `group` for a group library |
| `ZOTERO_API_KEY` | A Zotero key with library read/write and file access |
| `OPENAI_API_KEY` | Your OpenAI API key |
| `OPENAI_MODEL` | A model supporting Responses and Structured Outputs; example: `gpt-5-mini` |
| `CROSSREF_EMAIL` | Optional email for metadata requests |

Create your Zotero key at <https://www.zotero.org/settings/keys/new>. For a group,
use its numeric group ID and grant access to that group. Keep keys in your local
`.env`, not in chat or a shared repository. Existing shell environment variables
take priority over `.env`. The file accepts `NAME=value` with optional surrounding
quotes; it does not execute shell commands or expand variables.

This version uploads to **Zotero File Storage** using the Web API. Zotero desktop
does not need to be running for the upload; enable Zotero syncing to see changes
there. WebDAV and linked-file storage are not implemented. Available Zotero file
storage and model API access are required; model requests may incur API charges.

## Import one paper

Omit `--pdf` to open a desktop file-selection window. Select your PDF and click
**Open**; the agent uses its full path automatically:

```bash
zotero-agent import --doi '10.xxxx/your-doi'
```

The picker also works with `preview` and `enqueue`:

```bash
zotero-agent preview --doi '10.xxxx/your-doi'
zotero-agent enqueue --inbox inbox --doi '10.xxxx/your-doi'
```

Clicking **Cancel** exits without making API requests or changing Zotero. On
**macOS**, the agent uses the built-in native file picker through `osascript`;
**Tk is not required**, including in virtual environments, Homebrew or Conda
Python installations. On other desktop platforms, the picker uses Python's
optional Tk support; check it with `python -m tkinter`. If you are using SSH/a
server without a desktop, supply `--pdf` explicitly. The automatic watcher
continues to read paths from its job files without opening windows.

Run an optional preview first. It retrieves DOI metadata and asks the model to
read extracted PDF text, but makes **no Zotero requests or changes**:

```bash
zotero-agent preview --doi '10.xxxx/your-doi' --pdf '/path/to/paper.pdf'
```

The JSON output shows metadata, category, five tags, reason, confidence and any
review issues. Then import:

```bash
zotero-agent import --doi '10.xxxx/your-doi' --pdf '/path/to/paper.pdf'
```

The same DOI and PDF reuse the cached analysis, so preview followed by import
does not normally make a second model request. `--refresh` fetches metadata and
analyses the paper again. Files are attached as child PDFs of the bibliographic
record rather than imported as unlabelled standalone files.

## Automatic inbox

Start the watcher:

```bash
zotero-agent watch --inbox inbox
```

Leave it running. In another Terminal window, activate the same environment and
queue a paper:

```bash
cd /path/to/zotero-agent
source .venv/bin/activate
zotero-agent enqueue --inbox inbox --doi '10.xxxx/your-doi' --pdf '/path/to/paper.pdf'
```

The queue command writes a small job file referencing your PDF. It does not move
or delete your original. Keep that PDF available until the job completes. You
can also place `paper.pdf` in `inbox` and write `paper.json` alongside it:

```json
{
  "doi": "10.xxxx/your-doi",
  "pdf": "paper.pdf"
}
```

Copy the PDF first and save the JSON last. Relative PDF paths resolve from the
JSON file's directory. The watcher checks every ten seconds and waits for stable
input across two polls. Results go into `inbox/reports`. It skips unchanged jobs
that already have a result. To retry a failed job after fixing credentials or
connectivity, delete **only its result JSON** in `inbox/reports`, or run the
single-paper import command. Never delete `.zotero-agent` to retry an upload.

To run one batch and exit:

```bash
zotero-agent watch --inbox inbox --once
```

The watcher runs while the process and computer are awake. It is not a hosted
ChatGPT task or an installed login service. Run one watcher per inbox and use the
same state directory for all imports into that library.

## Collection and tag rules

The agent reuses or creates this exact collection structure:

| Collection | Main contribution |
| --- | --- |
| PhD → Methods | New reusable method, algorithm, tool, protocol or dataset |
| PhD → Perspectives | Viewpoint, opinion, commentary or research agenda |
| PhD → Research Article | Original empirical findings or other original studies |
| PhD → Reviews | Literature synthesis, systematic review or meta-analysis |
| PhD → Theory | Mathematical theory, formal derivation or explanatory model of general principles |

Explicit publication genre takes priority; otherwise the dominant contribution
decides. A paper using RNNs does not automatically count as Theory. Overlapping
or uncertain cases receive a lower confidence score. The score is the model's
self-assessment, **not a calibrated probability of correctness**.

The record is added directly to **PhD** and to exactly one of these five children.
On reclassification it leaves the other four category children; unrelated
collection memberships remain. If multiple root `PhD` collections exist, resolve
them or set `ZOTERO_PHD_COLLECTION_KEY` to the key of the intended `PhD`
collection. Repeated child names under that parent need manual resolution.

Five distinct tags are generated from the paper's scientific content. They aim
to cover topic, system, task, method and mechanism where supported. Relevant
terminology for your neuroscience/ML library includes `hippocampus`, `statistical
learning` and `recurrent neural networks`; these are not forced onto unrelated
papers. New records receive exactly five tags. Existing manual tags and other
imported tags remain, so existing records may have more than five total tags.
Reruns replace the agent's own previous automatic tags rather than accumulating
them. A `ZoteroAgent:` line in Zotero's Extra field records tag ownership and
metadata provenance; other Extra content is preserved.

## Metadata, checks and review

Bibliographic fields come from Crossref's DOI record, with DOI CSL content
negotiation as a fallback for DOIs not found there. Author order and given/family
name fields are preserved; consortium names use single-field creators. The date
is a publication date (print, then online, then issued), never a metadata deposit
timestamp. An available registry abstract takes priority. Otherwise, the model
copies the abstract from the PDF and the program checks that the text occurs in
the extracted PDF. It never substitutes a generated summary for an abstract.

Before any Zotero write, the program checks the paper's front-matter title
against the DOI metadata and checks the model's match assessment. This is a
heuristic identity check, not a guarantee. Difficult layouts can require review.
Missing required metadata, a title mismatch or category confidence below 0.65
returns `review_required` and leaves Zotero untouched for that attempt.

Choose a category explicitly when needed:

```bash
zotero-agent import --doi '10.xxxx/your-doi' --pdf '/path/to/paper.pdf' --category 'Methods'
```

For a paper that legitimately lacks an abstract or journal field, you may opt
into blank fields using `--allow-missing`. It never fills them with guesses or
bypasses the DOI/PDF identity check. Inbox JSON supports `"category": "Methods"`
and `"allow_missing": true` for the same overrides. Edit the JSON to trigger a
new attempt after review.

Existing bibliographic records are matched by exact normalised DOI across the
paginated library. The agent fills empty fields and preserves non-empty existing
values; differing fields are listed in the report as `preserved_existing_fields`.
It stops if multiple records have the same DOI. PDF attachments are deduplicated
by their MD5 hash under that record. Distinct PDF versions remain distinct
attachments. Existing record types other than `journalArticle` require manual
reconciliation; this first version creates journal-article records. Preprints
without journal information will normally require review.

Readable text PDFs up to 100 MiB are supported. Scans need OCR first. For papers
longer than 90,000 extracted characters, the model receives front matter plus
samples from the remaining pages; reports mark this with `sampled: true`.
Classification can miss details in omitted text.

## Recovery and privacy

Local state and reports are in `.zotero-agent` relative to the current working
directory. Use an absolute location if you run from different directories:

```bash
zotero-agent --state-dir "$HOME/ZoteroAgentState" import --doi '10.xxxx/your-doi' --pdf '/path/to/paper.pdf'
```

Keep that directory: reserved Zotero item/attachment keys allow an interrupted
import to resume. Upload completion is checked against the remote attachment
hash. Versioned updates stop on remote editing conflicts. If storage is full,
metadata may already have been saved; free storage and rerun the same command.
The same state directory prevents overlapping imports on one computer. Separate
computers/state directories do not provide a distributed lock; avoid concurrent
imports of the same DOI.

The DOI goes to metadata services. Extracted text and metadata go to OpenAI for
classification with `store: false`; this is not a promise of zero retention.
The PDF goes to Zotero and its authorised storage endpoint. API keys are scoped
to their respective services and never sent with the storage upload. Paper text
is treated as untrusted evidence and has no tool access. Reports contain paper
metadata and abstracts, but no API keys. Protect the state directory as you
would your reading library.

## Validation and limitations

Run the offline protocol and recovery tests:

```bash
python -m unittest discover -s tests -v
```

These tests simulate the metadata, model, Zotero and storage services, including
lost write responses, upload failures, duplicate imports, collection changes,
tag preservation and malformed inbox input. No live Zotero account or paid
model request was used during development. A live preview and one real import
are still needed with your credentials. Network/authentication errors and model
availability are reported at runtime. Linux/macOS are supported; the file lock
uses `fcntl`, so native Windows is not currently supported.

Exit codes: `0` success/preview, `1` error, `2` review required or failed batch,
`130` interrupted. Global `--env` and `--state-dir` options precede the subcommand.

Official references used for implementation:

- Zotero API v3: <https://www.zotero.org/support/dev/web_api/v3/basics>
- Item/collection writes: <https://www.zotero.org/support/dev/web_api/v3/write_requests>
- Attachment uploads: <https://www.zotero.org/support/dev/web_api/v3/file_upload>
- DOI metadata: <https://www.crossref.org/documentation/retrieve-metadata/rest-api/>
- Structured model output: <https://developers.openai.com/api/docs/guides/structured-outputs>
