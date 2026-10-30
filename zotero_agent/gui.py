"""Optional desktop file picker; importing the CLI never requires Tk."""
from pathlib import Path

from .net import AgentError


def choose_pdf():
    """Return a selected absolute path, or None when the user cancels."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise AgentError(
            "The PDF picker needs Python with Tk support. Check with 'python -m tkinter', "
            "or pass --pdf '/path/to/paper.pdf'. On macOS, python.org installers include Tk."
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
