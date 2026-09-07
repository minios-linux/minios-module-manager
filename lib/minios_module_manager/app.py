"""Application entry point."""

import gi

gi.require_version('Gtk', '3.0')
from gi.repository import Gio, Gtk

from minios_gui import apply_minios_css

from .ui import ModuleManagerWindow


APPLICATION_ID = 'org.minios.ModuleManager'


class ModuleManagerApplication(Gtk.Application):
    def __init__(self):
        Gtk.Application.__init__(
            self, application_id=APPLICATION_ID,
            flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.window = None
        self._css_loaded = False

    def do_activate(self):
        if not self._css_loaded:
            apply_minios_css('/usr/share/minios-module-manager/style.css')
            self._css_loaded = True
        if self.window is None:
            self.window = ModuleManagerWindow(self)
        self.window.show_all()
        self.window.present()

    def do_open(self, files, _count, _hint):
        self.do_activate()
        for item in files:
            path = item.get_path()
            if path:
                self.window.open_local_module(path)
                break


def main(argv=None):
    app = ModuleManagerApplication()
    return app.run(argv)
