import logging
import unittest
from unittest.mock import Mock, patch

from zotero_agent import pdf_text
from zotero_agent.content import make_plan
from zotero_agent.net import AgentError

TEXT = "Readable scientific paper text. " * 15


class PDFTextTests(unittest.TestCase):
    def test_primary_extractor_avoids_problematic_pypdf_parser(self):
        with patch.object(pdf_text, "pymupdf_pages", return_value=([TEXT], [])), \
                patch.object(pdf_text, "pypdf_pages") as fallback:
            pages, info = pdf_text.extract_pages("paper.pdf")
        self.assertEqual(pages, [TEXT])
        self.assertEqual(info["engine"], "pymupdf")
        self.assertEqual(info["failed_pages"], [])
        fallback.assert_not_called()

    def test_document_failure_uses_second_extractor(self):
        with patch.object(pdf_text, "pymupdf_pages", side_effect=RuntimeError("parser failed")), \
                patch.object(pdf_text, "pypdf_pages", return_value=([TEXT], [])):
            pages, info = pdf_text.extract_pages("paper.pdf")
        self.assertEqual(pages, [TEXT])
        self.assertEqual(info["engine"], "pypdf")
        self.assertFalse(info["failed_pages"])

    def test_failed_page_is_recovered(self):
        with patch.object(pdf_text, "pymupdf_pages", return_value=([TEXT, ""], [1])), \
                patch.object(pdf_text, "pypdf_pages", return_value=(["", TEXT], [])) as fallback:
            pages, info = pdf_text.extract_pages("paper.pdf")
        self.assertEqual(pages, [TEXT, TEXT])
        self.assertEqual(info["engine"], "pymupdf+pypdf")
        self.assertEqual(info["failed_pages"], [])
        fallback.assert_called_once_with("paper.pdf", indices=[1])

    def test_unrecovered_page_is_reported_and_requires_review(self):
        with patch.object(pdf_text, "pymupdf_pages", return_value=([TEXT, ""], [1])), \
                patch.object(pdf_text, "pypdf_pages", return_value=(["", "partial text"], [1])):
            pages, info = pdf_text.extract_pages("paper.pdf")
        self.assertEqual(info["failed_pages"], [2])
        pdf = {"path": "paper.pdf", "sha256": "test", "pages": 2, "sampled": False,
               "front_text": "Test title", "text": TEXT, "extraction": info}
        meta = {"title": "Test title", "DOI": "10.1234/test", "creators": ["Author"],
                "publicationTitle": "Journal", "date": "2026", "abstractNote": "Abstract"}
        analysis = {"matches_doi_metadata": True, "pdf_title": "Test title", "pdf_abstract": "", "confidence": .99,
                    "category": "Theory", "tags": ["a", "b", "c", "d", "e"], "reason": "New theory"}
        plan = make_plan(meta, pdf, analysis, "local-model")
        self.assertTrue(any("PDF extraction incomplete" in reason for reason in plan["review_reasons"]))
        self.assertEqual(plan["pdf_extraction"]["failed_pages"], [2])

    def test_scanned_pdf_explains_ocr_requirement(self):
        with patch.object(pdf_text, "pymupdf_pages", return_value=(["", ""], [])), \
                patch.object(pdf_text, "pypdf_pages", return_value=(["", ""], [])):
            with self.assertRaisesRegex(AgentError, "OCR"):
                pdf_text.extract_pages("scan.pdf")

    def test_both_parsers_fail_with_actionable_error(self):
        with patch.object(pdf_text, "pymupdf_pages", side_effect=RuntimeError), \
                patch.object(pdf_text, "pypdf_pages", side_effect=RuntimeError):
            with self.assertRaisesRegex(AgentError, "before the model"):
                pdf_text.extract_pages("broken.pdf")

    def test_password_failure_stops_without_second_parser(self):
        with patch.object(pdf_text, "pymupdf_pages", side_effect=pdf_text.LockedPDF("password protected")), \
                patch.object(pdf_text, "pypdf_pages") as fallback:
            with self.assertRaisesRegex(AgentError, "password"):
                pdf_text.extract_pages("locked.pdf")
        fallback.assert_not_called()

    def test_pypdf_limit_warning_marks_page_incomplete_and_reader_closes(self):
        reader = Mock(is_encrypted=False)
        page = Mock()

        def limited_text():
            logging.getLogger("pypdf._page").warning(
                "Exceeded 5000 form XObject invocations while extracting text; further form content is skipped.")
            return TEXT

        page.extract_text.side_effect = limited_text
        reader.pages = [page]
        logger = logging.getLogger("pypdf")
        prior_handlers = list(logger.handlers)
        with patch("pypdf.PdfReader", return_value=reader), self.assertLogs("pypdf", level="WARNING"):
            pages, failed = pdf_text.pypdf_pages("complex.pdf")
        self.assertEqual(pages, [TEXT])
        self.assertEqual(failed, [0])
        self.assertEqual(logger.handlers, prior_handlers)
        reader.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
