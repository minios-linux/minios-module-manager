import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock


sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))

from minios_module_manager import backend
from minios_module_manager.model import LoadState


def result(modules=None, union='aufs'):
    return json.dumps({
        'type': 'result',
        'product_kind': 'minios-tool-result',
        'schema_version': 1,
        'tool': 'sb',
        'operation': 'list',
        'union_backend': union,
        'modules': modules or [],
    })


class ParseRunningResultTests(unittest.TestCase):
    def test_ready_result_preserves_backend_order(self):
        snapshot = backend.parse_running_result(result([
            {'name': '00-core.sb', 'mount': '/bundles/00-core.sb', 'source': '/media/00-core.sb'},
            {'name': '50-user.sb', 'mount': '/bundles/50-user.sb', 'source': None},
        ]))
        self.assertEqual(snapshot.state, LoadState.READY)
        self.assertEqual(snapshot.union_backend, 'aufs')
        self.assertEqual(
            [item.name for item in snapshot.modules],
            ['00-core.sb', '50-user.sb'])
        self.assertIsNone(snapshot.modules[1].source)

    def test_empty_result_is_authoritative(self):
        snapshot = backend.parse_running_result(result())
        self.assertEqual(snapshot.state, LoadState.EMPTY)
        self.assertTrue(snapshot.usable)

    def test_wrong_schema_is_rejected(self):
        value = json.loads(result())
        value['schema_version'] = 2
        with self.assertRaises(backend.ProtocolError):
            backend.parse_running_result(json.dumps(value))

    def test_invalid_module_record_is_rejected(self):
        with self.assertRaises(backend.ProtocolError):
            backend.parse_running_result(result([
                {'name': 'bad.sb', 'mount': None, 'source': None},
            ]))


