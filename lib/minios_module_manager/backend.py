"""Fixed-argv adapters for MiniOS command-line backends."""

import json
import os
import shutil
import subprocess
import tempfile
import threading

from .i18n import _
from .model import Inspection, LoadState, ModuleRecord, Snapshot


CAPTURE_CANCELLED = object()


class ProtocolError(Exception):
    pass


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
    if not isinstance(value, dict) or any(
            value.get(key) != item for key, item in expected.items()):
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
    return Inspection(
        state=LoadState.READY, path=path, size=size, entries=entries)

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
    if not isinstance(value, dict) or any(
            value.get(key) != item for key, item in expected.items()):
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
    if not isinstance(value, dict) or any(
            value.get(key) != item for key, item in expected.items()):
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
    if not isinstance(value, dict) or any(
            value.get(key) != item for key, item in expected.items()):
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
    if not isinstance(value, dict) or any(
            value.get(key) != item for key, item in expected.items()):
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
    if not isinstance(value, dict) or any(
            value.get(key) != item for key, item in expected.items()):
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
        'schema_version': 1, 'tool': 'savechanges',
        'operation': 'capture-module',
    }
    if not isinstance(value, dict) or any(
            value.get(key) != item for key, item in expected.items()):
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
    if not isinstance(digest, str) or len(digest) != 64:
        raise ProtocolError(_('savechanges digest is invalid'))
    if compression not in ('zstd', 'gzip', 'lzo', 'xz'):
        raise ProtocolError(_('savechanges compression is invalid'))
    return value


def capture_current_session(target, compression='zstd', phase_callback=None,
                            cancel_context=None, log_callback=None):
    savechanges = shutil.which('savechanges')
    pkexec = shutil.which('pkexec')
    if not savechanges:
        return False, _('MiniOS Tools is not installed.')
    if not pkexec:
        return False, _('pkexec is not available.')
    if compression not in ('zstd', 'gzip', 'lzo', 'xz'):
        return False, _('Unsupported compression type.')
    if cancel_context is None:
        cancel_context = new_capture_cancel_marker()
    _parent, marker = cancel_context
    argv = [
        pkexec, savechanges, '--json', '--cancel-file', marker,
        '--comp', compression, target,
    ]
    final_result = None
    stderr = ''
    returncode = 1
    protocol_error = None
    cancelled = False
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
            if value.get('event') == 'phase':
                phase = value.get('phase')
                if phase not in SESSION_CAPTURE_PHASES:
                    protocol_error = _('savechanges returned an unknown phase.')
                    break
                if phase_callback is not None:
                    phase_callback(phase)
            elif value.get('tool') == 'savechanges':
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
        cancelled = os.path.exists(marker)
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
    return True, final_result
