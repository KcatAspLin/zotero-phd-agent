import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

from .content import CATEGORIES, analyze, file_hash, make_plan, metadata, normalize_doi, read_pdf
from .gui import choose_pdf
from .local_llm import LLMConfig, analyze_local, local_text_budget
from .net import AgentError, HTTP
from .state import State, atomic_json
from .zotero import Zotero


def load_env(path):
    path = Path(path).expanduser()
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            raise AgentError("Invalid .env line; expected NAME=value")
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(name, value)


def process(args, state, http):
    doi = normalize_doi(args.doi)
    path = Path(args.pdf).expanduser().resolve()
    if not path.is_file():
        raise AgentError(f"PDF not found: {path}")
    sha = file_hash(path)
    config = LLMConfig.from_env()
    library = os.getenv("ZOTERO_LIBRARY_TYPE", "user") + ":" + os.getenv("ZOTERO_LIBRARY_ID", "")
    zot = None
    if args.command != "preview":
        zot = Zotero(http, os.getenv("ZOTERO_LIBRARY_ID", ""), os.getenv("ZOTERO_LIBRARY_TYPE", "user"),
                     os.getenv("ZOTERO_API_KEY", ""))
    with state.lock():
        job, identity = state.job(library, doi, sha)
        plan = job.get("plan")
        if plan is None or args.refresh or not config.matches(plan):
            key = os.getenv("OPENAI_API_KEY", "")
            if config.provider == "openai" and not key:
                raise AgentError("Set OPENAI_API_KEY in .env")
            meta = metadata(http, doi, os.getenv("CROSSREF_EMAIL", ""))
            if config.provider == "ollama":
                pdf = read_pdf(path, byte_budget=local_text_budget(meta, config.num_ctx))
            else:
                pdf = read_pdf(path)
            if pdf["sha256"] != sha:
                raise AgentError("PDF changed during reading; rerun once the copy finishes")
            classification = (analyze_local(http, meta, pdf, config) if config.provider == "ollama"
                              else analyze(http, meta, pdf, key, config.model))
            plan = make_plan(meta, pdf, classification, config.model)
            plan.update(provider=config.provider, analysis_config=config.cache_settings())
            job["plan"] = plan
            state.save()
        # Copy so a one-off manual choice does not modify the cached original analysis.
        plan = {**plan, "pdf": str(path), "review_reasons": list(plan["review_reasons"])}
        if args.category:
            plan["category"] = args.category
            plan["reason"] = "User category override. " + plan["reason"]
            plan["review_reasons"] = [r for r in plan["review_reasons"] if not r.startswith("Classification confidence")]
        if args.allow_missing:
            plan["review_reasons"] = [r for r in plan["review_reasons"] if not r.startswith("Missing metadata:")]
        report_path = state.directory / "reports" / (identity + ("-preview.json" if args.command == "preview" else ".json"))
        report = {"plan": plan, "status": "preview" if args.command == "preview" else "review_required"}
        if args.command == "preview":
            atomic_json(report_path, report)
            return {**report, "report": str(report_path)}
        if plan["review_reasons"]:
            atomic_json(report_path, report)
            return {**report, "report": str(report_path)}
        if file_hash(path) != sha:
            raise AgentError("PDF changed during analysis; rerun")
        result = zot.import_plan(plan, job, state.save, os.getenv("ZOTERO_PHD_COLLECTION_KEY", ""))
        report = {"plan": plan, **result}
        atomic_json(report_path, report)
        return {**report, "report": str(report_path)}


def enqueue(args):
    doi = normalize_doi(args.doi)
    pdf = Path(args.pdf).expanduser().resolve()
    if not pdf.is_file():
        raise AgentError(f"PDF not found: {pdf}")
    inbox = Path(args.inbox).expanduser().resolve()
    path = inbox / f"paper-{uuid.uuid4().hex[:12]}.json"
    atomic_json(path, {"doi": doi, "pdf": str(pdf)})
    return {"status": "queued", "manifest": str(path)}


