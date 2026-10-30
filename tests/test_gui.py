import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from zotero_agent import cli, gui
from zotero_agent.net import AgentError


class PickerTests(unittest.TestCase):
    def fake_tk(self, selection=""):
        class TclError(Exception):
            pass

        root = Mock()
        dialog = SimpleNamespace(askopenfilename=Mock(return_value=selection))
        tk = SimpleNamespace(Tk=Mock(return_value=root), filedialog=dialog, TclError=TclError)
        return tk, root, dialog

    def test_picker_returns_absolute_path_and_cleans_up_window(self):
        path = str(Path(tempfile.gettempdir()) / "papers with spaces" / "paper.pdf")
        tk, root, dialog = self.fake_tk(path)
        with patch.dict(sys.modules, {"tkinter": tk, "tkinter.filedialog": dialog}):
            self.assertEqual(gui.choose_pdf(), str(Path(path).resolve()))
        root.withdraw.assert_called_once()
        root.destroy.assert_called_once()
        self.assertIs(dialog.askopenfilename.call_args.kwargs["parent"], root)

    def test_cancel_closes_window(self):
        tk, root, dialog = self.fake_tk()
        with patch.dict(sys.modules, {"tkinter": tk, "tkinter.filedialog": dialog}):
            self.assertIsNone(gui.choose_pdf())
        root.destroy.assert_called_once()

    def test_missing_tk_provides_command_line_fallback(self):
        with patch.dict(sys.modules, {"tkinter": None}):
            with self.assertRaisesRegex(AgentError, "pass --pdf"):
                gui.choose_pdf()

    def test_no_desktop_provides_command_line_fallback(self):
        tk, root, dialog = self.fake_tk()
        tk.Tk.side_effect = tk.TclError("no display")
        with patch.dict(sys.modules, {"tkinter": tk, "tkinter.filedialog": dialog}):
            with self.assertRaisesRegex(AgentError, "desktop session"):
                gui.choose_pdf()
        dialog.askopenfilename.assert_not_called()

    def test_dialog_error_still_closes_hidden_window(self):
        tk, root, dialog = self.fake_tk()
        dialog.askopenfilename.side_effect = tk.TclError("dialog unavailable")
        with patch.dict(sys.modules, {"tkinter": tk, "tkinter.filedialog": dialog}):
            with self.assertRaises(AgentError):
                gui.choose_pdf()
        root.destroy.assert_called_once()

    def test_cancel_makes_no_network_requests_or_state(self):
        for command in ("preview", "import", "enqueue"):
            with self.subTest(command=command), \
                    patch.object(sys, "argv", ["zotero-agent", command, "--doi", "10.1234/test"]), \
                    patch.object(cli, "load_env"), patch.object(cli, "choose_pdf", return_value=None), \
                    patch.object(cli, "HTTP") as http, patch.object(cli, "State") as state, \
                    patch.object(cli, "enqueue") as enqueue, contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(), 0)
                self.assertEqual(json.loads(output.getvalue())["status"], "cancelled")
                http.assert_not_called()
                state.assert_not_called()
                enqueue.assert_not_called()

    def test_selected_pdf_is_passed_to_import_and_preview(self):
        for command in ("import", "preview"):
            with self.subTest(command=command), \
                    patch.object(sys, "argv", ["zotero-agent", command, "--doi", "10.1234/test"]), \
                    patch.object(cli, "load_env"), patch.object(cli, "choose_pdf", return_value="/papers/a b.pdf"), \
                    patch.object(cli, "HTTP"), patch.object(cli, "State"), \
                    patch.object(cli, "process", return_value={"status": "complete"}) as process, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(), 0)
                self.assertEqual(process.call_args.args[0].pdf, "/papers/a b.pdf")

    def test_selected_pdf_is_passed_to_queue(self):
        with patch.object(sys, "argv", ["zotero-agent", "enqueue", "--doi", "10.1234/test"]), \
                patch.object(cli, "load_env"), patch.object(cli, "choose_pdf", return_value="/papers/paper.pdf"), \
                patch.object(cli, "enqueue", return_value={"status": "queued"}) as enqueue, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(), 0)
            self.assertEqual(enqueue.call_args.args[0].pdf, "/papers/paper.pdf")

    def test_explicit_pdf_does_not_open_picker(self):
        with patch.object(sys, "argv", ["zotero-agent", "import", "--doi", "10.1234/test", "--pdf", "/papers/test.pdf"]), \
                patch.object(cli, "load_env"), patch.object(cli, "choose_pdf") as picker, \
                patch.object(cli, "HTTP"), patch.object(cli, "State"), \
                patch.object(cli, "process", return_value={"status": "complete"}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(), 0)
            picker.assert_not_called()

    def test_watch_never_opens_picker(self):
        with patch.object(sys, "argv", ["zotero-agent", "watch", "--once"]), \
                patch.object(cli, "load_env"), patch.object(cli, "choose_pdf") as picker, \
                patch.object(cli, "HTTP"), patch.object(cli, "State"), \
                patch.object(cli, "watch", return_value=0):
            self.assertEqual(cli.main(), 0)
            picker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
