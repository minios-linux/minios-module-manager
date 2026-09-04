"""Fixed-argv adapters for MiniOS command-line backends."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading

from .i18n import _
from .model import Inspection, InspectionEntry, LoadState, ModuleRecord, Snapshot


CAPTURE_CANCELLED = object()
PKEXEC_PATH = '/usr/bin/pkexec'
SAVECHANGES_PATH = '/usr/bin/savechanges'


class ProtocolError(Exception):
    pass


def _schema_version_is_one(value):
    return type(value.get('schema_version')) is int and value['schema_version'] == 1


def _trusted_root_executable(path):
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode) and
        metadata.st_uid == 0 and
        not metadata.st_mode & 0o022 and
        bool(metadata.st_mode & 0o111)
    )


def _start_stderr_reader(process, callback=None):
    chunks = []

    def reader():
        stream = getattr(process, 'stderr', None)
        if stream is None:
            return
        while True:
            line = stream.readline()
            if not isinstance(line, str) or line == '':
                break
            chunks.append(line)
            if callback is not None:
                callback(line)

    thread = threading.Thread(target=reader)
    thread.daemon = True
    thread.start()
    return chunks, thread


def _finish_stderr_reader(chunks, thread):
    thread.join()
    return ''.join(chunks).strip()


def _module_record(value, require_mount=True):
    if not isinstance(value, dict):
        raise ProtocolError(_('module record is not an object'))
    name = value.get('name')
    mount = value.get('mount')
    source = value.get('source')
    origin = value.get('origin')
    removable = value.get('removable', False)
    if not isinstance(name, str) or not name:
        raise ProtocolError(_('module name is invalid'))
    if require_mount and (not isinstance(mount, str) or not mount):
        raise ProtocolError(_('module mountpoint is invalid'))
    if mount is not None and not isinstance(mount, str):
        raise ProtocolError(_('module mountpoint is invalid'))
    if source is not None and not isinstance(source, str):
        raise ProtocolError(_('module source is invalid'))
    if origin is not None and origin not in ('base', 'modules', 'persistence'):
        raise ProtocolError(_('module origin is invalid'))
    if not isinstance(removable, bool):
        raise ProtocolError(_('module removability is invalid'))
    return ModuleRecord(
        name=name, mount=mount, source=source, origin=origin,
        removable=removable)

def parse_running_result(text):
    try:
        value = json.loads(text)
    except (TypeError, ValueError) as error:
        raise ProtocolError(_('sb returned invalid JSON: {}').format(error))

    if not isinstance(value, dict):
        raise ProtocolError(_('sb result is not an object'))
    if not _schema_version_is_one(value):
        raise ProtocolError(_('unsupported sb result'))
    expected = {
        'type': 'result',
        'product_kind': 'minios-tool-result',
        'schema_version': 1,
        'tool': 'sb',
        'operation': 'list',
    }
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise ProtocolError(_('unsupported sb result'))

    union = value.get('union_backend')
    if union not in ('aufs', 'overlayfs'):
        raise ProtocolError(_('unsupported root union'))
    modules_value = value.get('modules')
    if not isinstance(modules_value, list):
        raise ProtocolError(_('module list is invalid'))
    modules = [_module_record(item) for item in modules_value]
    state = LoadState.READY if modules else LoadState.EMPTY
    return Snapshot(state=state, modules=modules, union_backend=union)


def parse_next_boot_result(text):
    try:
        value = json.loads(text)
    except (TypeError, ValueError) as error:
        raise ProtocolError(_('sb returned invalid JSON: {}').format(error))

    if not isinstance(value, dict):
        raise ProtocolError(_('sb result is not an object'))
    if not _schema_version_is_one(value):
        raise ProtocolError(_('unsupported sb result'))
    expected = {
        'type': 'result',
        'product_kind': 'minios-tool-result',
        'schema_version': 1,
        'tool': 'sb',
        'operation': 'next-boot',
    }
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise ProtocolError(_('unsupported sb result'))

    data_root = value.get('data_root')
    extension = value.get('bundle_extension')
    if not isinstance(data_root, str) or not data_root:
        raise ProtocolError(_('MiniOS data root is invalid'))
    if not isinstance(extension, str) or not extension:
        raise ProtocolError(_('bundle extension is invalid'))
    add_available = value.get('add_available', False)
    if not isinstance(add_available, bool):
        raise ProtocolError(_('next-boot add availability is invalid'))
    modules_value = value.get('modules')
    if not isinstance(modules_value, list):
        raise ProtocolError(_('module list is invalid'))
    modules = [_module_record(item, require_mount=False) for item in modules_value]
    if any(item.origin is None or item.source is None for item in modules):
        raise ProtocolError(_('next-boot module source is incomplete'))
    state = LoadState.READY if modules else LoadState.EMPTY
    return Snapshot(
        state=state, modules=modules, data_root=data_root,
        bundle_extension=extension, add_available=add_available)


def load_running_snapshot():
    executable = shutil.which('sb')
    if not executable:
        return Snapshot(
            state=LoadState.UNAVAILABLE,
            message=_('MiniOS Tools is not installed.'))

    try:
        process = subprocess.Popen(
            [executable, 'list', '--json'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True)
        stdout, stderr = process.communicate()
    except OSError as error:
        return Snapshot(
            state=LoadState.ERROR,
            message=_('Could not start sb: {}').format(error))

    if process.returncode != 0:
        detail = stderr.strip() or _('sb could not read the running module state.')
        return Snapshot(state=LoadState.ERROR, message=detail)
    try:
        return parse_running_result(stdout)
    except ProtocolError as error:
        return Snapshot(state=LoadState.ERROR, message=str(error))


def load_next_boot_snapshot():
    executable = shutil.which('sb')
    if not executable:
        return Snapshot(
            state=LoadState.UNAVAILABLE,
            message=_('MiniOS Tools is not installed.'))

    try:
        process = subprocess.Popen(
            [executable, 'next-boot', '--json'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True)
        stdout, stderr = process.communicate()
    except OSError as error:
        return Snapshot(
            state=LoadState.ERROR,
            message=_('Could not start sb: {}').format(error))

    if process.returncode != 0:
        detail = stderr.strip() or _('sb could not read the next-boot module state.')
        return Snapshot(state=LoadState.ERROR, message=detail)
    try:
        return parse_next_boot_result(stdout)
    except ProtocolError as error:
        return Snapshot(state=LoadState.ERROR, message=str(error))


def parse_inspection_result(text):
    try:
        value = json.loads(text)
    except (TypeError, ValueError) as error:
        raise ProtocolError(_('sb returned invalid JSON: {}').format(error))

    expected = {
        'type': 'result', 'product_kind': 'minios-tool-result',
        'schema_version': 1, 'tool': 'sb', 'operation': 'inspect',
    }
    if (not isinstance(value, dict) or not _schema_version_is_one(value) or any(
            value.get(key) != item for key, item in expected.items())):
        raise ProtocolError(_('unsupported sb inspection result'))

    path = value.get('path')
    size = value.get('size')
    entries = value.get('entries')
    count = value.get('entry_count')
    if not isinstance(path, str) or not path:
        raise ProtocolError(_('module path is invalid'))
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ProtocolError(_('module size is invalid'))
    if not isinstance(entries, list) or any(
            not isinstance(entry, str) or not entry for entry in entries):
        raise ProtocolError(_('module entry list is invalid'))
    if count != len(entries):
        raise ProtocolError(_('module entry count is invalid'))

    details = value.get('entry_details')
    parsed_entries = []
    if details is not None:
        if not isinstance(details, list) or len(details) != len(entries):
            raise ProtocolError(_('module entry details are invalid'))
        for expected_path, detail in zip(entries, details):
            if not isinstance(detail, dict) or detail.get('path') != expected_path:
                raise ProtocolError(_('module entry details are invalid'))
            kind = detail.get('kind', 'unknown')
            entry_size = detail.get('size')
            mode = detail.get('mode')
            target = detail.get('target')
            if kind not in ('file', 'directory', 'symlink', 'device', 'fifo', 'socket', 'unknown'):
                raise ProtocolError(_('module entry type is invalid'))
            if entry_size is not None and (not isinstance(entry_size, int) or isinstance(entry_size, bool) or entry_size < 0):
                raise ProtocolError(_('module entry size is invalid'))
            if mode is not None and not isinstance(mode, str):
                raise ProtocolError(_('module entry mode is invalid'))
            if target is not None and not isinstance(target, str):
                raise ProtocolError(_('module link target is invalid'))
            parsed_entries.append(InspectionEntry(
                expected_path, kind=kind, size=entry_size, mode=mode, target=target))
    else:
        directories = set()
        for entry in entries:
            parts = entry.split('/')
            for index in range(1, len(parts)):
                directories.add('/'.join(parts[:index]))
        parsed_entries = [InspectionEntry(
            entry, kind='directory' if entry in directories else 'unknown')
            for entry in entries]
    return Inspection(
        state=LoadState.READY, path=path, size=size, entries=parsed_entries)

def load_module_inspection(path):
    executable = shutil.which('sb')
    if not executable:
        return Inspection(
            state=LoadState.UNAVAILABLE,
            path=path,
            message=_('MiniOS Tools is not installed.'))
    try:
        process = subprocess.Popen(
            [executable, 'inspect', path, '--json'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True)
        stdout, stderr = process.communicate()
    except OSError as error:
        return Inspection(
            state=LoadState.ERROR, path=path,
            message=_('Could not start sb: {}').format(error))
    if process.returncode != 0:
        detail = stderr.strip() or _('sb could not inspect the module.')
        return Inspection(state=LoadState.ERROR, path=path, message=detail)
    try:
        return parse_inspection_result(stdout)
    except ProtocolError as error:
        return Inspection(state=LoadState.ERROR, path=path, message=str(error))


def extract_module(source, target):
    executable = shutil.which('sb2dir')
    if not executable:
        return False, _('MiniOS Tools is not installed.')
    try:
        process = subprocess.Popen(
            [executable, '--json', source, target],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True)
        _stdout, stderr = process.communicate()
    except OSError as error:
        return False, _('Could not start sb2dir: {}').format(error)
    if process.returncode != 0:
        return False, stderr.strip() or _('sb2dir could not extract the module.')
    return True, target


def _run_privileged_sb(arguments):
    sb = shutil.which('sb')
    pkexec = shutil.which('pkexec')
    if not sb:
        return False, _('MiniOS Tools is not installed.')
    if not pkexec:
        return False, _('pkexec is not available.')
    try:
        process = subprocess.Popen(
            [pkexec, sb] + list(arguments),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True)
        stdout, stderr = process.communicate()
    except OSError as error:
        return False, _('Could not start privileged MiniOS Tools: {}').format(error)
    if process.returncode != 0:
        return False, stderr.strip() or stdout.strip() or _('MiniOS Tools operation failed.')
    return True, ''


def activate_for_session(source):
    return _run_privileged_sb(['activate', source])


def deactivate_for_session(name):
    return _run_privileged_sb(['deactivate', name])


def add_to_next_boot(source):
    return _run_privileged_sb([
        'next-boot', 'add', source, '--json'])


def remove_from_next_boot(name):
    return _run_privileged_sb([
        'next-boot', 'remove', name, '--json'])


FOLDER_PHASES = ('prepare', 'compress', 'verify', 'publish', 'complete')


def _folder_result(value):
    if not isinstance(value, dict) or value.get('product') != 'dir2sb':
        raise ProtocolError(_('unsupported dir2sb result'))
    output = value.get('output')
    size = value.get('size')
    digest = value.get('sha256')
    compression = value.get('compression')
    if not isinstance(output, str) or not output:
        raise ProtocolError(_('dir2sb output path is invalid'))
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ProtocolError(_('dir2sb output size is invalid'))
    if not isinstance(digest, str) or len(digest) != 64:
        raise ProtocolError(_('dir2sb digest is invalid'))
    if compression not in ('zstd', 'gzip', 'lzo', 'lz4', 'xz'):
        raise ProtocolError(_('dir2sb compression is invalid'))
    return value


def create_module_from_folder(source, target, compression='zstd', phase_callback=None,
                              log_callback=None):
    executable = shutil.which('dir2sb')
    if not executable:
        return False, _('MiniOS Tools is not installed.')
    if compression not in ('zstd', 'gzip', 'lzo', 'lz4', 'xz'):
        return False, _('Unsupported compression type.')
    argv = [
        executable, '--json', '--comp', compression, '--', source, target]
    final_result = None
    protocol_error = None
    try:
        process = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True)
        stderr_chunks, stderr_thread = _start_stderr_reader(process, log_callback)
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except ValueError:
                protocol_error = _('dir2sb returned invalid JSON.')
                break
            if not isinstance(value, dict):
                protocol_error = _('dir2sb returned an unsupported record.')
                break
            if value.get('event') == 'phase':
                phase = value.get('phase')
                if phase not in FOLDER_PHASES:
                    protocol_error = _('dir2sb returned an unknown phase.')
                    break
                if phase_callback is not None:
                    phase_callback(phase)
            elif value.get('product') == 'dir2sb':
                try:
                    final_result = _folder_result(value)
                except ProtocolError as error:
                    protocol_error = str(error)
                    break
            else:
                protocol_error = _('dir2sb returned an unsupported record.')
                break
        if protocol_error is not None:
            for _ignored_line in process.stdout:
                pass
        returncode = process.wait()
        stderr = _finish_stderr_reader(stderr_chunks, stderr_thread)
    except OSError as error:
        return False, str(error)
    if protocol_error is not None:
        return False, protocol_error
    if returncode != 0:
        return False, stderr or _('dir2sb could not create the module.')
    if final_result is None:
        return False, _('dir2sb completed without a result.')
    return True, final_result


PACKAGE_PHASES = ('prepare', 'update', 'packages', 'capture', 'complete')


def _package_result(value, expected_count):
    expected = {
        'type': 'result', 'product_kind': 'minios-tool-result',
        'schema_version': 1, 'tool': 'apt2sb', 'operation': 'install',
    }
    if (not isinstance(value, dict) or not _schema_version_is_one(value) or any(
            value.get(key) != item for key, item in expected.items())):
        raise ProtocolError(_('unsupported apt2sb result'))
    output = value.get('output')
    compressed = value.get('compressed_size')
    uncompressed = value.get('uncompressed_size')
    entries = value.get('entry_count')
    digest = value.get('sha256')
    compression = value.get('compression')
    count = value.get('package_count')
    if not isinstance(output, str) or not output:
        raise ProtocolError(_('apt2sb output path is invalid'))
    for number in (compressed, uncompressed, entries, count):
        if not isinstance(number, int) or isinstance(number, bool) or number < 0:
            raise ProtocolError(_('apt2sb numeric result is invalid'))
    if compressed <= 0 or count != expected_count:
        raise ProtocolError(_('apt2sb package result is inconsistent'))
    if not isinstance(digest, str) or len(digest) != 64:
        raise ProtocolError(_('apt2sb digest is invalid'))
    if compression not in ('zstd', 'gzip', 'lzo', 'xz'):
        raise ProtocolError(_('apt2sb compression is invalid'))
    return value
def create_module_from_packages(package_names, local_packages, target,
                                compression='zstd', install_recommends=True,
                                phase_callback=None, log_callback=None):
    apt2sb = shutil.which('apt2sb')
    pkexec = shutil.which('pkexec')
    if not apt2sb:
        return False, _('MiniOS Tools is not installed.')
    if not pkexec:
        return False, _('pkexec is not available.')
    if compression not in ('zstd', 'gzip', 'lzo', 'xz'):
        return False, _('Unsupported compression type.')
    requested = list(package_names) + list(local_packages)
    if not requested:
        return False, _('Choose at least one package.')
    if any(not isinstance(item, str) or not item for item in requested):
        return False, _('Package input is invalid.')

    argv = [
        pkexec, apt2sb, 'install', '--json', '-y',
        '--name', target, '--comp', compression,
    ]
    if not install_recommends:
        argv.append('--no-install-recommends')
    argv.extend(requested)
    final_result = None
    protocol_error = None
    try:
        process = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True)
        stderr_chunks, stderr_thread = _start_stderr_reader(process, log_callback)
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except ValueError:
                protocol_error = _('apt2sb returned invalid JSON.')
                break
            if not isinstance(value, dict):
                protocol_error = _('apt2sb returned an unsupported record.')
                break
            if value.get('event') == 'phase':
                phase = value.get('phase')
                if phase not in PACKAGE_PHASES:
                    protocol_error = _('apt2sb returned an unknown phase.')
                    break
                if phase_callback is not None:
                    phase_callback(phase)
            elif value.get('tool') == 'apt2sb':
                try:
                    final_result = _package_result(value, len(requested))
                except ProtocolError as error:
                    protocol_error = str(error)
                    break
            else:
                protocol_error = _('apt2sb returned an unsupported record.')
                break
        if protocol_error is not None:
            for _ignored_line in process.stdout:
                pass
        returncode = process.wait()
        stderr = _finish_stderr_reader(stderr_chunks, stderr_thread)
    except OSError as error:
        return False, str(error)
    if protocol_error is not None:
        return False, protocol_error
    if returncode != 0:
        return False, stderr or _('apt2sb could not create the module.')
    if final_result is None:
        return False, _('apt2sb completed without a result.')
    return True, final_result


SCRIPT_PHASES = ('prepare', 'seed', 'script', 'capture', 'complete')


def _script_result(value, expected_seed):
    expected = {
        'type': 'result', 'product_kind': 'minios-tool-result',
        'schema_version': 1, 'tool': 'script2sb', 'operation': 'create',
    }
    if (not isinstance(value, dict) or not _schema_version_is_one(value) or any(
            value.get(key) != item for key, item in expected.items())):
        raise ProtocolError(_('unsupported script2sb result'))
    output = value.get('output')
    compressed = value.get('compressed_size')
    uncompressed = value.get('uncompressed_size')
    entries = value.get('entry_count')
    digest = value.get('sha256')
    compression = value.get('compression')
    seed = value.get('seed_directory')
    if not isinstance(output, str) or not output:
        raise ProtocolError(_('script2sb output path is invalid'))
    for number in (compressed, uncompressed, entries):
        if not isinstance(number, int) or isinstance(number, bool) or number < 0:
            raise ProtocolError(_('script2sb numeric result is invalid'))
    if compressed <= 0 or seed is not expected_seed:
        raise ProtocolError(_('script2sb result is inconsistent'))
    if not isinstance(digest, str) or len(digest) != 64:
        raise ProtocolError(_('script2sb digest is invalid'))
    if compression not in ('zstd', 'gzip', 'lzo', 'xz'):
        raise ProtocolError(_('script2sb compression is invalid'))
    return value


def create_module_from_script(script, target, compression='zstd',
                              seed_directory=None, phase_callback=None,
                              log_callback=None):
    script2sb = shutil.which('script2sb')
    pkexec = shutil.which('pkexec')
    if not script2sb:
        return False, _('MiniOS Tools is not installed.')
    if not pkexec:
        return False, _('pkexec is not available.')
    if compression not in ('zstd', 'gzip', 'lzo', 'xz'):
        return False, _('Unsupported compression type.')
    if not isinstance(script, str) or not script:
        return False, _('Choose an installation script.')

    argv = [pkexec, script2sb, '--json', '--script', script,
            '--name', target, '--comp', compression]
    if seed_directory:
        argv.extend(['--directory', seed_directory])
    final_result = None
    protocol_error = None
    try:
        process = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True)
        stderr_chunks, stderr_thread = _start_stderr_reader(process, log_callback)
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except ValueError:
                protocol_error = _('script2sb returned invalid JSON.')
                break
            if not isinstance(value, dict):
                protocol_error = _('script2sb returned an unsupported record.')
                break
            if value.get('event') == 'phase':
                phase = value.get('phase')
                if phase not in SCRIPT_PHASES:
                    protocol_error = _('script2sb returned an unknown phase.')
                    break
                if phase_callback is not None:
                    phase_callback(phase)
            elif value.get('tool') == 'script2sb':
                try:
                    final_result = _script_result(value, bool(seed_directory))
                except ProtocolError as error:
                    protocol_error = str(error)
                    break
            else:
                protocol_error = _('script2sb returned an unsupported record.')
                break
        if protocol_error is not None:
            for _ignored_line in process.stdout:
                pass
        returncode = process.wait()
        stderr = _finish_stderr_reader(stderr_chunks, stderr_thread)
    except OSError as error:
        return False, str(error)
    if protocol_error is not None:
        return False, protocol_error
    if returncode != 0:
        return False, stderr or _('script2sb could not create the module.')
    if final_result is None:
        return False, _('script2sb completed without a result.')
    return True, final_result


def query_package_names(prefix, limit=200):
    """Return locally known APT package names beginning with *prefix*."""
    prefix = (prefix or '').strip()
    if len(prefix) < 2 or any(character.isspace() for character in prefix):
        return ()
    apt_cache = shutil.which('apt-cache')
    if not apt_cache:
        return ()
    try:
        result = subprocess.run(
            [apt_cache, 'pkgnames', prefix],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            universal_newlines=True, check=False)
    except OSError:
        return ()
    if result.returncode != 0:
        return ()
    names = []
    seen = set()
    for line in result.stdout.splitlines():
        name = line.strip()
        if (not name or name in seen or not name.startswith(prefix) or
                any(character.isspace() for character in name)):
            continue
        seen.add(name)
        names.append(name)
        if len(names) >= limit:
            break
    return tuple(sorted(names))


CHROOT_PREPARE_PHASES = ('prepare', 'seed')
CHROOT_FINISH_PHASES = ('capture', 'complete')


def _valid_chroot_session_id(value):
    if not isinstance(value, str) or not value.startswith('session.'):
        return False
    suffix = value[len('session.'):]
    return bool(suffix) and all(character.isalnum() for character in suffix)


def _chroot_prepare_result(value, target, compression, seed_directory):
    expected = {
        'type': 'result', 'product_kind': 'minios-tool-result',
        'schema_version': 1, 'tool': 'chroot2sb', 'operation': 'prepare',
    }
    if (not isinstance(value, dict) or not _schema_version_is_one(value) or any(
            value.get(key) != item for key, item in expected.items())):
        raise ProtocolError(_('unsupported chroot2sb prepare result'))
    if not _valid_chroot_session_id(value.get('session_id')):
        raise ProtocolError(_('chroot2sb session id is invalid'))
    if value.get('output') != target or value.get('compression') != compression:
        raise ProtocolError(_('chroot2sb prepare result is inconsistent'))
    if value.get('seed_directory') is not bool(seed_directory):
        raise ProtocolError(_('chroot2sb seed result is inconsistent'))
    return value


def _chroot_finish_result(value, compression, seed_directory):
    expected = {
        'type': 'result', 'product_kind': 'minios-tool-result',
        'schema_version': 1, 'tool': 'chroot2sb', 'operation': 'finish',
    }
    if (not isinstance(value, dict) or not _schema_version_is_one(value) or any(
            value.get(key) != item for key, item in expected.items())):
        raise ProtocolError(_('unsupported chroot2sb finish result'))
    for key in ('compressed_size', 'uncompressed_size', 'entry_count'):
        number = value.get(key)
        if not isinstance(number, int) or isinstance(number, bool) or number < 0:
            raise ProtocolError(_('chroot2sb numeric result is invalid'))
    if value.get('compressed_size', 0) <= 0:
        raise ProtocolError(_('chroot2sb compressed size is invalid'))
    digest = value.get('sha256')
    output = value.get('output')
    if not isinstance(output, str) or not output:
        raise ProtocolError(_('chroot2sb output path is invalid'))
    if not isinstance(digest, str) or len(digest) != 64:
        raise ProtocolError(_('chroot2sb digest is invalid'))
    if value.get('compression') != compression:
        raise ProtocolError(_('chroot2sb compression result is inconsistent'))
    if value.get('seed_directory') is not bool(seed_directory):
        raise ProtocolError(_('chroot2sb seed result is inconsistent'))
    return value


def _chroot_tools():
    chroot2sb = shutil.which('chroot2sb')
    pkexec = shutil.which('pkexec')
    if not chroot2sb:
        return None, None, _('MiniOS Tools is not installed.')
    if not pkexec:
        return None, None, _('pkexec is not available.')
    return chroot2sb, pkexec, ''


def _run_chroot_ndjson(argv, phases, result_parser, phase_callback):
    final_result = None
    protocol_error = None
    try:
        with tempfile.TemporaryFile(mode='w+') as error_stream:
            process = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=error_stream,
                universal_newlines=True)
            for line in process.stdout:
                if protocol_error is not None:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except ValueError:
                    protocol_error = _('chroot2sb returned invalid JSON.')
                    continue
                if not isinstance(value, dict):
                    protocol_error = _('chroot2sb returned an unsupported record.')
                    continue
                if value.get('event') == 'phase':
                    phase = value.get('phase')
                    if phase not in phases:
                        protocol_error = _('chroot2sb returned an unknown phase.')
                        continue
                    if phase_callback is not None:
                        phase_callback(phase)
                elif value.get('tool') == 'chroot2sb':
                    try:
                        final_result = result_parser(value)
                    except ProtocolError as error:
                        protocol_error = str(error)
                else:
                    protocol_error = _('chroot2sb returned an unsupported record.')
            returncode = process.wait()
            error_stream.seek(0)
            stderr = error_stream.read().strip()
    except OSError as error:
        return False, str(error)
    if protocol_error is not None:
        return False, protocol_error
    if returncode != 0:
        return False, stderr or _('chroot2sb operation failed.')
    if final_result is None:
        return False, _('chroot2sb completed without a result.')
    return True, final_result


def prepare_chroot_session(seed_directory, target, compression='zstd',
                           phase_callback=None):
    chroot2sb, pkexec, error = _chroot_tools()
    if error:
        return False, error
    if compression not in ('zstd', 'gzip', 'lzo', 'xz'):
        return False, _('Unsupported compression type.')
    argv = [
        pkexec, chroot2sb, 'prepare', '--json',
        '--name', target, '--comp', compression,
    ]
    if seed_directory:
        argv.extend(['--directory', seed_directory])
    return _run_chroot_ndjson(
        argv, CHROOT_PREPARE_PHASES,
        lambda value: _chroot_prepare_result(
            value, target, compression, bool(seed_directory)),
        phase_callback)


def chroot_shell_argv(session_id):
    if not _valid_chroot_session_id(session_id):
        return False, _('Invalid chroot session id.')
    chroot2sb, pkexec, error = _chroot_tools()
    if error:
        return False, error
    return True, [pkexec, chroot2sb, 'shell', session_id]


def finish_chroot_session(session_id, compression, seed_directory,
                          phase_callback=None):
    if not _valid_chroot_session_id(session_id):
        return False, _('Invalid chroot session id.')
    chroot2sb, pkexec, error = _chroot_tools()
    if error:
        return False, error
    argv = [pkexec, chroot2sb, 'finish', session_id, '--json']
    return _run_chroot_ndjson(
        argv, CHROOT_FINISH_PHASES,
        lambda value: _chroot_finish_result(
            value, compression, bool(seed_directory)),
        phase_callback)


def cancel_chroot_session(session_id):
    if not _valid_chroot_session_id(session_id):
        return False, _('Invalid chroot session id.')
    chroot2sb, pkexec, error = _chroot_tools()
    if error:
        return False, error
    argv = [pkexec, chroot2sb, 'cancel', session_id, '--json']
    try:
        process = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True)
        stdout, stderr = process.communicate()
    except OSError as error_value:
        return False, _('Could not start chroot2sb: {}').format(error_value)
    if process.returncode != 0:
        return False, stderr.strip() or _('Could not cancel the chroot session.')
    try:
        value = json.loads(stdout)
    except ValueError:
        return False, _('chroot2sb returned invalid JSON.')
    expected = {
        'type': 'result', 'product_kind': 'minios-tool-result',
        'schema_version': 1, 'tool': 'chroot2sb', 'operation': 'cancel',
        'session_id': session_id,
    }
    if (not isinstance(value, dict) or not _schema_version_is_one(value) or any(
            value.get(key) != item for key, item in expected.items())):
        return False, _('chroot2sb returned an unsupported cancel result.')
    return True, value


SESSION_CAPTURE_PHASES = (
    'prepare', 'inventory', 'capture', 'compress',
    'verify', 'publish', 'complete',
)


def new_capture_cancel_marker():
    parent = tempfile.mkdtemp(prefix='minios-module-capture-')
    os.chmod(parent, 0o700)
    return parent, os.path.join(parent, 'cancel')


def request_capture_cancel(marker):
    descriptor = os.open(
        marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)


def cleanup_capture_cancel_marker(context):
    parent, marker = context
    try:
        os.unlink(marker)
    except FileNotFoundError:
        pass
    try:
        os.rmdir(parent)
    except FileNotFoundError:
        pass


def _session_capture_result(value, target, compression):
    expected = {
        'type': 'result', 'product_kind': 'minios-tool-result',
        'tool': 'savechanges',
        'operation': 'capture-module',
    }
    if not isinstance(value, dict) or any(
            value.get(key) != item for key, item in expected.items()):
        raise ProtocolError(_('unsupported savechanges result'))
    if type(value.get('schema_version')) is not int or value['schema_version'] != 1:
        raise ProtocolError(_('unsupported savechanges result'))
    if value.get('output') != target:
        raise ProtocolError(_('savechanges output path is inconsistent'))
    for key in ('compressed_size', 'uncompressed_size', 'entry_count'):
        number = value.get(key)
        if not isinstance(number, int) or isinstance(number, bool) or number < 0:
            raise ProtocolError(_('savechanges numeric result is invalid'))
    if value.get('compressed_size', 0) <= 0:
        raise ProtocolError(_('savechanges compressed size is invalid'))
    digest = value.get('sha256')
    if (not isinstance(digest, str) or len(digest) != 64 or
            any(character not in '0123456789abcdef' for character in digest)):
        raise ProtocolError(_('savechanges digest is invalid'))
    if value.get('profile') != 'legacy':
        raise ProtocolError(_('unsupported savechanges result'))
    if value.get('union_backend') not in ('aufs', 'overlayfs'):
        raise ProtocolError(_('unsupported savechanges result'))
    identity = value.get('output_identity')
    if not isinstance(identity, dict):
        raise ProtocolError(_('unsupported savechanges result'))
    for key in ('device', 'inode'):
        number = identity.get(key)
        minimum = 1 if key == 'inode' else 0
        if (not isinstance(number, int) or isinstance(number, bool) or
                number < minimum):
            raise ProtocolError(_('unsupported savechanges result'))
    footprint = value.get('extraction_footprint')
    footprint_fields = {
        'product_kind', 'schema_version', 'regular_file_bytes',
        'regular_file_inodes', 'directory_count', 'symlink_count',
        'symlink_target_bytes', 'whiteout_count', 'inode_count',
        'directory_entry_count', 'filename_bytes',
        'hardlink_reference_count', 'xattr_count', 'xattr_name_bytes',
        'xattr_value_bytes', 'compressor', 'block_size',
    }
    if (not isinstance(footprint, dict) or set(footprint) != footprint_fields or
            footprint.get('product_kind') != 'minios-extraction-footprint' or
            type(footprint.get('schema_version')) is not int or
            footprint['schema_version'] != 1 or
            footprint.get('compressor') != compression or
            footprint.get('regular_file_bytes') != value.get('uncompressed_size') or
            footprint.get('directory_entry_count') != value.get('entry_count')):
        raise ProtocolError(_('unsupported savechanges result'))
    count_fields = footprint_fields - {
        'product_kind', 'schema_version', 'compressor', 'block_size',
    }
    block_size = footprint['block_size']
    if (any(type(footprint[key]) is not int or footprint[key] < 0
            for key in count_fields) or
            type(block_size) is not int or block_size < 4096 or
            block_size > 1024 * 1024 or block_size & (block_size - 1) or
            footprint['directory_count'] < 1 or footprint['inode_count'] < 1):
        raise ProtocolError(_('unsupported savechanges result'))
    regular_inodes = footprint['regular_file_inodes']
    directories = footprint['directory_count']
    symlinks = footprint['symlink_count']
    whiteouts = footprint['whiteout_count']
    hardlinks = footprint['hardlink_reference_count']
    if ((hardlinks and not regular_inodes) or
            footprint['directory_entry_count'] != (
                directories - 1 + regular_inodes + hardlinks + symlinks + whiteouts) or
            footprint['inode_count'] != (
                directories + regular_inodes + symlinks + whiteouts) or
            footprint['filename_bytes'] < footprint['directory_entry_count'] or
            footprint['symlink_target_bytes'] < symlinks or
            footprint['xattr_name_bytes'] < footprint['xattr_count']):
        raise ProtocolError(_('unsupported savechanges result'))
    if compression not in ('zstd', 'gzip', 'lzo', 'xz'):
        raise ProtocolError(_('savechanges compression is invalid'))
    return value


def _validate_capture_output(target, result):
    identity = result['output_identity']
    descriptor = None
    parent_descriptor = None
    try:
        absolute_target = os.path.abspath(target)
        parent, name = os.path.split(absolute_target)
        parent_descriptor = os.open(
            parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_descriptor)
        metadata = os.fstat(descriptor)
        digests = []
        for _pass in range(2):
            os.lseek(descriptor, 0, os.SEEK_SET)
            digest = hashlib.sha256()
            first_block = True
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    break
                if first_block and not block.startswith(b'hsqs'):
                    raise ProtocolError(
                        _('savechanges output path is inconsistent'))
                first_block = False
                digest.update(block)
            digests.append(digest.hexdigest())
        final_metadata = os.fstat(descriptor)
        published_metadata = os.stat(
            name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError:
        raise ProtocolError(_('savechanges output path is inconsistent'))
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)
    stable_fields = ('st_mode', 'st_size', 'st_nlink', 'st_dev', 'st_ino',
                     'st_mtime_ns', 'st_ctime_ns')
    if (any(getattr(metadata, field) != getattr(final_metadata, field)
            for field in stable_fields) or
            any(getattr(metadata, field) != getattr(published_metadata, field)
                for field in stable_fields) or
            not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or
            metadata.st_size != result['compressed_size'] or
            metadata.st_dev != identity['device'] or
            metadata.st_ino != identity['inode'] or
            published_metadata.st_dev != metadata.st_dev or
            published_metadata.st_ino != metadata.st_ino or
            digests[0] != result['sha256'] or digests[1] != result['sha256']):
        raise ProtocolError(_('savechanges output path is inconsistent'))


def capture_current_session(target, compression='zstd', phase_callback=None,
                            cancel_context=None, log_callback=None):
    if not _trusted_root_executable(SAVECHANGES_PATH):
        return False, _('MiniOS Tools is not installed.')
    if not _trusted_root_executable(PKEXEC_PATH):
        return False, _('pkexec is not available.')
    if compression not in ('zstd', 'gzip', 'lzo', 'xz'):
        return False, _('Unsupported compression type.')
    if cancel_context is None:
        cancel_context = new_capture_cancel_marker()
    _parent, marker = cancel_context
    argv = [
        PKEXEC_PATH, SAVECHANGES_PATH, '--json', '--cancel-file', marker,
        '--comp', compression, target,
    ]
    final_result = None
    stderr = ''
    returncode = 1
    protocol_error = None
    cancelled_phase = False
    phase_index = 0
    try:
        process = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True)
        stderr_chunks, stderr_thread = _start_stderr_reader(process, log_callback)
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except ValueError:
                protocol_error = _('savechanges returned invalid JSON.')
                break
            if not isinstance(value, dict):
                protocol_error = _('savechanges returned an unsupported record.')
                break
            if cancelled_phase:
                protocol_error = _('savechanges returned an unsupported record.')
                break
            if final_result is not None:
                protocol_error = _('savechanges returned an unsupported record.')
                break
            if value.get('type') == 'phase':
                phase = value.get('phase')
                if phase == 'cancelled':
                    if (final_result is not None or
                            phase_index == len(SESSION_CAPTURE_PHASES)):
                        protocol_error = _('savechanges returned an unsupported record.')
                        break
                    cancelled_phase = True
                    continue
                if (phase_index >= len(SESSION_CAPTURE_PHASES) or
                        phase != SESSION_CAPTURE_PHASES[phase_index]):
                    protocol_error = _('savechanges returned an unknown phase.')
                    break
                phase_index += 1
                if phase_callback is not None:
                    phase_callback(phase)
            elif value.get('tool') == 'savechanges':
                if phase_index != len(SESSION_CAPTURE_PHASES):
                    protocol_error = _('savechanges returned an unsupported record.')
                    break
                try:
                    final_result = _session_capture_result(
                        value, target, compression)
                except ProtocolError as error:
                    protocol_error = str(error)
                    break
            else:
                protocol_error = _('savechanges returned an unsupported record.')
                break
        if protocol_error is not None:
            for _ignored_line in process.stdout:
                pass
        returncode = process.wait()
        stderr = _finish_stderr_reader(stderr_chunks, stderr_thread)
        if protocol_error is None and cancelled_phase and returncode != 130:
            protocol_error = _('savechanges returned an unsupported record.')
        if protocol_error is None and final_result is not None and returncode == 130:
            protocol_error = _('savechanges returned an unsupported record.')
        if (protocol_error is None and final_result is None and
                phase_index == len(SESSION_CAPTURE_PHASES)):
            protocol_error = _('savechanges returned an unsupported record.')
        cancelled = (returncode == 130 and final_result is None and
                     phase_index < len(SESSION_CAPTURE_PHASES))
    except OSError as error:
        return False, _('Could not start savechanges: {}').format(error)
    finally:
        cleanup_capture_cancel_marker(cancel_context)

    if protocol_error is not None:
        return False, protocol_error
    if returncode != 0:
        if cancelled:
            return False, CAPTURE_CANCELLED
        return False, stderr or _('savechanges could not capture current session changes.')
    if final_result is None:
        return False, _('savechanges completed without a result.')
    try:
        _validate_capture_output(target, final_result)
    except ProtocolError as error:
        return False, str(error)
    return True, final_result