class LoadRunningSnapshotTests(unittest.TestCase):
    def test_missing_sb_is_unavailable(self):
        with mock.patch.object(backend.shutil, 'which', return_value=None):
            snapshot = backend.load_running_snapshot()
        self.assertEqual(snapshot.state, LoadState.UNAVAILABLE)

    def test_backend_failure_is_not_empty(self):
        process = mock.Mock()
        process.communicate.return_value = ('', 'runtime unavailable')
        process.returncode = 1
        with mock.patch.object(backend.shutil, 'which', return_value='/usr/bin/sb'), \
                mock.patch.object(backend.subprocess, 'Popen', return_value=process) as popen:
            snapshot = backend.load_running_snapshot()
        self.assertEqual(snapshot.state, LoadState.ERROR)
        self.assertEqual(snapshot.modules, ())
        self.assertIn('runtime unavailable', snapshot.message)
        popen.assert_called_once_with(
            ['/usr/bin/sb', 'list', '--json'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True)


def next_boot_result(modules=None, extension='sb', add_available=False):
    return json.dumps({
        'type': 'result',
        'product_kind': 'minios-tool-result',
        'schema_version': 1,
        'tool': 'sb',
        'operation': 'next-boot',
        'data_root': '/run/initramfs/memory/data/minios',
        'bundle_extension': extension,
        'add_available': add_available,
        'modules': modules or [],
    })


class ParseNextBootResultTests(unittest.TestCase):
    def test_ready_result_preserves_order_origin_and_actions(self):
        snapshot = backend.parse_next_boot_result(next_boot_result([
            {'name': '00-core.sb', 'source': '/minios/00-core.sb',
             'origin': 'base', 'removable': False},
            {'name': '50-user.sb', 'source': '/minios/modules/50-user.sb',
             'origin': 'modules', 'removable': True},
        ], add_available=True))
        self.assertEqual(snapshot.state, LoadState.READY)
        self.assertEqual(snapshot.bundle_extension, 'sb')
        self.assertEqual(snapshot.modules[1].origin, 'modules')
        self.assertTrue(snapshot.modules[1].removable)
        self.assertTrue(snapshot.add_available)
        self.assertEqual(snapshot.data_root, '/run/initramfs/memory/data/minios')

    def test_incomplete_source_is_rejected(self):
        with self.assertRaises(backend.ProtocolError):
            backend.parse_next_boot_result(next_boot_result([
                {'name': 'bad.sb', 'source': None, 'origin': 'base'},
            ]))

    def test_empty_result_is_authoritative(self):
        snapshot = backend.parse_next_boot_result(next_boot_result())
        self.assertEqual(snapshot.state, LoadState.EMPTY)
        self.assertTrue(snapshot.usable)


class LoadNextBootSnapshotTests(unittest.TestCase):
    def test_backend_failure_is_not_empty(self):
        process = mock.Mock()
        process.communicate.return_value = ('', 'next boot unavailable')
        process.returncode = 1
        with mock.patch.object(backend.shutil, 'which', return_value='/usr/bin/sb'), \
                mock.patch.object(backend.subprocess, 'Popen', return_value=process) as popen:
            snapshot = backend.load_next_boot_snapshot()
        self.assertEqual(snapshot.state, LoadState.ERROR)
        self.assertEqual(snapshot.modules, ())
        self.assertIn('next boot unavailable', snapshot.message)
        popen.assert_called_once_with(
            ['/usr/bin/sb', 'next-boot', '--json'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True)


def inspect_result(entries=None):
    entries = entries or []
    return json.dumps({
        'type': 'result',
        'product_kind': 'minios-tool-result',
        'schema_version': 1,
        'tool': 'sb',
        'operation': 'inspect',
        'path': '/modules/example.sb',
        'size': 4096,
        'entry_count': len(entries),
        'entries': entries,
    })


class ParseInspectionResultTests(unittest.TestCase):
    def test_inspection_preserves_entries(self):
        result_value = backend.parse_inspection_result(
            inspect_result(['etc', 'etc/example.conf']))
        self.assertEqual(result_value.state, LoadState.READY)
        self.assertEqual(result_value.size, 4096)
        self.assertEqual(
            tuple(entry.path for entry in result_value.entries),
            ('etc', 'etc/example.conf'))
        self.assertEqual(result_value.entries[0].kind, 'directory')
        self.assertEqual(result_value.entries[1].kind, 'unknown')

    def test_wrong_entry_count_is_rejected(self):
        value = json.loads(inspect_result(['etc']))
        value['entry_count'] = 2
        with self.assertRaises(backend.ProtocolError):
            backend.parse_inspection_result(json.dumps(value))


class LoadInspectionTests(unittest.TestCase):
    def test_backend_failure_is_reported(self):
        process = mock.Mock()
        process.communicate.return_value = ('', 'inspection failed')
        process.returncode = 1
        with mock.patch.object(backend.shutil, 'which', return_value='/usr/bin/sb'), \
                mock.patch.object(backend.subprocess, 'Popen', return_value=process) as popen:
            result_value = backend.load_module_inspection('/modules/example.sb')
        self.assertEqual(result_value.state, LoadState.ERROR)
        self.assertIn('inspection failed', result_value.message)
        popen.assert_called_once_with(
            ['/usr/bin/sb', 'inspect', '/modules/example.sb', '--json'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True)


class ExtractModuleTests(unittest.TestCase):
    def test_success_uses_fixed_argv(self):
        process = mock.Mock()
        process.communicate.return_value = ('{"event":"phase","phase":"complete"}\n', '')
        process.returncode = 0
        with mock.patch.object(backend.shutil, 'which', return_value='/usr/bin/sb2dir'), \
                mock.patch.object(backend.subprocess, 'Popen', return_value=process) as popen:
            success, result_value = backend.extract_module(
                '/modules/example.sb', '/tmp/example')
        self.assertTrue(success)
        self.assertEqual(result_value, '/tmp/example')
        popen.assert_called_once_with(
            ['/usr/bin/sb2dir', '--json', '/modules/example.sb', '/tmp/example'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True)

    def test_failure_preserves_backend_message(self):
        process = mock.Mock()
        process.communicate.return_value = ('', 'target already exists')
        process.returncode = 4
        with mock.patch.object(backend.shutil, 'which', return_value='/usr/bin/sb2dir'), \
                mock.patch.object(backend.subprocess, 'Popen', return_value=process):
            success, message = backend.extract_module(
                '/modules/example.sb', '/tmp/example')
        self.assertFalse(success)
        self.assertIn('target already exists', message)


class PrivilegedMutationTests(unittest.TestCase):
    def _run_success(self, function, argument, expected):
        process = mock.Mock()
        process.communicate.return_value = ('', '')
        process.returncode = 0
        def which(name):
            return {'sb': '/usr/bin/sb', 'pkexec': '/usr/bin/pkexec'}.get(name)
        with mock.patch.object(backend.shutil, 'which', side_effect=which), \
                mock.patch.object(backend.subprocess, 'Popen', return_value=process) as popen:
            success, message = function(argument)
        self.assertTrue(success)
        self.assertEqual(message, '')
        popen.assert_called_once_with(
            expected, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True)

    def test_runtime_mutations_use_pkexec_and_fixed_argv(self):
        self._run_success(
            backend.activate_for_session, '/modules/50-user.sb',
            ['/usr/bin/pkexec', '/usr/bin/sb', 'activate', '/modules/50-user.sb'])
        self._run_success(
            backend.deactivate_for_session, '50-user.sb',
            ['/usr/bin/pkexec', '/usr/bin/sb', 'deactivate', '50-user.sb'])

    def test_next_boot_mutations_use_pkexec_and_json_mode(self):
        self._run_success(
            backend.add_to_next_boot, '/modules/50-user.sb',
            ['/usr/bin/pkexec', '/usr/bin/sb', 'next-boot', 'add',
             '/modules/50-user.sb', '--json'])
        self._run_success(
            backend.remove_from_next_boot, '50-user.sb',
            ['/usr/bin/pkexec', '/usr/bin/sb', 'next-boot', 'remove',
             '50-user.sb', '--json'])


class FolderCreationTests(unittest.TestCase):
    def test_success_streams_backend_phases_and_result(self):
        process = mock.Mock()
        process.stdout = io.StringIO(
            '{"event":"phase","phase":"prepare"}\n'
            '{"event":"phase","phase":"compress"}\n'
            '{"event":"phase","phase":"complete"}\n'
            '{"product":"dir2sb","output":"/tmp/out.sb","device":1,'
            '"inode":2,"size":4096,"sha256":"' + ('a' * 64) + '",'
            '"compression":"zstd"}\n')
        process.stderr = io.StringIO('mksquashfs: working\n')
        process.wait.return_value = 0
        phases = []
        logs = []
        with mock.patch.object(backend.shutil, 'which', return_value='/usr/bin/dir2sb'), \
                mock.patch.object(backend.subprocess, 'Popen', return_value=process) as popen:
            success, result = backend.create_module_from_folder(
                '/source dir', '/tmp/out.sb', 'zstd', phases.append, logs.append)
        self.assertTrue(success)
        self.assertEqual(result['output'], '/tmp/out.sb')
        self.assertEqual(phases, ['prepare', 'compress', 'complete'])
        self.assertEqual(logs, ['mksquashfs: working\n'])
        argv = popen.call_args[0][0]
        self.assertEqual(argv, [
            '/usr/bin/dir2sb', '--json', '--comp', 'zstd', '--',
            '/source dir', '/tmp/out.sb'])

    def test_backend_failure_preserves_diagnostics(self):
        process = mock.Mock()
        process.stdout = io.StringIO('{"event":"phase","phase":"prepare"}\n')
        process.stderr = io.StringIO('compression failed\n')
        process.wait.return_value = 5
        logs = []
        with mock.patch.object(backend.shutil, 'which', return_value='/usr/bin/dir2sb'), \
                mock.patch.object(backend.subprocess, 'Popen', return_value=process):
            success, message = backend.create_module_from_folder(
                '/source', '/tmp/out.sb', log_callback=logs.append)
        self.assertFalse(success)
        self.assertIn('compression failed', message)
        self.assertEqual(logs, ['compression failed\n'])

    def test_success_without_final_result_is_rejected(self):
        process = mock.Mock()
        process.stdout = io.StringIO('{"event":"phase","phase":"complete"}\n')
        process.stderr = io.StringIO('')
        process.wait.return_value = 0
        with mock.patch.object(backend.shutil, 'which', return_value='/usr/bin/dir2sb'), \
                mock.patch.object(backend.subprocess, 'Popen', return_value=process):
            success, message = backend.create_module_from_folder('/source', '/tmp/out.sb')
        self.assertFalse(success)
        self.assertIn('without a result', message)


if __name__ == '__main__':
    unittest.main()


class ProtocolDrainTests(unittest.TestCase):
    def test_folder_protocol_error_drains_stdout_before_wait(self):
        output = 'not-json\n' + ('diagnostic output\n' * 4096)
        process = mock.Mock()
        process.stdout = io.StringIO(output)
        process.stderr = io.StringIO('')
        process.wait.return_value = 0
        with mock.patch.object(backend.shutil, 'which', return_value='/usr/bin/dir2sb'), \
                mock.patch.object(backend.subprocess, 'Popen', return_value=process):
            success, message = backend.create_module_from_folder('/source', '/tmp/out.sb')
        self.assertFalse(success)
        self.assertIn('invalid JSON', message)
        self.assertEqual(process.stdout.tell(), len(output))


class PackageCreationTests(unittest.TestCase):
    @staticmethod
    def _result(count=2):
        return json.dumps({
            'type': 'result', 'product_kind': 'minios-tool-result',
            'schema_version': 1, 'tool': 'apt2sb', 'operation': 'install',
            'output': '/tmp/packages.sb', 'compressed_size': 4096,
            'uncompressed_size': 8192, 'entry_count': 8,
            'sha256': 'a' * 64, 'compression': 'zstd',
            'package_count': count,
        })

    def test_success_uses_pkexec_fixed_argv_and_phases(self):
        process = mock.Mock()
        process.stdout = io.StringIO(
            '{"event":"phase","phase":"prepare"}\n'
            '{"event":"phase","phase":"update"}\n'
            '{"event":"phase","phase":"packages"}\n'
            '{"event":"phase","phase":"capture"}\n'
            '{"event":"phase","phase":"complete"}\n' + self._result() + '\n')
        process.stderr = io.StringIO('Get:1 package index\n')
        process.wait.return_value = 0
        phases = []
        logs = []
        def which(name):
            return {'apt2sb': '/usr/bin/apt2sb', 'pkexec': '/usr/bin/pkexec'}.get(name)
        with mock.patch.object(backend.shutil, 'which', side_effect=which), \
                mock.patch.object(backend.subprocess, 'Popen', return_value=process) as popen:
            success, result_value = backend.create_module_from_packages(
                ['curl'], ['/tmp/local.deb'], '/tmp/packages.sb',
                'zstd', False, phases.append, logs.append)
        self.assertTrue(success)
        self.assertEqual(result_value['output'], '/tmp/packages.sb')
        self.assertEqual(phases, list(backend.PACKAGE_PHASES))
        self.assertEqual(logs, ['Get:1 package index\n'])
        self.assertEqual(popen.call_args[0][0], [
            '/usr/bin/pkexec', '/usr/bin/apt2sb', 'install', '--json', '-y',
            '--name', '/tmp/packages.sb', '--comp', 'zstd',
            '--no-install-recommends', 'curl', '/tmp/local.deb'])
    def test_inconsistent_result_is_rejected(self):
        process = mock.Mock()
        process.stdout = io.StringIO(self._result(count=1) + '\n')
        process.stderr = io.StringIO('')
        process.wait.return_value = 0
        def which(name):
            return {'apt2sb': '/usr/bin/apt2sb', 'pkexec': '/usr/bin/pkexec'}.get(name)
        with mock.patch.object(backend.shutil, 'which', side_effect=which), \
                mock.patch.object(backend.subprocess, 'Popen', return_value=process):
            success, message = backend.create_module_from_packages(
                ['curl'], ['/tmp/local.deb'], '/tmp/packages.sb')
        self.assertFalse(success)
        self.assertIn('inconsistent', message)


class ScriptCreationTests(unittest.TestCase):
    def test_success_uses_pkexec_fixed_argv_and_phases(self):
        process = mock.Mock()
        process.stdout = io.StringIO(
            '{"event":"phase","phase":"prepare"}\n'
            '{"event":"phase","phase":"seed"}\n'
            '{"event":"phase","phase":"script"}\n'
            '{"event":"phase","phase":"capture"}\n'
            '{"event":"phase","phase":"complete"}\n'
            '{"type":"result","product_kind":"minios-tool-result",'
            '"schema_version":1,"tool":"script2sb","operation":"create",'
            '"output":"/tmp/script.sb","compressed_size":4096,'
            '"uncompressed_size":8192,"entry_count":5,'
            '"sha256":"' + ('a' * 64) + '","compression":"zstd",'
            '"seed_directory":true}\n')
        process.stderr = io.StringIO('installer output\n')
        process.wait.return_value = 0
        phases = []
        logs = []
        def which(name):
            return {'script2sb': '/usr/bin/script2sb',
                    'pkexec': '/usr/bin/pkexec'}.get(name)
        with mock.patch.object(backend.shutil, 'which', side_effect=which), \
                mock.patch.object(backend.subprocess, 'Popen', return_value=process) as popen:
            success, result_value = backend.create_module_from_script(
                '/tmp/install.sh', '/tmp/script.sb', 'zstd', '/tmp/seed',
                phases.append, logs.append)
        self.assertTrue(success)
        self.assertEqual(result_value['output'], '/tmp/script.sb')
        self.assertEqual(
            phases, ['prepare', 'seed', 'script', 'capture', 'complete'])
        self.assertEqual(logs, ['installer output\n'])
        self.assertEqual(popen.call_args[0][0], [
            '/usr/bin/pkexec', '/usr/bin/script2sb', '--json', '--script',
            '/tmp/install.sh', '--name', '/tmp/script.sb', '--comp', 'zstd',
            '--directory', '/tmp/seed'])

    def test_seed_mismatch_is_rejected(self):
        value = {
            'type': 'result', 'product_kind': 'minios-tool-result',
            'schema_version': 1, 'tool': 'script2sb', 'operation': 'create',
            'output': '/tmp/script.sb', 'compressed_size': 4096,
            'uncompressed_size': 8192, 'entry_count': 5,
            'sha256': 'a' * 64, 'compression': 'zstd',
            'seed_directory': False,
        }
        with self.assertRaises(backend.ProtocolError):
            backend._script_result(value, True)


class ChrootLifecycleTests(unittest.TestCase):
    def _which(self, name):
        return {
            'chroot2sb': '/usr/bin/chroot2sb',
            'pkexec': '/usr/bin/pkexec',
        }.get(name)

    def test_prepare_uses_pkexec_and_returns_session(self):
        process = mock.Mock()
        process.stdout = io.StringIO(
            '{"event":"phase","phase":"prepare"}\n'
            '{"event":"phase","phase":"seed"}\n'
            '{"type":"result","product_kind":"minios-tool-result",'
            '"schema_version":1,"tool":"chroot2sb","operation":"prepare",'
            '"session_id":"session.ABC123","output":"/tmp/out.sb",'
            '"compression":"zstd","seed_directory":true}\n')
        process.wait.return_value = 0
        phases = []
        with mock.patch.object(backend.shutil, 'which', side_effect=self._which), \
                mock.patch.object(backend.subprocess, 'Popen', return_value=process) as popen:
            success, value = backend.prepare_chroot_session(
                '/seed', '/tmp/out.sb', 'zstd', phases.append)
        self.assertTrue(success)
        self.assertEqual(value['session_id'], 'session.ABC123')
        self.assertEqual(phases, ['prepare', 'seed'])
        self.assertEqual(popen.call_args[0][0], [
            '/usr/bin/pkexec', '/usr/bin/chroot2sb', 'prepare', '--json',
            '--name', '/tmp/out.sb', '--comp', 'zstd', '--directory', '/seed'])

    def test_prepare_protocol_error_drains_stdout_before_wait(self):
        output = 'not-json\n' + ('diagnostic output\n' * 4096)
        process = mock.Mock()
        process.stdout = io.StringIO(output)
        process.wait.return_value = 0
        with mock.patch.object(backend.shutil, 'which', side_effect=self._which), \
                mock.patch.object(backend.subprocess, 'Popen', return_value=process):
            success, message = backend.prepare_chroot_session(
                None, '/tmp/out.sb', 'zstd')
        self.assertFalse(success)
        self.assertIn('invalid JSON', message)
        self.assertEqual(process.stdout.tell(), len(output))

    def test_shell_argv_is_fixed_and_opaque(self):
        with mock.patch.object(backend.shutil, 'which', side_effect=self._which):
            success, argv = backend.chroot_shell_argv('session.ABC123')
        self.assertTrue(success)
        self.assertEqual(argv, [
            '/usr/bin/pkexec', '/usr/bin/chroot2sb', 'shell', 'session.ABC123'])
        success, message = backend.chroot_shell_argv('../bad')
        self.assertFalse(success)
        self.assertIn('Invalid', message)

    def test_finish_and_cancel_validate_results(self):
        process = mock.Mock()
        process.stdout = io.StringIO(
            '{"event":"phase","phase":"capture"}\n'
            '{"event":"phase","phase":"complete"}\n'
            '{"type":"result","product_kind":"minios-tool-result",'
            '"schema_version":1,"tool":"chroot2sb","operation":"finish",'
            '"output":"/tmp/out.sb","compressed_size":4096,'
            '"uncompressed_size":8192,"entry_count":5,'
            '"sha256":"' + ('a' * 64) + '","compression":"zstd",'
            '"seed_directory":false}\n')
        process.wait.return_value = 0
        with mock.patch.object(backend.shutil, 'which', side_effect=self._which), \
                mock.patch.object(backend.subprocess, 'Popen', return_value=process):
            success, value = backend.finish_chroot_session(
                'session.ABC123', 'zstd', False)
        self.assertTrue(success)
        self.assertEqual(value['operation'], 'finish')

        cancel_process = mock.Mock()
        cancel_process.communicate.return_value = (
            '{"type":"result","product_kind":"minios-tool-result",'
            '"schema_version":1,"tool":"chroot2sb","operation":"cancel",'
            '"session_id":"session.ABC123"}\n', '')
        cancel_process.returncode = 0
        with mock.patch.object(backend.shutil, 'which', side_effect=self._which), \
                mock.patch.object(backend.subprocess, 'Popen', return_value=cancel_process):
            success, value = backend.cancel_chroot_session('session.ABC123')
        self.assertTrue(success)
        self.assertEqual(value['session_id'], 'session.ABC123')


class CurrentSessionCaptureTests(unittest.TestCase):
    def _which(self, name):
        return {
            'savechanges': '/usr/bin/savechanges',
            'pkexec': '/usr/bin/pkexec',
        }.get(name)

    def _result_line(self):
        return json.dumps({
            'type': 'result', 'product_kind': 'minios-tool-result',
            'schema_version': 1, 'tool': 'savechanges',
            'operation': 'capture-module', 'output': '/tmp/session.sb',
            'compressed_size': 4096, 'uncompressed_size': 8192,
            'entry_count': 5, 'sha256': 'a' * 64,
            'profile': 'legacy', 'union_backend': 'overlayfs',
        }) + '\n'

    def test_capture_uses_pkexec_fixed_argv_and_phases(self):
        process = mock.Mock()
        process.stdout = io.StringIO(
            '{"event":"phase","phase":"prepare"}\n'
            '{"event":"phase","phase":"capture"}\n' + self._result_line())
        process.stderr = io.StringIO('capture output\n')
        process.wait.return_value = 0
        phases = []
        logs = []
        context = backend.new_capture_cancel_marker()
        marker = context[1]
        with mock.patch.object(backend.shutil, 'which', side_effect=self._which), \
                mock.patch.object(backend.subprocess, 'Popen', return_value=process) as popen:
            success, value = backend.capture_current_session(
                '/tmp/session.sb', 'zstd', phases.append, context, logs.append)
        self.assertTrue(success)
        self.assertEqual(value['output'], '/tmp/session.sb')
        self.assertEqual(phases, ['prepare', 'capture'])
        self.assertEqual(logs, ['capture output\n'])
        self.assertFalse(os.path.exists(context[0]))
        self.assertEqual(popen.call_args[0][0], [
            '/usr/bin/pkexec', '/usr/bin/savechanges', '--json',
            '--cancel-file', marker, '--comp', 'zstd', '/tmp/session.sb'])

    def test_cancel_marker_makes_failed_capture_cancelled(self):
        context = backend.new_capture_cancel_marker()
        marker = context[1]
        process = mock.Mock()
        process.stdout = io.StringIO(
            '{"event":"phase","phase":"prepare"}\n')
        process.stderr = io.StringIO('')

        def wait():
            backend.request_capture_cancel(marker)
            return 130

        process.wait.side_effect = wait
        with mock.patch.object(backend.shutil, 'which', side_effect=self._which), \
                mock.patch.object(backend.subprocess, 'Popen', return_value=process):
            success, message = backend.capture_current_session(
                '/tmp/session.sb', 'zstd', cancel_context=context)
        self.assertFalse(success)
        self.assertIs(message, backend.CAPTURE_CANCELLED)
        self.assertFalse(os.path.exists(context[0]))

    def test_cancel_marker_is_exclusive(self):
        context = backend.new_capture_cancel_marker()
        try:
            backend.request_capture_cancel(context[1])
            with self.assertRaises(FileExistsError):
                backend.request_capture_cancel(context[1])
        finally:
            backend.cleanup_capture_cancel_marker(context)


class PackageNameCompletionTests(unittest.TestCase):
    def test_query_uses_local_apt_cache_prefix_and_limits_results(self):
        completed = subprocess.CompletedProcess(
            ['/usr/bin/apt-cache', 'pkgnames', 'curl'], 0,
            stdout='curlftpfs\ncurl\ncurl\nother\nbad name\n', stderr='')
        with mock.patch.object(backend.shutil, 'which', return_value='/usr/bin/apt-cache'), \
                mock.patch.object(backend.subprocess, 'run', return_value=completed) as run:
            names = backend.query_package_names('curl', limit=2)
        self.assertEqual(names, ('curl', 'curlftpfs'))
        run.assert_called_once_with(
            ['/usr/bin/apt-cache', 'pkgnames', 'curl'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            universal_newlines=True, check=False)

    def test_short_prefix_does_not_spawn_apt_cache(self):
        with mock.patch.object(backend.subprocess, 'run') as run:
            self.assertEqual(backend.query_package_names('p'), ())
        run.assert_not_called()
