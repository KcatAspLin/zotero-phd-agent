import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_agent import ABSTRACT, DOI, TITLE, FakeHTTP, write_pdf
from zotero_agent import cli
from zotero_agent.net import AgentError
from zotero_agent.state import State
from zotero_agent.zotero import agent_record

VENUE = "Conference on Neural Information Processing Systems"
FOOTER = VENUE + " (NeurIPS 2026)"


class PDFOnlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pdf = self.root / "paper.pdf"
        write_pdf(self.pdf, TITLE + ". Ada Example and Bob Researcher. Abstract. " + ABSTRACT + " " + FOOTER + ". " + "Auditory learning. " * 20)
        self.http = FakeHTTP()
        self.http.classification["pdf_metadata"] = {
            "authors": ["Ada Example", "Bob Researcher"], "venue": VENUE, "year": "2026",
            "year_evidence": FOOTER, "item_type": "conferencePaper", "item_type_evidence": FOOTER}
        self.state = State(self.root / "state")
        env = patch.dict(os.environ, {"ZOTERO_LIBRARY_ID": "123", "ZOTERO_LIBRARY_TYPE": "user",
            "ZOTERO_API_KEY": "fake", "OPENAI_API_KEY": "", "OPENAI_MODEL": "fake-model", "LLM_PROVIDER": "ollama",
            "OLLAMA_MODEL": "qwen3:8b", "OLLAMA_BASE_URL": "http://127.0.0.1:11434", "OLLAMA_NUM_CTX": "32768",
            "ZOTERO_PHD_COLLECTION_KEY": ""})
        env.start()
        self.addCleanup(env.stop)

    def process(self, command="import", doi=None, path=None, state=None, allow_missing=False):
        args = argparse.Namespace(command=command, doi=doi, pdf=str(path or self.pdf),
                                  refresh=False, category=None, allow_missing=allow_missing)
        return cli.process(args, state or self.state, self.http)

    def test_conference_pdf_import_without_doi_or_registry_request(self):
        result = self.process()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["plan"]["doi"], "")
        self.assertEqual(result["plan"]["review_reasons"], [])
        data = self.http.objects["items"][result["item_key"]]["data"]
        self.assertEqual(data["itemType"], "conferencePaper")
        self.assertEqual(data["proceedingsTitle"], VENUE)
        self.assertEqual(data["date"], "2026")
        self.assertEqual(data["abstractNote"], ABSTRACT)
        self.assertEqual(data["creators"][1], {"creatorType": "author", "name": "Bob Researcher"})
        self.assertNotIn("publicationTitle", data)
        self.assertNotIn("input_mode", data)
        self.assertNotIn("verification_issues", data)
        self.assertEqual(len(data["tags"]), 5)
        self.assertEqual(result["collection"], "PhD/Theory")
        self.assertFalse(any("crossref.org" in url or "doi.org/" in url or "api.openai.com" in url for _, url, _ in self.http.calls))
        request = next(kw["json_data"] for _, url, kw in self.http.calls if url.endswith("/api/chat"))
        self.assertIn("pdf_metadata", request["format"]["required"])

    def test_pdf_only_preview_does_not_contact_zotero(self):
        self.assertEqual(self.process(command="preview")["status"], "preview")
        self.assertFalse(any("api.zotero.org" in url for _, url, _ in self.http.calls))

    def test_openai_also_supports_pdf_only_schema(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "fake"}):
            result = self.process(command="preview")
        self.assertEqual(result["plan"]["review_reasons"], [])
        request = next(kw["json_data"] for _, url, kw in self.http.calls if "api.openai.com" in url)
        self.assertIn("pdf_metadata", request["text"]["format"]["schema"]["required"])

    def test_unverifiable_author_blocks_import_even_with_allow_missing(self):
        self.http.classification["pdf_metadata"]["authors"] = ["Invented Author"]
        result = self.process(allow_missing=True)
        self.assertEqual(result["status"], "review_required")
        self.assertEqual(result["plan"]["metadata"]["creators"], [])
        self.assertTrue(any("author name" in r for r in result["plan"]["review_reasons"]))
        self.assertEqual(self.http.objects["items"], {})

    def test_unverifiable_year_is_not_used(self):
        self.http.classification["pdf_metadata"].update(year="2025", year_evidence="Unseen 2025 proceedings")
        result = self.process()
        self.assertEqual(result["status"], "review_required")
        self.assertEqual(result["plan"]["metadata"]["date"], "")

    def test_absent_metadata_can_be_left_blank_without_fabricated_doi(self):
        self.http.classification["pdf_metadata"].update(year="", year_evidence="", authors=[])
        self.assertEqual(self.process()["status"], "review_required")
        result = self.process(allow_missing=True)
        self.assertEqual(result["status"], "complete")
        data = self.http.objects["items"][result["item_key"]]["data"]
        self.assertFalse(data.get("DOI"))
        self.assertFalse(data.get("creators"))
        self.assertFalse(data.get("date"))

    def test_same_pdf_renamed_reuses_cache_record_and_attachment(self):
        first = self.process()
        renamed = self.root / "renamed.pdf"
        renamed.write_bytes(self.pdf.read_bytes())
        second = self.process(path=renamed)
        self.assertEqual(second["item_key"], first["item_key"])
        self.assertEqual(self.http.local_calls, 1)
        self.assertEqual(self.http.uploads, 1)

    def test_remote_hash_dedup_survives_new_state_and_manual_title_edit(self):
        first = self.process()
        data = self.http.objects["items"][first["item_key"]]["data"]
        data["extra"] = ""
        data["title"] = "Manually corrected title"
        second = self.process(state=State(self.root / "other-state"))
        self.assertEqual(second["item_key"], first["item_key"])
        self.assertEqual(data["title"], "Manually corrected title")
        self.assertEqual(self.http.uploads, 1)

    def test_distinct_pdf_version_matches_title_author_and_adds_attachment(self):
        first = self.process()
        version = self.root / "version2.pdf"
        version.write_bytes(self.pdf.read_bytes() + b"\n% version 2\n")
        second = self.process(path=version)
        self.assertEqual(second["item_key"], first["item_key"])
        self.assertEqual(self.http.uploads, 2)
        data = self.http.objects["items"][first["item_key"]]["data"]
        self.assertEqual(len(agent_record(data)["pdf_hashes"]), 2)

    def test_pdf_only_lost_parent_response_resumes(self):
        self.http.failure = "conferencePaper"
        with self.assertRaises(AgentError):
            self.process()
        self.assertEqual(self.process()["status"], "complete")
        self.assertEqual(len(self.http.objects["items"]), 2)

    def test_adding_doi_later_reuses_existing_pdf_only_item(self):
        first = self.process()
        second = self.process(doi=DOI)
        self.assertEqual(second["item_key"], first["item_key"])
        data = self.http.objects["items"][first["item_key"]]["data"]
        self.assertEqual(data["DOI"], DOI)
        self.assertEqual(data["itemType"], "conferencePaper")
        self.assertEqual(data["proceedingsTitle"], VENUE)
        self.assertEqual(self.http.uploads, 1)

    def test_watch_accepts_manifest_with_only_pdf(self):
        inbox = self.root / "inbox"
        inbox.mkdir()
        (inbox / "paper.json").write_text(json.dumps({"pdf": str(self.pdf)}))
        args = argparse.Namespace(inbox=str(inbox), once=True, interval=1)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.watch(args, self.state, self.http), 0)

    def test_enqueue_accepts_pdf_only(self):
        args = argparse.Namespace(doi=None, pdf=str(self.pdf), inbox=str(self.root / "inbox"))
        result = cli.enqueue(args)
        self.assertEqual(json.loads(Path(result["manifest"]).read_text()), {"pdf": str(self.pdf)})

    def test_bare_command_opens_picker_and_passes_empty_doi(self):
        with patch.object(sys, "argv", ["zotero-agent", "import"]), patch.object(cli, "load_env"), \
                patch.object(cli, "choose_pdf", return_value=str(self.pdf)), \
                patch.object(cli, "State"), patch.object(cli, "HTTP"), \
                patch.object(cli, "process", return_value={"status": "complete"}) as process, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(process.call_args.args[0].doi, "")


if __name__ == "__main__":
    unittest.main()
