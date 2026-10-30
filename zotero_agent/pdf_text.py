"""Extract PDF text with two independent parsers and track unreadable pages."""
import logging

from .net import AgentError


class LockedPDF(AgentError):
    pass


class SkippedContent(logging.Handler):
    """Detect pypdf's logged extraction limits, even when no exception is raised."""
    def __init__(self):
        super().__init__()
        self.skipped = False

    def emit(self, record):
        message = record.getMessage().lower()
        if ("xobject" in message and "invocations" in message) or ("form content" in message and "skipped" in message):
            self.skipped = True


def pymupdf_pages(path):
    import pymupdf

    pages, failed = [], []
    with pymupdf.open(str(path)) as document:
        if not document.is_pdf:
            raise AgentError("Selected file is not a PDF")
        if document.needs_pass and not document.authenticate(""):
            raise LockedPDF("PDF is password protected; supply an unlocked copy")
        for index in range(len(document)):
            try:
                pages.append(document[index].get_text("text") or "")
            except Exception:
                pages.append("")
                failed.append(index)
    return pages, failed


def pypdf_pages(path, indices=None):
    from pypdf import PdfReader

    reader = PdfReader(path)
    try:
        if reader.is_encrypted and not reader.decrypt(""):
            raise LockedPDF("PDF is password protected; supply an unlocked copy")
        pages = [""] * len(reader.pages)
        failed = []
        handler = SkippedContent()
        logger = logging.getLogger("pypdf")
        logger.addHandler(handler)
        try:
            for index in (range(len(pages)) if indices is None else indices):
                handler.skipped = False
                try:
                    pages[index] = reader.pages[index].extract_text() or ""
                    if handler.skipped:
                        # Partial text is not treated as a fully extracted page.
                        failed.append(index)
                except Exception:
                    failed.append(index)
        finally:
            logger.removeHandler(handler)
        return pages, failed
    finally:
        reader.close()


def extract_pages(path):
    """Prefer MuPDF; recover failed pages with pypdf without changing the PDF."""
    warnings = []
    try:
        pages, failed = pymupdf_pages(path)
        engine = "pymupdf"
    except LockedPDF:
        raise
    except Exception:
        pages, failed = None, []

    if pages is None or sum(len(p.strip()) for p in pages) < 200:
        try:
            other, other_failed = pypdf_pages(path)
        except LockedPDF:
            raise
        except Exception as exc:
            if pages is None:
                raise AgentError(
                    "PDF text extraction failed with both PyMuPDF and pypdf. "
                    "Try a fresh publisher download or an OCR-processed PDF. "
                    "This failure occurs before the model receives any text."
                ) from exc
        else:
            if pages is None or sum(len(p.strip()) for p in other) > sum(len(p.strip()) for p in pages):
                pages, failed, engine = other, other_failed, "pypdf"
                warnings.append("Used pypdf after PyMuPDF could not extract enough text.")
    elif failed:
        try:
            recovered, still_failed = pypdf_pages(path, indices=failed)
        except LockedPDF:
            raise
        except Exception:
            pass
        else:
            remaining = []
            for index in failed:
                if index not in still_failed and index < len(recovered) and recovered[index].strip():
                    pages[index] = recovered[index]
                else:
                    remaining.append(index)
            if len(remaining) < len(failed):
                engine = "pymupdf+pypdf"
                warnings.append("Recovered unreadable pages with pypdf.")
            failed = remaining

    failed_pages = [index + 1 for index in failed]
    if failed_pages:
        warnings.append("Text extraction incomplete on page(s): " + ", ".join(map(str, failed_pages)))
    if sum(len(p.strip()) for p in pages) < 200:
        if failed_pages:
            raise AgentError("PDF text extraction is incomplete. Try a fresh publisher PDF or an OCR-processed copy; increasing model context will not fix PDF parsing.")
        raise AgentError("Too little PDF text; run OCR on scanned PDFs first")
    return pages, {"engine": engine, "failed_pages": failed_pages, "warnings": warnings}
