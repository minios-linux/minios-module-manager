"""GTK 3 interface for MiniOS Module Manager."""

from collections import deque
import json
import os
import re
import threading

import gi

gi.require_version('Gdk', '3.0')
gi.require_version('Gtk', '3.0')
gi.require_version('Vte', '2.91')
gi.require_foreign('cairo')
from gi.repository import Gdk, Gio, GLib, Gtk, Pango, Vte

from minios_gui import (
    BackgroundTask, CommandRunner, HelpPopoverButton, LogView, ProgressDialog,
    StatePlaceholder, StatusBanner, TokenCompletionPopover, ask_confirmation,
    choose_folder, choose_open_file,
    choose_open_files, choose_save_file, classify_module, format_bytes,
    new_header_bar, new_header_icon_button, new_icon, resolve_icon,
    show_error_dialog, show_info_dialog,
)

from .backend import (
    CAPTURE_CANCELLED, activate_for_session, add_to_next_boot, cancel_chroot_session,
    capture_current_session, chroot_shell_argv, create_module_from_folder,
    create_module_from_packages, create_module_from_script,
    deactivate_for_session, delete_disabled_module, enable_for_next_boot,
    finish_chroot_session,
    load_module_inspection, load_next_boot_snapshot, load_running_snapshot,
    module_extraction_argv, new_capture_cancel_marker, package_indexes_available,
    prepare_chroot_session,
    query_package_names, remove_from_next_boot, request_capture_cancel,
    update_package_indexes,
)
from .i18n import _
from .model import LoadState, ModuleRecord


CREATE_METHODS = (
    ('packages', _('Packages'),
     _('For software from APT repositories or local .deb packages; dependencies are installed automatically.')),
    ('script', _('Installation Script'),
     _('For repeatable setup that can be expressed as commands without interactive input.')),
    ('chroot', _('Interactive Chroot'),
     _('For manual or interactive setup in a temporary root shell.')),
    ('folder', _('Folder'),
     _('For an already prepared filesystem tree that should become the module contents.')),
    ('session', _('Current Session Changes'),
     _('For eligible changes that have already been made in this live session.')),
)


def module_presentation_order(modules):
    """Keep backend order within system and custom module groups."""
    def is_custom(module):
        if module.origin in ('modules', 'persistence'):
            return True
        if module.origin == 'base':
            return False
        if module.source:
            parent = os.path.basename(os.path.dirname(module.source))
            if parent == 'modules':
                return True
            if parent == 'minios':
                return False
        return classify_module(module.name)[0] == 'custom'

    return tuple(sorted(modules, key=is_custom))


CREATE_HELP = {
    'packages': (
        _('Install packages from repositories or local .deb files and save the result as a module.'),
        (
            (_('What to enter'),
             _('• <b>Repository packages</b> — package names separated by spaces. Use <tt>↑</tt>/<tt>↓</tt> to choose a suggestion and <tt>Tab</tt> or <tt>Enter</tt> to accept it.\n• <b>Local packages</b> — add <tt>.deb</tt> files from disk.\n• <b>Output module</b> — choose the new <tt>.sb</tt> file.\n• <b>Compression</b> — leave <tt>zstd</tt> unless you need another format.\n• <b>Build base level</b> — choose the highest numbered active module to include; all active numbered modules up to it and all active unnumbered modules are used together.')),
            (_('How it works'),
             _('APT installs the selected packages and their dependencies. MiniOS does not install recommended or suggested packages by default; enable the corresponding checkboxes to include them explicitly. Package installation runs with administrator privileges. The current MiniOS session is not changed, and the new module is not loaded automatically.')),
        )),
    'script': (
        _('Run an installation script and save the changes it makes as a module.'),
        (
            (_('What to enter'),
             _('• <b>Installation script</b> — the script that performs the installation or setup.\n• <b>Seed folder</b> — optional files to copy into the module before the script runs.\n• <b>Output module</b> — choose the new <tt>.sb</tt> file.\n• <b>Compression</b> — leave <tt>zstd</tt> unless you need another format.\n• <b>Build base level</b> — choose the highest numbered active module to include; all active numbered modules up to it and all active unnumbered modules are used together.')),
            (_('How it works'),
             _('The script runs with administrator privileges and <b>without an interactive terminal</b>. If it asks questions or needs manual work, use <b>Interactive Chroot</b> instead. The script itself is not stored in the module. The current MiniOS session is not changed.')),
        )),
    'chroot': (
        _('Open a temporary MiniOS system in a terminal and save your changes as a module.'),
        (
            (_('Before you start'),
             _('• <b>Seed folder</b> — optional files to copy into the temporary system before the shell opens.\n• <b>Output module</b> — choose the new <tt>.sb</tt> file.\n• <b>Compression</b> — leave <tt>zstd</tt> unless you need another format.\n• <b>Build base level</b> — choose the highest numbered active module to include; all active numbered modules up to it and all active unnumbered modules are used together.')),
            (_('How to finish'),
             _('Work in the terminal as <b>root</b>. When you are finished, type <tt>exit</tt>. Then choose <b>Create Module</b> to save the changes, <b>Reopen Shell</b> to continue working, or <b>Discard Changes</b> to cancel. The current MiniOS session is not changed.')),
        )),
    'folder': (
        _('Convert the contents of a folder directly into a compressed .sb module.'),
        (
            (_('What to enter'),
             _('• <b>Source folder</b> — its contents become the root of the module. The source folder name itself is not added.\n• <b>Output module</b> — choose the new <tt>.sb</tt> file.\n• <b>Compression</b> — leave <tt>zstd</tt> unless you need another format.')),
            (_('Result'),
             _('The source folder is not modified and an existing output file is never overwritten. Ordinary conversion does not need administrator privileges. File ownership inside the module is normalized to <b>root</b>. The new module is not loaded automatically.')),
        )),
    'session': (
        _('Save changes already made in the current MiniOS session as a module.'),
        (
            (_('What to enter'),
             _('• <b>Output module</b> — choose the new <tt>.sb</tt> file.\n• <b>Compression</b> — leave <tt>zstd</tt> unless you need another format.\nMiniOS finds the current session changes automatically.')),
            (_('What is saved'),
             _('Files and configuration changes from the current session are saved, including supported file deletions. Logs, caches, boot data and temporary runtime files are skipped by the standard MiniOS save policy. Reading all session changes requires administrator privileges. The current session is not modified.')),
        )),
}


class ModuleExtractionJob(object):
    """Run sb2dir with phase feedback and cancellable streamed diagnostics."""

    def __init__(self, parent, source, target, finished_callback):
        self.target = target
        self.finished_callback = finished_callback
        self.result = None
        self.protocol_error = None
        self.stderr = deque(maxlen=200)
        self.dialog = ProgressDialog(
            parent=parent, title=_('Extract Module'), status=_('Preparing…'),
            show_log=True, cancellable=True)
        self.dialog.set_default_size(520, 260)
        self.dialog.set_deletable(False)
        self.dialog.connect('response', self._on_response)
        argv = module_extraction_argv(source, target)
        self.runner = None if argv is None else CommandRunner(
            argv, self._handle_stdout, self._finished,
            stderr_callback=self._handle_stderr,
            maximum_output_bytes=1024 * 1024)

    def start(self):
        if self.runner is None:
            self.finished_callback(
                False, False, _('MiniOS Tools is not installed.'))
            return
        self.dialog.show_all()
        self.dialog.operation_view.set_state('running')
        try:
            self.runner.start()
        except Exception as error:
            self.dialog.destroy()
            self.finished_callback(False, False, str(error))

    def _on_response(self, _dialog, response):
        if response != Gtk.ResponseType.CANCEL or self.runner is None:
            return
        self.dialog.operation_view.set_status(_('Cancelling…'))
        self.runner.cancel()

    def _set_phase(self, phase):
        labels = {
            'prepare': _('Preparing extraction…'),
            'extract': _('Extracting module…'),
            'publish': _('Publishing extracted files…'),
            'complete': _('Finishing…'),
        }
        text = labels.get(phase, _('Working…'))
        self.dialog.operation_view.set_status(text)
        self.dialog.operation_view.feed(text + '\n')

    def _handle_stdout(self, line):
        line = line.strip()
        if not line:
            return
        try:
            record = json.loads(line)
        except ValueError:
            self.protocol_error = _(
                'The module tool returned invalid progress data.')
            return
        if not isinstance(record, dict):
            self.protocol_error = _(
                'The module tool returned invalid progress data.')
            return
        if record.get('event') == 'phase':
            phase = record.get('phase')
            if isinstance(phase, str):
                self._set_phase(phase)
            else:
                self.protocol_error = _(
                    'The module tool returned invalid progress data.')
            return
        if record.get('product') == 'sb2dir':
            output = record.get('output')
            if (not isinstance(output, str) or
                    os.path.abspath(output) != os.path.abspath(self.target)):
                self.protocol_error = _(
                    'The module tool returned an unexpected extraction result.')
                return
            self.result = record
            return
        self.protocol_error = _(
            'The module tool returned invalid progress data.')

    def _handle_stderr(self, line):
        self.stderr.append(line.rstrip())
        self.dialog.operation_view.feed(line)

    def _finished(self, returncode, cancelled):
        details = '\n'.join(self.stderr).strip()
        if self.protocol_error:
            details = '{}{}{}'.format(
                self.protocol_error, '\n' if details else '', details)
        success = returncode == 0 and not cancelled and not self.protocol_error
        if success and self.result is None:
            success = False
            details = _('The module tool completed without an extraction result.')
        self.dialog.destroy()
        self.finished_callback(success, cancelled, details)
        return False


class BuildBaseSelector(Gtk.MenuButton):
    """Compact build-base field backed by a scrollable module popover."""

    def __init__(self):
        Gtk.MenuButton.__init__(self)
        self.set_hexpand(True)
        self.set_halign(Gtk.Align.FILL)
        self.set_focus_on_click(False)
        self._build_base_levels = {}
        self._items = {}
        self._rows = {}
        self._active_id = None

        summary = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._summary_icon = Gtk.Image()
        summary.pack_start(self._summary_icon, False, False, 0)
        self._summary_label = Gtk.Label(xalign=0)
        self._summary_label.set_ellipsize(Pango.EllipsizeMode.END)
        summary.pack_start(self._summary_label, True, True, 0)
        arrow = Gtk.Image.new_from_icon_name('pan-down-symbolic', Gtk.IconSize.BUTTON)
        summary.pack_end(arrow, False, False, 0)
        self.add(summary)

        self._popover = Gtk.Popover.new(self)
        self._popover.set_no_show_all(True)
        self._popover.set_position(Gtk.PositionType.BOTTOM)
        self._popover.connect('show', self._on_popover_show)
        self.connect('size-allocate', self._on_size_allocate)
        self._scrolled = Gtk.ScrolledWindow()
        self._scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scrolled.set_max_content_height(280)
        self._scrolled.set_propagate_natural_height(True)
        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list.set_activate_on_single_click(True)
        self._list.set_header_func(self._build_row_header)
        self._list.connect('row-activated', self._on_row_activated)
        self._scrolled.add(self._list)
        self._popover.add(self._scrolled)
        self._scrolled.show_all()
        self.set_popover(self._popover)

    @staticmethod
    def _build_row_header(row, before):
        row.set_header(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
                       if before is not None else None)

    def _on_size_allocate(self, _widget, allocation):
        # Keep the picker compact on wide windows.  The field itself spans the
        # form, but the rich choices do not need to become a full-width panel.
        width = min(max(280, allocation.width - 16), 680)
        self._scrolled.set_min_content_width(width)
        # Gtk 3.22 does not reliably propagate min-content-width from a
        # ScrolledWindow into Popover sizing, so keep an explicit width request.
        self._scrolled.set_size_request(width, -1)

    def _on_popover_show(self, _popover):
        width = min(max(280, self.get_allocated_width() - 16), 680)
        self._scrolled.set_min_content_width(width)
        self._scrolled.set_size_request(width, -1)

    def _make_row(self, item_id, title, subtitle, icon_name, size):
        row = Gtk.ListBoxRow()
        row._build_base_id = item_id
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        content.set_margin_top(2)
        content.set_margin_bottom(2)
        content.set_margin_start(8)
        content.set_margin_end(8)
        icon = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
        content.pack_start(icon, False, False, 0)

        title_label = Gtk.Label(label=title, xalign=0)
        title_label.get_style_context().add_class('row-title')
        title_label.set_ellipsize(Pango.EllipsizeMode.END)
        content.pack_start(title_label, True, True, 0)

        if subtitle:
            filename_label = Gtk.Label(label=subtitle, xalign=1)
            filename_label.get_style_context().add_class('row-meta')
            filename_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            filename_label.set_max_width_chars(38)
            content.pack_end(filename_label, False, False, 0)
        row.add(content)
        return row

    def set_choices(self, choices, active_id='all'):
        for child in self._list.get_children():
            self._list.remove(child)
        self._items = {}
        self._rows = {}
        self._build_base_levels = {}
        for item_id, level, title, subtitle, icon_name, size in choices:
            self._items[item_id] = (title, subtitle, icon_name, size)
            self._build_base_levels[item_id] = level
            row = self._make_row(item_id, title, subtitle, icon_name, size)
            self._rows[item_id] = row
            self._list.add(row)
        self._list.show_all()
        self.set_active_id(active_id if active_id in self._items else 'all')

    def get_active_id(self):
        return self._active_id

    def set_active_id(self, item_id):
        if item_id not in self._items:
            return False
        self._active_id = item_id
        title, subtitle, icon_name, size = self._items[item_id]
        self._summary_icon.set_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
        self._summary_label.set_text(subtitle or title)
        tooltip = title
        if subtitle:
            tooltip += '\n' + subtitle
        if size:
            tooltip += ' · ' + size
        self.set_tooltip_text(tooltip)
        row = self._rows.get(item_id)
        if row is not None:
            self._list.select_row(row)
        return True

    def _on_row_activated(self, _listbox, row):
        self.set_active_id(row._build_base_id)
        self._popover.popdown()


class ModuleFileViewer(Gtk.Overlay):
    """Tree-view container that paints rounded masking and frame above its child."""

    def __init__(self):
        Gtk.Overlay.__init__(self)
        self.get_style_context().add_class('content-card')
        self.get_style_context().add_class('module-file-viewer-frame')

    def do_draw(self, cr):
        # Let GtkOverlay draw the scrolled TreeView first.  Painting the mask
        # and frame here afterwards keeps them visually above the table while
        # leaving pointer events entirely to the TreeView/ScrolledWindow.
        result = Gtk.Overlay.do_draw(self, cr)
        allocation = self.get_allocation()
        width = float(allocation.width)
        height = float(allocation.height)
        radius = min(6.0, width / 2.0, height / 2.0)
        if radius <= 0:
            return result

        cr.save()
        self._clip_outside_rounded_corners(cr, width, height, radius)
        found, color = self.get_style_context().lookup_color('theme_bg_color')
        if not found:
            color = self.get_style_context().get_background_color(Gtk.StateFlags.NORMAL)
        Gdk.cairo_set_source_rgba(cr, color)
        cr.paint()
        cr.restore()
        Gtk.render_frame(self.get_style_context(), cr, 0, 0, width, height)
        return result

    @staticmethod
    def _clip_outside_rounded_corners(cr, width, height, radius):
        half_pi = 1.5707963267948966
        pi = 3.141592653589793

        cr.move_to(0, 0)
        cr.line_to(radius, 0)
        cr.arc_negative(radius, radius, radius, -half_pi, -pi)
        cr.close_path()
        cr.move_to(width - radius, 0)
        cr.line_to(width, 0)
        cr.line_to(width, radius)
        cr.arc_negative(width - radius, radius, radius, 0, -half_pi)
        cr.close_path()
        cr.move_to(0, height - radius)
        cr.line_to(0, height)
        cr.line_to(radius, height)
        cr.arc(radius, height - radius, radius, half_pi, pi)
        cr.close_path()
        cr.move_to(width, height - radius)
        cr.line_to(width, height)
        cr.line_to(width - radius, height)
        cr.arc_negative(width - radius, height - radius, radius, half_pi, 0)
        cr.close_path()
        cr.clip()


