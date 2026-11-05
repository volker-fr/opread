import contextlib
import importlib.util
import io
import os
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "opread.py"
spec = importlib.util.spec_from_file_location("opread_under_test", SCRIPT)
opread = importlib.util.module_from_spec(spec)
spec.loader.exec_module(opread)
REAL_RESOLVE_OP = opread.resolve_op


class Child:
    pid = 987654

    def __init__(self, status):
        self.status = status

    def wait(self, timeout=None):
        return self.status

    def poll(self):
        return self.status


class OpreadTests(unittest.TestCase):
    def setUp(self):
        self.op_resolver = mock.patch.object(opread, "resolve_op", return_value=("/validated/bin/op", "2.41.1"))
        self.op_resolver.start()
        self.addCleanup(self.op_resolver.stop)

    def make_fake_op(self, directory, version="2.41.1", help_text="1Password CLI brings 1Password to your terminal."):
        executable = Path(directory) / "op"
        executable.write_text(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            f"  --version) printf '%s\\n' {version!r} ;;\n"
            f"  --help) printf '%s\\n' {help_text!r} ;;\n"
            "  *) exit 9 ;;\n"
            "esac\n"
        )
        executable.chmod(0o700)
        return executable

    def test_resolver_finds_and_validates_path_selected_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = self.make_fake_op(directory)
            with mock.patch.dict(opread.os.environ, {"PATH": directory}, clear=False):
                self.assertEqual(REAL_RESOLVE_OP(), (str(executable.resolve()), "2.41.1"))

    def test_resolver_rejects_impostor_with_plausible_version(self):
        with tempfile.TemporaryDirectory() as directory:
            self.make_fake_op(directory, help_text="A generic command line tool.")
            with mock.patch.dict(opread.os.environ, {"PATH": directory}, clear=False):
                with self.assertRaisesRegex(RuntimeError, "not identified as 1Password"):
                    REAL_RESOLVE_OP()

    def test_resolver_reports_missing_cli(self):
        with mock.patch.dict(opread.os.environ, {"PATH": ""}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "cannot find executable 'op'"):
                REAL_RESOLVE_OP()

    def test_resolver_reports_probe_failure_and_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = self.make_fake_op(directory)
            with mock.patch.dict(opread.os.environ, {"PATH": directory}, clear=False), \
                 mock.patch.object(opread.subprocess, "run", return_value=mock.Mock(returncode=7)):
                with self.assertRaisesRegex(RuntimeError, "failed --version with status 7"):
                    REAL_RESOLVE_OP()
            with mock.patch.dict(opread.os.environ, {"PATH": directory}, clear=False), \
                 mock.patch.object(opread.subprocess, "run", side_effect=opread.subprocess.TimeoutExpired([str(executable), "--version"], 3)):
                with self.assertRaisesRegex(RuntimeError, "timed out running --version"):
                    REAL_RESOLVE_OP()

    def test_normal_failure_happens_before_temp_or_gui_child(self):
        self.op_resolver.stop()
        self.addCleanup(self.op_resolver.start)
        with mock.patch.object(opread, "resolve_op", side_effect=RuntimeError("wrong program")), \
             mock.patch.object(opread.sys, "argv", ["opread", "op://vault/item", "Finance"]), \
             mock.patch.object(opread.tempfile, "mkdtemp") as mkdtemp, \
             mock.patch.object(opread.subprocess, "Popen") as popen, \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(opread.main(), 69)
        self.assertIn("wrong program", errors.getvalue())
        mkdtemp.assert_not_called()
        popen.assert_not_called()

    def test_runtime_selection_uses_real_interpreter_and_base_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "base"
            base.mkdir()
            target = base / "python3.12"
            target.write_text("interpreter")
            target.chmod(0o700)
            link = Path(directory) / "python"
            link.symlink_to(target)
            with mock.patch.object(opread.sys, "executable", str(link)), \
                 mock.patch.object(opread.sys, "base_prefix", str(base)):
                self.assertEqual(opread.select_python_runtime(), (str(target), str(base)))

    def test_runtime_check_uses_selected_interpreter_and_pythonhome(self):
        with mock.patch.object(opread, "resolve_op", return_value=("/validated/bin/op", "2.41.1")), \
             mock.patch.object(opread, "select_python_runtime", return_value=("/base/bin/python3", "/base")), \
             mock.patch.object(opread.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(opread.check_runtime(), 0)
        self.assertEqual(run.call_args.args[0][0], "/base/bin/python3")
        self.assertEqual(run.call_args.kwargs["env"]["PYTHONHOME"], "/base")

    def test_rejects_option_like_and_non_op_references(self):
        for reference in ("--help", "vault/item", "op://vault/item\n--help"):
            with self.subTest(reference=reference):
                with self.assertRaises(ValueError):
                    opread.validate_reference(reference)

    def test_missing_reference_fails_before_probe(self):
        with mock.patch.object(opread.sys, "argv", ["opread"]), \
             mock.patch.object(opread, "resolve_op") as resolve_op, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(opread.main(), 2)
        resolve_op.assert_not_called()

    def test_label_omits_vault_and_preserves_reason(self):
        ref = "op://Private/fooo/section/API_TOKEN_READ_ONLY"
        self.assertEqual(opread.display_label(ref), "fooo/section/API_TOKEN_READ_ONLY")
        self.assertEqual(opread.display_label(ref, "Fastmail / résumé"),
                         "fooo/section/API_TOKEN_READ_ONLY: Fastmail / résumé")
        self.assertEqual(opread.desktop_identity(opread.display_label(ref, "Fastmail")),
                         "fooo_section_API_TOKEN_READ_ONLY__Fastmail")

    def test_desktop_identity_is_safe_and_bounded(self):
        self.assertEqual(opread.desktop_identity("../a\nb"), ".._a_b")
        self.assertEqual(opread.desktop_identity(".."), "request")
        identity = opread.desktop_identity("é" * 300)
        self.assertLessEqual(len(identity.encode("utf-8")), 240)
        self.assertNotIn("/", identity)

    def test_explicit_reason_validation_before_probe(self):
        for reason in ("", ".", "..", "é" * 65):
            with self.subTest(reason=reason), \
                 mock.patch.object(opread.sys, "argv", ["opread", "op://vault/item/field", reason]), \
                 mock.patch.object(opread, "resolve_op") as resolve_op, \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(opread.main(), 2)
                resolve_op.assert_not_called()

    def test_help_does_not_start_gui_or_require_bindings(self):
        with mock.patch.object(opread.sys, "argv", ["opread", "--help"]):
            self.assertEqual(opread.main(), 0)

    def test_unsupported_op_arguments_fail_before_probe_or_gui(self):
        cases = (
            ["opread", "--no-newline", "op://vault/item", "Finance"],
            ["opread", "op://vault/item", "Finance", "--out-file", "file"],
            ["opread", "op://vault/item", "--no-newline"],
            ["opread", "op://vault/item", "-n"],
        )
        for argv in cases:
            with self.subTest(argv=argv), \
                 mock.patch.object(opread.sys, "argv", argv), \
                 mock.patch.object(opread, "resolve_op") as resolve_op, \
                 mock.patch.object(opread.tempfile, "mkdtemp") as mkdtemp, \
                 mock.patch.object(opread.subprocess, "Popen") as popen, \
                 contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(opread.main(), 2)
                self.assertEqual(errors.getvalue().strip(), opread.UNSUPPORTED_ARGUMENTS_ERROR)
                resolve_op.assert_not_called()
                mkdtemp.assert_not_called()
                popen.assert_not_called()

    def test_child_status_is_forwarded_and_tempdir_removed(self):
        with tempfile.TemporaryDirectory() as parent:
            created = []

            def mkdtemp(**kwargs):
                path = os.path.join(parent, "private")
                os.mkdir(path, 0o700)
                created.append(path)
                return path

            child = Child(7)
            with mock.patch.object(opread.sys, "argv", ["opread", "op://vault/item", "Finance approval"]), \
                 mock.patch.object(opread.tempfile, "mkdtemp", side_effect=mkdtemp), \
                 mock.patch.object(opread.subprocess, "Popen", return_value=child) as popen, \
                 mock.patch.object(opread, "terminate_process_group") as cleanup:
                self.assertEqual(opread.main(), 7)
            cleanup.assert_called_once_with(child)
            argv = popen.call_args.args[0]
            self.assertEqual(argv[-4:], ["--gui-child", "op://vault/item", "item: Finance approval", "/validated/bin/op"])
            self.assertEqual(popen.call_args.kwargs["env"]["OP_BIOMETRIC_UNLOCK_ENABLED"], "true")
            self.assertEqual(popen.call_args.kwargs["env"]["PYTHONHOME"], opread.select_python_runtime()[1])
            self.assertFalse(os.path.exists(created[0]))

    @contextlib.contextmanager
    def fake_gui(self):
        window = mock.Mock()
        gtk = mock.Mock()
        gtk.init_check.return_value = (True, [])
        gtk.Window.return_value = window
        gdk = mock.Mock()
        glib = mock.Mock(SOURCE_CONTINUE=True, SOURCE_REMOVE=False)
        handlers, timers = {}, {}

        def timeout_add(delay, callback):
            source_id = len(timers) + 1
            timers[source_id] = (delay, callback)
            return source_id

        window.connect.side_effect = lambda event, callback: handlers.__setitem__(event, callback)
        window.show_all.side_effect = lambda: handlers["map"](window)
        glib.timeout_add.side_effect = timeout_add
        gi = types.ModuleType("gi")
        gi.require_version = mock.Mock()
        repository = types.ModuleType("gi.repository")
        x11 = types.ModuleType("gi.repository.GdkX11")
        x11.X11Display = type("X11Display", (), {})
        wayland = types.ModuleType("gi.repository.GdkWayland")
        wayland.WaylandDisplay = type("WaylandDisplay", (), {})
        wayland.WaylandWindow = type("WaylandWindow", (), {"set_application_id": mock.Mock()})
        display, surface = wayland.WaylandDisplay(), wayland.WaylandWindow()
        gdk.Display.get_default.return_value = display
        window.get_display.return_value = display
        window.get_window.return_value = surface
        repository.Gtk, repository.Gdk, repository.GLib = gtk, gdk, glib
        repository.GdkX11, repository.GdkWayland = x11, wayland
        modules = {"gi": gi, "gi.repository": repository,
                   "gi.repository.GdkX11": x11, "gi.repository.GdkWayland": wayland}
        with mock.patch.dict(opread.sys.modules, modules), \
             mock.patch.object(opread.signal, "signal"), \
             mock.patch.object(opread.subprocess, "Popen") as popen:
            yield types.SimpleNamespace(window=window, gtk=gtk, gdk=gdk, glib=glib,
                                        wayland=wayland, surface=surface, handlers=handlers,
                                        timers=timers, popen=popen)

    def test_gui_keeps_transparent_labeled_identity_window_resizable(self):
        with self.fake_gui() as gui:
            self.assertEqual(opread.gui("op://Private/fooo/field", "fooo/field: Fastmail", "/op"), 1)
        gui.gtk.Window.assert_called_once_with(title="fooo/field: Fastmail")
        gui.glib.set_prgname.assert_called_once_with("fooo/field: Fastmail")
        gui.gdk.set_program_class.assert_called_once_with("fooo/field: Fastmail")
        gui.wayland.WaylandWindow.set_application_id.assert_called_once_with(
            gui.surface, "fooo/field: Fastmail")
        gui.gtk.Widget.set_opacity.assert_called_once_with(gui.window, 0.0)
        gui.gtk.Label.assert_called_once_with(label="opread 1Password CLI request window")
        gui.window.add.assert_called_once_with(gui.gtk.Label.return_value)
        # Disabling resizing was observed to lose the request identity.
        self.assertNotIn(mock.call(False), gui.window.set_resizable.call_args_list)
        gui.window.set_default_size.assert_called_once_with(1, 1)
        gui.window.set_size_request.assert_called_once_with(1, 1)
        gui.popen.assert_not_called()

    def test_gui_closes_window_while_polling_until_child_status(self):
        for returncode, expected in ((7, 7), (-15, 143)):
            with self.subTest(returncode=returncode), self.fake_gui() as gui:
                proc = gui.popen.return_value
                proc.poll.side_effect = [None, None, returncode]

                def main_loop():
                    # Repeated map notifications must not schedule another spawn.
                    gui.handlers["map"](gui.window)
                    self.assertEqual(len(gui.timers), 1)
                    delay, spawn = gui.timers[1]
                    self.assertEqual(delay, 300)
                    gui.popen.assert_not_called()
                    self.assertIs(spawn(), gui.glib.SOURCE_REMOVE)
                    self.assertIs(spawn(), gui.glib.SOURCE_REMOVE)
                    gui.popen.assert_called_once_with(["/validated/bin/op", "read", "op://Private/fooo/field"])
                    self.assertEqual(len(gui.timers), 3)
                    close_delay, close_window = gui.timers[2]
                    poll_delay, poll_op = gui.timers[3]
                    self.assertEqual((close_delay, poll_delay), (300, 50))
                    self.assertIs(poll_op(), gui.glib.SOURCE_CONTINUE)
                    self.assertIs(close_window(), gui.glib.SOURCE_REMOVE)
                    gui.window.destroy.assert_called_once_with()
                    proc.terminate.assert_not_called()
                    proc.wait.assert_not_called()
                    gui.gtk.main_quit.assert_not_called()
                    # The op child can remain alive after its window disappears.
                    self.assertIs(poll_op(), gui.glib.SOURCE_CONTINUE)
                    gui.gtk.main_quit.assert_not_called()
                    self.assertIs(poll_op(), gui.glib.SOURCE_REMOVE)
                    gui.gtk.main_quit.assert_called_once_with()

                gui.gtk.main.side_effect = main_loop
                self.assertEqual(opread.gui("op://Private/fooo/field", "fooo/field: Fastmail",
                                           "/validated/bin/op"), expected)

    def test_gui_spawn_failure_reports_status_without_polling(self):
        with self.fake_gui() as gui, contextlib.redirect_stderr(io.StringIO()) as errors:
            gui.popen.side_effect = OSError("cannot execute")

            def main_loop():
                self.assertIs(gui.timers[1][1](), gui.glib.SOURCE_REMOVE)
                self.assertEqual(len(gui.timers), 1)
                gui.gtk.main_quit.assert_called_once_with()

            gui.gtk.main.side_effect = main_loop
            self.assertEqual(opread.gui("op://Private/fooo/field", "fooo/field", "/op"), 127)
        self.assertIn("cannot start op: cannot execute", errors.getvalue())

    def test_optional_reason_reaches_child_with_item_path(self):
        child = Child(0)
        with mock.patch.object(opread.sys, "argv", ["opread", "op://Private/fooo/API_TOKEN_READ_ONLY"]), \
             mock.patch.object(opread.subprocess, "Popen", return_value=child) as popen, \
             mock.patch.object(opread, "terminate_process_group"):
            self.assertEqual(opread.main(), 0)
        argv = popen.call_args.args[0]
        self.assertEqual(os.path.basename(argv[0]), "fooo_API_TOKEN_READ_ONLY")
        self.assertEqual(argv[-4:], ["--gui-child", "op://Private/fooo/API_TOKEN_READ_ONLY",
                                     "fooo/API_TOKEN_READ_ONLY", "/validated/bin/op"])

    def test_sigterm_after_interpreter_copy_cleans_tempdir_before_spawn(self):
        with tempfile.TemporaryDirectory() as parent:
            path = os.path.join(parent, "private")
            real_copyfile = opread.shutil.copyfile

            def make_tempdir(**_kwargs):
                os.mkdir(path, 0o700)
                return path

            def copy_then_signal(source, destination):
                result = real_copyfile(source, destination)
                os.kill(os.getpid(), opread.signal.SIGTERM)
                return result

            with mock.patch.object(opread.sys, "argv", ["opread", "op://vault/item", "Finance"]), \
                 mock.patch.object(opread.tempfile, "mkdtemp", side_effect=make_tempdir), \
                 mock.patch.object(opread.shutil, "copyfile", side_effect=copy_then_signal), \
                 mock.patch.object(opread.subprocess, "Popen") as popen:
                self.assertEqual(opread.main(), 143)
            popen.assert_not_called()
            self.assertFalse(os.path.exists(path))

    def test_sigterm_immediately_after_mkdtemp_removes_empty_private_dir(self):
        with tempfile.TemporaryDirectory() as parent:
            path = os.path.join(parent, "private")

            def make_tempdir(**_kwargs):
                os.mkdir(path, 0o700)
                os.kill(os.getpid(), opread.signal.SIGTERM)
                return path

            with mock.patch.object(opread.sys, "argv", ["opread", "op://vault/item", "Finance"]), \
                 mock.patch.object(opread.tempfile, "mkdtemp", side_effect=make_tempdir), \
                 mock.patch.object(opread.subprocess, "Popen") as popen:
                self.assertEqual(opread.main(), 143)
            popen.assert_not_called()
            self.assertFalse(os.path.exists(path))

    def test_sigterm_during_popen_publication_cleans_published_child(self):
        with tempfile.TemporaryDirectory() as parent:
            path = os.path.join(parent, "private")
            child = Child(0)

            def make_tempdir(**_kwargs):
                os.mkdir(path, 0o700)
                return path

            def publish_then_signal(*_args, **_kwargs):
                os.kill(os.getpid(), opread.signal.SIGTERM)
                return child

            with mock.patch.object(opread.sys, "argv", ["opread", "op://vault/item", "Finance"]), \
                 mock.patch.object(opread.tempfile, "mkdtemp", side_effect=make_tempdir), \
                 mock.patch.object(opread.subprocess, "Popen", side_effect=publish_then_signal), \
                 mock.patch.object(opread, "terminate_process_group") as cleanup:
                self.assertEqual(opread.main(), 143)
            cleanup.assert_called_once_with(child)
            self.assertFalse(os.path.exists(path))

    @unittest.skipUnless(os.name == "posix" and Path("/proc").exists(), "requires Linux process groups")
    def test_group_cleanup_kills_descendant_after_group_leader_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory) / "descendant.pid"
            descendant = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
            leader_code = (
                "import subprocess,sys,time; "
                f"p=subprocess.Popen([sys.executable,'-c',{descendant!r}]); "
                f"open({str(pidfile)!r},'w').write(str(p.pid)); time.sleep(.1)"
            )
            leader = __import__("subprocess").Popen(
                [opread.sys.executable, "-c", leader_code], start_new_session=True,
                stdin=__import__("subprocess").DEVNULL,
                stdout=__import__("subprocess").DEVNULL,
                stderr=__import__("subprocess").DEVNULL,
            )
            self.assertEqual(leader.wait(timeout=3), 0)
            descendant_pid = int(pidfile.read_text())
            opread.terminate_process_group(leader)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                try:
                    state = Path(f"/proc/{descendant_pid}/stat").read_text().split()[2]
                except FileNotFoundError:
                    break
                if state in ("Z", "X"):
                    break
                time.sleep(0.02)
            else:
                self.fail("SIGTERM-resistant descendant survived process-group cleanup")

    def test_backend_conflict_is_reported_without_echoing_value(self):
        with mock.patch.dict(opread.os.environ, {"OP_CONNECT_TOKEN": "secret-token"}, clear=False), \
             mock.patch.object(opread.sys, "argv", ["opread", "op://vault/item", "Finance"]), \
             mock.patch.object(opread.tempfile, "mkdtemp") as mkdtemp, \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(opread.main(), 2)
        self.assertIn("non-interactive", errors.getvalue())
        self.assertNotIn("secret-token", errors.getvalue())
        mkdtemp.assert_not_called()

    def test_manual_session_token_is_rejected_without_echoing_value(self):
        with mock.patch.dict(opread.os.environ, {"OP_SESSION_WORK": "secret-token"}, clear=False), \
             mock.patch.object(opread.sys, "argv", ["opread", "op://vault/item", "Finance"]), \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(opread.main(), 2)
        self.assertNotIn("secret-token", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
