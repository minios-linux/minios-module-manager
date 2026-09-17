# MiniOS Module Manager

## Overview

GTK 3 application for inspecting, creating, and managing MiniOS `.sb` modules.

The application is organized around two top-level notebook tabs:

- **Manage Modules** — inspect and manage the modules used by the current session and those configured for the next boot.
- **Create Module** — create a new module from packages, scripts, chroot, folders, or current-session changes.

Manage Modules uses native **Running Now** and **Next Boot** tabs. A shared `minios-gui` help popover beside the Module Sets heading explains that Running Now changes only the current live session, while Next Boot controls what MiniOS will load after restart; the two sets can intentionally differ.

`minios-tools` owns the actual module operations. Module Manager runs as the desktop user and does not reimplement backend filesystem or package logic. Running Now is read rootlessly from `sb list --json`; Next Boot is read from `sb next-boot --json`, which applies the current MiniOS boot module rules to the existing runtime layout. Composition rows use the shared MiniOS module role/icon metadata and stay compact: role and filename are primary, compressed size and next-boot origin use the right edge, while full paths remain in tooltips/details. Module Details loads contents lazily through `sb inspect FILE --json`: readable backing files are inspected rootlessly, while an active module whose backing file is protected is inspected through `pkexec`; extraction delegates to rootless `sb2dir --json`. Reviewed runtime and Next Boot actions use fixed argv through `pkexec`; runtime mounting and unmounting are delegated completely to `sb activate` and `sb deactivate`, while base modules and unavailable durable stores stay read-only. Create → Folder provides a rootless Configure → Review → Run → Result flow through `dir2sb --json`. Create → Packages uses the same in-window flow through privileged `apt2sb --json`, supporting repository packages and selected local `.deb` files; Recommended packages are disabled by default. Repository package names use asynchronous local APT-index completion through the shared `minios-gui` token completion widget; when binary package lists have not been downloaded yet, the Packages page offers a privileged `apt-get update` so completion can be enabled before review. Packages, Installation Script, and Interactive Chroot present the build base as a compact full-width selector: the closed field stays at normal form height, while a bounded scrollable popover shows single-line rows with role icons, role names, and module filenames aligned on the right. The selected numbered module is a cutoff for the active stack, not a single base: all active numbered modules through that level plus all active unnumbered modules are used together. Context help explains how lower levels can make a module more self-contained but larger, while the full active stack can produce a smaller module with more dependencies on higher-level modules. Create → Installation Script uses privileged `script2sb --json` with an optional seed folder and an explicit executable-code review. Create → Interactive Chroot uses VTE 2.91 with the protected `chroot2sb prepare/shell/finish/cancel` lifecycle; the GUI never receives a privileged workspace path. Create → Current Session Changes delegates to cancellable `savechanges --json` and keeps the historical MiniOS savechanges policy. Folder, Packages, Installation Script, and Current Session Changes show a framed scrolling build log with phase progress and backend output while they run; completed logs remain available from the Result page. Interactive Chroot uses a framed VTE terminal and retains its terminal transcript with the result. Creation never changes Running Now or Next Boot automatically. Backend failures remain distinct from authoritative empty module lists.

Local `.sb` files can be opened from the file manager and are inspected without activation. When the running root union is AUFS, Module Details can mount an inactive module into the current session or unmount an eligible active module without changing Next Boot. Drag-and-drop is input-only: modules open inspection, `.deb` files populate Packages, folders populate Folder, and regular files populate Installation Script. The canonical `application/x-sb` MIME type and `*.sb` association are owned and installed by `minios-tools`; Module Manager only declares that it can open that type. The redesigned interface uses gettext and ships complete German, Spanish, French, Indonesian, Italian, Portuguese, Brazilian Portuguese, and Russian translations; untranslated locales fall back to the English source strings. The embedded chroot terminal uses `Monospace 10` and explicitly tells the user to run `exit` before finishing or discarding the prepared session. Create forms use the available width instead of fixed natural control sizes. Each creation method keeps a one-line purpose visible and provides concise, formatted contextual Help based on the matching MiniOS Tools workflow: what to enter and how the method works or what result it produces. The Packages field completes locally known package names from the current APT indexes without network access. Back navigation stays in the window header while configure-page headers and primary actions remain outside the scrollable body. Creation review pages use structured field/value cards, and successful results expose a selectable SHA-256 value. Context-help controls use the shared icon-only MiniOS GUI help button consistently. The application uses `python3-minios-gui` for the canonical MiniOS stylesheet, header bar, semantic status banners, icon resolution, and standard dialogs; specialized NDJSON/VTE workers stay local because they preserve backend protocol boundaries that the generic command helper does not provide.

## Development

Requirements: Python 3.6+, PyGObject, GTK 3, the VTE 2.91 typelib, `python3-minios-gui >= 1.4.0`, and `minios-tools >= 1.7.0`. PolicyKit with `pkexec` is required for privileged actions. Building packages requires gettext; updating man-page translations also requires po4a.

Run from the source tree:

```sh
./bin/minios-module-manager
```

Run tests:

```sh
make test
```

## License

Distributed under the GNU General Public License v2 or later.