def load_manifest(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) - {"doi", "pdf", "category", "allow_missing"}:
        raise AgentError("Manifest accepts doi, pdf, category and allow_missing only")
    if not all(isinstance(value.get(k), str) and value[k] for k in ("doi", "pdf")):
        raise AgentError("Manifest requires non-empty doi and pdf strings")
    if value.get("category") is not None and value["category"] not in CATEGORIES:
        raise AgentError("Invalid manifest category")
    if "allow_missing" in value and not isinstance(value["allow_missing"], bool):
        raise AgentError("allow_missing must be boolean")
    pdf = Path(value["pdf"]).expanduser()
    if not pdf.is_absolute():
        pdf = path.parent / pdf
    return value, pdf.resolve()


def watch(args, state, http):
    inbox = Path(args.inbox).expanduser().resolve()
    inbox.mkdir(parents=True, exist_ok=True)
    reports = inbox / "reports"
    reports.mkdir(exist_ok=True)
    observed = {}
    print(f"Watching {inbox}; Ctrl-C stops. Reports: {reports}", flush=True)
    failures = False
    while True:
        for manifest in sorted(inbox.glob("*.json")):
            raw = manifest.read_bytes()
            signature = hashlib.sha256(raw).hexdigest()
            pdf = None
            value = None
            error = None
            try:
                value, pdf = load_manifest(manifest)
                stat = pdf.stat()
                signature += f":{stat.st_size}:{stat.st_mtime_ns}"
            except (AgentError, OSError, ValueError) as exc:
                error = str(exc)
            result_path = reports / (manifest.stem + ".result.json")
            if result_path.exists():
                prior = json.loads(result_path.read_text(encoding="utf-8"))
                if prior.get("input_signature") == signature:
                    failures |= prior.get("status") != "complete"
                    continue
            previous = observed.get(str(manifest))
            observed[str(manifest)] = signature
            # Normal watch mode requires identical input stats in two consecutive polls.
            if not args.once and previous != signature:
                continue
            try:
                if error:
                    raise AgentError(error)
                single = argparse.Namespace(command="import", doi=value["doi"], pdf=str(pdf), refresh=False,
                                            category=value.get("category"), allow_missing=value.get("allow_missing", False))
                result = process(single, state, http)
            except (AgentError, OSError, ValueError, KeyError, TypeError) as exc:
                result = {"status": "failed", "error": str(exc)}
            atomic_json(result_path, {**result, "input_signature": signature})
            failures |= result["status"] != "complete"
            print(json.dumps({"manifest": manifest.name, "status": result["status"], "report": str(result_path)}), flush=True)
        if args.once:
            return 2 if failures else 0
        time.sleep(args.interval)


def parser():
    result = argparse.ArgumentParser(description="DOI + PDF → Zotero/PhD, one category and five generated tags")
    result.add_argument("--env", default=".env", help="Environment file (default .env in current directory)")
    result.add_argument("--state-dir", default=".zotero-agent", help="Keep this directory to resume uploads")
    sub = result.add_subparsers(dest="command", required=True)
    for name in ("preview", "import", "enqueue"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--doi", required=True)
        cmd.add_argument("--pdf", help="PDF path; omit to choose a file in a desktop window")
        if name == "enqueue":
            cmd.add_argument("--inbox", default="inbox")
        else:
            cmd.add_argument("--category", choices=CATEGORIES, help="Override automatic category")
            cmd.add_argument("--allow-missing", action="store_true", help="Allow empty metadata fields; never invent them")
            cmd.add_argument("--refresh", action="store_true", help="Fetch fresh metadata and repeat model analysis")
    cmd = sub.add_parser("watch")
    cmd.add_argument("--inbox", default="inbox")
    cmd.add_argument("--interval", type=float, default=10)
    cmd.add_argument("--once", action="store_true", help="Process one batch then exit")
    return result


def main():
    args = parser().parse_args()
    try:
        load_env(args.env)
        if args.command in ("preview", "import", "enqueue"):
            args.doi = normalize_doi(args.doi)
            if args.pdf is None:
                args.pdf = choose_pdf()
                if args.pdf is None:
                    print(json.dumps({"status": "cancelled", "message": "No PDF selected."}))
                    return 0
        if args.command == "enqueue":
            result = enqueue(args)
        else:
            state = State(args.state_dir)
            http = HTTP()
            if args.command == "watch":
                if args.interval < 1:
                    raise AgentError("Watch interval must be at least 1 second")
                return watch(args, state, http)
            result = process(args, state, http)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 2 if result["status"] == "review_required" else 0
    except KeyboardInterrupt:
        return 130
    except (AgentError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
