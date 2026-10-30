import argparse
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zotero_agent import cli
from zotero_agent.content import (CATEGORIES, analyze, date_string, file_hash, make_plan,
                                  metadata, normalize_doi, read_pdf, validate_analysis)
from zotero_agent.net import AgentError, HttpError, Response
from zotero_agent.state import State
from zotero_agent.zotero import MARKER, Zotero, merged_tags

DOI = "10.1234/test"
TITLE = "Statistical learning in recurrent neural networks"
ABSTRACT = "We study how recurrent neural networks learn auditory sequences and form predictions."
TAGS = ["statistical learning", "recurrent neural networks", "auditory sequences", "prediction", "generalisation"]


def response(value, status=200):
    return Response(status, {}, json.dumps(value).encode())


def write_pdf(path):
    text = TITLE + ". Abstract. " + ABSTRACT + " " + ("Auditory sequence learning. " * 12)
    stream = f"BT /F1 10 Tf 20 700 Td ({text}) Tj ET".encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"]
    result = b"%PDF-1.4\n"
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(result)
    result += f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode()
    result += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    result += f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    path.write_bytes(result)


def analysis():
    return {"category": "Theory", "tags": TAGS[:], "confidence": .9,
            "reason": "An explanatory computational model is the primary contribution.",
            "pdf_title": TITLE, "pdf_abstract": ABSTRACT, "matches_doi_metadata": True}


