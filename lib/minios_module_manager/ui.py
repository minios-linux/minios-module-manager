"""GTK 3 interface for MiniOS Module Manager."""

import os
import threading

import gi

gi.require_version('Gtk', '3.0')
gi.require_version('Vte', '2.91')
from gi.repository import Gdk, Gio, GLib, Gtk, Pango, Vte

from minios_gui import (
    HelpPopoverButton, LogView, StatusBanner, TokenCompletionPopover, ask_confirmation,
    classify_module, format_bytes, new_header_bar, new_icon, show_error_dialog,
    show_info_dialog,
)

from .backend import (
    CAPTURE_CANCELLED, activate_for_session, add_to_next_boot, cancel_chroot_session,
    capture_current_session, chroot_shell_argv, create_module_from_folder,
    create_module_from_packages, create_module_from_script,
    deactivate_for_session, extract_module, finish_chroot_session,
    load_module_inspection, load_next_boot_snapshot, load_running_snapshot,
    new_capture_cancel_marker, prepare_chroot_session, query_package_names,
    remove_from_next_boot, request_capture_cancel,
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

CREATE_HELP = {
    'packages': (
        _('Install packages from repositories or local .deb files and save the result as a module.'),
        (
            (_('What to enter'),
             _('• <b>Repository packages</b> — package names separated by spaces. Use <tt>↑</tt>/<tt>↓</tt> to choose a suggestion and <tt>Tab</tt> or <tt>Enter</tt> to accept it.\n• <b>Local packages</b> — add <tt>.deb</tt> files from disk.\n• <b>Output module</b> — choose the new <tt>.sb</tt> file.\n• <b>Compression</b> — leave <tt>zstd</tt> unless you need another format.')),
            (_('How it works'),
             _('APT installs the selected packages and their dependencies. Recommended packages are included when the checkbox is enabled. Package installation runs with administrator privileges. The current MiniOS session is not changed, and the new module is not loaded automatically.')),
        )),
    'script': (
        _('Run an installation script and save the changes it makes as a module.'),
        (
            (_('What to enter'),
             _('• <b>Installation script</b> — the script that performs the installation or setup.\n• <b>Seed folder</b> — optional files to copy into the module before the script runs.\n• <b>Output module</b> — choose the new <tt>.sb</tt> file.\n• <b>Compression</b> — leave <tt>zstd</tt> unless you need another format.')),
            (_('How it works'),
             _('The script runs with administrator privileges and <b>without an interactive terminal</b>. If it asks questions or needs manual work, use <b>Interactive Chroot</b> instead. The script itself is not stored in the module. The current MiniOS session is not changed.')),
        )),
    'chroot': (
        _('Open a temporary MiniOS system in a terminal and save your changes as a module.'),
        (
            (_('Before you start'),
             _('• <b>Seed folder</b> — optional files to copy into the temporary system before the shell opens.\n• <b>Output module</b> — choose the new <tt>.sb</tt> file.\n• <b>Compression</b> — leave <tt>zstd</tt> unless you need another format.')),
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


class ModuleManagerWindow(Gtk.ApplicationWindow):
    def __init__(self, application):
        Gtk.ApplicationWindow.__init__(
            self, application=application, title=_('MiniOS Module Manager'))
        self.set_default_size(800, 460)
        self._running_request = 0
        self._next_boot_request = 0
        self._inspection_request = 0
        self._detail_module = None
        self._detail_scope = None
        self._running_snapshot = None
        self._next_boot_snapshot = None
        self._detail_runtime_action = None
        self._detail_next_boot_action = None
        self._chroot_session_id = None
        self._chroot_config = None
        self._chroot_shell_running = False
        self._chroot_busy = False
        self._chroot_preparing = False
        self._close_after_chroot_cancel = False
        self._session_capture_busy = False
        self._session_capture_cancel = None
        self.connect('delete-event', self._on_window_delete)
        self._build_header()
        self._build_content()
        self._disable_pointer_focus(self)
        self._setup_drag_and_drop()
        self.refresh_running_snapshot()
        self.refresh_next_boot_snapshot()

    def _build_header(self):
        self.set_titlebar(new_header_bar(_('MiniOS Module Manager')))

    def _disable_pointer_focus(self, widget):
        if isinstance(widget, Gtk.Button):
            widget.set_focus_on_click(False)
        if isinstance(widget, Gtk.Container):
            for child in widget.get_children():
                self._disable_pointer_focus(child)

    def _back_button(self, callback):
        button = Gtk.Button(label=_('Back'))
        button.set_image(new_icon('go-previous-symbolic', accessible_name=_('Back')))
        button.set_always_show_image(True)
        button.get_style_context().add_class('minios-text-button')
        button.set_focus_on_click(False)
        button.connect('clicked', callback)
        return button

    def _page_title_label(self, text):
        label = Gtk.Label(label=text, xalign=0)
        label.get_style_context().add_class('page-title')
        return label

    def _build_run_output(self, initial_text):
        progress = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        spinner = Gtk.Spinner()
        spinner.set_valign(Gtk.Align.CENTER)
        progress.pack_start(spinner, False, False, 0)
        status = Gtk.Label(label=initial_text, xalign=0)
        status.set_line_wrap(True)
        progress.pack_start(status, True, True, 0)
        log = LogView(maximum_characters=200000)
        log.set_min_content_height(100)
        return progress, status, spinner, log

    def _build_result_output(self):
        stack = Gtk.Stack()
        stack.set_hhomogeneous(False)
        stack.set_vhomogeneous(False)
        summary = Gtk.Label(xalign=0, yalign=0)
        summary.set_line_wrap(True)
        log = LogView(maximum_characters=200000)
        log.set_min_content_height(160)
        stack.add_named(summary, 'summary')
        stack.add_named(log, 'log')
        stack.set_visible_child_name('summary')
        return stack, summary, log

    @staticmethod
    def _result_uses_log(text, diagnostic=False):
        text = str(text or '')
        return diagnostic and ('\n' in text or len(text) > 240)

    def _set_result_output(self, stack, summary, log, text, diagnostic=False):
        text = str(text or '')
        if self._result_uses_log(text, diagnostic):
            log.clear()
            log.feed(text)
            stack.set_visible_child_name('log')
        else:
            summary.set_text(text)
            stack.set_visible_child_name('summary')

    def _create_method_header(self, outer, method_id, title, back_callback):
        summary, sections = CREATE_HELP[method_id]
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.pack_start(self._back_button(back_callback), False, False, 0)
        controls.pack_start(self._page_title_label(title), True, True, 0)
        help_button = HelpPopoverButton(
            _('Help: {}').format(title), summary, sections,
            label=_('Help'), tooltip=_('Show help for this creation method'),
            markup=True)
        help_button.set_focus_on_click(False)
        controls.pack_end(help_button, False, False, 0)
        outer.pack_start(controls, False, False, 0)
        purpose = Gtk.Label(label=summary, xalign=0)
        purpose.set_line_wrap(True)
        purpose.get_style_context().add_class('page-subtitle')
        outer.pack_start(purpose, False, False, 0)

    def _build_content(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root.set_margin_top(8)
        root.set_margin_bottom(8)
        root.set_margin_start(8)
        root.set_margin_end(8)
        self.add(root)

        self.workspace_stack = Gtk.Stack()
        self.workspace_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        switcher = Gtk.StackSwitcher(stack=self.workspace_stack)
        switcher.set_halign(Gtk.Align.CENTER)
        root.pack_start(switcher, False, False, 0)

        self.workspace_stack.add_titled(
            self._build_modules_workspace(), 'modules', _('Modules'))
        self.workspace_stack.add_titled(
            self._build_create_workspace(), 'create', _('Create'))
        root.pack_start(self.workspace_stack, True, True, 0)

    def _build_modules_workspace(self):
        self.module_pages = Gtk.Stack()
        self.module_pages.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        title = Gtk.Label(label=_('Module composition'), xalign=0)
        title.get_style_context().add_class('page-title')
        box.pack_start(title, False, False, 0)

        snapshots = Gtk.Stack()
        snapshots.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        switcher = Gtk.StackSwitcher(stack=snapshots)
        switcher.set_halign(Gtk.Align.START)
        box.pack_start(switcher, False, False, 0)

        snapshots.add_titled(
            self._build_running_snapshot(), 'running', _('Running Now'))
        snapshots.add_titled(
            self._build_next_boot_snapshot(), 'next-boot', _('Next Boot'))
        box.pack_start(snapshots, True, True, 0)
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
        self.next_boot_add.set_no_show_all(True)
        self.next_boot_add.hide()
        self.next_boot_add.connect('clicked', self._choose_next_boot_module)
        controls.pack_end(self.next_boot_add, False, False, 0)
        refresh = Gtk.Button(label=_('Refresh'))
        refresh.set_image(new_icon('view-refresh-symbolic', accessible_name=_('Refresh')))
        refresh.get_style_context().add_class('minios-text-button')
        refresh.set_always_show_image(True)
        refresh.connect('clicked', lambda _button: self.refresh_next_boot_snapshot())
        controls.pack_end(refresh, False, False, 0)
        box.pack_start(controls, False, False, 0)

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
        back = self._back_button(lambda _button: self._show_module_composition())
        controls.pack_start(back, False, False, 0)
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
        self.detail_status = Gtk.Label(xalign=0)
        self.detail_status.set_line_wrap(True)
        outer.pack_start(self.detail_status, False, False, 0)

        search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.detail_search = Gtk.SearchEntry()
        self.detail_search.set_placeholder_text(_('Search module contents…'))
        self.detail_search.connect('search-changed', self._on_detail_search_changed)
        search_row.pack_start(self.detail_search, True, True, 0)
        outer.pack_start(search_row, False, False, 0)

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
        outer.pack_start(scrolled, True, True, 0)
        self._detail_inspection = None
        return outer

    def _snapshot_placeholder(self, title, detail):
        frame = Gtk.Frame()
        frame.get_style_context().add_class('empty-state')
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.set_valign(Gtk.Align.CENTER)
        body.set_halign(Gtk.Align.CENTER)
        body.set_margin_top(24)
        body.set_margin_bottom(24)
        body.set_margin_start(12)
        body.set_margin_end(12)
        heading = Gtk.Label(label=title)
        heading.get_style_context().add_class('empty-state-title')
        heading.set_line_wrap(True)
        heading.set_justify(Gtk.Justification.CENTER)
        body.pack_start(heading, False, False, 0)
        text = Gtk.Label(label=detail)
        text.set_line_wrap(True)
        text.set_justify(Gtk.Justification.CENTER)
        body.pack_start(text, False, False, 0)
        frame.add(body)
        return frame

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
        thread = threading.Thread(
            target=self._load_running_worker, args=(request,))
        thread.daemon = True
        thread.start()

    def _load_running_worker(self, request):
        snapshot = load_running_snapshot()
        GLib.idle_add(self._apply_running_snapshot, request, snapshot)

    def _apply_running_snapshot(self, request, snapshot):
        if request != self._running_request:
            return False
        self._running_snapshot = snapshot
        for child in self.running_list.get_children():
            self.running_list.remove(child)

        if snapshot.state == LoadState.READY:
            for index, module in enumerate(snapshot.modules):
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

        order = Gtk.Label(label=str(index + 1))
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
        self.next_boot_add.hide()
        self.next_boot_status.set_text(_('Loading next-boot module state…'))
        self._set_next_boot_message(
            _('Loading next-boot modules…'),
            _('Applying the current MiniOS boot module rules.'))
        thread = threading.Thread(
            target=self._load_next_boot_worker, args=(request,))
        thread.daemon = True
        thread.start()

    def _load_next_boot_worker(self, request):
        snapshot = load_next_boot_snapshot()
        GLib.idle_add(self._apply_next_boot_snapshot, request, snapshot)

    def _apply_next_boot_snapshot(self, request, snapshot):
        if request != self._next_boot_request:
            return False
        self._next_boot_snapshot = snapshot
        self.next_boot_add.hide()
        if snapshot.usable and snapshot.add_available:
            self.next_boot_add.show()
        for child in self.next_boot_list.get_children():
            self.next_boot_list.remove(child)

        if snapshot.state == LoadState.READY:
            for index, module in enumerate(snapshot.modules):
                self.next_boot_list.add(self._next_boot_module_row(index, module))
            self.next_boot_list.show_all()
            self.next_boot_status.set_text(
                _('{} next-boot modules · .{}').format(
                    len(snapshot.modules), snapshot.bundle_extension))
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
        }
        return self._module_row(
            index, module, 'next-boot',
            origin_names.get(module.origin, module.origin))

    def _choose_next_boot_module(self, _button):
        snapshot = self._next_boot_snapshot
        if snapshot is None or not snapshot.usable or not snapshot.add_available:
            return
        dialog = Gtk.FileChooserDialog(
            title=_('Add Module to Next Boot'), parent=self,
            action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons(
            _('Cancel'), Gtk.ResponseType.CANCEL,
            _('Select'), Gtk.ResponseType.ACCEPT)
        dialog.set_local_only(True)
        file_filter = Gtk.FileFilter()
        file_filter.set_name(_('MiniOS modules'))
        file_filter.add_pattern('*.{}'.format(snapshot.bundle_extension or 'sb'))
        dialog.add_filter(file_filter)
        response = dialog.run()
        source = dialog.get_filename() if response == Gtk.ResponseType.ACCEPT else None
        dialog.destroy()
        if not source:
            return
        if not self._confirm_action(
                _('Add {} to Next Boot? The running system will not change.').format(
                    os.path.basename(source))):
            return
        self.next_boot_add.set_sensitive(False)
        self.next_boot_status.set_text(_('Adding module to Next Boot…'))
        thread = threading.Thread(
            target=self._next_boot_add_worker, args=(source,))
        thread.daemon = True
        thread.start()

    def _next_boot_add_worker(self, source):
        success, message = add_to_next_boot(source)
        GLib.idle_add(self._apply_next_boot_add_result, success, message)

    def _apply_next_boot_add_result(self, success, message):
        if not success:
            self.next_boot_status.set_text(message or _('Could not add module to Next Boot.'))
            snapshot = self._next_boot_snapshot
            if snapshot is not None and snapshot.usable and snapshot.add_available:
                self.next_boot_add.set_sensitive(True)
                self.next_boot_add.show()
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
        self.detail_meta.set_text('')
        self.detail_extract.set_sensitive(False)
        self.module_pages.set_visible_child_name('details')
        self._update_detail_actions()

        if not module.source:
            self.detail_status.set_text(
                _('The backing module source is unavailable for inspection.'))
            return

        self.detail_status.set_text(_('Reading module contents…'))
        thread = threading.Thread(
            target=self._load_inspection_worker,
            args=(request, module.source))
        thread.daemon = True
        thread.start()

    def _load_inspection_worker(self, request, source):
        inspection = load_module_inspection(source)
        GLib.idle_add(self._apply_module_inspection, request, inspection)

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
            self.detail_status.set_text('')
            self.detail_extract.set_sensitive(True)
        else:
            self.detail_meta.set_text('')
            self.detail_status.set_text(
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
                self.detail_status.set_text('')
            return
        path = model.get_value(tree_iter, 4)
        target = model.get_value(tree_iter, 6)
        mode = model.get_value(tree_iter, 7)
        details = path
        if target:
            details += _(' → {}').format(target)
        if mode:
            details += _(' · {}').format(mode)
        self.detail_status.set_text(details)

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
        button.set_sensitive(True)
        button.show()

    def _update_detail_actions(self):
        self._set_detail_action('runtime', None)
        self._set_detail_action('next-boot', None)
        module = self._detail_module
        running = self._running_snapshot
        next_boot = self._next_boot_snapshot
        if module is None:
            return

        running_match = self._snapshot_module(running, module.name)
        next_match = self._snapshot_module(next_boot, module.name)
        if self._detail_scope == 'running':
            if (running is not None and running.usable and
                    running.union_backend == 'aufs' and
                    next_boot is not None and next_boot.usable and
                    (next_match is None or next_match.origin != 'base')):
                self._set_detail_action('runtime', (
                    _('Deactivate for This Session'),
                    _('Deactivate {} for this session? Next Boot will not change.').format(
                        module.name),
                    deactivate_for_session, module.name))
            if (next_boot is not None and next_boot.usable and
                    next_boot.add_available and next_match is None and
                    module.source):
                self._set_detail_action('next-boot', (
                    _('Add to Next Boot'),
                    _('Add {} to Next Boot? The running system will not change.').format(
                        module.name),
                    add_to_next_boot, module.source))
        elif self._detail_scope == 'next-boot':
            if module.removable:
                self._set_detail_action('next-boot', (
                    _('Remove from Next Boot'),
                    _('Remove {} from Next Boot? The running system will not change.').format(
                        module.name),
                    remove_from_next_boot, module.name))
            if (module.origin != 'base' and module.source and
                    running is not None and running.usable and
                    running.union_backend == 'aufs' and running_match is None):
                self._set_detail_action('runtime', (
                    _('Activate for This Session'),
                    _('Activate {} for this session? Next Boot will not change.').format(
                        module.name),
                    activate_for_session, module.source))

    def _confirm_action(self, text, confirm_label=None):
        return ask_confirmation(
            self, text, confirm_label=confirm_label or _('Continue'))

    def _run_detail_action(self, scope):
        action = (self._detail_runtime_action if scope == 'runtime'
                  else self._detail_next_boot_action)
        module = self._detail_module
        if (action is None or module is None or
                not self._confirm_action(action[1], action[0])):
            return
        self.detail_runtime.set_sensitive(False)
        self.detail_next_boot.set_sensitive(False)
        self.detail_status.set_text(_('Applying module change…'))
        thread = threading.Thread(
            target=self._detail_action_worker,
            args=(module.name, action[2], action[3]))
        thread.daemon = True
        thread.start()

    def _detail_action_worker(self, module_name, function, argument):
        success, message = function(argument)
        GLib.idle_add(
            self._apply_detail_action_result, module_name, success, message)

    def _apply_detail_action_result(self, module_name, success, message):
        module = self._detail_module
        if module is None or module.name != module_name:
            return False
        if not success:
            self.detail_status.set_text(message or _('Module change failed.'))
            self._update_detail_actions()
            return False
        self.module_pages.set_visible_child_name('composition')
        self.refresh_running_snapshot()
        self.refresh_next_boot_snapshot()
        return False

    def _choose_extract_target(self, _button):
        module = self._detail_module
        if module is None or not module.source:
            return
        dialog = Gtk.FileChooserDialog(
            title=_('Extract Module to Folder'), parent=self,
            action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons(
            _('Cancel'), Gtk.ResponseType.CANCEL,
            _('Extract'), Gtk.ResponseType.ACCEPT)
        dialog.set_create_folders(True)
        dialog.set_local_only(True)
        default_name = os.path.splitext(module.name)[0] or module.name
        dialog.set_current_name(default_name)
        response = dialog.run()
        target = dialog.get_filename() if response == Gtk.ResponseType.ACCEPT else None
        dialog.destroy()
        if target:
            self._start_module_extraction(module.source, target)

    def _start_module_extraction(self, source, target):
        self.detail_extract.set_sensitive(False)
        self.detail_status.set_text(_('Extracting module…'))
        thread = threading.Thread(
            target=self._extract_module_worker, args=(source, target))
        thread.daemon = True
        thread.start()

    def _extract_module_worker(self, source, target):
        success, message = extract_module(source, target)
        GLib.idle_add(
            self._apply_extraction_result, source, success, message)

    def _apply_extraction_result(self, source, success, message):
        module = self._detail_module
        if module is None or module.source != source:
            return False
        self.detail_extract.set_sensitive(True)
        if success:
            self.detail_status.set_text(_('Extracted to {}').format(message))
        else:
            self.detail_status.set_text(message)
        return False

    def _build_create_workspace(self):
        self.create_pages = Gtk.Stack()
        self.create_pages.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.create_pages.set_hhomogeneous(False)
        self.create_pages.set_vhomogeneous(False)
        self.create_pages.add_named(self._build_create_methods(), 'methods')
        self.create_pages.add_named(self._build_package_configure(), 'package-configure')
        self.create_pages.add_named(self._build_package_review(), 'package-review')
        self.create_pages.add_named(self._build_package_run(), 'package-run')
        self.create_pages.add_named(self._build_package_result(), 'package-result')
        self.create_pages.add_named(self._build_script_configure(), 'script-configure')
        self.create_pages.add_named(self._build_script_review(), 'script-review')
        self.create_pages.add_named(self._build_script_run(), 'script-run')
        self.create_pages.add_named(self._build_script_result(), 'script-result')
        self.create_pages.add_named(self._build_chroot_configure(), 'chroot-configure')
        self.create_pages.add_named(self._build_chroot_review(), 'chroot-review')
        self.create_pages.add_named(self._build_chroot_run(), 'chroot-run')
        self.create_pages.add_named(self._build_chroot_result(), 'chroot-result')
        self.create_pages.add_named(self._build_folder_configure(), 'folder-configure')
        self.create_pages.add_named(self._build_folder_review(), 'folder-review')
        self.create_pages.add_named(self._build_folder_run(), 'folder-run')
        self.create_pages.add_named(self._build_folder_result(), 'folder-result')
        self.create_pages.add_named(self._build_session_configure(), 'session-configure')
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
            self.package_config_status.set_text('')
            self.create_pages.set_visible_child_name('package-configure')
        elif method == 'script':
            self.script_config_status.set_text('')
            self.create_pages.set_visible_child_name('script-configure')
        elif method == 'chroot':
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
        grid.attach(Gtk.Label(label=_('Compression'), xalign=0), 0, 2, 1, 1)
        grid.attach(self.folder_compression, 1, 2, 1, 1)
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

    def _choose_folder_source(self, _button):
        dialog = Gtk.FileChooserDialog(
            title=_('Choose Source Folder'), parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER)
        dialog.add_buttons(
            _('Cancel'), Gtk.ResponseType.CANCEL,
            _('Select'), Gtk.ResponseType.ACCEPT)
        dialog.set_local_only(True)
        response = dialog.run()
        source = dialog.get_filename() if response == Gtk.ResponseType.ACCEPT else None
        dialog.destroy()
        if not source:
            return
        self.folder_source.set_text(source)
        if not self.folder_target.get_text().strip():
            name = os.path.basename(source.rstrip(os.sep)) or 'module'
            self.folder_target.set_text(
                os.path.join(os.path.dirname(source),
                             '{}.{}'.format(name, self._current_bundle_extension())))

    def _choose_folder_target(self, _button):
        dialog = Gtk.FileChooserDialog(
            title=_('Choose Output Module'), parent=self,
            action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons(
            _('Cancel'), Gtk.ResponseType.CANCEL,
            _('Select'), Gtk.ResponseType.ACCEPT)
        dialog.set_local_only(True)
        current = self.folder_target.get_text().strip()
        if current:
            directory = os.path.dirname(current)
            if os.path.isdir(directory):
                dialog.set_current_folder(directory)
            dialog.set_current_name(os.path.basename(current))
        else:
            dialog.set_current_name('module.{}'.format(self._current_bundle_extension()))
        response = dialog.run()
        target = dialog.get_filename() if response == Gtk.ResponseType.ACCEPT else None
        dialog.destroy()
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
        back = self._back_button(
            lambda _b: self.create_pages.set_visible_child_name('folder-configure'))
        controls.pack_start(back, False, False, 0)
        controls.pack_start(self._page_title_label(_('Review Folder Module')), True, True, 0)
        outer.pack_start(controls, False, False, 0)
        self.folder_review_source = Gtk.Label(xalign=0)
        self.folder_review_source.set_selectable(True)
        self.folder_review_target = Gtk.Label(xalign=0)
        self.folder_review_target.set_selectable(True)
        self.folder_review_options = Gtk.Label(xalign=0)
        for widget in (
                self.folder_review_source, self.folder_review_target,
                self.folder_review_options):
            widget.set_line_wrap(True)
            outer.pack_start(widget, False, False, 0)
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
        self.folder_review_source.set_text(_('Source: {}').format(source))
        self.folder_review_target.set_text(_('Output: {}').format(target))
        self.folder_review_options.set_text(
            _('Compression: {} · Privileges: none').format(compression))
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
        done = Gtk.Button(label=_('Done'))
        done.get_style_context().add_class('suggested-action')
        done.connect('clicked', lambda _b: self.create_pages.set_visible_child_name('methods'))
        controls.pack_end(done, False, False, 0)
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
        self.folder_run_status.set_text(labels.get(phase, phase))
        return False

    def _apply_folder_result(self, success, result):
        self.folder_spinner.stop()
        if success:
            self.folder_result_title.set_text(_('Module Created'))
            self._set_result_output(
                self.folder_result_output, self.folder_result_text,
                self.folder_result_log,
                _('Output: {output}\nSize: {size} bytes\nCompression: {compression}'
                  '\nSHA-256: {sha256}').format(**result))
            self.folder_result_back.set_label(_('Create Another'))
        else:
            self.folder_result_title.set_text(_('Module Creation Failed'))
            self._set_result_output(
                self.folder_result_output, self.folder_result_text,
                self.folder_result_log, result, diagnostic=True)
            self.folder_result_back.set_label(_('Back to Folder'))
        self.create_pages.set_visible_child_name('folder-result')
        return False


    def _build_package_configure(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self._create_method_header(
            outer, 'packages', _('Packages'),
            lambda _b: self.create_pages.set_visible_child_name('methods'))

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
        for item in ('zstd', 'xz', 'gzip', 'lzo'):
            self.package_compression.append_text(item)
        self.package_compression.set_active(0)
        grid.attach(Gtk.Label(label=_('Compression'), xalign=0), 0, 3, 1, 1)
        grid.attach(self.package_compression, 1, 3, 1, 1)

        self.package_recommends = Gtk.CheckButton(label=_('Install recommended packages'))
        self.package_recommends.set_active(True)
        grid.attach(self.package_recommends, 1, 4, 2, 1)
        outer.pack_start(grid, False, False, 0)

        self.package_config_status = Gtk.Label(xalign=0)
        self.package_config_status.get_style_context().add_class('inline-error')
        self.package_config_status.set_line_wrap(True)
        outer.pack_start(self.package_config_status, False, False, 0)
        review = Gtk.Button(label=_('Review'))
        review.get_style_context().add_class('suggested-action')
        review.set_halign(Gtk.Align.END)
        review.connect('clicked', self._review_package_creation)
        outer.pack_end(review, False, False, 0)
        return outer

    def _choose_package_debs(self, _button):
        dialog = Gtk.FileChooserDialog(
            title=_('Choose Local Debian Packages'), parent=self,
            action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons(
            _('Cancel'), Gtk.ResponseType.CANCEL,
            _('Add'), Gtk.ResponseType.ACCEPT)
        dialog.set_local_only(True)
        dialog.set_select_multiple(True)
        file_filter = Gtk.FileFilter()
        file_filter.set_name(_('Debian packages'))
        file_filter.add_pattern('*.deb')
        dialog.add_filter(file_filter)
        response = dialog.run()
        files = dialog.get_filenames() if response == Gtk.ResponseType.ACCEPT else []
        dialog.destroy()
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
        dialog = Gtk.FileChooserDialog(
            title=_('Choose Output Module'), parent=self,
            action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons(
            _('Cancel'), Gtk.ResponseType.CANCEL,
            _('Select'), Gtk.ResponseType.ACCEPT)
        dialog.set_local_only(True)
        current = self.package_target.get_text().strip()
        if current:
            directory = os.path.dirname(current)
            if os.path.isdir(directory):
                dialog.set_current_folder(directory)
            dialog.set_current_name(os.path.basename(current))
        else:
            dialog.set_current_name('packages.{}'.format(self._current_bundle_extension()))
        response = dialog.run()
        target = dialog.get_filename() if response == Gtk.ResponseType.ACCEPT else None
        dialog.destroy()
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
        return (names, local_files, target, compression, recommends), ''
    def _build_package_review(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        back = self._back_button(
            lambda _b: self.create_pages.set_visible_child_name('package-configure'))
        controls.pack_start(back, False, False, 0)
        controls.pack_start(self._page_title_label(_('Review Package Module')), True, True, 0)
        outer.pack_start(controls, False, False, 0)

        self.package_review_packages = Gtk.Label(xalign=0)
        self.package_review_packages.set_line_wrap(True)
        self.package_review_local = Gtk.Label(xalign=0)
        self.package_review_local.set_line_wrap(True)
        self.package_review_target = Gtk.Label(xalign=0)
        self.package_review_target.set_line_wrap(True)
        self.package_review_options = Gtk.Label(xalign=0)
        self.package_review_options.set_line_wrap(True)
        for widget in (
                self.package_review_packages, self.package_review_local,
                self.package_review_target, self.package_review_options):
            widget.set_selectable(True)
            outer.pack_start(widget, False, False, 0)

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
        names, local_files, target, compression, recommends = configuration
        self.package_config_status.set_text('')
        self.package_review_packages.set_text(
            _('Repository packages: {}').format(' '.join(names) if names else _('none')))
        self.package_review_local.set_text(
            _('Local packages: {}').format(
                ', '.join(local_files) if local_files else _('none')))
        self.package_review_target.set_text(_('Output: {}').format(target))
        self.package_review_options.set_text(
            _('Compression: {} · Recommends: {} · Privileges: authentication required').format(
                compression, _('yes') if recommends else _('no')))
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
        done = Gtk.Button(label=_('Done'))
        done.get_style_context().add_class('suggested-action')
        done.connect('clicked', lambda _b: self.create_pages.set_visible_child_name('methods'))
        controls.pack_end(done, False, False, 0)
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
                                 compression, recommends):
        def phase_callback(phase):
            GLib.idle_add(self._apply_package_phase, phase)
        def log_callback(text):
            GLib.idle_add(self.package_run_log.feed, text)
        success, result = create_module_from_packages(
            names, local_files, target, compression, recommends,
            phase_callback, log_callback)
        GLib.idle_add(self._apply_package_result, success, result)

    def _apply_package_phase(self, phase):
        labels = {
            'prepare': _('Preparing build environment…'),
            'update': _('Updating package indexes…'),
            'packages': _('Installing packages…'),
            'capture': _('Capturing module…'),
            'complete': _('Finishing…'),
        }
        self.package_run_status.set_text(labels.get(phase, phase))
        return False
    def _apply_package_result(self, success, result):
        self.package_spinner.stop()
        if success:
            self.package_result_title.set_text(_('Module Created'))
            self._set_result_output(
                self.package_result_output, self.package_result_text,
                self.package_result_log,
                _('Output: {output}\nCompressed size: {compressed_size} bytes'
                  '\nUncompressed size: {uncompressed_size} bytes'
                  '\nEntries: {entry_count}\nCompression: {compression}'
                  '\nSHA-256: {sha256}').format(**result))
            self.package_result_back.set_label(_('Create Another'))
        else:
            self.package_result_title.set_text(_('Module Creation Failed'))
            self._set_result_output(
                self.package_result_output, self.package_result_text,
                self.package_result_log, result, diagnostic=True)
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
        for item in ('zstd', 'xz', 'gzip', 'lzo'):
            self.script_compression.append_text(item)
        self.script_compression.set_active(0)
        grid.attach(Gtk.Label(label=_('Compression'), xalign=0), 0, 3, 1, 1)
        grid.attach(self.script_compression, 1, 3, 1, 1)
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
        dialog = Gtk.FileChooserDialog(
            title=_('Choose Installation Script'), parent=self,
            action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons(
            _('Cancel'), Gtk.ResponseType.CANCEL,
            _('Select'), Gtk.ResponseType.ACCEPT)
        dialog.set_local_only(True)
        response = dialog.run()
        source = dialog.get_filename() if response == Gtk.ResponseType.ACCEPT else None
        dialog.destroy()
        if not source:
            return
        self.script_source.set_text(source)
        if not self.script_target.get_text().strip():
            base = os.path.splitext(os.path.basename(source))[0] or 'module'
            self.script_target.set_text(os.path.join(
                os.path.dirname(source),
                '{}.{}'.format(base, self._current_bundle_extension())))

    def _choose_script_seed(self, _button):
        dialog = Gtk.FileChooserDialog(
            title=_('Choose Seed Folder'), parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER)
        dialog.add_buttons(
            _('Cancel'), Gtk.ResponseType.CANCEL,
            _('Select'), Gtk.ResponseType.ACCEPT)
        dialog.set_local_only(True)
        response = dialog.run()
        seed = dialog.get_filename() if response == Gtk.ResponseType.ACCEPT else None
        dialog.destroy()
        if seed:
            self.script_seed.set_text(seed)

    def _choose_script_target(self, _button):
        dialog = Gtk.FileChooserDialog(
            title=_('Choose Output Module'), parent=self,
            action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons(
            _('Cancel'), Gtk.ResponseType.CANCEL,
            _('Select'), Gtk.ResponseType.ACCEPT)
        dialog.set_local_only(True)
        current = self.script_target.get_text().strip()
        if current:
            directory = os.path.dirname(current)
            if os.path.isdir(directory):
                dialog.set_current_folder(directory)
            dialog.set_current_name(os.path.basename(current))
        else:
            dialog.set_current_name(
                'script-module.{}'.format(self._current_bundle_extension()))
        response = dialog.run()
        target = dialog.get_filename() if response == Gtk.ResponseType.ACCEPT else None
        dialog.destroy()
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
        return (script, target, compression, seed), ''

    def _build_script_review(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        back = self._back_button(
            lambda _b: self.create_pages.set_visible_child_name('script-configure'))
        controls.pack_start(back, False, False, 0)
        controls.pack_start(
            self._page_title_label(_('Review Installation Script')), True, True, 0)
        outer.pack_start(controls, False, False, 0)

        self.script_review_source = Gtk.Label(xalign=0)
        self.script_review_seed = Gtk.Label(xalign=0)
        self.script_review_target = Gtk.Label(xalign=0)
        self.script_review_options = Gtk.Label(xalign=0)
        for widget in (
                self.script_review_source, self.script_review_seed,
                self.script_review_target, self.script_review_options):
            widget.set_line_wrap(True)
            widget.set_selectable(True)
            outer.pack_start(widget, False, False, 0)

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
        script, target, compression, seed = configuration
        self.script_config_status.set_text('')
        self.script_review_source.set_text(_('Script: {}').format(script))
        self.script_review_seed.set_text(
            _('Seed folder: {}').format(seed if seed else _('none')))
        self.script_review_target.set_text(_('Output: {}').format(target))
        self.script_review_options.set_text(
            _('Compression: {} · Privileges: authentication required').format(compression))
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
        done = Gtk.Button(label=_('Done'))
        done.get_style_context().add_class('suggested-action')
        done.connect('clicked', lambda _b: self.create_pages.set_visible_child_name('methods'))
        controls.pack_end(done, False, False, 0)
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

    def _script_creation_worker(self, script, target, compression, seed):
        def phase_callback(phase):
            GLib.idle_add(self._apply_script_phase, phase)
        def log_callback(text):
            GLib.idle_add(self.script_run_log.feed, text)
        success, result = create_module_from_script(
            script, target, compression, seed, phase_callback, log_callback)
        GLib.idle_add(self._apply_script_result, success, result)

    def _apply_script_phase(self, phase):
        labels = {
            'prepare': _('Preparing build environment…'),
            'seed': _('Copying seed folder…'),
            'script': _('Running installation script…'),
            'capture': _('Capturing module…'),
            'complete': _('Finishing…'),
        }
        self.script_run_status.set_text(labels.get(phase, phase))
        return False
    def _apply_script_result(self, success, result):
        self.script_spinner.stop()
        if success:
            self.script_result_title.set_text(_('Module Created'))
            self._set_result_output(
                self.script_result_output, self.script_result_text,
                self.script_result_log,
                _('Output: {output}\nCompressed size: {compressed_size} bytes'
                  '\nUncompressed size: {uncompressed_size} bytes'
                  '\nEntries: {entry_count}\nCompression: {compression}'
                  '\nSeed folder: {seed_directory}\nSHA-256: {sha256}').format(**result))
            self.script_result_back.set_label(_('Create Another'))
        else:
            self.script_result_title.set_text(_('Module Creation Failed'))
            self._set_result_output(
                self.script_result_output, self.script_result_text,
                self.script_result_log, result, diagnostic=True)
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
        for item in ('zstd', 'xz', 'gzip', 'lzo'):
            self.chroot_compression.append_text(item)
        self.chroot_compression.set_active(0)
        grid.attach(Gtk.Label(label=_('Compression'), xalign=0), 0, 2, 1, 1)
        grid.attach(self.chroot_compression, 1, 2, 1, 1)
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
        dialog = Gtk.FileChooserDialog(
            title=_('Choose Seed Folder'), parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER)
        dialog.add_buttons(
            _('Cancel'), Gtk.ResponseType.CANCEL,
            _('Select'), Gtk.ResponseType.ACCEPT)
        dialog.set_local_only(True)
        response = dialog.run()
        seed = dialog.get_filename() if response == Gtk.ResponseType.ACCEPT else None
        dialog.destroy()
        if seed:
            self.chroot_seed.set_text(seed)
            if not self.chroot_target.get_text().strip():
                self.chroot_target.set_text(os.path.join(
                    os.path.dirname(seed),
                    'chroot.{}'.format(self._current_bundle_extension())))

    def _choose_chroot_target(self, _button):
        dialog = Gtk.FileChooserDialog(
            title=_('Choose Output Module'), parent=self,
            action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons(
            _('Cancel'), Gtk.ResponseType.CANCEL,
            _('Select'), Gtk.ResponseType.ACCEPT)
        dialog.set_local_only(True)
        current = self.chroot_target.get_text().strip()
        if current:
            directory = os.path.dirname(current)
            if os.path.isdir(directory):
                dialog.set_current_folder(directory)
            dialog.set_current_name(os.path.basename(current))
        else:
            dialog.set_current_name(
                'chroot.{}'.format(self._current_bundle_extension()))
        response = dialog.run()
        target = dialog.get_filename() if response == Gtk.ResponseType.ACCEPT else None
        dialog.destroy()
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
        return (seed, target, compression), ''

    def _build_chroot_review(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        back = self._back_button(
            lambda _b: self.create_pages.set_visible_child_name('chroot-configure'))
        controls.pack_start(back, False, False, 0)
        controls.pack_start(self._page_title_label(_('Review Interactive Chroot')), True, True, 0)
        outer.pack_start(controls, False, False, 0)

        self.chroot_review_seed = Gtk.Label(xalign=0)
        self.chroot_review_target = Gtk.Label(xalign=0)
        self.chroot_review_options = Gtk.Label(xalign=0)
        for widget in (
                self.chroot_review_seed, self.chroot_review_target,
                self.chroot_review_options):
            widget.set_line_wrap(True)
            widget.set_selectable(True)
            outer.pack_start(widget, False, False, 0)
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
        seed, target, compression = configuration
        self.chroot_config_status.set_text('')
        self.chroot_review_seed.set_text(
            _('Seed folder: {}').format(seed if seed else _('none')))
        self.chroot_review_target.set_text(_('Output: {}').format(target))
        self.chroot_review_options.set_text(
            _('Compression: {} · Privileges: authentication required').format(compression))
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
        done = Gtk.Button(label=_('Done'))
        done.get_style_context().add_class('suggested-action')
        done.connect('clicked', lambda _b: self.create_pages.set_visible_child_name('methods'))
        controls.pack_end(done, False, False, 0)
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

    def _prepare_chroot_worker(self, seed, target, compression):
        def phase_callback(phase):
            GLib.idle_add(self._apply_chroot_prepare_phase, phase)
        success, result = prepare_chroot_session(
            seed, target, compression, phase_callback)
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

    def _finish_chroot_session(self, _button):
        if not self._chroot_session_id or self._chroot_shell_running:
            return
        self.chroot_reopen.set_sensitive(False)
        self.chroot_finish.set_sensitive(False)
        self.chroot_discard.set_sensitive(False)
        self._chroot_busy = True
        self.chroot_run_status.set_text(_('Capturing chroot changes…'))
        session_id = self._chroot_session_id
        seed, _target, compression = self._chroot_config
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
        self._set_result_output(
            self.chroot_result_output, self.chroot_result_text,
            self.chroot_result_log,
            _('Output: {output}\nCompressed size: {compressed_size} bytes'
              '\nUncompressed size: {uncompressed_size} bytes'
              '\nEntries: {entry_count}\nCompression: {compression}'
              '\nSHA-256: {sha256}').format(**result))
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
        for item in ('zstd', 'xz', 'gzip', 'lzo'):
            self.session_compression.append_text(item)
        self.session_compression.set_active(0)
        grid.attach(Gtk.Label(label=_('Compression'), xalign=0), 0, 1, 1, 1)
        grid.attach(self.session_compression, 1, 1, 1, 1)
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
        dialog = Gtk.FileChooserDialog(
            title=_('Choose Output Module'), parent=self,
            action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons(
            _('Cancel'), Gtk.ResponseType.CANCEL,
            _('Select'), Gtk.ResponseType.ACCEPT)
        dialog.set_local_only(True)
        current = self.session_target.get_text().strip()
        if current:
            directory = os.path.dirname(current)
            if os.path.isdir(directory):
                dialog.set_current_folder(directory)
            dialog.set_current_name(os.path.basename(current))
        else:
            dialog.set_current_name(
                'session-changes.{}'.format(self._current_bundle_extension()))
        response = dialog.run()
        target = dialog.get_filename() if response == Gtk.ResponseType.ACCEPT else None
        dialog.destroy()
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
        back = self._back_button(
            lambda _b: self.create_pages.set_visible_child_name('session-configure'))
        controls.pack_start(back, False, False, 0)
        controls.pack_start(
            self._page_title_label(_('Review Current Session Changes')),
            True, True, 0)
        outer.pack_start(controls, False, False, 0)

        self.session_review_target = Gtk.Label(xalign=0)
        self.session_review_target.set_line_wrap(True)
        self.session_review_target.set_selectable(True)
        outer.pack_start(self.session_review_target, False, False, 0)
        self.session_review_options = Gtk.Label(xalign=0)
        self.session_review_options.set_line_wrap(True)
        outer.pack_start(self.session_review_options, False, False, 0)
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
        self.session_review_target.set_text(_('Output: {}').format(target))
        self.session_review_options.set_text(
            _('Compression: {} · Policy: standard MiniOS savechanges · '
            'Privileges: authentication required').format(compression))
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
        done = Gtk.Button(label=_('Done'))
        done.get_style_context().add_class('suggested-action')
        done.connect('clicked', lambda _b: self.create_pages.set_visible_child_name('methods'))
        controls.pack_end(done, False, False, 0)
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
        self.session_run_status.set_text(labels.get(phase, phase))
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
            self._set_result_output(
                self.session_result_output, self.session_result_text,
                self.session_result_log,
                _('Output: {output}\nCompressed size: {compressed_size} bytes'
                  '\nUncompressed size: {uncompressed_size} bytes'
                  '\nEntries: {entry_count}\nSHA-256: {sha256}').format(**result))
        elif result is CAPTURE_CANCELLED:
            self.session_result_title.set_text(_('Capture Cancelled'))
            self._set_result_output(
                self.session_result_output, self.session_result_text,
                self.session_result_log,
                _('Current session capture was cancelled. No completed module was reported.'))
        else:
            self.session_result_title.set_text(_('Session Capture Failed'))
            self._set_result_output(
                self.session_result_output, self.session_result_text,
                self.session_result_log, result, diagnostic=True)
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
        self.workspace_stack.set_visible_child_name('modules')
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
            self.workspace_stack.set_visible_child_name('create')
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
            self.workspace_stack.set_visible_child_name('create')
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
            self.workspace_stack.set_visible_child_name('create')
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