class ModuleManagerWindow(Gtk.ApplicationWindow):
    def __init__(self, application):
        Gtk.ApplicationWindow.__init__(
            self, application=application, title=_('MiniOS Module Manager'))
        self.set_default_size(800, 490)
        self._running_request = 0
        self._next_boot_request = 0
        self._inspection_request = 0
        self._detail_module = None
        self._detail_scope = None
        self._running_snapshot = None
        self._next_boot_snapshot = None
        self._detail_runtime_action = None
        self._detail_next_boot_action = None
        self._detail_delete_action = None
        self._chroot_session_id = None
        self._chroot_config = None
        self._chroot_shell_running = False
        self._chroot_busy = False
        self._chroot_preparing = False
        self._close_after_chroot_cancel = False
        self._session_capture_busy = False
        self._session_capture_cancel = None
        self._package_indexes_busy = False
        self._extract_job = None
        self._build_base_combos = []
        self.connect('delete-event', self._on_window_delete)
        self._build_header()
        self._build_content()
        self._disable_pointer_focus(self)
        self._setup_drag_and_drop()
        self.refresh_running_snapshot()
        self.refresh_next_boot_snapshot()

    def _build_header(self):
        self.header_bar = new_header_bar(_('MiniOS Module Manager'))
        self.header_back = new_header_icon_button(
            ('go-previous', 'go-previous-symbolic'), _('Back'))
        self.header_back.set_always_show_image(True)
        self.header_back.set_no_show_all(True)
        self.header_back.connect('clicked', self._on_header_back)
        self.header_back.hide()
        self.header_bar.pack_start(self.header_back)
        self._header_back_target = None
        self.set_titlebar(self.header_bar)

    def _on_header_back(self, _button):
        target = self._header_back_target
        if target is None:
            return
        scope, page = target
        if scope == 'modules':
            self._show_module_composition()
        elif scope == 'create':
            self.create_pages.set_visible_child_name(page)

    def _update_header_back(self, widget=None, _page=None, page_num=None):
        target = None
        if hasattr(self, 'workspace_notebook'):
            # GtkNotebook::switch-page is emitted while get_current_page() may
            # still report the page being left. Use the signal's destination
            # page directly so Back never inherits stale visibility state.
            if widget is self.workspace_notebook and page_num is not None:
                workspace = page_num
            else:
                workspace = self.workspace_notebook.get_current_page()
            if workspace == 0 and self.module_pages.get_visible_child_name() == 'details':
                target = ('modules', 'composition')
            elif workspace == 1:
                page = self.create_pages.get_visible_child_name()
                target_page = {
                    'folder-configure': 'methods',
                    'package-configure': 'methods',
                    'script-configure': 'methods',
                    'chroot-configure': 'methods',
                    'session-configure': 'methods',
                    'folder-review': 'folder-configure',
                    'package-review': 'package-configure',
                    'script-review': 'script-configure',
                    'chroot-review': 'chroot-configure',
                    'session-review': 'session-configure',
                }.get(page)
                if target_page is not None:
                    target = ('create', target_page)
        self._header_back_target = target
        if target is None:
            self.header_back.hide()
        else:
            self.header_back.show()

    def _disable_pointer_focus(self, widget):
        if isinstance(widget, Gtk.Button):
            widget.set_focus_on_click(False)
        if isinstance(widget, Gtk.Container):
            for child in widget.get_children():
                self._disable_pointer_focus(child)

    def _page_title_label(self, text):
        label = Gtk.Label(label=text, xalign=0)
        label.get_style_context().add_class('page-title')
        return label

    @staticmethod
    def _field_label_with_help(text, help_button):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        box.set_halign(Gtk.Align.FILL)
        box.set_valign(Gtk.Align.CENTER)
        label = Gtk.Label(label=text, xalign=0)
        label.set_halign(Gtk.Align.START)
        help_button.set_halign(Gtk.Align.END)
        help_button.set_valign(Gtk.Align.CENTER)
        box.pack_start(label, False, False, 0)
        box.pack_end(help_button, False, False, 0)
        return box

    @staticmethod
    def _review_card(field_labels):
        grid = Gtk.Grid(column_spacing=18, row_spacing=8)
        grid.set_hexpand(True)
        grid.get_style_context().add_class('content-card')
        values = {}
        for row, (field_id, caption) in enumerate(field_labels):
            key = Gtk.Label(label=caption, xalign=0, yalign=0)
            key.set_valign(Gtk.Align.START)
            key.get_style_context().add_class('dim-label')
            value = Gtk.Label(xalign=0, yalign=0)
            value.set_hexpand(True)
            value.set_line_wrap(True)
            value.set_selectable(True)
            value.set_valign(Gtk.Align.START)
            grid.attach(key, 0, row, 1, 1)
            grid.attach(value, 1, row, 1, 1)
            values[field_id] = value
        return grid, values

    @staticmethod
    def _set_review_values(values, rows):
        for field_id, value in rows:
            values[field_id].set_text(str(value))
            values[field_id].select_region(0, 0)

    @staticmethod
    def _module_build_level(name):
        match = re.match(r'^(\d+)', name or '')
        return int(match.group(1), 10) if match else None

    def _new_build_base_combo(self):
        combo = BuildBaseSelector()
        combo.set_choices((
            ('all', None, _('All active modules'), '',
             resolve_icon(('application-x-sb', 'package-x-generic'),
                          fallback='package-x-generic'), ''),
        ))
        self._build_base_combos.append(combo)
        return combo

    def _refresh_build_base_choices(self):
        snapshot = self._running_snapshot
        modules = snapshot.modules if snapshot is not None and snapshot.usable else ()
        for combo in self._build_base_combos:
            active_id = combo.get_active_id() or 'all'
            choices = [(
                'all', None, _('All active modules'), '',
                resolve_icon(('application-x-sb', 'package-x-generic'),
                             fallback='package-x-generic'), '')]
            for module in modules:
                level = self._module_build_level(module.name)
                if level is None:
                    continue
                role, icons, size = self._module_display(module)
                choices.append((
                    module.name, level, role, module.name,
                    resolve_icon(icons, fallback='package-x-generic'), size or ''))
            combo.set_choices(choices, active_id)

    @staticmethod
    def _build_level_value(combo):
        item_id = combo.get_active_id() or 'all'
        levels = getattr(combo, '_build_base_levels', {})
        if item_id not in levels:
            return None, _('The selected build base is no longer active.')
        return levels[item_id], ''

    def _compression_help_button(self):
        return HelpPopoverButton(
            _('Compression'),
            _('Compression controls the trade-off between module size and CPU work when MiniOS reads files from it.'),
            (
                (_('Quick choice'),
                 _('<b>LZ4</b> — fastest decompression and the best choice when runtime responsiveness matters most; it produces the largest modules.\n<b>Zstandard (zstd)</b> — recommended balance: fast decompression with much better compression than LZ4.\n<b>XZ</b> — usually produces the smallest modules, but needs the most CPU and is normally the slowest to read.')),
                (_('Other formats'),
                 _('<b>LZO</b> — fast legacy option; usually larger than zstd.\n<b>Gzip</b> — compatibility-oriented legacy option; usually neither as fast as LZ4 nor as compact as zstd or XZ.\nActual performance also depends on the CPU and storage: on slow media, reading a smaller module can sometimes offset slower decompression.')),
            ),
            compact=True, tooltip=_('Which compression should I choose?'),
            markup=True, width=500)

    def _build_base_help_button(self):
        return HelpPopoverButton(
            _('Build base level'),
            _('The selected module is a cutoff level for the active module stack, not a single module to build on.'),
            (
                (_('How the level works'),
                 _('If you select <b>03-gui-base…</b>, the temporary system is composed from every active numbered module from 00 through 03, plus every active unnumbered module. Numbered modules 04 and above are excluded. <b>All active modules</b> uses the complete active stack.')),
                (_('Why it matters'),
                 _('Packages and libraries that already exist in the selected base stack are not copied into the new module. Building on a lower level therefore causes missing dependencies to be installed into the new module, which can make it more self-contained but larger. Building on the full stack can make a smaller module, but it may depend on higher-level modules that happened to be active during the build.')),
                (_('What is changed'),
                 _('The base modules are never modified or merged. Only changes made in the temporary build environment are captured into the new .sb module.')),
            ),
            compact=True, tooltip=_('How does the build level work?'),
            markup=True, width=540)

    def _recommended_packages_help_button(self):
        return HelpPopoverButton(
            _('Recommended packages'),
            _('APT classifies some packages as recommended rather than required dependencies.'),
            (
                (_('When disabled'),
                 _('Module Manager uses the MiniOS default: APT does not automatically install recommended packages.')),
                (_('When enabled'),
                 _('Module Manager passes --install-recommends, explicitly overriding the MiniOS default so APT installs recommended packages.')),
            ),
            compact=True, tooltip=_('How are recommended packages handled?'),
            width=500)

    def _suggested_packages_help_button(self):
        return HelpPopoverButton(
            _('Suggested packages'),
            _('APT classifies suggested packages as optional additions that may complement an installed package.'),
            (
                (_('When disabled'),
                 _('Module Manager uses the MiniOS default: APT does not automatically install suggested packages.')),
                (_('When enabled'),
                 _('Module Manager passes --install-suggests, explicitly overriding the MiniOS default so APT installs suggested packages.')),
            ),
            compact=True, tooltip=_('How are suggested packages handled?'),
            width=500)

    def _build_level_text(self, combo, level):
        if level is None:
            return _('all active modules')
        item_id = combo.get_active_id()
        return item_id or _('selected active module')

    def _style_log_view(self, log):
        log.set_shadow_type(Gtk.ShadowType.NONE)
        log.get_style_context().add_class('minios-list')
        return log

    def _build_run_output(self, initial_text):
        progress = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        spinner = Gtk.Spinner()
        spinner.set_valign(Gtk.Align.CENTER)
        progress.pack_start(spinner, False, False, 0)
        status = Gtk.Label(label=initial_text, xalign=0)
        status.set_line_wrap(True)
        progress.pack_start(status, True, True, 0)
        log = self._style_log_view(LogView(maximum_characters=200000))
        log.set_min_content_height(100)
        return progress, status, spinner, log

    def _build_result_output(self):
        output = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        stack = Gtk.Stack()
        stack.set_hhomogeneous(False)
        stack.set_vhomogeneous(False)

        summary_stack = Gtk.Stack()
        summary_stack.set_hhomogeneous(False)
        summary_stack.set_vhomogeneous(False)
        summary = Gtk.Label(xalign=0, yalign=0)
        summary.set_line_wrap(True)
        summary.set_selectable(True)
        details = Gtk.Grid(column_spacing=18, row_spacing=8)
        details.set_hexpand(True)
        details.get_style_context().add_class('content-card')
        summary_stack.add_named(summary, 'text')
        summary_stack.add_named(details, 'details')
        summary_stack.set_visible_child_name('text')

        log = self._style_log_view(LogView(maximum_characters=200000))
        log.set_min_content_height(160)
        stack.add_named(summary_stack, 'summary')
        stack.add_named(log, 'log')
        stack.set_visible_child_name('summary')
        output.pack_start(stack, True, True, 0)
        toggle = Gtk.ToggleButton(label=_('View Build Log'))
        toggle.set_halign(Gtk.Align.START)
        toggle.set_no_show_all(True)
        toggle.hide()
        toggle.connect('toggled', self._toggle_result_log, output)
        output.pack_start(toggle, False, False, 0)
        output._result_stack = stack
        output._result_summary_stack = summary_stack
        output._result_details = details
        output._result_toggle = toggle
        return output, summary, log

    @staticmethod
    def _result_size(value):
        try:
            count = int(value)
        except (TypeError, ValueError):
            return str(value)
        return '{} ({:,} {})'.format(format_bytes(count), count, _('bytes'))

    def _set_result_details(self, output, log, rows, build_log=''):
        grid = output._result_details
        for child in grid.get_children():
            grid.remove(child)
        for row, (caption, value) in enumerate(rows):
            key = Gtk.Label(label=caption, xalign=0, yalign=0)
            key.set_valign(Gtk.Align.START)
            key.get_style_context().add_class('dim-label')
            value_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            value_box.set_hexpand(True)
            label = Gtk.Label(label=str(value), xalign=0, yalign=0)
            label.set_hexpand(True)
            label.set_line_wrap(True)
            label.set_selectable(True)
            label.set_valign(Gtk.Align.START)
            value_box.pack_start(label, True, True, 0)
            grid.attach(key, 0, row, 1, 1)
            grid.attach(value_box, 1, row, 1, 1)
        grid.show_all()
        output._result_summary_stack.set_visible_child_name('details')
        self._set_result_log_state(output, log, build_log, False, '')

    def _set_result_log_state(self, output, log, build_log, diagnostic, text):
        build_log = str(build_log or '')
        log.clear()
        log_text = build_log
        if diagnostic:
            if log_text and not log_text.endswith('\n'):
                log_text += '\n'
            if log_text:
                log_text += '\n'
            log_text += _('Result: {}').format(text)
        if log_text:
            log.feed(log_text)
            output._result_toggle.show()
        else:
            output._result_toggle.hide()
        show_log = self._result_uses_log(text, diagnostic)
        output._result_toggle.set_active(show_log)
        output._result_stack.set_visible_child_name(
            'log' if show_log else 'summary')
        output._result_toggle.set_label(
            _('Show Summary') if show_log else _('View Build Log'))

    @staticmethod
    def _result_uses_log(text, diagnostic=False):
        text = str(text or '')
        return diagnostic and ('\n' in text or len(text) > 240)

    def _toggle_result_log(self, button, output):
        show_log = button.get_active()
        output._result_stack.set_visible_child_name(
            'log' if show_log else 'summary')
        button.set_label(_('Show Summary') if show_log else _('View Build Log'))

    def _set_result_output(self, output, summary, log, text, diagnostic=False,
                           build_log=''):
        text = str(text or '')
        summary.set_text(text)
        summary.select_region(0, 0)
        output._result_summary_stack.set_visible_child_name('text')
        self._set_result_log_state(
            output, log, build_log, diagnostic, text)

    def _create_method_header(self, outer, method_id, title, _back_callback):
        summary, sections = CREATE_HELP[method_id]
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.pack_start(self._page_title_label(title), False, False, 0)
        help_button = HelpPopoverButton(
            _('Help: {}').format(title), summary, sections,
            compact=True, tooltip=_('Show help for this creation method'),
            markup=True)
        controls.pack_start(help_button, False, False, 0)
        outer.pack_start(controls, False, False, 0)
        purpose = Gtk.Label(label=summary, xalign=0)
        purpose.set_line_wrap(True)
        purpose.get_style_context().add_class('page-subtitle')
        outer.pack_start(purpose, False, False, 0)

    def _module_state_help_button(self):
        return HelpPopoverButton(
            _('Module Sets'),
            _('MiniOS can use one module set in the current session and a different set after restart.'),
            (
                (_('Running Now'),
                 _('Shows modules that are currently loaded into the live system. Changes made here affect this session only and do not decide what MiniOS will load after restart.')),
                (_('Next Boot'),
                 _('Use Add Module to copy a module into persistent storage for the next startup. Excluding a user module moves it to the disabled module store instead of deleting it; select an excluded module to include it again. Base modules and modules outside persistent writable storage cannot be changed here. These actions do not change the current session.')),
                (_('Why the lists can differ'),
                 _('You can temporarily activate or deactivate a module for testing while keeping the next-boot configuration unchanged, or prepare a different module set for the next restart. Compare both tabs before rebooting when you want the change to persist.')),
            ),
            compact=True, tooltip=_('How do Running Now and Next Boot differ?'),
            markup=True, width=520)

    def _build_content(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root.set_margin_top(8)
        root.set_margin_bottom(8)
        root.set_margin_start(8)
        root.set_margin_end(8)
        self.add(root)

        self.workspace_notebook = Gtk.Notebook()
        self.workspace_notebook.get_style_context().add_class('module-manager-notebook')
        self.workspace_notebook.set_tab_pos(Gtk.PositionType.TOP)
        self.workspace_notebook.set_scrollable(False)
        modules_page = self._build_modules_workspace()
        create_page = self._build_create_workspace()
        for page in (modules_page, create_page):
            page.set_margin_top(12)
            page.set_margin_bottom(12)
            page.set_margin_start(12)
            page.set_margin_end(12)
        self.workspace_notebook.append_page(
            modules_page, Gtk.Label(label=_('Manage Modules')))
        self.workspace_notebook.append_page(
            create_page, Gtk.Label(label=_('Create Module')))
        self.workspace_notebook.connect('switch-page', self._update_header_back)
        self.module_pages.connect('notify::visible-child-name', self._update_header_back)
        self.create_pages.connect('notify::visible-child-name', self._update_header_back)
        root.pack_start(self.workspace_notebook, True, True, 0)
        self._update_header_back()

    def _build_modules_workspace(self):
        self.module_pages = Gtk.Stack()
        self.module_pages.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label=_('Module Sets'), xalign=0)
        title.get_style_context().add_class('page-title')
        heading.pack_start(title, False, False, 0)
        heading.pack_start(self._module_state_help_button(), False, False, 0)
        box.pack_start(heading, False, False, 0)

        self.snapshot_notebook = Gtk.Notebook()
        self.snapshot_notebook.get_style_context().add_class('module-manager-notebook')
        self.snapshot_notebook.set_tab_pos(Gtk.PositionType.TOP)
        self.snapshot_notebook.set_scrollable(False)
        running_page = self._build_running_snapshot()
        next_boot_page = self._build_next_boot_snapshot()
        for page in (running_page, next_boot_page):
            page.set_margin_top(12)
            page.set_margin_bottom(12)
            page.set_margin_start(12)
            page.set_margin_end(12)
        self.snapshot_notebook.append_page(
            running_page, Gtk.Label(label=_('Running Now')))
        self.snapshot_notebook.append_page(
            next_boot_page, Gtk.Label(label=_('Next Boot')))
        box.pack_start(self.snapshot_notebook, True, True, 0)
        self.module_pages.add_named(box, 'composition')
        self.module_pages.add_named(self._build_module_details(), 'details')
        return self.module_pages

    def _build_running_snapshot(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.running_status = Gtk.Label(xalign=0)
        self.running_status.set_line_wrap(True)
        controls.pack_start(self.running_status, True, True, 0)
        refresh = Gtk.Button(label=_('Refresh'))
        refresh.set_image(new_icon('view-refresh-symbolic', accessible_name=_('Refresh')))
        refresh.get_style_context().add_class('minios-text-button')
        refresh.set_always_show_image(True)
        refresh.connect('clicked', lambda _button: self.refresh_running_snapshot())
        controls.pack_end(refresh, False, False, 0)
        box.pack_start(controls, False, False, 0)

        self.running_stack = Gtk.Stack()
        self.running_message = self._snapshot_placeholder(
            _('Loading running modules…'),
            _('Reading the module order from MiniOS Tools.'))
        self.running_stack.add_named(self.running_message, 'message')

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.running_list = Gtk.ListBox()
        self.running_list.get_style_context().add_class('minios-list')
        self.running_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.running_list.set_activate_on_single_click(True)
        self.running_list.connect('row-activated', self._on_module_row_activated)
        scrolled.add(self.running_list)
        self.running_stack.add_named(scrolled, 'list')
        box.pack_start(self.running_stack, True, True, 0)
        return box

    def _build_next_boot_snapshot(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.next_boot_status = Gtk.Label(xalign=0)
        self.next_boot_status.set_line_wrap(True)
        controls.pack_start(self.next_boot_status, True, True, 0)
        self.next_boot_add = Gtk.Button(label=_('Add Module…'))
        self.next_boot_add.set_image(new_icon('list-add-symbolic', accessible_name=_('Add Module…')))
        self.next_boot_add.get_style_context().add_class('minios-text-button')
        self.next_boot_add.set_always_show_image(True)
        self.next_boot_add.set_sensitive(False)
        self.next_boot_add.connect('clicked', self._choose_next_boot_module)
        controls.pack_end(self.next_boot_add, False, False, 0)
        refresh = Gtk.Button(label=_('Refresh'))
        refresh.set_image(new_icon('view-refresh-symbolic', accessible_name=_('Refresh')))
        refresh.get_style_context().add_class('minios-text-button')
        refresh.set_always_show_image(True)
        refresh.connect('clicked', lambda _button: self.refresh_next_boot_snapshot())
        controls.pack_end(refresh, False, False, 0)
        box.pack_start(controls, False, False, 0)

        self.next_boot_storage_notice = StatusBanner(
            _('Modules cannot be added or removed because no durable writable MiniOS module storage is available.'),
            intent='warning')
        self.next_boot_storage_notice.set_no_show_all(True)
        self.next_boot_storage_notice.hide()
        box.pack_start(self.next_boot_storage_notice, False, False, 0)

        self.next_boot_stack = Gtk.Stack()
        self.next_boot_message = self._snapshot_placeholder(
            _('Loading next-boot modules…'),
            _('Applying the current MiniOS boot module rules.'))
        self.next_boot_stack.add_named(self.next_boot_message, 'message')

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.next_boot_list = Gtk.ListBox()
        self.next_boot_list.get_style_context().add_class('minios-list')
        self.next_boot_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.next_boot_list.set_activate_on_single_click(True)
        self.next_boot_list.connect('row-activated', self._on_module_row_activated)
        scrolled.add(self.next_boot_list)
        self.next_boot_stack.add_named(scrolled, 'list')
        box.pack_start(self.next_boot_stack, True, True, 0)
        return box

    def _build_module_details(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.detail_title = Gtk.Label(label=_('Module Details'), xalign=0)
        self.detail_title.get_style_context().add_class('page-title')
        controls.pack_start(self.detail_title, True, True, 0)
        self.detail_runtime = Gtk.Button()
        self.detail_runtime.set_no_show_all(True)
        self.detail_runtime.hide()
        self.detail_runtime.connect(
            'clicked', lambda _button: self._run_detail_action('runtime'))
        controls.pack_end(self.detail_runtime, False, False, 0)
        self.detail_next_boot = Gtk.Button()
        self.detail_next_boot.set_no_show_all(True)
        self.detail_next_boot.hide()
        self.detail_next_boot.connect(
            'clicked', lambda _button: self._run_detail_action('next-boot'))
        controls.pack_end(self.detail_next_boot, False, False, 0)
        self.detail_delete = Gtk.Button(label=_('Delete Module'))
        self.detail_delete.set_image(new_icon(
            ('edit-delete-symbolic', 'edit-delete'), Gtk.IconSize.BUTTON,
            accessible_name=_('Delete Module')))
        self.detail_delete.set_always_show_image(True)
        self.detail_delete.set_no_show_all(True)
        self.detail_delete.get_style_context().add_class('destructive-action')
        self.detail_delete.hide()
        self.detail_delete.connect(
            'clicked', lambda _button: self._run_detail_action('delete'))
        controls.pack_end(self.detail_delete, False, False, 0)
        self.detail_extract = Gtk.Button(label=_('Extract to Folder…'))
        self.detail_extract.set_image(new_icon('document-save-symbolic', accessible_name=_('Extract to Folder…')))
        self.detail_extract.get_style_context().add_class('minios-text-button')
        self.detail_extract.set_always_show_image(True)
        self.detail_extract.set_sensitive(False)
        self.detail_extract.connect('clicked', self._choose_extract_target)
        controls.pack_end(self.detail_extract, False, False, 0)
        outer.pack_start(controls, False, False, 0)

        self.detail_source = Gtk.Label(xalign=0)
        self.detail_source.set_selectable(True)
        self.detail_source.set_ellipsize(3)
        outer.pack_start(self.detail_source, False, False, 0)
        self.detail_meta = Gtk.Label(xalign=0)
        outer.pack_start(self.detail_meta, False, False, 0)

        self.detail_notice_revealer = Gtk.Revealer()
        self.detail_notice_revealer.set_transition_type(
            Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.detail_notice = StatusBanner('', intent='error')
        self.detail_notice_revealer.add(self.detail_notice)
        self.detail_notice_revealer.set_reveal_child(False)
        outer.pack_start(self.detail_notice_revealer, False, False, 0)

        search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.detail_search = Gtk.SearchEntry()
        self.detail_search.set_placeholder_text(_('Search module contents…'))
        self.detail_search.connect('search-changed', self._on_detail_search_changed)
        search_row.pack_start(self.detail_search, True, True, 0)
        outer.pack_start(search_row, False, False, 0)

        # Keep transient selection/action text below the search field so the
        # search position never moves as inspection state changes.
        self.detail_status = Gtk.Label(xalign=0)
        self.detail_status.set_line_wrap(True)
        self.detail_status.set_no_show_all(True)
        self.detail_status.hide()
        outer.pack_start(self.detail_status, False, False, 0)

        # icon, name, type, size, full path, is directory, target, mode
        self.detail_store = Gtk.TreeStore(str, str, str, str, str, bool, str, str)
        self.detail_tree = Gtk.TreeView(model=self.detail_store)
        self.detail_tree.set_search_column(1)
        self.detail_tree.set_headers_clickable(True)

        icon_renderer = Gtk.CellRendererPixbuf()
        name_renderer = Gtk.CellRendererText()
        name_column = Gtk.TreeViewColumn(_('Name'))
        name_column.pack_start(icon_renderer, False)
        name_column.add_attribute(icon_renderer, 'icon-name', 0)
        name_column.pack_start(name_renderer, True)
        name_column.add_attribute(name_renderer, 'text', 1)
        name_column.set_resizable(True)
        name_column.set_expand(True)
        self.detail_tree.append_column(name_column)

        type_renderer = Gtk.CellRendererText()
        type_column = Gtk.TreeViewColumn(_('Type'), type_renderer, text=2)
        type_column.set_resizable(True)
        self.detail_tree.append_column(type_column)

        size_renderer = Gtk.CellRendererText()
        size_renderer.set_property('xalign', 1.0)
        size_column = Gtk.TreeViewColumn(_('Size'), size_renderer, text=3)
        size_column.set_resizable(True)
        self.detail_tree.append_column(size_column)

        self.detail_tree.get_selection().connect('changed', self._on_detail_selection_changed)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.add(self.detail_tree)

        # Paint the rounded mask and frame after GtkTreeView without adding
        # overlay child widgets.  That preserves the visual stacking while all
        # pointer events continue to reach the table normally.
        viewer = ModuleFileViewer()
        viewer.add(scrolled)
        outer.pack_start(viewer, True, True, 0)
        self._detail_inspection = None
        return outer

    def _set_detail_status(self, text):
        text = str(text or '')
        self.detail_status.set_text(text)
        if text:
            self.detail_status.show()
        else:
            self.detail_status.hide()

    def _set_detail_notice(self, text=None, intent='error'):
        if not text:
            self.detail_notice_revealer.set_reveal_child(False)
            self.detail_notice.set_text('')
            return
        self.detail_notice.set_intent(intent)
        self.detail_notice.set_text(str(text))
        self.detail_notice_revealer.set_reveal_child(True)

    def _snapshot_placeholder(self, title, detail):
        return StatePlaceholder(title, detail)

    @staticmethod
    def _task_pair(outcome):
        if outcome.succeeded:
            return outcome.value
        return False, str(outcome.error or '')

    def _set_running_message(self, title, detail):
        current = self.running_stack.get_child_by_name('message')
        if current is not None:
            self.running_stack.remove(current)
        self.running_message = self._snapshot_placeholder(title, detail)
        self.running_stack.add_named(self.running_message, 'message')
        self.running_message.show_all()
        self.running_stack.set_visible_child_name('message')

    def refresh_running_snapshot(self):
        self._running_request += 1
        request = self._running_request
        self.running_status.set_text(_('Loading running module state…'))
        self._set_running_message(
            _('Loading running modules…'),
            _('Reading the authoritative module order from MiniOS Tools.'))
        BackgroundTask(
            lambda _token: load_running_snapshot(),
            lambda outcome: self._apply_running_snapshot_outcome(request, outcome),
            owner=self).start()

    def _apply_running_snapshot_outcome(self, request, outcome):
        if outcome.succeeded:
            return self._apply_running_snapshot(request, outcome.value)
        if request == self._running_request:
            self.running_status.set_text(_('Running module state unavailable'))
            self._set_running_message(
                _('Could not load running modules'), str(outcome.error))
        return False

    def _apply_running_snapshot(self, request, snapshot):
        if request != self._running_request:
            return False
        self._running_snapshot = snapshot
        for child in self.running_list.get_children():
            self.running_list.remove(child)

        if snapshot.state == LoadState.READY:
            for index, module in enumerate(
                    module_presentation_order(snapshot.modules)):
                self.running_list.add(self._running_module_row(index, module))
            self.running_list.show_all()
            self.running_status.set_text(
                _('{} · {} running modules').format(
                    snapshot.union_backend.upper(), len(snapshot.modules)))
            self.running_stack.set_visible_child_name('list')
        elif snapshot.state == LoadState.EMPTY:
            self.running_status.set_text(
                _('{} · no running modules').format(snapshot.union_backend.upper()))
            self._set_running_message(
                _('No running modules'),
                _('MiniOS Tools returned an authoritative empty module list.'))
        else:
            self.running_status.set_text(_('Running module state unavailable'))
            self._set_running_message(
                _('Could not load running modules'),
                snapshot.message or _('The running module state is unavailable.'))
        self._refresh_build_base_choices()
        if self._detail_module is not None:
            self._update_detail_actions()
        return False

    def _module_display(self, module):
        role_id, icons = classify_module(module.name)
        role = {
            'core': _('Core system'),
            'kernel': _('Kernel and drivers'),
            'firmware': _('Hardware firmware'),
            'gui-base': _('Graphical base'),
            'desktop': _('Desktop environment'),
            'toolbox': _('Toolbox utilities'),
            'ultra': _('Ultra applications'),
            'apps': _('Application bundle'),
            'browser': _('Web browser'),
            'custom': _('Custom module'),
        }[role_id]
        size = None
        if module.source:
            try:
                size = os.stat(module.source).st_size
            except OSError:
                pass
        return role, icons, format_bytes(size)

    def _module_row(self, index, module, scope, meta=None):
        row = Gtk.ListBoxRow()
        row.module = module
        row.scope = scope
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        order = Gtk.Label(label='' if index is None else str(index + 1))
        order.set_width_chars(2)
        order.set_xalign(1)
        order.get_style_context().add_class('row-meta')
        content.pack_start(order, False, False, 0)

        role, icons, size = self._module_display(module)
        icon = new_icon(icons, Gtk.IconSize.DND, fallback='package-x-generic')
        icon.set_valign(Gtk.Align.CENTER)
        content.pack_start(icon, False, False, 0)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        title = Gtk.Label(label=role, xalign=0)
        title.get_style_context().add_class('row-title')
        title.set_ellipsize(3)
        text.pack_start(title, False, False, 0)
        filename = Gtk.Label(label=module.name, xalign=0)
        filename.get_style_context().add_class('row-subtitle')
        filename.set_ellipsize(3)
        text.pack_start(filename, False, False, 0)
        content.pack_start(text, True, True, 0)

        if size or meta:
            trailing = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            trailing.set_halign(Gtk.Align.END)
            trailing.set_valign(Gtk.Align.CENTER)
            if size:
                size_label = Gtk.Label(label=size, xalign=1)
                size_label.get_style_context().add_class('row-meta')
                trailing.pack_start(size_label, False, False, 0)
            if meta:
                meta_label = Gtk.Label(label=meta, xalign=1)
                meta_label.get_style_context().add_class('row-meta')
                trailing.pack_start(meta_label, False, False, 0)
            content.pack_end(trailing, False, False, 0)

        tooltip = module.source or ''
        if scope == 'running' and module.mount:
            tooltip = '{}\n{}'.format(tooltip, module.mount) if tooltip else module.mount
        if tooltip:
            row.set_tooltip_text(tooltip)
        row.add(content)
        return row

    def _running_module_row(self, index, module):
        return self._module_row(index, module, 'running')

    def _set_next_boot_message(self, title, detail):
        current = self.next_boot_stack.get_child_by_name('message')
        if current is not None:
            self.next_boot_stack.remove(current)
        self.next_boot_message = self._snapshot_placeholder(title, detail)
        self.next_boot_stack.add_named(self.next_boot_message, 'message')
        self.next_boot_message.show_all()
        self.next_boot_stack.set_visible_child_name('message')

    def refresh_next_boot_snapshot(self):
        self._next_boot_request += 1
        request = self._next_boot_request
        self.next_boot_add.set_sensitive(False)
        self.next_boot_storage_notice.hide()
        self.next_boot_status.set_text(_('Loading next-boot module state…'))
        self._set_next_boot_message(
            _('Loading next-boot modules…'),
            _('Applying the current MiniOS boot module rules.'))
        BackgroundTask(
            lambda _token: load_next_boot_snapshot(),
            lambda outcome: self._apply_next_boot_snapshot_outcome(request, outcome),
            owner=self).start()

    def _apply_next_boot_snapshot_outcome(self, request, outcome):
        if outcome.succeeded:
            return self._apply_next_boot_snapshot(request, outcome.value)
        if request == self._next_boot_request:
            self.next_boot_status.set_text(_('Next-boot module state unavailable'))
            self._set_next_boot_message(
                _('Could not load next-boot modules'), str(outcome.error))
        return False

    def _apply_next_boot_snapshot(self, request, snapshot):
        if request != self._next_boot_request:
            return False
        self._next_boot_snapshot = snapshot
        add_available = snapshot.usable and snapshot.add_available
        self.next_boot_add.set_sensitive(add_available)
        self.next_boot_add.set_tooltip_text(
            None if add_available else
            _('Adding modules requires durable writable MiniOS module storage.'))
        if snapshot.usable and not snapshot.add_available:
            self.next_boot_storage_notice.show()
        else:
            self.next_boot_storage_notice.hide()
        for child in self.next_boot_list.get_children():
            self.next_boot_list.remove(child)

        if snapshot.state == LoadState.READY:
            for index, module in enumerate(
                    module_presentation_order(snapshot.modules)):
                self.next_boot_list.add(self._next_boot_module_row(index, module))
            for module in snapshot.disabled_modules:
                self.next_boot_list.add(self._next_boot_module_row(None, module))
            self.next_boot_list.show_all()
            self.next_boot_status.set_text(
                _('{} next-boot modules').format(len(snapshot.modules)))
            self.next_boot_status.set_tooltip_text(snapshot.data_root)
            self.next_boot_stack.set_visible_child_name('list')
        elif snapshot.state == LoadState.EMPTY:
            self.next_boot_status.set_text(_('No modules selected for next boot'))
            self.next_boot_status.set_tooltip_text(snapshot.data_root)
            self._set_next_boot_message(
                _('No modules selected for next boot'),
                _('MiniOS Tools returned an authoritative empty module list.'))
        else:
            self.next_boot_status.set_text(_('Next-boot module state unavailable'))
            self.next_boot_status.set_tooltip_text(None)
            self._set_next_boot_message(
                _('Could not load next-boot modules'),
                snapshot.message or _('The next-boot module state is unavailable.'))
        if self._detail_module is not None:
            self._update_detail_actions()
        return False

    def _next_boot_module_row(self, index, module):
        origin_names = {
            'base': _('Base system'),
            'modules': _('Modules'),
            'persistence': _('Persistence'),
            'disabled-modules': _('Excluded'),
            'disabled-persistence': _('Excluded'),
        }
        scope = ('next-boot-disabled'
                 if module.origin.startswith('disabled-') else 'next-boot')
        return self._module_row(
            index, module, scope,
            origin_names.get(module.origin, module.origin))

    def _choose_next_boot_module(self, _button):
        snapshot = self._next_boot_snapshot
        if snapshot is None or not snapshot.usable or not snapshot.add_available:
            return
        source = choose_open_file(
            self, _('Add Module to Next Boot'),
            filters=((_('MiniOS modules'),
                      ('*.{}'.format(snapshot.bundle_extension or 'sb'),)),),
            accept_label=_('Select'))
        if not source:
            return
        if not self._confirm_action(
                _('Add {} to Next Boot? The running system will not change.').format(
                    os.path.basename(source))):
            return
        self.next_boot_add.set_sensitive(False)
        self.next_boot_status.set_text(_('Adding module to Next Boot…'))
        BackgroundTask(
            lambda _token: add_to_next_boot(source),
            lambda outcome: self._apply_next_boot_add_result(
                *self._task_pair(outcome)), owner=self).start()

    def _apply_next_boot_add_result(self, success, message):
        if not success:
            self.next_boot_status.set_text(message or _('Could not add module to Next Boot.'))
            snapshot = self._next_boot_snapshot
            if snapshot is not None and snapshot.usable and snapshot.add_available:
                self.next_boot_add.set_sensitive(True)
            return False
        self.refresh_next_boot_snapshot()
        return False

    def _on_module_row_activated(self, listbox, row):
        listbox.unselect_all()
        module = getattr(row, 'module', None)
        if module is not None:
            self._open_module_details(module, getattr(row, 'scope', None))

    def _show_module_composition(self):
        self._inspection_request += 1
        self.module_pages.set_visible_child_name('composition')

    def _open_module_details(self, module, scope):
        self._inspection_request += 1
        request = self._inspection_request
        self._detail_module = module
        self._detail_scope = scope
        self.detail_store.clear()
        self._detail_inspection = None
        self.detail_search.set_text('')
        self.detail_title.set_text(module.name)
        self.detail_source.set_text(module.source or _('Backing source unavailable'))
        # GtkLabel is selectable so the path can still be copied, but opening
        # the details page must not present the path as a pre-selected value.
        self.detail_source.select_region(0, 0)
        # Use the metadata line itself as the loading indicator. It is
        # replaced by the size/count summary when inspection completes, which
        # keeps the search field at exactly the same vertical position.
        self.detail_meta.set_text(_('Reading module contents…'))
        self._set_detail_status('')
        self._set_detail_notice()
        self.detail_extract.set_sensitive(False)
        self.module_pages.set_visible_child_name('details')
        # Clear any focus GTK transferred to the first selectable label while
        # switching Stack pages. Manual mouse selection remains available.
        self.set_focus(None)
        self.detail_source.select_region(0, 0)
        self._update_detail_actions()

        if not module.source:
            self.detail_meta.set_text('')
            self._set_detail_notice(
                _('The backing module source is unavailable for inspection.'),
                intent='warning')
            return

        source = module.source
        allow_privileged = scope == 'runtime'
        BackgroundTask(
            lambda _token: load_module_inspection(
                source, allow_privileged=allow_privileged),
            lambda outcome: self._apply_inspection_outcome(request, outcome),
            owner=self).start()

    def _apply_inspection_outcome(self, request, outcome):
        if outcome.succeeded:
            return self._apply_module_inspection(request, outcome.value)
        if request == self._inspection_request:
            self.detail_meta.set_text('')
            self._set_detail_notice(str(outcome.error))
            self.detail_extract.set_sensitive(False)
        return False

    def _apply_module_inspection(self, request, inspection):
        if request != self._inspection_request:
            return False
        self.detail_store.clear()
        self._detail_inspection = inspection if inspection.state == LoadState.READY else None
        if inspection.state == LoadState.READY:
            self._populate_detail_tree()
            counts = {'directory': 0, 'file': 0, 'symlink': 0}
            for entry in inspection.entries:
                if entry.kind in counts:
                    counts[entry.kind] += 1
            summary = _('{size} · {entries} entries').format(
                size=format_bytes(inspection.size), entries=len(inspection.entries))
            if counts['directory'] or counts['file'] or counts['symlink']:
                summary += _(' · {directories} folders · {files} files · {links} links').format(
                    directories=counts['directory'], files=counts['file'], links=counts['symlink'])
            self.detail_meta.set_text(summary)
            self._set_detail_status('')
            self._set_detail_notice()
            self.detail_extract.set_sensitive(True)
        else:
            self.detail_meta.set_text('')
            self._set_detail_status('')
            self._set_detail_notice(
                inspection.message or _('Module inspection failed.'))
            self.detail_extract.set_sensitive(False)
        return False


    def _detail_kind_label(self, kind):
        return {
            'directory': _('Folder'),
            'file': _('File'),
            'symlink': _('Symbolic link'),
            'device': _('Device'),
            'fifo': _('FIFO'),
            'socket': _('Socket'),
            'unknown': _('Item'),
        }.get(kind, _('Item'))

    @staticmethod
    def _detail_kind_icon(kind):
        return {
            'directory': 'folder',
            'file': 'text-x-generic',
            'symlink': 'emblem-symbolic-link',
            'device': 'drive-harddisk',
            'fifo': 'text-x-generic',
            'socket': 'network-wired',
            'unknown': 'text-x-generic',
        }.get(kind, 'text-x-generic')

    def _populate_detail_tree(self):
        self.detail_store.clear()
        inspection = self._detail_inspection
        if inspection is None:
            return
        query = self.detail_search.get_text().strip().casefold()
        entries = list(inspection.entries)
        directory_paths = set()
        for item in entries:
            parts = item.path.split('/')
            for index in range(1, len(parts)):
                directory_paths.add('/'.join(parts[:index]))
        visible = None
        if query:
            visible = set()
            for entry in entries:
                if query in entry.path.casefold():
                    parts = entry.path.split('/')
                    for index in range(1, len(parts) + 1):
                        visible.add('/'.join(parts[:index]))

        iters = {}
        ordered = sorted(entries, key=lambda entry: (entry.path.count('/'), entry.path.casefold()))
        for entry in ordered:
            if visible is not None and entry.path not in visible:
                continue
            parent_path, _, name = entry.path.rpartition('/')
            parent = iters.get(parent_path) if parent_path else None
            kind = entry.kind
            if kind == 'unknown' and entry.path in directory_paths:
                kind = 'directory'
            size = format_bytes(entry.size) if entry.size is not None and kind != 'directory' else ''
            tree_iter = self.detail_store.append(parent, (
                self._detail_kind_icon(kind), name or entry.path,
                self._detail_kind_label(kind), size, entry.path,
                kind == 'directory', entry.target or '', entry.mode or ''))
            iters[entry.path] = tree_iter
        if query:
            self.detail_tree.expand_all()

    def _on_detail_search_changed(self, _entry):
        self._populate_detail_tree()

    def _on_detail_selection_changed(self, selection):
        model, tree_iter = selection.get_selected()
        if tree_iter is None:
            if self._detail_inspection is not None:
                self._set_detail_status('')
            return
        path = model.get_value(tree_iter, 4)
        target = model.get_value(tree_iter, 6)
        mode = model.get_value(tree_iter, 7)
        details = path
        if target:
            details += _(' → {}').format(target)
        if mode:
            details += _(' · {}').format(mode)
        self._set_detail_status(details)

    @staticmethod
    def _snapshot_module(snapshot, name):
        if snapshot is None or not snapshot.usable:
            return None
        for module in snapshot.modules:
            if module.name == name:
                return module
        return None

    def _set_detail_action(self, scope, action):
        if scope == 'runtime':
            self._detail_runtime_action = action
            button = self.detail_runtime
        else:
            self._detail_next_boot_action = action
            button = self.detail_next_boot
        if action is None:
            button.hide()
            return
        button.set_label(action[0])
        button.set_image(new_icon(
            action[5], Gtk.IconSize.BUTTON, accessible_name=action[0]))
        button.set_always_show_image(True)
        button.set_sensitive(True)
        button.show()

    def _update_detail_actions(self):
        self._set_detail_action('runtime', None)
        self._set_detail_action('next-boot', None)
        self._detail_delete_action = None
        self.detail_delete.hide()
        module = self._detail_module
        running = self._running_snapshot
        next_boot = self._next_boot_snapshot
        if module is None:
            return

        running_match = self._snapshot_module(running, module.name)
        next_match = self._snapshot_module(next_boot, module.name)
        aufs_available = (
            running is not None and running.usable and
            running.union_backend == 'aufs')
        if aufs_available:
            if running_match is None and module.source and module.origin != 'base':
                self._set_detail_action('runtime', (
                    _('Mount Module'),
                    _('Mount {} in the running MiniOS session? Next Boot will not change.').format(
                        module.name),
                    activate_for_session, module.source, False,
                    ('media-mount', 'drive-harddisk')))
            elif (running_match is not None and
                  next_boot is not None and next_boot.usable and
                  (next_match is None or next_match.origin != 'base')):
                self._set_detail_action('runtime', (
                    _('Unmount Module'),
                    _('Unmount {} from the running MiniOS session? Next Boot will not change.').format(
                        module.name),
                    deactivate_for_session, running_match.name, False,
                    ('media-eject', 'media-eject-symbolic')))

        if self._detail_scope in ('running', 'local'):
            if (next_boot is not None and next_boot.usable and
                    next_boot.add_available and next_match is None and
                    module.source):
                self._set_detail_action('next-boot', (
                    _('Install Module') if self._detail_scope == 'local' else
                    _('Add to Next Boot'),
                    _('Add {} to Next Boot? The running system will not change.').format(
                        module.name),
                    add_to_next_boot, module.source, False,
                    ('list-add-symbolic', 'list-add')))
        elif self._detail_scope == 'next-boot' and module.removable:
            self._set_detail_action('next-boot', (
                _('Remove from Next Boot'),
                _('Remove {} from Next Boot? Its file will be kept in the disabled module store so it can be restored later. The running system will not change.').format(
                    module.name),
                remove_from_next_boot, module.name, False,
                ('list-remove-symbolic', 'list-remove')))
        elif self._detail_scope == 'next-boot-disabled' and module.removable:
            self._set_detail_action('next-boot', (
                _('Include in Next Boot'),
                _('Include {} in Next Boot? The running system will not change.').format(
                    module.name),
                enable_for_next_boot, module.name, False,
                ('list-add-symbolic', 'list-add')))
            self._detail_delete_action = (
                _('Delete Module'),
                _('Permanently delete {}? This cannot be undone.').format(
                    module.name),
                delete_disabled_module, module.name, True,
                ('edit-delete-symbolic', 'edit-delete'))
            self.detail_delete.set_sensitive(True)
            self.detail_delete.show()

    def _confirm_action(self, text, confirm_label=None, destructive=False):
        return ask_confirmation(
            self, text, destructive=destructive,
            confirm_label=confirm_label or _('Continue'))

    def _run_detail_action(self, scope):
        if scope == 'runtime':
            action = self._detail_runtime_action
        elif scope == 'delete':
            action = self._detail_delete_action
        else:
            action = self._detail_next_boot_action
        module = self._detail_module
        if (action is None or module is None or
                not self._confirm_action(action[1], action[0], action[4])):
            return
        self.detail_runtime.set_sensitive(False)
        self.detail_next_boot.set_sensitive(False)
        self.detail_delete.set_sensitive(False)
        self._set_detail_notice()
        self._set_detail_status(_('Applying module change…'))
        module_name = module.name
        function, argument = action[2], action[3]
        BackgroundTask(
            lambda _token: function(argument),
            lambda outcome: self._apply_detail_action_result(
                module_name, scope, *self._task_pair(outcome)),
            owner=self).start()

    def _apply_detail_action_result(self, module_name, scope, success, message):
        module = self._detail_module
        if module is None or module.name != module_name:
            return False
        if not success:
            self._set_detail_status('')
            self._set_detail_notice(message or _('Module change failed.'))
            self._update_detail_actions()
            return False
        if scope == 'delete':
            self._detail_module = None
            self._show_module_composition()
            self.refresh_next_boot_snapshot()
            return False
        self._set_detail_notice()
        self._set_detail_status(_('Module state updated.'))
        if scope == 'runtime':
            self.refresh_running_snapshot()
        else:
            self.refresh_next_boot_snapshot()
        return False

    def _choose_extract_target(self, _button):
        module = self._detail_module
        if module is None or not module.source:
            return
        default_name = os.path.splitext(module.name)[0] or module.name
        target = choose_save_file(
            self, _('Extract Module to Folder'), current_name=default_name,
            accept_label=_('Extract'), overwrite_confirmation=False)
        if target:
            self._start_module_extraction(module.source, target)

    def _start_module_extraction(self, source, target):
        self.detail_extract.set_sensitive(False)
        self._set_detail_status('')
        self._set_detail_notice()
        self._extract_job = ModuleExtractionJob(
            self, source, target,
            lambda success, cancelled, message: self._apply_extraction_result(
                source, target, success, cancelled, message))
        self._extract_job.start()

    def _apply_extraction_result(self, source, target, success, cancelled, message):
        self._extract_job = None
        module = self._detail_module
        if module is None or module.source != source:
            return False
        self.detail_extract.set_sensitive(True)
        if success:
            self._set_detail_status(_('Extracted to {}').format(target))
        elif cancelled:
            self._set_detail_status(_('Extraction cancelled.'))
        else:
            self._set_detail_status('')
            show_error_dialog(
                self, _('Could not extract the module.'),
                message or _('The module operation failed.'))
        return False

    def _scrollable_configure_page(self, child):
        # Keep page navigation/help and the primary action fixed.  Only the
        # configure body scrolls when the window is too short.
        children = child.get_children()
        if len(children) < 4:
            return child

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        header = children[:2]
        body_children = children[2:-1]
        footer = children[-1]

        for widget in children:
            child.remove(widget)
        for widget in header:
            page.pack_start(widget, False, False, 0)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for widget in body_children:
            body.pack_start(widget, False, False, 0)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_shadow_type(Gtk.ShadowType.NONE)
        scrolled.set_propagate_natural_height(False)
        scrolled.set_hexpand(True)
        scrolled.set_vexpand(True)
        scrolled.add_with_viewport(body)
        viewport = body.get_parent()
        if isinstance(viewport, Gtk.Viewport):
            viewport.set_shadow_type(Gtk.ShadowType.NONE)
        page.pack_start(scrolled, True, True, 0)
        page.pack_end(footer, False, False, 0)
        return page

    def _build_create_workspace(self):
        self.create_pages = Gtk.Stack()
        self.create_pages.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.create_pages.set_hhomogeneous(False)
        self.create_pages.set_vhomogeneous(False)
        self.create_pages.add_named(self._build_create_methods(), 'methods')
        self.create_pages.add_named(self._scrollable_configure_page(
            self._build_package_configure()), 'package-configure')
        self.create_pages.add_named(self._build_package_review(), 'package-review')
        self.create_pages.add_named(self._build_package_run(), 'package-run')
        self.create_pages.add_named(self._build_package_result(), 'package-result')
        self.create_pages.add_named(self._scrollable_configure_page(
            self._build_script_configure()), 'script-configure')
        self.create_pages.add_named(self._build_script_review(), 'script-review')
        self.create_pages.add_named(self._build_script_run(), 'script-run')
        self.create_pages.add_named(self._build_script_result(), 'script-result')
        self.create_pages.add_named(self._scrollable_configure_page(
            self._build_chroot_configure()), 'chroot-configure')
        self.create_pages.add_named(self._build_chroot_review(), 'chroot-review')
        self.create_pages.add_named(self._build_chroot_run(), 'chroot-run')
        self.create_pages.add_named(self._build_chroot_result(), 'chroot-result')
        self.create_pages.add_named(self._scrollable_configure_page(
            self._build_folder_configure()), 'folder-configure')
        self.create_pages.add_named(self._build_folder_review(), 'folder-review')
        self.create_pages.add_named(self._build_folder_run(), 'folder-run')
        self.create_pages.add_named(self._build_folder_result(), 'folder-result')
        self.create_pages.add_named(self._scrollable_configure_page(
            self._build_session_configure()), 'session-configure')
        self.create_pages.add_named(self._build_session_review(), 'session-review')
        self.create_pages.add_named(self._build_session_run(), 'session-run')
        self.create_pages.add_named(self._build_session_result(), 'session-result')
        return self.create_pages

    def _build_create_methods(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        title = Gtk.Label(label=_('Create a Module'), xalign=0)
        title.get_style_context().add_class('page-title')
        outer.pack_start(title, False, False, 0)
        subtitle = Gtk.Label(
            label=_('Choose what the new immutable module should contain.'), xalign=0)
        subtitle.set_line_wrap(True)
        subtitle.get_style_context().add_class('page-subtitle')
        outer.pack_start(subtitle, False, False, 0)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        methods = Gtk.ListBox()
        methods.get_style_context().add_class('minios-list')
        methods.set_selection_mode(Gtk.SelectionMode.SINGLE)
        methods.set_activate_on_single_click(True)
        methods.connect('row-activated', self._on_create_method_activated)
        for method_id, label, description in CREATE_METHODS:
            row = Gtk.ListBoxRow()
            row.method_name = method_id
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            row_title = Gtk.Label(label=label, xalign=0)
            row_title.get_style_context().add_class('row-title')
            content.pack_start(row_title, False, False, 0)
            detail = Gtk.Label(label=description, xalign=0)
            detail.get_style_context().add_class('row-subtitle')
            detail.set_line_wrap(True)
            content.pack_start(detail, False, False, 0)
            row.add(content)
            methods.add(row)
        scrolled.add(methods)
        outer.pack_start(scrolled, True, True, 0)
        return outer


    def _on_create_method_activated(self, listbox, row):
        listbox.unselect_all()
        method = getattr(row, 'method_name', None)
        if method == 'packages':
            self._refresh_build_base_choices()
            self.package_config_status.set_text('')
            self.create_pages.set_visible_child_name('package-configure')
            self._refresh_package_index_notice()
        elif method == 'script':
            self._refresh_build_base_choices()
            self.script_config_status.set_text('')
            self.create_pages.set_visible_child_name('script-configure')
        elif method == 'chroot':
            self._refresh_build_base_choices()
            self.chroot_config_status.set_text('')
            self.create_pages.set_visible_child_name('chroot-configure')
        elif method == 'folder':
            self.folder_config_status.set_text('')
            self.create_pages.set_visible_child_name('folder-configure')
        elif method == 'session':
            self.session_config_status.set_text('')
            self.create_pages.set_visible_child_name('session-configure')

    def _build_folder_configure(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self._create_method_header(
            outer, 'folder', _('Folder'),
            lambda _b: self.create_pages.set_visible_child_name('methods'))

        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        grid.get_style_context().add_class('content-card')
        self.folder_source = Gtk.Entry()
        self.folder_source.set_hexpand(True)
        self.folder_target = Gtk.Entry()
        self.folder_target.set_hexpand(True)
        grid.attach(Gtk.Label(label=_('Source folder'), xalign=0), 0, 0, 1, 1)
        grid.attach(self.folder_source, 1, 0, 1, 1)
        choose_source = Gtk.Button(label=_('Choose…'))
        choose_source.connect('clicked', self._choose_folder_source)
        grid.attach(choose_source, 2, 0, 1, 1)
        grid.attach(Gtk.Label(label=_('Output module'), xalign=0), 0, 1, 1, 1)
        grid.attach(self.folder_target, 1, 1, 1, 1)
        choose_target = Gtk.Button(label=_('Choose…'))
        choose_target.connect('clicked', self._choose_folder_target)
        grid.attach(choose_target, 2, 1, 1, 1)

        self.folder_compression = Gtk.ComboBoxText()
        self.folder_compression.set_hexpand(True)
        for item in ('zstd', 'xz', 'gzip', 'lzo', 'lz4'):
            self.folder_compression.append_text(item)
        self.folder_compression.set_active(0)
        grid.attach(self._field_label_with_help(
            _('Compression'), self._compression_help_button()), 0, 2, 1, 1)
        grid.attach(self.folder_compression, 1, 2, 2, 1)
        outer.pack_start(grid, False, False, 0)

        self.folder_config_status = Gtk.Label(xalign=0)
        self.folder_config_status.get_style_context().add_class('inline-error')
        self.folder_config_status.set_line_wrap(True)
        outer.pack_start(self.folder_config_status, False, False, 0)
        review = Gtk.Button(label=_('Review'))
        review.get_style_context().add_class('suggested-action')
        review.set_halign(Gtk.Align.END)
        review.connect('clicked', self._review_folder_creation)
        outer.pack_end(review, False, False, 0)
        return outer

    def _current_bundle_extension(self):
        snapshot = self._next_boot_snapshot
        if snapshot is not None and snapshot.usable and snapshot.bundle_extension:
            return snapshot.bundle_extension
        return 'sb'

    def _choose_output_path(self, entry, default_name):
        current = entry.get_text().strip()
        current_folder = None
        current_name = default_name
        if current:
            directory = os.path.dirname(current)
            if os.path.isdir(directory):
                current_folder = directory
            current_name = os.path.basename(current)
        return choose_save_file(
            self, _('Choose Output Module'), current_folder=current_folder,
            current_name=current_name, accept_label=_('Select'),
            overwrite_confirmation=False)

    def _choose_folder_source(self, _button):
        source = choose_folder(
            self, _('Choose Source Folder'), accept_label=_('Select'))
        if not source:
            return
        self.folder_source.set_text(source)
        if not self.folder_target.get_text().strip():
            name = os.path.basename(source.rstrip(os.sep)) or 'module'
            self.folder_target.set_text(
                os.path.join(os.path.dirname(source),
                             '{}.{}'.format(name, self._current_bundle_extension())))

    def _choose_folder_target(self, _button):
        target = self._choose_output_path(
            self.folder_target,
            'module.{}'.format(self._current_bundle_extension()))
        if target:
            self.folder_target.set_text(target)

    def _folder_configuration(self):
        source_text = self.folder_source.get_text().strip()
        target_text = self.folder_target.get_text().strip()
        if not source_text or not target_text:
            return None, _('Choose both a source folder and an output module.')
        source = os.path.abspath(os.path.expanduser(source_text))
        target = os.path.abspath(os.path.expanduser(target_text))
        compression = self.folder_compression.get_active_text() or 'zstd'
        if not os.path.isdir(source) or not os.access(source, os.R_OK | os.X_OK):
            return None, _('Source folder is not readable.')
        if os.path.lexists(target):
            return None, _('Output module already exists.')
        parent = os.path.dirname(target)
        if not os.path.isdir(parent) or not os.access(parent, os.W_OK | os.X_OK):
            return None, _('Output directory is not writable.')
        return (source, target, compression), ''

    def _build_folder_review(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.pack_start(self._page_title_label(_('Review Folder Module')), True, True, 0)
        outer.pack_start(controls, False, False, 0)
        card, values = self._review_card((
            ('source', _('Source folder')),
            ('target', _('Output module')),
            ('compression', _('Compression')),
            ('privileges', _('Privileges')),
        ))
        self.folder_review_values = values
        outer.pack_start(card, False, False, 0)
        effect = StatusBanner(
            _('The source folder is not modified. A new module is created '
              'rootlessly; an existing output is never replaced.'),
            intent='info')
        outer.pack_start(effect, False, False, 0)
        run = Gtk.Button(label=_('Create Module'))
        run.get_style_context().add_class('suggested-action')
        run.set_halign(Gtk.Align.END)
        run.connect('clicked', self._start_folder_creation)
        outer.pack_end(run, False, False, 0)
        return outer

    def _review_folder_creation(self, _button):
        configuration, error = self._folder_configuration()
        if error:
            self.folder_config_status.set_text(error)
            return
        self._folder_config = configuration
        source, target, compression = configuration
        self.folder_config_status.set_text('')
        self._set_review_values(self.folder_review_values, (
            ('source', source),
            ('target', target),
            ('compression', compression),
            ('privileges', _('None')),
        ))
        self.create_pages.set_visible_child_name('folder-review')

    def _build_folder_run(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        title = Gtk.Label(label=_('Creating Module'), xalign=0)
        title.get_style_context().add_class('page-title')
        outer.pack_start(title, False, False, 0)
        (progress, self.folder_run_status, self.folder_spinner,
         self.folder_run_log) = self._build_run_output(_('Preparing…'))
        outer.pack_start(progress, False, False, 0)
        outer.pack_start(self.folder_run_log, True, True, 0)
        return outer

    def _build_folder_result(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.folder_result_title = Gtk.Label(label=_('Module Result'), xalign=0)
        self.folder_result_title.get_style_context().add_class('page-title')
        outer.pack_start(self.folder_result_title, False, False, 0)
        (self.folder_result_output, self.folder_result_text,
         self.folder_result_log) = self._build_result_output()
        outer.pack_start(self.folder_result_output, True, True, 0)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.folder_result_back = Gtk.Button(label=_('Back to Folder'))
        self.folder_result_back.connect(
            'clicked', lambda _b: self.create_pages.set_visible_child_name('folder-configure'))
        controls.pack_start(self.folder_result_back, False, False, 0)
        outer.pack_end(controls, False, False, 0)
        return outer

    def _start_folder_creation(self, _button):
        configuration, error = self._folder_configuration()
        if error:
            self.folder_config_status.set_text(error)
            self.create_pages.set_visible_child_name('folder-configure')
            return
        if getattr(self, '_folder_config', None) != configuration:
            self._folder_config = configuration
        self.folder_run_status.set_text(_('Preparing…'))
        self.folder_run_log.clear()
        self.folder_spinner.start()
        self.create_pages.set_visible_child_name('folder-run')
        thread = threading.Thread(
            target=self._folder_creation_worker, args=configuration)
        thread.daemon = True
        thread.start()

    def _folder_creation_worker(self, source, target, compression):
        def phase_callback(phase):
            GLib.idle_add(self._apply_folder_phase, phase)
        def log_callback(text):
            GLib.idle_add(self.folder_run_log.feed, text)
        success, result = create_module_from_folder(
            source, target, compression, phase_callback, log_callback)
        GLib.idle_add(self._apply_folder_result, success, result)

    def _apply_folder_phase(self, phase):
        labels = {
            'prepare': _('Preparing source…'),
            'compress': _('Compressing folder…'),
            'verify': _('Verifying module…'),
            'publish': _('Publishing module…'),
            'complete': _('Finishing…'),
        }
        text = labels.get(phase, phase)
        self.folder_run_status.set_text(text)
        self.folder_run_log.feed(text + '\n')
        return False

    def _apply_folder_result(self, success, result):
        self.folder_spinner.stop()
        if success:
            self.folder_result_title.set_text(_('Module Created'))
            self._set_result_details(
                self.folder_result_output, self.folder_result_log, (
                    (_('Output module'), result['output']),
                    (_('Size'), self._result_size(result['size'])),
                    (_('Compression'), result['compression']),
                    (_('SHA-256'), result['sha256']),
                ), build_log=self.folder_run_log.get_text())
            self.folder_result_back.set_label(_('Create Another'))
        else:
            self.folder_result_title.set_text(_('Module Creation Failed'))
            self._set_result_output(
                self.folder_result_output, self.folder_result_text,
                self.folder_result_log, result, diagnostic=True,
                build_log=self.folder_run_log.get_text())
            self.folder_result_back.set_label(_('Back to Folder'))
        self.create_pages.set_visible_child_name('folder-result')
        return False


    def _build_package_configure(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self._create_method_header(
            outer, 'packages', _('Packages'),
            lambda _b: self.create_pages.set_visible_child_name('methods'))

        self.package_index_revealer = Gtk.Revealer()
        self.package_index_revealer.set_transition_type(
            Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.package_index_notice = StatusBanner('', intent='warning')
        self.package_index_action = Gtk.Stack()
        self.package_index_update = Gtk.Button(label=_('Update Package Lists'))
        self.package_index_update.get_style_context().add_class('suggested-action')
        self.package_index_update.connect('clicked', self._update_package_indexes)
        self.package_index_spinner = Gtk.Spinner()
        self.package_index_spinner.set_halign(Gtk.Align.CENTER)
        self.package_index_action.add_named(self.package_index_update, 'update')
        self.package_index_action.add_named(self.package_index_spinner, 'progress')
        self.package_index_action.set_visible_child_name('update')
        self.package_index_notice.pack_end(self.package_index_action, False, False, 0)
        self.package_index_revealer.add(self.package_index_notice)
        self.package_index_revealer.set_reveal_child(False)
        outer.pack_start(self.package_index_revealer, False, False, 0)

        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        grid.get_style_context().add_class('content-card')
        self.package_names = Gtk.Entry()
        self.package_names.set_hexpand(True)
        self.package_names.set_placeholder_text('curl git htop')
        self.package_completion = TokenCompletionPopover(
            self.package_names, provider=query_package_names,
            min_chars=2, max_results=12, append_text=' ')
        grid.attach(Gtk.Label(label=_('Repository packages'), xalign=0), 0, 0, 1, 1)
        grid.attach(self.package_names, 1, 0, 2, 1)

        self._package_local_files = []
        self.package_local_label = Gtk.Label(label=_('No local .deb files selected'), xalign=0)
        self.package_local_label.set_line_wrap(True)
        grid.attach(Gtk.Label(label=_('Local packages'), xalign=0), 0, 1, 1, 1)
        grid.attach(self.package_local_label, 1, 1, 1, 1)
        local_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        add_deb = Gtk.Button(label=_('Add .deb…'))
        add_deb.connect('clicked', self._choose_package_debs)
        local_controls.pack_start(add_deb, False, False, 0)
        clear_deb = Gtk.Button(label=_('Clear'))
        clear_deb.connect('clicked', self._clear_package_debs)
        local_controls.pack_start(clear_deb, False, False, 0)
        grid.attach(local_controls, 2, 1, 1, 1)
        self.package_target = Gtk.Entry()
        self.package_target.set_hexpand(True)
        grid.attach(Gtk.Label(label=_('Output module'), xalign=0), 0, 2, 1, 1)
        grid.attach(self.package_target, 1, 2, 1, 1)
        choose_target = Gtk.Button(label=_('Choose…'))
        choose_target.connect('clicked', self._choose_package_target)
        grid.attach(choose_target, 2, 2, 1, 1)

        self.package_compression = Gtk.ComboBoxText()
        self.package_compression.set_hexpand(True)
        for item in ('zstd', 'lz4', 'xz', 'gzip', 'lzo'):
            self.package_compression.append_text(item)
        self.package_compression.set_active(0)
        grid.attach(self._field_label_with_help(
            _('Compression'), self._compression_help_button()), 0, 3, 1, 1)
        grid.attach(self.package_compression, 1, 3, 2, 1)

        self.package_level = self._new_build_base_combo()
        grid.attach(self._field_label_with_help(
            _('Build base level'), self._build_base_help_button()), 0, 4, 1, 1)
        grid.attach(self.package_level, 1, 4, 2, 1)

        recommends_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.package_recommends = Gtk.CheckButton(
            label=_('Allow installation of recommended packages'))
        self.package_recommends.set_active(False)
        recommends_box.pack_start(self.package_recommends, True, True, 0)
        recommends_box.pack_end(
            self._recommended_packages_help_button(), False, False, 0)
        grid.attach(recommends_box, 1, 5, 2, 1)

        suggests_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.package_suggests = Gtk.CheckButton(
            label=_('Allow installation of suggested packages'))
        self.package_suggests.set_active(False)
        suggests_box.pack_start(self.package_suggests, True, True, 0)
        suggests_box.pack_end(
            self._suggested_packages_help_button(), False, False, 0)
        grid.attach(suggests_box, 1, 6, 2, 1)
        outer.pack_start(grid, False, False, 0)

        self.package_config_status = Gtk.Label(xalign=0)
        self.package_config_status.get_style_context().add_class('inline-error')
        self.package_config_status.set_line_wrap(True)
        outer.pack_start(self.package_config_status, False, False, 0)
        self.package_review = Gtk.Button(label=_('Review'))
        self.package_review.get_style_context().add_class('suggested-action')
        self.package_review.set_halign(Gtk.Align.END)
        self.package_review.connect('clicked', self._review_package_creation)
        outer.pack_end(self.package_review, False, False, 0)
        return outer

    def _refresh_package_index_notice(self):
        if self._package_indexes_busy:
            return
        self.package_index_notice.set_tooltip_text(None)
        if package_indexes_available():
            self.package_index_revealer.set_reveal_child(False)
            return
        self.package_index_notice.set_intent('warning')
        self.package_index_notice.set_text(
            _('Package lists have not been downloaded. Update them to enable '
              'autocomplete for repository package names.'))
        self.package_index_action.set_visible_child_name('update')
        self.package_index_revealer.set_reveal_child(True)

    def _update_package_indexes(self, _button):
        if self._package_indexes_busy:
            return
        self._package_indexes_busy = True
        self.package_review.set_sensitive(False)
        self.package_index_notice.set_intent('info')
        self.package_index_notice.set_text(_('Updating package lists…'))
        self.package_index_notice.set_tooltip_text(None)
        self.package_index_action.set_visible_child_name('progress')
        self.package_index_spinner.start()
        BackgroundTask(
            lambda _token: update_package_indexes(),
            lambda outcome: self._apply_package_index_update(
                *self._task_pair(outcome)), owner=self).start()

    def _apply_package_index_update(self, success, message):
        self._package_indexes_busy = False
        self.package_index_spinner.stop()
        self.package_review.set_sensitive(True)
        self.package_index_action.set_visible_child_name('update')
        if success:
            self._refresh_package_index_notice()
            self.package_names.emit('changed')
        else:
            self.package_index_notice.set_intent('error')
            self.package_index_notice.set_text(
                _('Could not update package lists. Check the network connection '
                  'and try again.'))
            self.package_index_notice.set_tooltip_text(message or None)
            self.package_index_revealer.set_reveal_child(True)
        return False

    def _choose_package_debs(self, _button):
        files = choose_open_files(
            self, _('Choose Local Debian Packages'),
            filters=((_('Debian packages'), ('*.deb',)),),
            accept_label=_('Add'))
        if not files:
            return
        for path in files:
            if path not in self._package_local_files:
                self._package_local_files.append(path)
        self._refresh_package_local_label()
        self._set_package_target_from_local()

    def _clear_package_debs(self, _button):
        self._package_local_files = []
        self._refresh_package_local_label()

    def _refresh_package_local_label(self):
        if self._package_local_files:
            self.package_local_label.set_text('\n'.join(self._package_local_files))
        else:
            self.package_local_label.set_text(_('No local .deb files selected'))

    def _choose_package_target(self, _button):
        target = self._choose_output_path(
            self.package_target,
            'packages.{}'.format(self._current_bundle_extension()))
        if target:
            self.package_target.set_text(target)

    def _package_configuration(self):
        names = tuple(self.package_names.get_text().split())
        local_files = tuple(self._package_local_files)
        if not names and not local_files:
            return None, _('Choose at least one repository package or local .deb file.')
        if any('/' in name for name in names):
            return None, _('Use Add .deb… for local package paths.')
        for path in local_files:
            if not os.path.isfile(path) or not os.access(path, os.R_OK):
                return None, _('A selected local .deb file is no longer readable.')
        target_text = self.package_target.get_text().strip()
        if not target_text:
            return None, _('Choose an output module.')
        target = os.path.abspath(os.path.expanduser(target_text))
        if os.path.lexists(target):
            return None, _('Output module already exists.')
        parent = os.path.dirname(target)
        if not os.path.isdir(parent) or not os.access(parent, os.W_OK | os.X_OK):
            return None, _('Output directory is not writable.')
        compression = self.package_compression.get_active_text() or 'zstd'
        recommends = self.package_recommends.get_active()
        suggests = self.package_suggests.get_active()
        level, error = self._build_level_value(self.package_level)
        if error:
            return None, error
        return (names, local_files, target, compression, recommends, suggests, level), ''
    def _build_package_review(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.pack_start(self._page_title_label(_('Review Package Module')), True, True, 0)
        outer.pack_start(controls, False, False, 0)

        card, values = self._review_card((
            ('packages', _('Repository packages')),
            ('local', _('Local packages')),
            ('target', _('Output module')),
            ('compression', _('Compression')),
            ('base', _('Build base')),
            ('recommends', _('Recommended packages')),
            ('suggests', _('Suggested packages')),
            ('privileges', _('Privileges')),
        ))
        self.package_review_values = values
        outer.pack_start(card, False, False, 0)

        effect = StatusBanner(
            _('APT and package maintainer scripts run as root inside a temporary '
              'MiniOS build union. The running system and Next Boot are not changed.'),
            intent='warning')
        outer.pack_start(effect, False, False, 0)
        run = Gtk.Button(label=_('Create Module'))
        run.get_style_context().add_class('suggested-action')
        run.set_halign(Gtk.Align.END)
        run.connect('clicked', self._start_package_creation)
        outer.pack_end(run, False, False, 0)
        return outer
    def _review_package_creation(self, _button):
        configuration, error = self._package_configuration()
        if error:
            self.package_config_status.set_text(error)
            return
        self._package_config = configuration
        names, local_files, target, compression, recommends, suggests, level = configuration
        self.package_config_status.set_text('')
        self._set_review_values(self.package_review_values, (
            ('packages', ' '.join(names) if names else _('None')),
            ('local', ', '.join(local_files) if local_files else _('None')),
            ('target', target),
            ('compression', compression),
            ('base', self._build_level_text(self.package_level, level)),
            ('recommends', _('Yes') if recommends else _('No')),
            ('suggests', _('Yes') if suggests else _('No')),
            ('privileges', _('Administrator authentication required')),
        ))
        self.create_pages.set_visible_child_name('package-review')

    def _build_package_run(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        title = Gtk.Label(label=_('Creating Package Module'), xalign=0)
        title.get_style_context().add_class('page-title')
        outer.pack_start(title, False, False, 0)
        (progress, self.package_run_status, self.package_spinner,
         self.package_run_log) = self._build_run_output(_('Preparing…'))
        outer.pack_start(progress, False, False, 0)
        outer.pack_start(self.package_run_log, True, True, 0)
        return outer

    def _build_package_result(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.package_result_title = Gtk.Label(label=_('Module Result'), xalign=0)
        self.package_result_title.get_style_context().add_class('page-title')
        outer.pack_start(self.package_result_title, False, False, 0)
        (self.package_result_output, self.package_result_text,
         self.package_result_log) = self._build_result_output()
        outer.pack_start(self.package_result_output, True, True, 0)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.package_result_back = Gtk.Button(label=_('Back to Packages'))
        self.package_result_back.connect(
            'clicked', lambda _b: self.create_pages.set_visible_child_name('package-configure'))
        controls.pack_start(self.package_result_back, False, False, 0)
        outer.pack_end(controls, False, False, 0)
        return outer

    def _start_package_creation(self, _button):
        configuration, error = self._package_configuration()
        if error:
            self.package_config_status.set_text(error)
            self.create_pages.set_visible_child_name('package-configure')
            return
        self._package_config = configuration
        self.package_run_status.set_text(_('Preparing…'))
        self.package_run_log.clear()
        self.package_spinner.start()
        self.create_pages.set_visible_child_name('package-run')
        thread = threading.Thread(
            target=self._package_creation_worker, args=configuration)
        thread.daemon = True
        thread.start()

    def _package_creation_worker(self, names, local_files, target,
                                 compression, recommends, suggests, level):
        def phase_callback(phase):
            GLib.idle_add(self._apply_package_phase, phase)
        def log_callback(text):
            GLib.idle_add(self.package_run_log.feed, text)
        success, result = create_module_from_packages(
            names, local_files, target, compression, recommends,
            phase_callback, log_callback, level=level,
            install_suggests=suggests)
        GLib.idle_add(self._apply_package_result, success, result)

    def _apply_package_phase(self, phase):
        labels = {
            'prepare': _('Preparing build environment…'),
            'update': _('Updating package indexes…'),
            'packages': _('Installing packages…'),
            'capture': _('Capturing module…'),
            'complete': _('Finishing…'),
        }
        text = labels.get(phase, phase)
        self.package_run_status.set_text(text)
        self.package_run_log.feed(text + '\n')
        return False
    def _apply_package_result(self, success, result):
        self.package_spinner.stop()
        if success:
            self.package_result_title.set_text(_('Module Created'))
            self._set_result_details(
                self.package_result_output, self.package_result_log, (
                    (_('Output module'), result['output']),
                    (_('Compressed size'), self._result_size(result['compressed_size'])),
                    (_('Uncompressed size'), self._result_size(result['uncompressed_size'])),
                    (_('Entries'), result['entry_count']),
                    (_('Compression'), result['compression']),
                    (_('SHA-256'), result['sha256']),
                ), build_log=self.package_run_log.get_text())
            self.package_result_back.set_label(_('Create Another'))
        else:
            self.package_result_title.set_text(_('Module Creation Failed'))
            self._set_result_output(
                self.package_result_output, self.package_result_text,
                self.package_result_log, result, diagnostic=True,
                build_log=self.package_run_log.get_text())
            self.package_result_back.set_label(_('Back to Packages'))
        self.create_pages.set_visible_child_name('package-result')
        return False


    def _build_script_configure(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self._create_method_header(
            outer, 'script', _('Installation Script'),
            lambda _b: self.create_pages.set_visible_child_name('methods'))

        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        grid.get_style_context().add_class('content-card')
        self.script_source = Gtk.Entry()
        self.script_source.set_hexpand(True)
        grid.attach(Gtk.Label(label=_('Installation script'), xalign=0), 0, 0, 1, 1)
        grid.attach(self.script_source, 1, 0, 1, 1)
        choose_script = Gtk.Button(label=_('Choose…'))
        choose_script.connect('clicked', self._choose_script_source)
        grid.attach(choose_script, 2, 0, 1, 1)

        self.script_seed = Gtk.Entry()
        self.script_seed.set_hexpand(True)
        self.script_seed.set_placeholder_text(_('Optional'))
        grid.attach(Gtk.Label(label=_('Seed folder'), xalign=0), 0, 1, 1, 1)
        grid.attach(self.script_seed, 1, 1, 1, 1)
        seed_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        choose_seed = Gtk.Button(label=_('Choose…'))
        choose_seed.connect('clicked', self._choose_script_seed)
        seed_controls.pack_start(choose_seed, False, False, 0)
        clear_seed = Gtk.Button(label=_('Clear'))
        clear_seed.connect('clicked', lambda _b: self.script_seed.set_text(''))
        seed_controls.pack_start(clear_seed, False, False, 0)
        grid.attach(seed_controls, 2, 1, 1, 1)
        self.script_target = Gtk.Entry()
        self.script_target.set_hexpand(True)
        grid.attach(Gtk.Label(label=_('Output module'), xalign=0), 0, 2, 1, 1)
        grid.attach(self.script_target, 1, 2, 1, 1)
        choose_target = Gtk.Button(label=_('Choose…'))
        choose_target.connect('clicked', self._choose_script_target)
        grid.attach(choose_target, 2, 2, 1, 1)

        self.script_compression = Gtk.ComboBoxText()
        self.script_compression.set_hexpand(True)
        for item in ('zstd', 'lz4', 'xz', 'gzip', 'lzo'):
            self.script_compression.append_text(item)
        self.script_compression.set_active(0)
        grid.attach(self._field_label_with_help(
            _('Compression'), self._compression_help_button()), 0, 3, 1, 1)
        grid.attach(self.script_compression, 1, 3, 2, 1)
        self.script_level = self._new_build_base_combo()
        grid.attach(self._field_label_with_help(
            _('Build base level'), self._build_base_help_button()), 0, 4, 1, 1)
        grid.attach(self.script_level, 1, 4, 2, 1)
        outer.pack_start(grid, False, False, 0)

        self.script_config_status = Gtk.Label(xalign=0)
        self.script_config_status.get_style_context().add_class('inline-error')
        self.script_config_status.set_line_wrap(True)
        outer.pack_start(self.script_config_status, False, False, 0)
        review = Gtk.Button(label=_('Review'))
        review.get_style_context().add_class('suggested-action')
        review.set_halign(Gtk.Align.END)
        review.connect('clicked', self._review_script_creation)
        outer.pack_end(review, False, False, 0)
        return outer

    def _choose_script_source(self, _button):
        source = choose_open_file(
            self, _('Choose Installation Script'), accept_label=_('Select'))
        if not source:
            return
        self.script_source.set_text(source)
        if not self.script_target.get_text().strip():
            base = os.path.splitext(os.path.basename(source))[0] or 'module'
            self.script_target.set_text(os.path.join(
                os.path.dirname(source),
                '{}.{}'.format(base, self._current_bundle_extension())))

    def _choose_script_seed(self, _button):
        seed = choose_folder(
            self, _('Choose Seed Folder'), accept_label=_('Select'))
        if seed:
            self.script_seed.set_text(seed)

    def _choose_script_target(self, _button):
        target = self._choose_output_path(
            self.script_target,
            'script-module.{}'.format(self._current_bundle_extension()))
        if target:
            self.script_target.set_text(target)

    def _script_configuration(self):
        script_text = self.script_source.get_text().strip()
        if not script_text:
            return None, _('Choose an installation script.')
        script = os.path.abspath(os.path.expanduser(script_text))
        if not os.path.isfile(script) or not os.access(script, os.R_OK):
            return None, _('Installation script is not readable.')
        seed_text = self.script_seed.get_text().strip()
        seed = None
        if seed_text:
            seed = os.path.abspath(os.path.expanduser(seed_text))
            if not os.path.isdir(seed) or not os.access(seed, os.R_OK | os.X_OK):
                return None, _('Seed folder is not readable.')
        target_text = self.script_target.get_text().strip()
        if not target_text:
            return None, _('Choose an output module.')
        target = os.path.abspath(os.path.expanduser(target_text))
        if os.path.lexists(target):
            return None, _('Output module already exists.')
        parent = os.path.dirname(target)
        if not os.path.isdir(parent) or not os.access(parent, os.W_OK | os.X_OK):
            return None, _('Output directory is not writable.')
        compression = self.script_compression.get_active_text() or 'zstd'
        level, error = self._build_level_value(self.script_level)
        if error:
            return None, error
        return (script, target, compression, seed, level), ''

    def _build_script_review(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.pack_start(
            self._page_title_label(_('Review Installation Script')), True, True, 0)
        outer.pack_start(controls, False, False, 0)

        card, values = self._review_card((
            ('script', _('Installation script')),
            ('seed', _('Seed folder')),
            ('target', _('Output module')),
            ('compression', _('Compression')),
            ('base', _('Build base')),
            ('privileges', _('Privileges')),
        ))
        self.script_review_values = values
        outer.pack_start(card, False, False, 0)

        effect = StatusBanner(
            _('The installation script runs as root inside a temporary MiniOS '
              'build union and may execute arbitrary code. The running system '
              'and Next Boot are not changed.'), intent='warning')
        outer.pack_start(effect, False, False, 0)
        run = Gtk.Button(label=_('Run Script and Create Module'))
        run.get_style_context().add_class('suggested-action')
        run.set_halign(Gtk.Align.END)
        run.connect('clicked', self._start_script_creation)
        outer.pack_end(run, False, False, 0)
        return outer

    def _review_script_creation(self, _button):
        configuration, error = self._script_configuration()
        if error:
            self.script_config_status.set_text(error)
            return
        self._script_config = configuration
        script, target, compression, seed, level = configuration
        self.script_config_status.set_text('')
        self._set_review_values(self.script_review_values, (
            ('script', script),
            ('seed', seed if seed else _('None')),
            ('target', target),
            ('compression', compression),
            ('base', self._build_level_text(self.script_level, level)),
            ('privileges', _('Administrator authentication required')),
        ))
        self.create_pages.set_visible_child_name('script-review')

    def _build_script_run(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        title = Gtk.Label(label=_('Running Installation Script'), xalign=0)
        title.get_style_context().add_class('page-title')
        outer.pack_start(title, False, False, 0)
        (progress, self.script_run_status, self.script_spinner,
         self.script_run_log) = self._build_run_output(_('Preparing…'))
        outer.pack_start(progress, False, False, 0)
        outer.pack_start(self.script_run_log, True, True, 0)
        return outer

    def _build_script_result(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.script_result_title = Gtk.Label(label=_('Module Result'), xalign=0)
        self.script_result_title.get_style_context().add_class('page-title')
        outer.pack_start(self.script_result_title, False, False, 0)
        (self.script_result_output, self.script_result_text,
         self.script_result_log) = self._build_result_output()
        outer.pack_start(self.script_result_output, True, True, 0)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.script_result_back = Gtk.Button(label=_('Back to Script'))
        self.script_result_back.connect(
            'clicked', lambda _b: self.create_pages.set_visible_child_name('script-configure'))
        controls.pack_start(self.script_result_back, False, False, 0)
        outer.pack_end(controls, False, False, 0)
        return outer

    def _start_script_creation(self, _button):
        configuration, error = self._script_configuration()
        if error:
            self.script_config_status.set_text(error)
            self.create_pages.set_visible_child_name('script-configure')
            return
        self._script_config = configuration
        self.script_run_status.set_text(_('Preparing…'))
        self.script_run_log.clear()
        self.script_spinner.start()
        self.create_pages.set_visible_child_name('script-run')
        thread = threading.Thread(
            target=self._script_creation_worker, args=configuration)
        thread.daemon = True
        thread.start()

    def _script_creation_worker(self, script, target, compression, seed, level):
        def phase_callback(phase):
            GLib.idle_add(self._apply_script_phase, phase)
        def log_callback(text):
            GLib.idle_add(self.script_run_log.feed, text)
        success, result = create_module_from_script(
            script, target, compression, seed, phase_callback, log_callback,
            level=level)
        GLib.idle_add(self._apply_script_result, success, result)

    def _apply_script_phase(self, phase):
        labels = {
            'prepare': _('Preparing build environment…'),
            'seed': _('Copying seed folder…'),
            'script': _('Running installation script…'),
            'capture': _('Capturing module…'),
            'complete': _('Finishing…'),
        }
        text = labels.get(phase, phase)
        self.script_run_status.set_text(text)
        self.script_run_log.feed(text + '\n')
        return False
    def _apply_script_result(self, success, result):
        self.script_spinner.stop()
        if success:
            self.script_result_title.set_text(_('Module Created'))
            self._set_result_details(
                self.script_result_output, self.script_result_log, (
                    (_('Output module'), result['output']),
                    (_('Compressed size'), self._result_size(result['compressed_size'])),
                    (_('Uncompressed size'), self._result_size(result['uncompressed_size'])),
                    (_('Entries'), result['entry_count']),
                    (_('Compression'), result['compression']),
                    (_('Seed folder'), _('Yes') if result.get('seed_directory') else _('No')),
                    (_('SHA-256'), result['sha256']),
                ), build_log=self.script_run_log.get_text())
            self.script_result_back.set_label(_('Create Another'))
        else:
            self.script_result_title.set_text(_('Module Creation Failed'))
            self._set_result_output(
                self.script_result_output, self.script_result_text,
                self.script_result_log, result, diagnostic=True,
                build_log=self.script_run_log.get_text())
            self.script_result_back.set_label(_('Back to Script'))
        self.create_pages.set_visible_child_name('script-result')
        return False


    def _build_chroot_configure(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self._create_method_header(
            outer, 'chroot', _('Interactive Chroot'),
            lambda _b: self.create_pages.set_visible_child_name('methods'))

        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        grid.get_style_context().add_class('content-card')
        self.chroot_seed = Gtk.Entry()
        self.chroot_seed.set_hexpand(True)
        grid.attach(Gtk.Label(label=_('Seed folder (optional)'), xalign=0), 0, 0, 1, 1)
        grid.attach(self.chroot_seed, 1, 0, 1, 1)
        choose_seed = Gtk.Button(label=_('Choose…'))
        choose_seed.connect('clicked', self._choose_chroot_seed)
        grid.attach(choose_seed, 2, 0, 1, 1)

        self.chroot_target = Gtk.Entry()
        self.chroot_target.set_hexpand(True)
        grid.attach(Gtk.Label(label=_('Output module'), xalign=0), 0, 1, 1, 1)
        grid.attach(self.chroot_target, 1, 1, 1, 1)
        choose_target = Gtk.Button(label=_('Choose…'))
        choose_target.connect('clicked', self._choose_chroot_target)
        grid.attach(choose_target, 2, 1, 1, 1)
        self.chroot_compression = Gtk.ComboBoxText()
        self.chroot_compression.set_hexpand(True)
        for item in ('zstd', 'lz4', 'xz', 'gzip', 'lzo'):
            self.chroot_compression.append_text(item)
        self.chroot_compression.set_active(0)
        grid.attach(self._field_label_with_help(
            _('Compression'), self._compression_help_button()), 0, 2, 1, 1)
        grid.attach(self.chroot_compression, 1, 2, 2, 1)
        self.chroot_level = self._new_build_base_combo()
        grid.attach(self._field_label_with_help(
            _('Build base level'), self._build_base_help_button()), 0, 3, 1, 1)
        grid.attach(self.chroot_level, 1, 3, 2, 1)
        outer.pack_start(grid, False, False, 0)

        self.chroot_config_status = Gtk.Label(xalign=0)
        self.chroot_config_status.get_style_context().add_class('inline-error')
        self.chroot_config_status.set_line_wrap(True)
        outer.pack_start(self.chroot_config_status, False, False, 0)
        review = Gtk.Button(label=_('Review'))
        review.get_style_context().add_class('suggested-action')
        review.set_halign(Gtk.Align.END)
        review.connect('clicked', self._review_chroot_creation)
        outer.pack_end(review, False, False, 0)
        return outer

    def _choose_chroot_seed(self, _button):
        seed = choose_folder(
            self, _('Choose Seed Folder'), accept_label=_('Select'))
        if seed:
            self.chroot_seed.set_text(seed)
            if not self.chroot_target.get_text().strip():
                self.chroot_target.set_text(os.path.join(
                    os.path.dirname(seed),
                    'chroot.{}'.format(self._current_bundle_extension())))

    def _choose_chroot_target(self, _button):
        target = self._choose_output_path(
            self.chroot_target,
            'chroot.{}'.format(self._current_bundle_extension()))
        if target:
            self.chroot_target.set_text(target)

    def _chroot_configuration(self):
        seed_text = self.chroot_seed.get_text().strip()
        seed = os.path.abspath(os.path.expanduser(seed_text)) if seed_text else None
        if seed and (not os.path.isdir(seed) or not os.access(seed, os.R_OK | os.X_OK)):
            return None, _('Seed folder is not readable.')
        target_text = self.chroot_target.get_text().strip()
        if not target_text:
            return None, _('Choose an output module.')
        target = os.path.abspath(os.path.expanduser(target_text))
        if os.path.lexists(target):
            return None, _('Output module already exists.')
        parent = os.path.dirname(target)
        if not os.path.isdir(parent) or not os.access(parent, os.W_OK | os.X_OK):
            return None, _('Output directory is not writable.')
        compression = self.chroot_compression.get_active_text() or 'zstd'
        level, error = self._build_level_value(self.chroot_level)
        if error:
            return None, error
        return (seed, target, compression, level), ''

    def _build_chroot_review(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.pack_start(self._page_title_label(_('Review Interactive Chroot')), True, True, 0)
        outer.pack_start(controls, False, False, 0)

        card, values = self._review_card((
            ('seed', _('Seed folder')),
            ('target', _('Output module')),
            ('compression', _('Compression')),
            ('base', _('Build base')),
            ('privileges', _('Privileges')),
        ))
        self.chroot_review_values = values
        outer.pack_start(card, False, False, 0)
        effect = StatusBanner(
            _('A root shell will run inside a temporary MiniOS build union. '
              'Exit the shell when finished; you can then create the module '
              'or discard all chroot changes.'), intent='warning')
        outer.pack_start(effect, False, False, 0)
        run = Gtk.Button(label=_('Open Interactive Chroot'))
        run.get_style_context().add_class('suggested-action')
        run.set_halign(Gtk.Align.END)
        run.connect('clicked', self._start_chroot_session)
        outer.pack_end(run, False, False, 0)
        return outer

    def _review_chroot_creation(self, _button):
        configuration, error = self._chroot_configuration()
        if error:
            self.chroot_config_status.set_text(error)
            return
        self._chroot_config = configuration
        seed, target, compression, level = configuration
        self.chroot_config_status.set_text('')
        self._set_review_values(self.chroot_review_values, (
            ('seed', seed if seed else _('None')),
            ('target', target),
            ('compression', compression),
            ('base', self._build_level_text(self.chroot_level, level)),
            ('privileges', _('Administrator authentication required')),
        ))
        self.create_pages.set_visible_child_name('chroot-review')

    def _build_chroot_run(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.chroot_run_status = Gtk.Label(label=_('Preparing…'), xalign=0)
        self.chroot_run_status.set_line_wrap(True)
        outer.pack_start(self.chroot_run_status, False, False, 0)

        self.chroot_shell_hint = Gtk.Label(
            label=_('Make your changes, then type exit to leave the chroot shell.'),
            xalign=0)
        self.chroot_shell_hint.set_line_wrap(True)
        self.chroot_shell_hint.get_style_context().add_class('page-subtitle')
        outer.pack_start(self.chroot_shell_hint, False, False, 0)

        self.chroot_terminal = Vte.Terminal()
        self.chroot_terminal.set_font(Pango.FontDescription('Monospace 10'))
        self.chroot_terminal.set_scrollback_lines(10000)
        self.chroot_terminal.set_hexpand(True)
        self.chroot_terminal.set_vexpand(True)
        self.chroot_terminal.get_style_context().add_class('terminal-frame')
        self.chroot_terminal.connect('child-exited', self._on_chroot_child_exited)
        outer.pack_start(self.chroot_terminal, True, True, 0)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.chroot_reopen = Gtk.Button(label=_('Reopen Shell'))
        self.chroot_reopen.set_no_show_all(True)
        self.chroot_reopen.hide()
        self.chroot_reopen.connect('clicked', lambda _b: self._spawn_chroot_shell())
        controls.pack_start(self.chroot_reopen, False, False, 0)
        self.chroot_discard = Gtk.Button(label=_('Discard Changes'))
        self.chroot_discard.get_style_context().add_class('destructive-action')
        self.chroot_discard.set_no_show_all(True)
        self.chroot_discard.hide()
        self.chroot_discard.connect('clicked', self._discard_chroot_session)
        controls.pack_start(self.chroot_discard, False, False, 0)
        self.chroot_finish = Gtk.Button(label=_('Create Module'))
        self.chroot_finish.set_no_show_all(True)
        self.chroot_finish.hide()
        self.chroot_finish.connect('clicked', self._finish_chroot_session)
        controls.pack_end(self.chroot_finish, False, False, 0)
        outer.pack_end(controls, False, False, 0)
        return outer

    def _build_chroot_result(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.chroot_result_title = Gtk.Label(label=_('Interactive Chroot Result'), xalign=0)
        self.chroot_result_title.get_style_context().add_class('page-title')
        outer.pack_start(self.chroot_result_title, False, False, 0)
        (self.chroot_result_output, self.chroot_result_text,
         self.chroot_result_log) = self._build_result_output()
        outer.pack_start(self.chroot_result_output, True, True, 0)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        again = Gtk.Button(label=_('Create Another'))
        again.connect(
            'clicked', lambda _b: self.create_pages.set_visible_child_name('chroot-configure'))
        controls.pack_start(again, False, False, 0)
        outer.pack_end(controls, False, False, 0)
        return outer

    def _start_chroot_session(self, _button):
        configuration, error = self._chroot_configuration()
        if error:
            self.chroot_config_status.set_text(error)
            self.create_pages.set_visible_child_name('chroot-configure')
            return
        self._chroot_config = configuration
        self._chroot_session_id = None
        self._chroot_shell_running = False
        self._chroot_busy = True
        self._chroot_preparing = True
        self.chroot_run_status.set_text(_('Preparing interactive chroot…'))
        self.chroot_reopen.hide()
        self.chroot_finish.hide()
        self.chroot_discard.hide()
        self.chroot_terminal.reset(True, True)
        self.create_pages.set_visible_child_name('chroot-run')
        thread = threading.Thread(
            target=self._prepare_chroot_worker, args=configuration)
        thread.daemon = True
        thread.start()

    def _prepare_chroot_worker(self, seed, target, compression, level):
        def phase_callback(phase):
            GLib.idle_add(self._apply_chroot_prepare_phase, phase)
        success, result = prepare_chroot_session(
            seed, target, compression, phase_callback, level=level)
        GLib.idle_add(self._apply_chroot_prepare_result, success, result)

    def _apply_chroot_prepare_phase(self, phase):
        labels = {
            'prepare': _('Preparing protected chroot session…'),
            'seed': _('Copying seed folder…'),
        }
        self.chroot_run_status.set_text(labels.get(phase, phase))
        return False

    def _apply_chroot_prepare_result(self, success, result):
        self._chroot_busy = False
        self._chroot_preparing = False
        if not success:
            self.chroot_result_title.set_text(_('Chroot Preparation Failed'))
            self._set_result_output(
                self.chroot_result_output, self.chroot_result_text,
                self.chroot_result_log, result, diagnostic=True)
            self.create_pages.set_visible_child_name('chroot-result')
            return False
        self._chroot_session_id = result['session_id']
        self.chroot_discard.show()
        self.chroot_discard.set_sensitive(True)
        self._spawn_chroot_shell()
        return False

    def _spawn_chroot_shell(self):
        if not self._chroot_session_id or self._chroot_shell_running:
            return
        success, argv = chroot_shell_argv(self._chroot_session_id)
        if not success:
            self.chroot_run_status.set_text(str(argv))
            self.chroot_reopen.show()
            self.chroot_finish.show()
            self.chroot_discard.show()
            return
        self.chroot_reopen.hide()
        self.chroot_finish.hide()
        self.chroot_discard.set_sensitive(False)
        self._chroot_shell_running = True
        self.chroot_run_status.set_text(_('Opening privileged chroot shell…'))
        try:
            spawned, _pid = self.chroot_terminal.spawn_sync(
                Vte.PtyFlags.DEFAULT, None, argv, None,
                GLib.SpawnFlags.DEFAULT, None, None, None)
            if not spawned:
                raise RuntimeError(_('VTE could not start the chroot shell.'))
        except (GLib.Error, TypeError, RuntimeError) as error:
            self._chroot_shell_running = False
            self.chroot_run_status.set_text(
                _('Could not open chroot shell: {}').format(error))
            self.chroot_reopen.show()
            self.chroot_finish.show()
            self.chroot_discard.show()
            self.chroot_discard.set_sensitive(True)
            return
        self.chroot_run_status.set_text(
            _('Interactive chroot is active. Type exit when finished.'))
        self.chroot_terminal.grab_focus()

    def _on_chroot_child_exited(self, _terminal, status):
        self._chroot_shell_running = False
        if os.WIFEXITED(status):
            detail = _('exit status {}').format(os.WEXITSTATUS(status))
        elif os.WIFSIGNALED(status):
            detail = _('signal {}').format(os.WTERMSIG(status))
        else:
            detail = _('status {}').format(status)
        self.chroot_run_status.set_text(
            _('Chroot shell closed ({}). Create the module, reopen the shell, '
            'or discard the session.').format(detail))
        if self._chroot_session_id:
            self.chroot_reopen.show()
            self.chroot_finish.show()
            self.chroot_discard.show()
            self.chroot_reopen.set_sensitive(True)
            self.chroot_finish.set_sensitive(True)
            self.chroot_discard.set_sensitive(True)

    def _chroot_build_log(self):
        try:
            text, _attributes = self.chroot_terminal.get_text(None, None)
        except (GLib.Error, TypeError):
            return ''
        return str(text or '').rstrip()

    def _finish_chroot_session(self, _button):
        if not self._chroot_session_id or self._chroot_shell_running:
            return
        self.chroot_reopen.set_sensitive(False)
        self.chroot_finish.set_sensitive(False)
        self.chroot_discard.set_sensitive(False)
        self._chroot_busy = True
        self.chroot_run_status.set_text(_('Capturing chroot changes…'))
        session_id = self._chroot_session_id
        seed, _target, compression, _level = self._chroot_config
        thread = threading.Thread(
            target=self._finish_chroot_worker,
            args=(session_id, compression, bool(seed)))
        thread.daemon = True
        thread.start()

    def _finish_chroot_worker(self, session_id, compression, seed_directory):
        def phase_callback(phase):
            GLib.idle_add(self._apply_chroot_finish_phase, phase)
        success, result = finish_chroot_session(
            session_id, compression, seed_directory, phase_callback)
        GLib.idle_add(
            self._apply_chroot_finish_result, session_id, success, result)

    def _apply_chroot_finish_phase(self, phase):
        labels = {
            'capture': _('Capturing chroot changes…'),
            'complete': _('Cleaning chroot session…'),
        }
        self.chroot_run_status.set_text(labels.get(phase, phase))
        return False

    def _apply_chroot_finish_result(self, session_id, success, result):
        self._chroot_busy = False
        if self._chroot_session_id != session_id:
            return False
        if not success:
            self.chroot_run_status.set_text(str(result))
            self.chroot_finish.set_sensitive(True)
            self.chroot_discard.set_sensitive(True)
            return False
        self._chroot_session_id = None
        self.chroot_result_title.set_text(_('Module Created'))
        self._set_result_details(
            self.chroot_result_output, self.chroot_result_log, (
                (_('Output module'), result['output']),
                (_('Compressed size'), self._result_size(result['compressed_size'])),
                (_('Uncompressed size'), self._result_size(result['uncompressed_size'])),
                (_('Entries'), result['entry_count']),
                (_('Compression'), result['compression']),
                (_('SHA-256'), result['sha256']),
            ), build_log=self._chroot_build_log())
        self.create_pages.set_visible_child_name('chroot-result')
        return False

    def _discard_chroot_session(self, _button=None):
        if not self._chroot_session_id or self._chroot_shell_running:
            return
        self.chroot_reopen.set_sensitive(False)
        self.chroot_finish.set_sensitive(False)
        self.chroot_discard.set_sensitive(False)
        self._chroot_busy = True
        self.chroot_run_status.set_text(_('Discarding chroot session…'))
        session_id = self._chroot_session_id
        thread = threading.Thread(
            target=self._discard_chroot_worker, args=(session_id,))
        thread.daemon = True
        thread.start()

    def _discard_chroot_worker(self, session_id):
        success, result = cancel_chroot_session(session_id)
        GLib.idle_add(
            self._apply_chroot_discard_result, session_id, success, result)

    def _apply_chroot_discard_result(self, session_id, success, result):
        self._chroot_busy = False
        if self._chroot_session_id != session_id:
            return False
        if not success:
            self.chroot_run_status.set_text(str(result))
            self.chroot_finish.set_sensitive(True)
            self.chroot_discard.set_sensitive(True)
            return False
        self._chroot_session_id = None
        if self._close_after_chroot_cancel:
            self._close_after_chroot_cancel = False
            self.destroy()
            return False
        self.chroot_result_title.set_text(_('Session Discarded'))
        self._set_result_output(
            self.chroot_result_output, self.chroot_result_text,
            self.chroot_result_log,
            _('Interactive chroot changes were discarded. No module was created.'))
        self.create_pages.set_visible_child_name('chroot-result')
        return False

    def _on_window_delete(self, _window, _event):
        if self._session_capture_busy:
            show_info_dialog(
                self, _('Current session capture is still running.'),
                _('Cancel the capture or wait for it to finish before closing MiniOS Module Manager.'))
            return True
        if self._chroot_preparing or self._chroot_busy:
            show_info_dialog(
                self, _('A chroot operation is still in progress.'),
                _('Wait for it to finish before closing MiniOS Module Manager.'))
            return True
        if self._chroot_shell_running:
            show_info_dialog(
                self, _('Exit the interactive chroot before closing.'),
                _('Type exit in the embedded terminal, then create the module or discard the session.'))
            return True
        if not self._chroot_session_id:
            return False
        if not ask_confirmation(
                self, _('Discard the prepared chroot session and close?'),
                _('No module will be created from the current interactive chroot changes.'),
                destructive=True, confirm_label=_('Discard Changes')):
            return True
        self._close_after_chroot_cancel = True
        self._discard_chroot_session()
        return True


    def _build_session_configure(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self._create_method_header(
            outer, 'session', _('Current Session Changes'),
            lambda _b: self.create_pages.set_visible_child_name('methods'))

        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        grid.get_style_context().add_class('content-card')
        self.session_target = Gtk.Entry()
        self.session_target.set_hexpand(True)
        grid.attach(Gtk.Label(label=_('Output module'), xalign=0), 0, 0, 1, 1)
        grid.attach(self.session_target, 1, 0, 1, 1)
        choose_target = Gtk.Button(label=_('Choose…'))
        choose_target.connect('clicked', self._choose_session_target)
        grid.attach(choose_target, 2, 0, 1, 1)

        self.session_compression = Gtk.ComboBoxText()
        self.session_compression.set_hexpand(True)
        for item in ('zstd', 'lz4', 'xz', 'gzip', 'lzo'):
            self.session_compression.append_text(item)
        self.session_compression.set_active(0)
        grid.attach(self._field_label_with_help(
            _('Compression'), self._compression_help_button()), 0, 1, 1, 1)
        grid.attach(self.session_compression, 1, 1, 2, 1)
        outer.pack_start(grid, False, False, 0)

        self.session_config_status = Gtk.Label(xalign=0)
        self.session_config_status.get_style_context().add_class('inline-error')
        self.session_config_status.set_line_wrap(True)
        outer.pack_start(self.session_config_status, False, False, 0)
        review = Gtk.Button(label=_('Review'))
        review.get_style_context().add_class('suggested-action')
        review.set_halign(Gtk.Align.END)
        review.connect('clicked', self._review_session_capture)
        outer.pack_end(review, False, False, 0)
        return outer

    def _choose_session_target(self, _button):
        target = self._choose_output_path(
            self.session_target,
            'session-changes.{}'.format(self._current_bundle_extension()))
        if target:
            self.session_target.set_text(target)

    def _session_capture_configuration(self):
        target_text = self.session_target.get_text().strip()
        if not target_text:
            return None, _('Choose an output module.')
        target = os.path.abspath(os.path.expanduser(target_text))
        if os.path.lexists(target):
            return None, _('Output module already exists.')
        parent = os.path.dirname(target)
        if not os.path.isdir(parent) or not os.access(parent, os.W_OK | os.X_OK):
            return None, _('Output directory is not writable.')
        compression = self.session_compression.get_active_text() or 'zstd'
        return (target, compression), ''

    def _build_session_review(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.pack_start(
            self._page_title_label(_('Review Current Session Changes')),
            True, True, 0)
        outer.pack_start(controls, False, False, 0)

        card, values = self._review_card((
            ('target', _('Output module')),
            ('compression', _('Compression')),
            ('policy', _('Capture policy')),
            ('privileges', _('Privileges')),
        ))
        self.session_review_values = values
        outer.pack_start(card, False, False, 0)
        effect = StatusBanner(
            _('MiniOS will capture the authoritative current writable layer '
              'using its standard savechanges policy. Runtime paths, logs, '
              'caches and other predefined noise are excluded. The running '
              'system and Next Boot are not changed.'), intent='info')
        outer.pack_start(effect, False, False, 0)
        run = Gtk.Button(label=_('Capture Changes'))
        run.get_style_context().add_class('suggested-action')
        run.set_halign(Gtk.Align.END)
        run.connect('clicked', self._start_session_capture)
        outer.pack_end(run, False, False, 0)
        return outer

    def _review_session_capture(self, _button):
        configuration, error = self._session_capture_configuration()
        if error:
            self.session_config_status.set_text(error)
            return
        self._session_capture_config = configuration
        target, compression = configuration
        self.session_config_status.set_text('')
        self._set_review_values(self.session_review_values, (
            ('target', target),
            ('compression', compression),
            ('policy', _('Standard MiniOS savechanges')),
            ('privileges', _('Administrator authentication required')),
        ))
        self.create_pages.set_visible_child_name('session-review')

    def _build_session_run(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        title = Gtk.Label(label=_('Capturing Current Session Changes'), xalign=0)
        title.get_style_context().add_class('page-title')
        outer.pack_start(title, False, False, 0)
        (progress, self.session_run_status, self.session_spinner,
         self.session_run_log) = self._build_run_output(_('Preparing…'))
        outer.pack_start(progress, False, False, 0)
        outer.pack_start(self.session_run_log, True, True, 0)
        self.session_cancel = Gtk.Button(label=_('Cancel Capture'))
        self.session_cancel.set_halign(Gtk.Align.END)
        self.session_cancel.connect('clicked', self._cancel_session_capture)
        outer.pack_end(self.session_cancel, False, False, 0)
        return outer

    def _build_session_result(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.session_result_title = Gtk.Label(label=_('Session Capture Result'), xalign=0)
        self.session_result_title.get_style_context().add_class('page-title')
        outer.pack_start(self.session_result_title, False, False, 0)
        (self.session_result_output, self.session_result_text,
         self.session_result_log) = self._build_result_output()
        outer.pack_start(self.session_result_output, True, True, 0)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        again = Gtk.Button(label=_('Capture Again'))
        again.connect(
            'clicked', lambda _b: self.create_pages.set_visible_child_name('session-configure'))
        controls.pack_start(again, False, False, 0)
        outer.pack_end(controls, False, False, 0)
        return outer

    def _start_session_capture(self, _button):
        configuration, error = self._session_capture_configuration()
        if error:
            self.session_config_status.set_text(error)
            self.create_pages.set_visible_child_name('session-configure')
            return
        self._session_capture_config = configuration
        self._session_capture_cancel = new_capture_cancel_marker()
        self._session_capture_busy = True
        self.session_run_status.set_text(_('Preparing current session capture…'))
        self.session_run_log.clear()
        self.session_cancel.set_sensitive(True)
        self.session_spinner.start()
        self.create_pages.set_visible_child_name('session-run')
        target, compression = configuration
        context = self._session_capture_cancel
        thread = threading.Thread(
            target=self._session_capture_worker,
            args=(target, compression, context))
        thread.daemon = True
        thread.start()

    def _session_capture_worker(self, target, compression, context):
        def phase_callback(phase):
            GLib.idle_add(self._apply_session_capture_phase, phase)
        def log_callback(text):
            GLib.idle_add(self.session_run_log.feed, text)
        success, result = capture_current_session(
            target, compression, phase_callback, context, log_callback)
        GLib.idle_add(
            self._apply_session_capture_result, context, success, result)

    def _apply_session_capture_phase(self, phase):
        labels = {
            'prepare': _('Preparing capture…'),
            'inventory': _('Inspecting current changes…'),
            'capture': _('Copying current changes…'),
            'compress': _('Compressing module…'),
            'verify': _('Verifying module…'),
            'publish': _('Publishing module…'),
            'complete': _('Finishing…'),
        }
        text = labels.get(phase, phase)
        self.session_run_status.set_text(text)
        self.session_run_log.feed(text + '\n')
        return False

    def _cancel_session_capture(self, _button):
        context = self._session_capture_cancel
        if not self._session_capture_busy or context is None:
            return
        try:
            request_capture_cancel(context[1])
        except FileExistsError:
            pass
        except OSError as error:
            self.session_run_status.set_text(
                _('Could not request cancellation: {}').format(error))
            return
        self.session_cancel.set_sensitive(False)
        self.session_run_status.set_text(_('Cancelling capture…'))

    def _apply_session_capture_result(self, context, success, result):
        if self._session_capture_cancel != context:
            return False
        self._session_capture_busy = False
        self._session_capture_cancel = None
        self.session_spinner.stop()
        self.session_cancel.set_sensitive(False)
        if success:
            self.session_result_title.set_text(_('Module Created'))
            self._set_result_details(
                self.session_result_output, self.session_result_log, (
                    (_('Output module'), result['output']),
                    (_('Compressed size'), self._result_size(result['compressed_size'])),
                    (_('Uncompressed size'), self._result_size(result['uncompressed_size'])),
                    (_('Entries'), result['entry_count']),
                    (_('SHA-256'), result['sha256']),
                ), build_log=self.session_run_log.get_text())
        elif result is CAPTURE_CANCELLED:
            self.session_result_title.set_text(_('Capture Cancelled'))
            self._set_result_output(
                self.session_result_output, self.session_result_text,
                self.session_result_log,
                _('Current session capture was cancelled. No completed module was reported.'),
                build_log=self.session_run_log.get_text())
        else:
            self.session_result_title.set_text(_('Session Capture Failed'))
            self._set_result_output(
                self.session_result_output, self.session_result_text,
                self.session_result_log, result, diagnostic=True,
                build_log=self.session_run_log.get_text())
        self.create_pages.set_visible_child_name('session-result')
        return False


    def open_local_module(self, path):
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(path):
            show_error_dialog(
                self, _('The selected module file is unavailable.'), path)
            return
        module = ModuleRecord(
            name=os.path.basename(path), source=path)
        self.workspace_notebook.set_current_page(0)
        self._open_module_details(module, 'local')

    def _setup_drag_and_drop(self):
        targets = [Gtk.TargetEntry.new('text/uri-list', 0, 0)]
        self.drag_dest_set(
            Gtk.DestDefaults.ALL, targets, Gdk.DragAction.COPY)
        self.connect('drag-data-received', self._on_drag_data_received)

    def _drop_paths(self, selection):
        paths = []
        for uri in selection.get_uris() or ():
            path = Gio.File.new_for_uri(uri).get_path()
            if path:
                paths.append(os.path.abspath(path))
        return paths

    def _on_drag_data_received(self, _widget, context, _x, _y,
                               selection, _info, timestamp):
        paths = self._drop_paths(selection)
        success = self._route_dropped_paths(paths)
        context.finish(success, False, timestamp)

    def _route_dropped_paths(self, paths):
        if not paths:
            return False
        if len(paths) == 1 and os.path.isdir(paths[0]):
            source = paths[0]
            self.workspace_notebook.set_current_page(1)
            self.create_pages.set_visible_child_name('folder-configure')
            self.folder_source.set_text(source)
            if not self.folder_target.get_text().strip():
                name = os.path.basename(source.rstrip(os.sep)) or 'module'
                self.folder_target.set_text(os.path.join(
                    os.path.dirname(source),
                    '{}.{}'.format(name, self._current_bundle_extension())))
            return True

        if all(os.path.isfile(path) and path.lower().endswith('.deb')
               for path in paths):
            self.workspace_notebook.set_current_page(1)
            self.create_pages.set_visible_child_name('package-configure')
            for path in paths:
                if path not in self._package_local_files:
                    self._package_local_files.append(path)
            self._refresh_package_local_label()
            self._set_package_target_from_local()
            return True

        if len(paths) == 1 and os.path.isfile(paths[0]):
            path = paths[0]
            extension = '.{}'.format(self._current_bundle_extension()).lower()
            if path.lower().endswith(('.sb', extension)):
                self.open_local_module(path)
                return True
            self.workspace_notebook.set_current_page(1)
            self.create_pages.set_visible_child_name('script-configure')
            self.script_source.set_text(path)
            if not self.script_target.get_text().strip():
                base = os.path.splitext(os.path.basename(path))[0] or 'module'
                self.script_target.set_text(os.path.join(
                    os.path.dirname(path),
                    '{}.{}'.format(base, self._current_bundle_extension())))
            return True

        show_info_dialog(
            self, _('These dropped items cannot be opened together.'),
            _('Drop one module, script, or folder, or one or more local .deb files.'))
        return False

    def _set_package_target_from_local(self):
        if not self._package_local_files or self.package_target.get_text().strip():
            return
        first = self._package_local_files[0]
        base = os.path.basename(first)
        if base.lower().endswith('.deb'):
            base = base[:-4]
        self.package_target.set_text(os.path.join(
            os.path.dirname(first),
            '{}.{}'.format(base or 'packages', self._current_bundle_extension())))
