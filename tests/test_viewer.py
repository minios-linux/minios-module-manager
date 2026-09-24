"""Render the actual details widget, including the Cairo frame override."""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import cairo
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gio, GLib, Gtk

from minios_module_manager.app import ModuleManagerApplication
from minios_module_manager.model import (
    Inspection, InspectionEntry, LoadState, ModuleRecord, Snapshot,
)
from minios_module_manager import backend


class ModuleViewerTests(unittest.TestCase):
    def test_package_declares_cairo_bridge(self):
        control = (Path(__file__).resolve().parents[1] /
                   'debian/control').read_text()
        source, binary = control.split('\nPackage:', 1)
        self.assertIn('python3-gi-cairo', source)
        self.assertIn('python3-gi-cairo', binary)

    def test_direct_open_renders_contents_and_supports_search_and_selection(self):
        self.assertTrue(Gtk.init_check(None)[0], 'Run GUI tests with xvfb-run')
        application = ModuleManagerApplication()
        application.set_flags(application.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
        application.register(None)
        errors = []
        draws = []
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch('minios_module_manager.ui.BackgroundTask'), \
                mock.patch.object(sys, 'excepthook', lambda *args: errors.append(args)):
            try:
                source = Path(temporary) / 'example.sb'
                source.write_bytes(b'hsqs')
                application.open([Gio.File.new_for_path(str(source))], '')
                window = application.window
                self.assertFalse(window.detail_next_boot.get_visible())
                window._next_boot_snapshot = Snapshot(
                    state=LoadState.EMPTY, add_available=True)
                window._update_detail_actions()
                self.assertEqual(window.detail_next_boot.get_label(), 'Install Module')
                self.assertTrue(window.detail_next_boot.get_visible())
                self.assertEqual(window._detail_next_boot_action[2:4],
                                 (backend.add_to_next_boot, str(source)))
                window._next_boot_snapshot = Snapshot(
                    state=LoadState.READY, add_available=True,
                    modules=[ModuleRecord('example.sb', source='/minios/modules/example.sb')])
                window._update_detail_actions()
                self.assertFalse(window.detail_next_boot.get_visible())
                window._next_boot_snapshot = Snapshot(
                    state=LoadState.EMPTY, add_available=False)
                window._update_detail_actions()
                self.assertFalse(window.detail_next_boot.get_visible())
                window._apply_module_inspection(window._inspection_request, Inspection(
                    state=LoadState.READY, path='/tmp/example.sb', size=4096,
                    entries=(InspectionEntry('usr', 'directory'),
                             InspectionEntry('usr/bin', 'directory'),
                             InspectionEntry('usr/bin/example', 'file', size=42),
                             InspectionEntry('README', 'file', size=12))))
                viewer = window.detail_tree.get_parent().get_parent()
                viewer.connect('draw', lambda _widget, context: draws.append(type(context)))
                deadline = time.monotonic() + 2
                while not draws and time.monotonic() < deadline:
                    while GLib.MainContext.default().pending():
                        GLib.MainContext.default().iteration(False)
                    time.sleep(0.01)
                self.assertTrue(draws, 'The file viewer was not painted')
                self.assertIn(cairo.Context, draws)
                self.assertFalse(errors, errors)
                self.assertTrue(window.detail_tree.get_mapped())
                self.assertGreater(window.detail_tree.get_allocated_height(), 20)
                self.assertEqual(len(window.detail_store), 2)
                window.detail_search.set_text('example')
                window._on_detail_search_changed(window.detail_search)
                self.assertEqual(len(window.detail_store), 1)
                selection = window.detail_tree.get_selection()
                selection.select_path(Gtk.TreePath.new_from_string('0:0:0'))
                model, tree_iter = selection.get_selected()
                self.assertIsNotNone(tree_iter)
                self.assertEqual(model.get_value(tree_iter, 4), 'usr/bin/example')
                self.assertIn('usr/bin/example', window.detail_status.get_text())
            finally:
                if application.window is not None:
                    application.window.destroy()
                application.quit()


if __name__ == '__main__':
    unittest.main()
