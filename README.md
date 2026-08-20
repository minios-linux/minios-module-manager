# MiniOS Module Manager

GTK 3 application for inspecting, creating, and managing MiniOS `.sb` modules.

The application is organized around two workspaces:

- **Modules** — Running Now and Next Boot composition.
- **Create** — module creation from packages, scripts, chroot, folders, or current-session changes.

`minios-tools` owns the actual module operations. Module Manager runs as the desktop user and does not reimplement backend filesystem or package logic. Running Now is read rootlessly from `sb list --json`; Next Boot is read from `sb next-boot --json`, which applies the current MiniOS boot module rules to the existing runtime layout. Composition rows use the shared MiniOS module role/icon metadata and stay compact: role and filename are primary, compressed size and next-boot origin use the right edge, while full paths remain in tooltips/details. Module Details loads contents lazily through rootless `sb inspect FILE --json`, and extraction delegates to rootless `sb2dir --json`. Reviewed runtime and Next Boot actions use fixed argv through `pkexec`; base modules and unavailable durable stores stay read-only. Create → Folder provides a rootless Configure → Review → Run → Result flow through `dir2sb --json`. Create → Packages uses the same in-window flow through privileged `apt2sb --json`, supporting repository packages and selected local `.deb` files. Repository package names use asynchronous local APT-index completion through the shared `minios-gui` token completion widget. Create → Installation Script uses privileged `script2sb --json` with an optional seed folder and an explicit executable-code review. Create → Interactive Chroot uses VTE 2.91 with the protected `chroot2sb prepare/shell/finish/cancel` lifecycle; the GUI never receives a privileged workspace path. Create → Current Session Changes delegates to cancellable `savechanges --json` and keeps the historical MiniOS savechanges policy. Folder, Packages, Installation Script, and Current Session Changes stream backend stderr into a scrollable LogView while they run, so the current phase and real tool output are visible immediately. Interactive Chroot uses its VTE terminal for the same purpose. Creation never changes Running Now or Next Boot automatically. Backend failures remain distinct from authoritative empty module lists.

Local `.sb` files can be opened from the file manager and are inspected without activation. Drag-and-drop is input-only: modules open inspection, `.deb` files populate Packages, folders populate Folder, and regular files populate Installation Script. The package installs a MiniOS-specific `*.sb` MIME type that inherits from SquashFS without claiming other SquashFS image extensions. The redesigned interface uses gettext and ships complete German, Spanish, French, Indonesian, Italian, Portuguese, Brazilian Portuguese, and Russian translations; untranslated locales fall back to the English source strings. The embedded chroot terminal uses `Monospace 10` and explicitly tells the user to run `exit` before finishing or discarding the prepared session. Create forms use the available width instead of fixed natural control sizes. Each creation method keeps a one-line purpose visible and provides concise, formatted contextual Help based on the matching MiniOS Tools workflow: what to enter and how the method works or what result it produces. The Packages field completes locally known package names from the current APT indexes without network access. Back navigation uses a consistent previous-arrow button while remaining fully keyboard accessible. The application uses `python3-minios-gui` for the canonical MiniOS stylesheet, header bar, semantic status banners, icon resolution, and standard dialogs; specialized NDJSON/VTE workers stay local because they preserve backend protocol boundaries that the generic command helper does not provide.

## Development

Requirements: Python 3.6+, PyGObject, GTK 3, the VTE 2.91 typelib, `python3-minios-gui >= 1.1.0`, and `minios-tools >= 1.6.0`. PolicyKit with `pkexec` is required for privileged actions. Building packages requires gettext; updating man-page translations also requires po4a.

Run from the source tree:

```sh
./bin/minios-module-manager
```

Run tests:

```sh
make test
```
