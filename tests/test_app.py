import configparser
from pathlib import Path
import unittest
from unittest import mock

from minios_module_manager import app


ROOT = Path(__file__).resolve().parents[1]


class ApplicationIconTests(unittest.TestCase):
    def test_startup_sets_the_desktop_icon_for_application_windows(self):
        desktop = configparser.ConfigParser(interpolation=None)
        desktop.read(str(ROOT / 'share/applications/minios-module-manager.desktop'))
        desktop_icon = desktop['Desktop Entry']['Icon']

        for resolved_icon in (desktop_icon, 'package-x-generic'):
            with self.subTest(resolved_icon=resolved_icon):
                application = object()
                calls = mock.Mock()
                with mock.patch.object(app.Gtk.Application, 'do_startup') as startup, \
                        mock.patch.object(app, 'resolve_icon', return_value=resolved_icon) as resolve, \
                        mock.patch.object(app.Gtk.Window, 'set_default_icon_name') as set_icon:
                    calls.attach_mock(startup, 'startup')
                    calls.attach_mock(resolve, 'resolve')
                    calls.attach_mock(set_icon, 'set_icon')
                    app.ModuleManagerApplication.do_startup(application)

                self.assertEqual(calls.mock_calls, [
                    mock.call.startup(application),
                    mock.call.resolve(desktop_icon, fallback='package-x-generic'),
                    mock.call.set_icon(resolved_icon),
                ])


if __name__ == '__main__':
    unittest.main()
