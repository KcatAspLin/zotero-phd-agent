"""Desktop PDF picker: native macOS dialog, optional Tk on other platforms."""
import subprocess
import sys
from pathlib import Path

from .net import AgentError


def choose_pdf():
    """Return a selected absolute path, or None when the user cancels."""
    if sys.platform == "darwin":
        return _choose_pdf_macos()
    return _choose_pdf_tk()


def _choose_pdf_macos():
    # Fixed script, no shell and no interpolation of paths or other user input.
    # Catch cancellation in AppleScript rather than parsing localised stderr.
    script = '''try
    set selectedFile to choose file with prompt "Select paper PDF" of type {"com.adobe.pdf"}
    return POSIX path of selectedFile
on error number -128
    return ""
end try'''
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise AgentError(
            "Could not launch the native macOS PDF picker. "
            "Run in a desktop session or pass --pdf '/path/to/paper.pdf'."
        ) from exc
    if result.returncode != 0:
        raise AgentError(
            "Could not open the native macOS PDF picker. "
            "Run in a desktop session or pass --pdf '/path/to/paper.pdf'."
        )
    selected = result.stdout.rstrip("\r\n")
    return str(Path(selected).expanduser().resolve()) if selected else None


def _choose_pdf_tk():
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise AgentError(
            "The PDF picker needs Python with Tk support. Check with 'python -m tkinter', "
            "or pass --pdf '/path/to/paper.pdf'."
        ) from exc

    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.update_idletasks()
        selected = filedialog.askopenfilename(
            parent=root,
            title="Select paper PDF",
            filetypes=(("PDF documents", "*.pdf"), ("All files", "*")),
        )
    except tk.TclError as exc:
        raise AgentError(
            "Could not open the PDF picker. Run in a desktop session with Tk support, "
            "or pass --pdf '/path/to/paper.pdf'."
        ) from exc
    finally:
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass
    return str(Path(selected).expanduser().resolve()) if selected else None