class FakeHTTP:
    """Protocol simulator including mutations, storage upload and remote versions."""
    def __init__(self):
        self.objects = {"items": {}, "collections": {}}
        self.calls = []
        self.model_calls = 0
        self.local_calls = 0
        self.classification = analysis()
        self.abstract = ""
        self.uploads = 0
        self.upload_md5 = {}
        self.failure = None
        self.version = 0
        self.crossref_missing = False

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if "api.crossref.org" in url:
            if self.crossref_missing:
                raise HttpError(404, "api.crossref.org")
            return response({"message": {"DOI": DOI, "title": [TITLE], "container-title": ["Test Journal"],
                                         "author": [{"given": "Ada", "family": "Example"}],
                                         "published-online": {"date-parts": [[2026, 10, 1]]}, "abstract": self.abstract}})
        if "doi.org/" in url:
            return response({"DOI": DOI, "title": TITLE, "container-title": "Test Journal",
                             "author": [{"literal": "Research Consortium"}],
                             "issued": {"date-parts": [[2026]]}})
        if "api.openai.com" in url:
            self.model_calls += 1
            return response({"status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": json.dumps(self.classification)}]}]})
        if url.endswith("/api/chat"):
            self.local_calls += 1
            assert not kw.get("headers", {}).get("Authorization")
            return response({"done": True, "done_reason": "stop", "message": {"content": json.dumps(self.classification)}})
        if "storage.example" in url:
            assert "Zotero-API-Key" not in kw.get("headers", {})
            assert "Authorization" not in kw.get("headers", {})
            self.uploads += 1
            if self.failure == "upload":
                self.failure = None
                raise AgentError("Simulated storage failure")
            return response({}, 201)
        path, _, query = url.split("/users/123/", 1)[1].partition("?")
        parts = path.split("/")
        endpoint = parts[0]
        bucket = self.objects[endpoint]
        if method == "GET":
            if path == "items/top":
                records = [v for v in bucket.values() if not v["data"].get("parentItem")]
            elif len(parts) == 3 and parts[2] == "children":
                records = [v for v in bucket.values() if v["data"].get("parentItem") == parts[1]]
            elif len(parts) == 2:
                if parts[1] not in bucket:
                    raise HttpError(404, "api.zotero.org")
                return response(copy.deepcopy(bucket[parts[1]]))
            else:
                records = list(bucket.values())
            from urllib.parse import parse_qs
            q = parse_qs(query)
            start = int(q.get("start", [0])[0])
            return response(copy.deepcopy(records[start:start+100]))
        if method == "PATCH":
            item = bucket[parts[1]]
            if kw["headers"]["If-Unmodified-Since-Version"] != str(item["version"]):
                raise HttpError(412, "api.zotero.org")
            item["data"].update(kw["json_data"])
            self.version += 1
            item["version"] = item["data"]["version"] = self.version
            return response({}, 204)
        if len(parts) == 3 and parts[2] == "file":
            key = parts[1]
            form = kw["form"]
            assert kw["headers"]["If-None-Match"] == "*"
            if "upload" in form:
                self.objects["items"][key]["data"]["md5"] = self.upload_md5[key]
                return response({}, 204)
            assert form["mtime"] > 10**12
            self.upload_md5[key] = form["md5"]
            return response({"url": "https://storage.example/upload", "prefix": "prefix", "suffix": "suffix",
                             "contentType": "multipart/form-data; boundary=test", "uploadKey": "upload"})
        data = copy.deepcopy(kw["json_data"][0])
        key = data["key"]
        if key in bucket:
            raise AgentError("Unexpected duplicate creation")
        self.version += 1
        data["version"] = self.version
        item = {"key": key, "version": self.version, "data": data}
        bucket[key] = item
        if self.failure and self.failure == data.get("itemType"):
            self.failure = None
            raise AgentError("Simulated lost response after committed write")
        return response({"successful": {"0": item}, "failed": {}, "unchanged": {}})


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pdf = self.root / "paper.pdf"
        write_pdf(self.pdf)
        self.http = FakeHTTP()
        self.state = State(self.root / "state")
        self.env = patch.dict(os.environ, {"ZOTERO_LIBRARY_ID": "123", "ZOTERO_LIBRARY_TYPE": "user",
                                          "ZOTERO_API_KEY": "fake-zotero", "OPENAI_API_KEY": "fake-openai",
                                          "OPENAI_MODEL": "fake-model", "ZOTERO_PHD_COLLECTION_KEY": "", "LLM_PROVIDER": "openai"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_agent(self, command="import", **kwargs):
        return cli.process(argparse.Namespace(command=command, doi=DOI, pdf=str(self.pdf),
                                              refresh=kwargs.pop("refresh", False), category=kwargs.pop("category", None),
                                              allow_missing=kwargs.pop("allow_missing", False), **kwargs), self.state, self.http)

    def test_full_import_pdf_metadata_five_tags_and_collections(self):
        result = self.run_agent()
        self.assertEqual(result["status"], "complete")
        item = self.http.objects["items"][result["item_key"]]["data"]
        self.assertEqual(item["abstractNote"], ABSTRACT)
        self.assertEqual(item["creators"][0]["lastName"], "Example")
        self.assertEqual(item["publicationTitle"], "Test Journal")
        self.assertEqual(item["date"], "2026-10-01")
        self.assertEqual(item["DOI"], DOI)
        self.assertEqual(len(item["tags"]), 5)
        names = [self.http.objects["collections"][k]["data"]["name"] for k in item["collections"]]
        self.assertEqual(names, ["PhD", "Theory"])
        self.assertEqual(len(self.http.objects["collections"]), 6)
        self.assertEqual(self.http.uploads, 1)
        self.assertEqual(self.http.objects["items"][result["attachment_key"]]["data"]["md5"], file_hash(self.pdf, "md5"))

    def test_reimport_no_duplicate_or_model_charge(self):
        first = self.run_agent()
        second = self.run_agent()
        self.assertEqual(first["item_key"], second["item_key"])
        self.assertEqual(len(self.http.objects["items"]), 2)
        self.assertEqual(self.http.uploads, 1)
        self.assertEqual(self.http.model_calls, 1)

    def test_preserves_manual_edits_tags_and_other_collections(self):
        result = self.run_agent()
        data = self.http.objects["items"][result["item_key"]]["data"]
        data["title"] = "My corrected title"
        data["tags"].append({"tag": "read"})
        data["collections"].append("OTHER123")
        self.run_agent(category="Methods")
        self.assertEqual(data["title"], "My corrected title")
        self.assertIn({"tag": "read"}, data["tags"])
        self.assertIn("OTHER123", data["collections"])
        chosen = [self.http.objects["collections"][k]["data"]["name"] for k in data["collections"] if k != "OTHER123"]
        self.assertEqual(chosen, ["PhD", "Methods"])

    def test_lost_parent_response_resumes(self):
        self.http.failure = "journalArticle"
        with self.assertRaises(AgentError):
            self.run_agent()
        self.assertEqual(self.run_agent()["status"], "complete")
        self.assertEqual(len(self.http.objects["items"]), 2)

    def test_lost_attachment_response_resumes(self):
        self.http.failure = "attachment"
        with self.assertRaises(AgentError):
            self.run_agent()
        self.assertEqual(self.run_agent()["status"], "complete")
        self.assertEqual(len(self.http.objects["items"]), 2)

    def test_storage_failure_resumes_same_attachment(self):
        self.http.failure = "upload"
        with self.assertRaises(AgentError):
            self.run_agent()
        self.assertEqual(self.run_agent()["status"], "complete")
        self.assertEqual(len(self.http.objects["items"]), 2)

    def test_preview_never_contacts_zotero(self):
        self.assertEqual(self.run_agent(command="preview")["status"], "preview")
        self.assertFalse(any("api.zotero.org" in url for _, url, _ in self.http.calls))

    def test_mismatch_blocks_all_remote_writes(self):
        self.http.classification["matches_doi_metadata"] = False
        result = self.run_agent(allow_missing=True, category="Methods")
        self.assertEqual(result["status"], "review_required")
        self.assertEqual(self.http.objects["items"], {})
        self.assertEqual(self.http.objects["collections"], {})

    def test_missing_abstract_not_replaced_by_generated_summary(self):
        self.http.classification["pdf_abstract"] = "An invented summary."
        self.assertEqual(self.run_agent()["status"], "review_required")
        result = self.run_agent(allow_missing=True)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.http.objects["items"][result["item_key"]]["data"].get("abstractNote", ""), "")

    def test_low_confidence_review_can_be_resolved_by_category(self):
        self.http.classification["confidence"] = .3
        self.assertEqual(self.run_agent()["status"], "review_required")
        self.assertEqual(self.run_agent(category="Reviews")["status"], "complete")

    def test_crossref_abstract_preferred_and_markup_cleaned(self):
        self.http.abstract = "<jats:p>Published &amp; verified abstract.</jats:p>"
        result = self.run_agent()
        data = self.http.objects["items"][result["item_key"]]["data"]
        self.assertEqual(data["abstractNote"], "Published & verified abstract.")

    def test_doi_negotiation_fallback_preserves_consortium_author(self):
        self.http.crossref_missing = True
        record = metadata(self.http, DOI)
        self.assertEqual(record["creators"], [{"creatorType": "author", "name": "Research Consortium"}])

    def test_duplicate_doi_items_require_review(self):
        self.run_agent()
        parent = next(v for v in self.http.objects["items"].values() if v["data"].get("DOI"))
        other = copy.deepcopy(parent)
        other["key"] = other["data"]["key"] = "DUPL2345"
        self.http.objects["items"]["DUPL2345"] = other
        with self.assertRaisesRegex(AgentError, "Multiple Zotero items"):
            self.run_agent()

    def test_pagination(self):
        for i in range(205):
            self.http.objects["items"][str(i)] = {"key": str(i), "data": {"title": "unrelated"}}
        self.assertEqual(len(Zotero(self.http, "123", "user", "fake").all("items/top")), 205)

    def test_normalize_doi(self):
        for value in ("https://doi.org/10.1234/TEST", "doi:10.1234/test", "10.1234/test"):
            self.assertEqual(normalize_doi(value), DOI)
        for value in ("not a doi", "10.123/a", "10.1234/ has spaces"):
            with self.assertRaises(AgentError):
                normalize_doi(value)

    def test_invalid_tags_rejected(self):
        for tags in (TAGS[:4], ["x"] * 5, ["", *TAGS[:4]]):
            value = {**analysis(), "tags": tags}
            with self.assertRaises(ValueError):
                validate_analysis(value)

    def test_retag_removes_only_agent_owned_automatic_tags(self):
        data = {"tags": [{"tag": "old", "type": 1}, {"tag": "manual"}, {"tag": "publisher", "type": 1}],
                "extra": MARKER + json.dumps({"tags": ["old"]})}
        tags = merged_tags(data, TAGS)
        self.assertEqual(len(tags), 7)
        self.assertNotIn("old", [t["tag"] for t in tags])

    def test_watch_once_and_skip_processed_manifest(self):
        inbox = self.root / "inbox"
        inbox.mkdir()
        manifest = inbox / "paper.json"
        manifest.write_text(json.dumps({"doi": DOI, "pdf": str(self.pdf)}))
        args = argparse.Namespace(inbox=str(inbox), interval=1, once=True)
        self.assertEqual(cli.watch(args, self.state, self.http), 0)
        self.assertEqual(cli.watch(args, self.state, self.http), 0)
        self.assertEqual(self.http.model_calls, 1)
        result = json.loads((inbox / "reports/paper.result.json").read_text())
        self.assertEqual(result["status"], "complete")

    def test_invalid_manifest_and_missing_pdf_do_not_crash_watch(self):
        inbox = self.root / "inbox"
        inbox.mkdir()
        (inbox / "bad.json").write_text("{broken")
        (inbox / "missing.json").write_text(json.dumps({"doi": DOI, "pdf": "missing.pdf"}))
        args = argparse.Namespace(inbox=str(inbox), interval=1, once=True)
        self.assertEqual(cli.watch(args, self.state, self.http), 2)
        self.assertEqual(len(list((inbox / "reports").glob("*.json"))), 2)

    def test_pdf_and_date(self):
        pdf = read_pdf(self.pdf)
        self.assertIn(TITLE, pdf["text"])
        self.assertFalse(pdf["sampled"])
        self.assertEqual(date_string({"created": {"date-parts": [[2020]]}}), "")

    def test_local_import_without_openai_key(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "ollama", "OPENAI_API_KEY": "", "OLLAMA_MODEL": "qwen3:8b",
                                    "OLLAMA_BASE_URL": "http://127.0.0.1:11434", "OLLAMA_NUM_CTX": "32768"}):
            result = self.run_agent()
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["plan"]["provider"], "ollama")
            self.assertEqual(self.http.local_calls, 1)
            self.assertEqual(self.http.model_calls, 0)
            self.run_agent()
            self.assertEqual(self.http.local_calls, 1)

    def test_switching_provider_reanalyzes_without_duplicate_upload(self):
        original = self.run_agent()
        with patch.dict(os.environ, {"LLM_PROVIDER": "ollama", "OLLAMA_MODEL": "qwen3:8b",
                                    "OLLAMA_BASE_URL": "http://127.0.0.1:11434", "OLLAMA_NUM_CTX": "32768"}):
            updated = self.run_agent()
            self.assertEqual(updated["item_key"], original["item_key"])
            self.assertEqual(self.http.local_calls, 1)
            self.assertEqual(self.http.uploads, 1)

    def test_pdf_byte_budget_is_respected(self):
        pdf = read_pdf(self.pdf, byte_budget=250)
        self.assertTrue(pdf["sampled"])
        self.assertLessEqual(len(pdf["text"].encode("utf-8")), 250)


if __name__ == "__main__":
    unittest.main()
