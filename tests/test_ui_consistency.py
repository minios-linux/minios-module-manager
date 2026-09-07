from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'lib/minios_module_manager/ui.py').read_text(encoding='utf-8')


class ReleaseMetadataTests(unittest.TestCase):
    def test_version_dependencies_and_mime_ownership_are_documented(self):
        changelog = (ROOT / 'debian/changelog').read_text(encoding='utf-8')
        version = re.search(
            r'^minios-module-manager \(([^)]+)\)', changelog).group(1)
        package_init = (ROOT / 'lib/minios_module_manager/__init__.py').read_text(
            encoding='utf-8')
        manpage = (ROOT / 'manpages/en/minios-module-manager.1').read_text(
            encoding='utf-8')
        readme = (ROOT / 'README.md').read_text(encoding='utf-8')
        self.assertIn("__version__ = {!r}".format(version), package_init)
        self.assertIn(
            'MiniOS Module Manager {}"'.format(version), manpage.splitlines()[0])
        self.assertIn('python3-minios-gui >= 1.4.0', readme)
        self.assertIn('minios-tools >= 1.7.0', readme)
        self.assertIn('owned and installed by `minios-tools`', readme)


class SharedGuiApiTests(unittest.TestCase):
    def test_icon_fallback_uses_shared_resolver(self):
        self.assertIn('resolve_icon(', SOURCE)
        self.assertNotIn('Gtk.IconTheme', SOURCE)
        self.assertNotIn('def _build_base_icon_name', SOURCE)

    def test_standard_dialogs_and_byte_formatting_are_shared(self):
        self.assertNotIn('Gtk.MessageDialog', SOURCE)
        self.assertNotIn('Gtk.FileChooserDialog', SOURCE)
        self.assertIn('choose_open_files(', SOURCE)
        self.assertIn('choose_save_file(', SOURCE)
        self.assertIn('format_bytes(', SOURCE)

    def test_snapshot_placeholders_and_simple_tasks_are_shared(self):
        self.assertIn('StatePlaceholder(', SOURCE)
        self.assertIn('BackgroundTask(', SOURCE)

    def test_extraction_uses_shared_cancellable_progress(self):
        self.assertIn('self.dialog = ProgressDialog(', SOURCE)
        self.assertIn('CommandRunner(', SOURCE)
        self.assertIn('stderr_callback=self._handle_stderr', SOURCE)
        self.assertIn('self.runner.cancel()', SOURCE)
        self.assertNotIn('Gtk.ProgressBar()', SOURCE)

    def test_disabled_module_deletion_is_explicit_and_destructive(self):
        self.assertIn("self.detail_delete = Gtk.Button(label=_('Delete Module'))", SOURCE)
        self.assertIn("self.detail_delete.get_style_context().add_class('destructive-action')", SOURCE)
        self.assertIn('delete_disabled_module, module.name, True,', SOURCE)

    def test_all_module_actions_show_semantic_icons(self):
        self.assertIn('button.set_image(new_icon(', SOURCE)
        self.assertIn('button.set_always_show_image(True)', SOURCE)
        for icon in ('media-mount', 'media-eject', 'list-add-symbolic',
                     'list-remove-symbolic', 'edit-delete-symbolic'):
            self.assertIn(icon, SOURCE)


if __name__ == '__main__':
    unittest.main()
