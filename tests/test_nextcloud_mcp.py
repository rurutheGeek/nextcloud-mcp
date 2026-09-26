"""Guard the Nextcloud MCP server.

nextcloud_mcp.py is standard-library only and forwards the caller's own
Authorization header to Nextcloud, so a bug here either lets an agent escape
its own Nextcloud permissions (path traversal, write-deny bypass) or crashes
the shared HTTP server for every user (an uncaught ToolError in a request
thread). Covers the pure helpers, the archive extraction guards, the tool
handlers against an in-memory fake Nextcloud, the tag API bridge, the
JSON-RPC dispatch, and real end-to-end HTTP round trips against fakes.
"""
import ast
import atexit
import base64
import http.client
import importlib.util
import io
import json
import posixpath
import re
import sys
import tarfile
import tempfile
import threading
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'nextcloud_mcp.py'

TAG_TOOLS = {'nextcloud_read_music_tags', 'nextcloud_search_musicbrainz',
             'nextcloud_write_music_tags'}

spec = importlib.util.spec_from_file_location('nextcloud_mcp', SOURCE)
mcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp)

_SCRATCH = tempfile.TemporaryDirectory(prefix='nextcloud-mcp-tests-')
atexit.register(_SCRATCH.cleanup)
_SCRATCH_PATH = Path(_SCRATCH.name)


def scratch_dir(prefix='test-'):
    """Return a new scratch directory, removed when the test process exits."""
    return Path(tempfile.mkdtemp(prefix=prefix, dir=_SCRATCH_PATH))


def read(relative):
    """Return a repository file as UTF-8 text."""
    return (ROOT / relative).read_text(encoding='utf-8')


def make_config(**overrides):
    env = {'NEXTCLOUD_MCP_TMP': str(scratch_dir('nc-mcp-'))}
    env.update(overrides)
    return mcp.Config(env)


class PathNormalizationTests(unittest.TestCase):
    def test_relative_paths_gain_a_leading_slash(self):
        self.assertEqual(mcp.normalize_dav_path('music/song.mp3'), '/music/song.mp3')

    def test_dot_segments_are_dropped_and_double_slashes_collapse(self):
        self.assertEqual(mcp.normalize_dav_path('//music/./a//b/'), '/music/a/b')

    def test_the_bare_root_normalizes_to_a_single_slash(self):
        self.assertEqual(mcp.normalize_dav_path('/'), '/')

    def test_parent_traversal_is_refused(self):
        with self.assertRaises(mcp.ToolError):
            mcp.normalize_dav_path('/music/../../etc/passwd')

    def test_backslashes_and_nul_are_refused(self):
        with self.assertRaises(mcp.ToolError):
            mcp.normalize_dav_path('music\\song.mp3')
        with self.assertRaises(mcp.ToolError):
            mcp.normalize_dav_path('music/\x00song.mp3')

    def test_empty_or_non_string_is_refused(self):
        with self.assertRaises(mcp.ToolError):
            mcp.normalize_dav_path('   ')
        with self.assertRaises(mcp.ToolError):
            mcp.normalize_dav_path(None)


class WriteGuardTests(unittest.TestCase):
    def test_the_conversion_originals_reject_writes(self):
        with self.assertRaises(mcp.ToolError):
            mcp.ensure_write_allowed('/music/Converted')
        with self.assertRaises(mcp.ToolError):
            mcp.ensure_write_allowed('/music/Converted/song.mp3')
        with self.assertRaises(mcp.ToolError):
            mcp.ensure_write_allowed('/MUSIC/CONVERTED/song.mp3')

    def test_other_paths_under_music_are_allowed(self):
        mcp.ensure_write_allowed('/music/YouTube/song.mp3')
        mcp.ensure_write_allowed('/music/ConvertedExtra/song.mp3')

    def test_ancestors_of_a_denied_prefix_are_refused(self):
        # Moving/deleting/overwriting an ancestor would take the protected
        # subtree with it, so /music and / must be refused too.
        for path in ('/music', '/MUSIC', '/music/', '/', ''):
            with self.assertRaises(mcp.ToolError, msg=path):
                mcp.ensure_write_allowed(path)

    def test_a_sibling_prefix_is_not_treated_as_an_ancestor(self):
        mcp.ensure_write_allowed('/mus')
        mcp.ensure_write_allowed('/music2')
        mcp.ensure_write_allowed('/music/ConvertedX')

    def test_ancestor_rule_follows_the_configured_deny_prefixes(self):
        config = make_config(NEXTCLOUD_MCP_WRITE_DENY='/private/inner')
        with self.assertRaises(mcp.ToolError):
            mcp.ensure_write_allowed('/private', config)
        mcp.ensure_write_allowed('/privateer', config)

    def test_the_converted_prefix_follows_the_configured_music_root(self):
        config = make_config(NEXTCLOUD_MCP_MUSIC_ROOT='/audio')
        with self.assertRaises(mcp.ToolError):
            mcp.ensure_write_allowed('/audio/Converted/song.mp3', config)
        mcp.ensure_write_allowed('/music/Converted/song.mp3', config)

    def test_extra_write_deny_prefixes_come_from_the_environment(self):
        config = make_config(NEXTCLOUD_MCP_WRITE_DENY='/Secrets, /private,')
        for path in ('/secrets', '/SEcrets/a.txt', '/private/x/y.txt',
                     '/private'):
            with self.assertRaises(mcp.ToolError, msg=path):
                mcp.ensure_write_allowed(path, config)
        mcp.ensure_write_allowed('/secretsx/a.txt', config)
        mcp.ensure_write_allowed('/privateer/a.txt', config)

    def test_tag_tools_are_limited_to_music_mp3(self):
        mcp.ensure_music_mp3('/music/a.mp3')
        for bad in ('/music/a.flac', '/library/a.mp3', '/Music/a.mp3.txt'):
            with self.assertRaises(mcp.ToolError, msg=bad):
                mcp.ensure_music_mp3(bad)
        mcp.ensure_music_mp3('/Music/A.MP3'.lower())


class MusicRootTests(unittest.TestCase):
    def test_a_custom_root_changes_what_ensure_music_mp3_accepts(self):
        mcp.ensure_music_mp3('/audio/a.mp3', '/audio')
        with self.assertRaises(mcp.ToolError):
            mcp.ensure_music_mp3('/music/a.mp3', '/audio')
        with self.assertRaises(mcp.ToolError):
            mcp.ensure_music_mp3('/audio/a.flac', '/audio')

    def test_the_relative_path_is_computed_from_the_music_root(self):
        self.assertEqual(mcp.music_relative_path('/music/Artist/a.mp3'), 'Artist/a.mp3')
        self.assertEqual(mcp.music_relative_path('/audio/a.mp3', '/audio/'), 'a.mp3')
        with self.assertRaises(mcp.ToolError):
            mcp.music_relative_path('/etc/a.mp3')
        with self.assertRaises(mcp.ToolError):
            mcp.music_relative_path('/audio/a.mp3')


class ArchiveHelperTests(unittest.TestCase):
    def test_archive_kind_recognizes_zip_and_tar_variants(self):
        self.assertEqual(mcp.archive_kind('a.zip'), 'zip')
        for name in ('a.tar', 'a.tar.gz', 'a.tgz', 'a.tar.bz2', 'a.tbz2', 'a.tar.xz', 'a.txz'):
            self.assertEqual(mcp.archive_kind(name), 'tar', name)
        with self.assertRaises(mcp.ToolError):
            mcp.archive_kind('a.rar')

    def test_archive_stem_strips_the_compound_suffix(self):
        self.assertEqual(mcp.archive_stem('/inbox/photos.tar.gz'), 'photos')
        self.assertEqual(mcp.archive_stem('archive.zip'), 'archive')
        self.assertEqual(mcp.archive_stem('backup.tar'), 'backup')

    def test_safe_member_name_accepts_a_plain_relative_path(self):
        self.assertEqual(mcp.safe_member_name('a/b/c.txt'), 'a/b/c.txt')
        self.assertEqual(mcp.safe_member_name('a\\b\\c.txt'), 'a/b/c.txt')

    def test_safe_member_name_refuses_escapes(self):
        for bad in ('/etc/passwd', '../evil', 'a/../../evil', '~root/.ssh', 'a/..'):
            with self.assertRaises(mcp.ToolError, msg=bad):
                mcp.safe_member_name(bad)

    def test_safe_member_name_refuses_empty_names(self):
        with self.assertRaises(mcp.ToolError):
            mcp.safe_member_name('')
        with self.assertRaises(mcp.ToolError):
            mcp.safe_member_name('./')

    def test_copy_limited_stops_at_the_byte_limit(self):
        source = io.BytesIO(b'x' * (2 * 1024 * 1024 + 10))
        target = io.BytesIO()
        with self.assertRaises(mcp.ToolError):
            mcp.copy_limited(source, target, limit=1024)

    def test_copy_limited_returns_the_total_written(self):
        source = io.BytesIO(b'hello world')
        target = io.BytesIO()
        total = mcp.copy_limited(source, target, limit=1024)
        self.assertEqual(total, len(b'hello world'))
        self.assertEqual(target.getvalue(), b'hello world')


class ConfigTests(unittest.TestCase):
    def test_defaults_match_the_documented_values(self):
        config = mcp.Config({})
        self.assertEqual(config.port, 5811)
        self.assertEqual(config.bind, '127.0.0.1')
        self.assertEqual(config.base_url, 'http://127.0.0.1:8080')
        self.assertEqual(config.tag_api_url, '')
        self.assertEqual(config.tag_api_token, '')
        self.assertFalse(config.tag_tools_enabled)
        self.assertEqual(config.music_root, '/music')
        self.assertEqual(config.write_deny_prefixes, ('/music/Converted',))
        self.assertEqual(str(config.tmp), '/var/tmp/nextcloud-mcp')
        self.assertFalse(config.read_only)

    def test_read_only_parses_common_truthy_and_falsy_spellings(self):
        for value in ('1', 'true', 'True', 'yes'):
            self.assertTrue(mcp.Config({'NEXTCLOUD_MCP_READ_ONLY': value}).read_only, value)
        for value in ('0', 'false', 'False', 'no', ''):
            self.assertFalse(mcp.Config({'NEXTCLOUD_MCP_READ_ONLY': value}).read_only, value)

    def test_base_url_and_tag_api_url_trailing_slashes_are_trimmed(self):
        config = mcp.Config({'NEXTCLOUD_MCP_BASE_URL': 'http://127.0.0.1:8080/',
                             'NEXTCLOUD_MCP_TAG_API_URL': 'http://127.0.0.1:5810/'})
        self.assertEqual(config.base_url, 'http://127.0.0.1:8080')
        self.assertEqual(config.tag_api_url, 'http://127.0.0.1:5810')

    def test_tag_tools_need_both_the_url_and_the_token(self):
        both = {'NEXTCLOUD_MCP_TAG_API_URL': 'http://127.0.0.1:5810',
                'NEXTCLOUD_MCP_TAG_API_TOKEN': 'token'}
        self.assertTrue(mcp.Config(both).tag_tools_enabled)
        self.assertFalse(mcp.Config({'NEXTCLOUD_MCP_TAG_API_URL': both['NEXTCLOUD_MCP_TAG_API_URL']}).tag_tools_enabled)
        self.assertFalse(mcp.Config({'NEXTCLOUD_MCP_TAG_API_TOKEN': 'token'}).tag_tools_enabled)

    def test_the_music_root_is_normalized(self):
        for raw, expected in (('music', '/music'), ('/music/', '/music'),
                              ('  /audio  ', '/audio'), ('', '/')):
            self.assertEqual(mcp.Config({'NEXTCLOUD_MCP_MUSIC_ROOT': raw}).music_root,
                             expected, raw)

    def test_write_deny_starts_with_the_configured_converted_prefix(self):
        config = mcp.Config({'NEXTCLOUD_MCP_MUSIC_ROOT': '/audio',
                             'NEXTCLOUD_MCP_WRITE_DENY': '/x, y/, ,/z///'})
        self.assertEqual(config.write_deny_prefixes,
                         ('/audio/Converted', '/x', '/y', '/z'))

    def test_allowed_origins_parse_comma_separated_values(self):
        config = mcp.Config({'NEXTCLOUD_MCP_ALLOWED_ORIGINS':
                             'https://a.example, http://b.example/ , '})
        self.assertEqual(config.allowed_origins,
                         ('https://a.example', 'http://b.example'))
        self.assertEqual(mcp.Config({}).allowed_origins, ())


def dav_multistatus(prefix, entries):
    """Build a minimal PROPFIND multistatus XML for the given entries.

    entries: iterable of (path, is_dir, size, etag, fileid, permissions).
    """
    parts = ['<?xml version="1.0"?>',
             '<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">']
    for path, is_dir, size, etag, fileid, permissions in entries:
        href = prefix + urllib.parse.quote(path if path != '/' else '')
        resourcetype = '<d:collection/>' if is_dir else ''
        parts.append(
            f'<d:response><d:href>{href}</d:href><d:propstat><d:prop>'
            f'<d:displayname>{posixpath.basename(path.rstrip("/")) or "root"}</d:displayname>'
            f'<d:getcontentlength>{size}</d:getcontentlength>'
            f'<d:getcontenttype>text/plain</d:getcontenttype>'
            f'<d:getetag>&quot;{etag}&quot;</d:getetag>'
            f'<d:getlastmodified>Mon, 01 Jan 2026 00:00:00 GMT</d:getlastmodified>'
            f'<d:resourcetype>{resourcetype}</d:resourcetype>'
            f'<oc:fileid>{fileid}</oc:fileid><oc:permissions>{permissions}</oc:permissions>'
            f'<oc:size>{size}</oc:size>'
            '</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>')
    parts.append('</d:multistatus>')
    return ''.join(parts).encode('utf-8')


class DavResponseParsingTests(unittest.TestCase):
    def test_a_file_entry_is_parsed_relative_to_the_dav_root(self):
        prefix = '/remote.php/dav/files/alice'
        body = dav_multistatus(prefix, [('/music/song.mp3', False, 123, 'e1', '42', 'RGDNVW')])
        tree = ET.fromstring(body)
        node = tree.find('{DAV:}response')
        entry = mcp.parse_dav_response(node, prefix)
        self.assertEqual(entry['path'], '/music/song.mp3')
        self.assertEqual(entry['size'], 123)
        self.assertEqual(entry['etag'], 'e1')
        self.assertEqual(entry['fileid'], '42')
        self.assertTrue(entry['writable'])
        self.assertFalse(entry['is_dir'])

    def test_the_root_href_becomes_a_single_slash(self):
        prefix = '/remote.php/dav/files/alice'
        body = dav_multistatus(prefix, [('/', True, 0, 'root-etag', '1', 'RGDNVCK')])
        tree = ET.fromstring(body)
        node = tree.find('{DAV:}response')
        entry = mcp.parse_dav_response(node, prefix)
        self.assertEqual(entry['path'], '/')
        self.assertTrue(entry['is_dir'])

    def test_a_read_only_entry_is_not_writable(self):
        prefix = '/remote.php/dav/files/alice'
        body = dav_multistatus(prefix, [('/shared/a.txt', False, 1, 'e', '9', 'RGDNV')])
        tree = ET.fromstring(body)
        node = tree.find('{DAV:}response')
        entry = mcp.parse_dav_response(node, prefix)
        self.assertFalse(entry['writable'])


class ArchiveRoundTripTests(unittest.TestCase):
    """extract_archive_file / build_zip_file against real archives on disk."""

    def setUp(self):
        self.config = make_config()
        self.workdir = scratch_dir('nc-mcp-archive-')

    def test_a_zip_extracts_its_files_and_reports_totals(self):
        archive = self.workdir / 'a.zip'
        with zipfile.ZipFile(archive, 'w') as zf:
            zf.writestr('a.txt', 'hello')
            zf.writestr('dir/b.txt', 'world!!')
        destination = self.workdir / 'out'
        destination.mkdir()
        extracted, total = mcp.extract_archive_file(archive, 'zip', destination, self.config)
        self.assertEqual(sorted(extracted), ['a.txt', 'dir/b.txt'])
        self.assertEqual(total['files'], 2)
        self.assertEqual(total['bytes'], len('hello') + len('world!!'))
        self.assertEqual((destination / 'a.txt').read_text(), 'hello')
        self.assertEqual((destination / 'dir/b.txt').read_text(), 'world!!')

    def test_a_tar_gz_extracts_its_files(self):
        archive = self.workdir / 'a.tar.gz'
        payload = self.workdir / 'payload.txt'
        payload.write_text('tar-content')
        with tarfile.open(archive, 'w:gz') as tf:
            tf.add(payload, arcname='nested/payload.txt')
        destination = self.workdir / 'out-tar'
        destination.mkdir()
        extracted, total = mcp.extract_archive_file(archive, 'tar', destination, self.config)
        self.assertEqual(extracted, ['nested/payload.txt'])
        self.assertEqual(total['bytes'], len('tar-content'))

    def test_a_zip_with_a_path_escape_member_is_refused_before_writing(self):
        archive = self.workdir / 'evil.zip'
        with zipfile.ZipFile(archive, 'w') as zf:
            zf.writestr('../../etc/passwd', 'pwned')
        destination = self.workdir / 'out-evil'
        destination.mkdir()
        with self.assertRaises(mcp.ToolError):
            mcp.extract_archive_file(archive, 'zip', destination, self.config)
        self.assertFalse((self.workdir / 'etc').exists())

    def test_a_zip_with_a_symlink_member_is_refused(self):
        archive = self.workdir / 'symlink.zip'
        with zipfile.ZipFile(archive, 'w') as zf:
            info = zipfile.ZipInfo('link')
            info.external_attr = (0o120777 << 16)
            zf.writestr(info, '/etc/passwd')
        destination = self.workdir / 'out-symlink'
        destination.mkdir()
        with self.assertRaises(mcp.ToolError):
            mcp.extract_archive_file(archive, 'zip', destination, self.config)

    def test_extraction_stops_once_the_file_count_limit_is_hit(self):
        archive = self.workdir / 'many.zip'
        with zipfile.ZipFile(archive, 'w') as zf:
            for index in range(5):
                zf.writestr(f'f{index}.txt', 'x')
        destination = self.workdir / 'out-many'
        destination.mkdir()
        config = make_config(NEXTCLOUD_MCP_MAX_EXTRACT_FILES='2')
        with self.assertRaises(mcp.ToolError):
            mcp.extract_archive_file(archive, 'zip', destination, config)

    def test_extraction_stops_once_the_byte_limit_is_hit(self):
        archive = self.workdir / 'big.zip'
        with zipfile.ZipFile(archive, 'w') as zf:
            zf.writestr('big.txt', 'x' * 1000)
        destination = self.workdir / 'out-big'
        destination.mkdir()
        config = make_config(NEXTCLOUD_MCP_MAX_EXTRACT_BYTES='10')
        with self.assertRaises(mcp.ToolError):
            mcp.extract_archive_file(archive, 'zip', destination, config)

    def test_build_zip_file_refuses_to_exceed_the_byte_limit(self):
        source = self.workdir / 'src.bin'
        source.write_bytes(b'x' * 1000)
        target = self.workdir / 'out.zip'
        with self.assertRaises(mcp.ToolError):
            mcp.build_zip_file([('src.bin', str(source))], str(target), max_bytes=10)

    def test_build_zip_file_within_the_limit_produces_a_readable_zip(self):
        source = self.workdir / 'src.bin'
        source.write_bytes(b'hello')
        target = self.workdir / 'out.zip'
        total = mcp.build_zip_file([('src.bin', str(source))], str(target), max_bytes=1000)
        self.assertEqual(total, 5)
        with zipfile.ZipFile(target) as zf:
            self.assertEqual(zf.read('src.bin'), b'hello')

    def test_the_member_guard_runs_for_every_zip_member_before_writing(self):
        archive = self.workdir / 'guard.zip'
        with zipfile.ZipFile(archive, 'w') as zf:
            zf.writestr('a.txt', 'A')
            zf.writestr('dir/b.txt', 'B')
        destination = self.workdir / 'out-guard'
        destination.mkdir()
        seen = []

        def guard(name):
            seen.append(name)
            if name == 'dir/b.txt':
                raise mcp.ToolError('blocked')

        with self.assertRaises(mcp.ToolError):
            mcp.extract_archive_file(archive, 'zip', destination, self.config, member_guard=guard)
        self.assertEqual(seen, ['a.txt', 'dir/b.txt'])
        self.assertFalse((destination / 'dir/b.txt').exists())

    def test_the_member_guard_is_applied_to_tar_members_too(self):
        archive = self.workdir / 'guard.tar'
        payload = self.workdir / 'payload.txt'
        payload.write_text('tar-content')
        with tarfile.open(archive, 'w') as tf:
            tf.add(payload, arcname='nested/payload.txt')
        destination = self.workdir / 'out-guard-tar'
        destination.mkdir()
        seen = []

        def guard(name):
            seen.append(name)
            raise mcp.ToolError('blocked')

        with self.assertRaises(mcp.ToolError):
            mcp.extract_archive_file(archive, 'tar', destination, self.config, member_guard=guard)
        self.assertEqual(seen, ['nested/payload.txt'])


class ArchiveSizePrecheckTests(unittest.TestCase):
    """list_archive/create_zip stat sizes before downloading anything."""

    def setUp(self):
        self.config = make_config(NEXTCLOUD_MCP_MAX_ZIP_BYTES='10')
        self.client = FakeNextcloudClient(files={
            '/': {'is_dir': True, 'writable': True, 'etag': 'root', 'fileid': '1'},
            '/big.zip': {'is_dir': False, 'data': b'x' * 11, 'writable': True, 'etag': 'z'},
            '/big.txt': {'is_dir': False, 'data': b'x' * 11, 'writable': True, 'etag': 't'},
        })
        self.downloads = []
        original = self.client.download_to

        def tracking(path, fileobj, timeout=None):
            self.downloads.append(path)
            return original(path, fileobj, timeout)

        self.client.download_to = tracking

    def test_list_archive_refuses_an_oversized_archive_before_downloading(self):
        with self.assertRaises(mcp.ToolError):
            mcp.tool_list_archive(self.client, self.config, {'path': '/big.zip'})
        self.assertEqual(self.downloads, [])

    def test_create_zip_refuses_an_oversized_source_before_downloading(self):
        with self.assertRaises(mcp.ToolError):
            mcp.tool_create_zip(self.client, self.config,
                                {'paths': ['/big.txt'], 'target': '/out.zip'})
        self.assertEqual(self.downloads, [])
        self.assertNotIn('/out.zip', self.client.files)

    def test_create_zip_allows_sizes_exactly_at_the_limit(self):
        self.client.files['/ok.txt'] = {'is_dir': False, 'data': b'x' * 10,
                                        'writable': True, 'etag': 'o'}
        result = mcp.tool_create_zip(self.client, self.config,
                                     {'paths': ['/ok.txt'], 'target': '/ok.zip'})
        self.assertEqual(result['source_bytes'], 10)
        self.assertEqual(self.downloads, ['/ok.txt'])


class ToolCatalogTests(unittest.TestCase):
    def tag_config(self):
        return make_config(NEXTCLOUD_MCP_TAG_API_URL='http://127.0.0.1:5810',
                           NEXTCLOUD_MCP_TAG_API_TOKEN='token')

    def test_every_write_tool_is_marked_read_write_in_its_annotations(self):
        for tool in mcp.TOOLS:
            definition = tool.definition()
            self.assertEqual(definition['annotations']['readOnlyHint'], not tool.write, tool.name)

    def test_destructive_tools_all_declare_the_hint(self):
        expected_destructive = {'nextcloud_write_file', 'nextcloud_move_file', 'nextcloud_delete_file',
                                'nextcloud_write_music_tags', 'nextcloud_create_zip',
                                'nextcloud_extract_archive'}
        actual = {tool.name for tool in mcp.TOOLS if tool.destructive}
        self.assertEqual(actual, expected_destructive)
        for tool in mcp.TOOLS:
            if tool.destructive:
                self.assertTrue(tool.definition()['annotations']['destructiveHint'], tool.name)

    def test_read_only_mode_hides_every_write_tool(self):
        config = make_config(NEXTCLOUD_MCP_READ_ONLY='1',
                             NEXTCLOUD_MCP_TAG_API_URL='http://127.0.0.1:5810',
                             NEXTCLOUD_MCP_TAG_API_TOKEN='token')
        names = {tool['name'] for tool in mcp.available_tools(config)}
        for tool in mcp.TOOLS:
            self.assertEqual(tool.name in names, not tool.write, tool.name)

    def test_the_full_catalog_is_listed_when_the_tag_api_is_configured(self):
        self.assertEqual(len(mcp.available_tools(self.tag_config())), len(mcp.TOOLS))

    def test_the_tag_tools_are_hidden_while_the_tag_api_is_not_configured(self):
        names = {tool['name'] for tool in mcp.available_tools(make_config())}
        self.assertEqual(len(mcp.TOOLS) - len(TAG_TOOLS), len(names))
        for name in TAG_TOOLS:
            self.assertNotIn(name, names)

    def test_a_url_without_a_token_still_hides_the_tag_tools(self):
        config = make_config(NEXTCLOUD_MCP_TAG_API_URL='http://127.0.0.1:5810')
        names = {tool['name'] for tool in mcp.available_tools(config)}
        for name in TAG_TOOLS:
            self.assertNotIn(name, names)

    def test_every_tool_schema_is_a_closed_object(self):
        for tool in mcp.TOOLS:
            self.assertEqual(tool.schema['type'], 'object', tool.name)
            self.assertFalse(tool.schema['additionalProperties'], tool.name)
            for required in tool.schema['required']:
                self.assertIn(required, tool.schema['properties'], tool.name)


class FakeNextcloudClient:
    """In-memory stand-in for NextcloudClient, keyed by normalized DAV path."""

    def __init__(self, user_id='alice', files=None):
        self._user_id = user_id
        # path -> {'data': bytes, 'is_dir': bool, 'writable': bool, 'etag': str, 'fileid': str}
        self.files = files if files is not None else {}
        self.files.setdefault('/', {'is_dir': True, 'writable': True, 'etag': 'root', 'fileid': '1'})
        self.deleted = []

    def whoami(self):
        return {'id': self._user_id, 'displayname': self._user_id}

    @property
    def user_id(self):
        return self._user_id

    def _entry(self, path):
        record = self.files[path]
        return {
            'path': path, 'name': posixpath.basename(path.rstrip('/')) or 'root',
            'is_dir': record['is_dir'], 'size': None if record['is_dir'] else len(record.get('data', b'')),
            'etag': record.get('etag', ''), 'fileid': record.get('fileid', ''),
            'permissions': 'RGDNVW' if record.get('writable', True) else 'RGDNV',
            'writable': record.get('writable', True),
            'last_modified': '', 'content_type': record.get('content_type', ''),
        }

    def stat(self, path):
        if path not in self.files:
            raise mcp.NextcloudError(404, f'not found: {path}')
        return self._entry(path)

    def list_files(self, path):
        if path not in self.files or not self.files[path]['is_dir']:
            raise mcp.NextcloudError(404, f'not found: {path}')
        prefix = path if path.endswith('/') else path + '/'
        entries = [self._entry(path)]
        for candidate in self.files:
            if candidate != path and candidate.startswith(prefix) and \
               '/' not in candidate[len(prefix):].rstrip('/'):
                entries.append(self._entry(candidate))
        return entries

    def walk(self, path):
        prefix = path if path.endswith('/') else path + '/'
        return [self._entry(candidate) for candidate, record in self.files.items()
                if candidate.startswith(prefix) and not record['is_dir']]

    def read_file(self, path, max_bytes):
        record = self.files.get(path)
        if record is None or record['is_dir']:
            raise mcp.NextcloudError(404, f'not found: {path}')
        data = record['data']
        truncated = len(data) > max_bytes
        return (data[:max_bytes] if truncated else data, truncated,
                record.get('content_type', ''), record.get('etag', ''))

    def download_to(self, path, fileobj, timeout=None):
        data, _truncated, _ct, _etag = self.read_file(path, len(self.files[path]['data']))
        fileobj.write(data)

    def put_file(self, path, data, size=None, if_match=None, create_only=False):
        if create_only and path in self.files:
            raise mcp.NextcloudError(412, f'already exists: {path}')
        if hasattr(data, 'read'):
            data = data.read()
        etag = f'etag-{len(self.files)}'
        self.files[path] = {'data': data, 'is_dir': False, 'writable': True, 'etag': etag}
        return {'etag': etag}

    def create_folder(self, path):
        if path in self.files:
            return {'created': False}
        self.files[path] = {'is_dir': True, 'writable': True, 'etag': 'dir'}
        return {'created': True}

    def move(self, path, destination, overwrite=True):
        if not overwrite and destination in self.files:
            raise mcp.NextcloudError(412, 'exists')
        self.files[destination] = self.files.pop(path)
        return {'status': 201}

    def copy(self, path, destination, overwrite=True):
        if not overwrite and destination in self.files:
            raise mcp.NextcloudError(412, 'exists')
        self.files[destination] = dict(self.files[path])
        return {'status': 201}

    def delete(self, path):
        self.deleted.append(path)
        del self.files[path]
        return {'status': 204}

    def search(self, term, limit=20):
        return [{'title': posixpath.basename(path), 'path': path} for path in self.files if term in path][:limit]


def music_client(root='/music'):
    return FakeNextcloudClient(files={
        '/': {'is_dir': True, 'writable': True, 'etag': 'root'},
        root: {'is_dir': True, 'writable': True, 'etag': 'm'},
        root + '/song.mp3': {'is_dir': False, 'data': b'x', 'writable': True, 'etag': 's'},
    })


class MandatoryWhoamiTests(unittest.TestCase):
    """Every tools/call verifies the caller with Nextcloud before running."""

    class RejectingClient(FakeNextcloudClient):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.list_files_calls = 0

        def whoami(self):
            raise mcp.NextcloudError(401, 'credentials rejected')

        def list_files(self, path):
            self.list_files_calls += 1
            return super().list_files(path)

    def rejecting_client(self):
        return self.RejectingClient(files={
            '/': {'is_dir': True, 'writable': True, 'etag': 'root'},
            '/notes.txt': {'is_dir': False, 'data': b'hello', 'writable': True, 'etag': 'e'},
        })

    def test_a_rejected_credential_turns_into_a_tool_error(self):
        result = mcp.call_tool('nextcloud_list_files', {'path': '/'},
                               self.rejecting_client(), make_config())
        self.assertTrue(result['isError'])
        self.assertIn('credentials rejected', result['content'][0]['text'])

    def test_the_handler_never_runs_when_whoami_is_rejected(self):
        client = self.rejecting_client()
        result = mcp.call_tool('nextcloud_list_files', {'path': '/'}, client, make_config())
        self.assertTrue(result['isError'])
        self.assertEqual(client.list_files_calls, 0)

    def test_a_rejected_credential_over_jsonrpc_is_a_tool_error(self):
        response = mcp.handle_message(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
             'params': {'name': 'nextcloud_list_files', 'arguments': {'path': '/'}}},
            self.rejecting_client(), make_config())
        self.assertTrue(response['result']['isError'])
        self.assertNotIn('Internal error', response['result']['content'][0]['text'])


class ToolHandlerTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config()
        self.client = FakeNextcloudClient(files={
            '/': {'is_dir': True, 'writable': True, 'etag': 'root', 'fileid': '1'},
            '/notes.txt': {'is_dir': False, 'data': b'hello', 'writable': True,
                           'etag': 'e1', 'content_type': 'text/plain'},
            '/music': {'is_dir': True, 'writable': True, 'etag': 'e2'},
            '/music/song.mp3': {'is_dir': False, 'data': b'id3', 'writable': True, 'etag': 'e3'},
        })

    def test_whoami_returns_the_forwarded_identity(self):
        self.assertEqual(mcp.tool_whoami(self.client, self.config, {}), {'id': 'alice', 'displayname': 'alice'})

    def test_list_files_summarizes_direct_children_only(self):
        result = mcp.tool_list_files(self.client, self.config, {'path': '/'})
        names = {entry['name'] for entry in result['entries']}
        self.assertEqual(names, {'root', 'notes.txt', 'music'})

    def test_read_file_auto_detects_text_by_extension(self):
        result = mcp.tool_read_file(self.client, self.config, {'path': '/notes.txt'})
        self.assertEqual(result['encoding'], 'text')
        self.assertEqual(result['content'], 'hello')

    def test_read_file_base64_encoding_round_trips_binary(self):
        result = mcp.tool_read_file(self.client, self.config, {'path': '/notes.txt', 'encoding': 'base64'})
        self.assertEqual(base64.b64decode(result['content']), b'hello')

    def test_read_file_rejects_an_unknown_encoding(self):
        with self.assertRaises(mcp.ToolError):
            mcp.tool_read_file(self.client, self.config, {'path': '/notes.txt', 'encoding': 'utf-16'})

    def test_read_file_clamps_a_negative_max_bytes_to_one_byte(self):
        result = mcp.tool_read_file(self.client, self.config,
                                    {'path': '/notes.txt', 'max_bytes': -5})
        self.assertEqual(result['bytes'], 1)
        self.assertTrue(result['truncated'])

    def test_read_file_clamps_an_oversized_max_bytes_to_the_server_limit(self):
        config = make_config(NEXTCLOUD_MCP_MAX_READ_BYTES='2')
        result = mcp.tool_read_file(self.client, config,
                                    {'path': '/notes.txt', 'max_bytes': 999})
        self.assertEqual(result['bytes'], 2)

    def test_read_file_rejects_a_non_integer_max_bytes(self):
        for value in ('many', 1.5, [], {}):
            with self.assertRaises(mcp.ToolError, msg=repr(value)):
                mcp.tool_read_file(self.client, self.config,
                                   {'path': '/notes.txt', 'max_bytes': value})

    def test_a_non_integer_max_bytes_over_jsonrpc_is_not_an_internal_error(self):
        response = mcp.handle_message(
            {'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call',
             'params': {'name': 'nextcloud_read_file',
                        'arguments': {'path': '/notes.txt', 'max_bytes': 'many'}}},
            self.client, self.config)
        self.assertTrue(response['result']['isError'])
        text = response['result']['content'][0]['text']
        self.assertIn('max_bytes', text)
        self.assertNotIn('Internal error', text)

    def test_write_file_stores_text_content(self):
        result = mcp.tool_write_file(self.client, self.config,
                                     {'path': '/new.txt', 'content': 'hi there'})
        self.assertEqual(result['bytes'], len('hi there'))
        self.assertEqual(self.client.files['/new.txt']['data'], b'hi there')

    def test_write_file_with_overwrite_false_refuses_an_existing_path(self):
        with self.assertRaises(mcp.ToolError):
            mcp.tool_write_file(self.client, self.config,
                                {'path': '/notes.txt', 'content': 'x', 'overwrite': False})

    def test_write_file_refuses_the_conversion_originals(self):
        self.client.files['/music/Converted'] = {'is_dir': True, 'writable': True, 'etag': 'c'}
        with self.assertRaises(mcp.ToolError):
            mcp.tool_write_file(self.client, self.config,
                                {'path': '/music/Converted/song.mp3', 'content': 'x'})

    def test_write_file_honours_the_environment_write_deny(self):
        config = make_config(NEXTCLOUD_MCP_WRITE_DENY='/private')
        with self.assertRaises(mcp.ToolError):
            mcp.tool_write_file(self.client, config,
                                {'path': '/private/x.txt', 'content': 'x'})

    def test_write_file_follows_a_custom_music_root_for_the_originals(self):
        config = make_config(NEXTCLOUD_MCP_MUSIC_ROOT='/audio')
        self.client.files['/audio'] = {'is_dir': True, 'writable': True, 'etag': 'a'}
        with self.assertRaises(mcp.ToolError):
            mcp.tool_write_file(self.client, config,
                                {'path': '/audio/Converted/song.mp3', 'content': 'x'})

    def test_write_file_enforces_the_size_limit(self):
        config = make_config(NEXTCLOUD_MCP_MAX_WRITE_BYTES='4')
        with self.assertRaises(mcp.ToolError):
            mcp.tool_write_file(self.client, config, {'path': '/new.txt', 'content': 'hello'})

    def test_write_file_rejects_invalid_base64(self):
        with self.assertRaises(mcp.ToolError):
            mcp.tool_write_file(self.client, self.config,
                                {'path': '/new.bin', 'content': 'not-base64!!', 'encoding': 'base64'})

    def test_move_file_refuses_to_move_the_root(self):
        with self.assertRaises(mcp.ToolError):
            mcp.tool_move_file(self.client, self.config, {'path': '/', 'destination': '/elsewhere'})

    def test_move_file_renames_and_keeps_the_record(self):
        result = mcp.tool_move_file(self.client, self.config,
                                    {'path': '/notes.txt', 'destination': '/renamed.txt'})
        self.assertEqual(result['destination'], '/renamed.txt')
        self.assertNotIn('/notes.txt', self.client.files)
        self.assertIn('/renamed.txt', self.client.files)

    def test_copy_file_refuses_a_denied_source(self):
        self.client.files['/music/Converted'] = {'is_dir': True, 'writable': True, 'etag': 'c'}
        self.client.files['/music/Converted/song.mp3'] = {'is_dir': False, 'data': b'x',
                                                          'writable': True, 'etag': 's'}
        with self.assertRaises(mcp.ToolError):
            mcp.tool_copy_file(self.client, self.config,
                               {'path': '/music/Converted/song.mp3', 'destination': '/copy.mp3'})
        self.assertNotIn('/copy.mp3', self.client.files)

    def test_move_file_refuses_a_denied_source(self):
        self.client.files['/music/Converted'] = {'is_dir': True, 'writable': True, 'etag': 'c'}
        self.client.files['/music/Converted/song.mp3'] = {'is_dir': False, 'data': b'x',
                                                          'writable': True, 'etag': 's'}
        with self.assertRaises(mcp.ToolError):
            mcp.tool_move_file(self.client, self.config,
                               {'path': '/music/Converted/song.mp3', 'destination': '/moved.mp3'})
        self.assertIn('/music/Converted/song.mp3', self.client.files)

    def test_delete_file_refuses_an_ancestor_of_a_denied_prefix(self):
        with self.assertRaises(mcp.ToolError):
            mcp.tool_delete_file(self.client, self.config, {'path': '/music'})
        self.assertEqual(self.client.deleted, [])

    def test_move_file_refuses_an_ancestor_of_a_denied_prefix(self):
        with self.assertRaises(mcp.ToolError):
            mcp.tool_move_file(self.client, self.config, {'path': '/music', 'destination': '/x'})
        self.assertIn('/music', self.client.files)

    def test_delete_file_refuses_the_root_and_moves_others_to_trash(self):
        with self.assertRaises(mcp.ToolError):
            mcp.tool_delete_file(self.client, self.config, {'path': '/'})
        mcp.tool_delete_file(self.client, self.config, {'path': '/notes.txt'})
        self.assertIn('/notes.txt', self.client.deleted)

    def test_read_music_tags_is_limited_to_music_mp3(self):
        with self.assertRaises(mcp.ToolError):
            mcp.tool_read_music_tags(self.client, self.config, {'path': '/notes.txt'})

    def test_write_music_tags_refuses_when_the_file_is_not_writable(self):
        self.client.files['/music/song.mp3']['writable'] = False
        with self.assertRaises(mcp.ToolError):
            mcp.tool_write_music_tags(self.client, self.config,
                                      {'path': '/music/song.mp3', 'tags': {'title': 'x'}})

    def test_write_music_tags_requires_at_least_one_tag(self):
        with self.assertRaises(mcp.ToolError):
            mcp.tool_write_music_tags(self.client, self.config, {'path': '/music/song.mp3', 'tags': {}})

    def test_create_zip_then_extract_archive_round_trips_the_files(self):
        self.client.files['/docs'] = {'is_dir': True, 'writable': True, 'etag': 'd'}
        self.client.files['/docs/a.txt'] = {'is_dir': False, 'data': b'A', 'writable': True, 'etag': 'a'}
        self.client.files['/docs/b.txt'] = {'is_dir': False, 'data': b'B', 'writable': True, 'etag': 'b'}
        zip_result = mcp.tool_create_zip(self.client, self.config,
                                         {'paths': ['/docs'], 'target': '/docs.zip'})
        self.assertEqual(zip_result['entries'], 2)
        self.assertIn('/docs.zip', self.client.files)

        extract_result = mcp.tool_extract_archive(self.client, self.config,
                                                  {'path': '/docs.zip', 'target': '/restored'})
        self.assertEqual(extract_result['files'], 2)
        self.assertEqual(self.client.files['/restored/docs/a.txt']['data'], b'A')
        self.assertEqual(self.client.files['/restored/docs/b.txt']['data'], b'B')

    def test_extract_archive_refuses_to_target_the_root(self):
        self.client.files['/a.zip'] = {'is_dir': False, 'data': b'', 'writable': True, 'etag': 'z'}
        with self.assertRaises(mcp.ToolError):
            mcp.tool_extract_archive(self.client, self.config, {'path': '/a.zip', 'target': '/'})

    def test_extract_archive_checks_every_member_final_path(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('a.txt', 'A')
            archive.writestr('sub/b.txt', 'B')
        self.client.files['/docs.zip'] = {'is_dir': False, 'data': buffer.getvalue(),
                                          'writable': True, 'etag': 'z'}
        seen = []
        original = mcp.ensure_write_allowed

        def spy(path, config=None):
            seen.append(path)
            original(path, config)

        with mock.patch.object(mcp, 'ensure_write_allowed', side_effect=spy):
            result = mcp.tool_extract_archive(self.client, self.config,
                                              {'path': '/docs.zip', 'target': '/restored'})
        self.assertEqual(result['files'], 2)
        self.assertIn('/restored', seen)
        self.assertIn('/restored/a.txt', seen)
        self.assertIn('/restored/sub/b.txt', seen)

    def test_extract_archive_refuses_a_denied_archive_it_would_remove(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('a.txt', 'A')
        self.client.files['/music/Converted'] = {'is_dir': True, 'writable': True, 'etag': 'c'}
        self.client.files['/music/Converted/a.zip'] = {'is_dir': False, 'data': buffer.getvalue(),
                                                       'writable': True, 'etag': 'z'}
        with self.assertRaises(mcp.ToolError):
            mcp.tool_extract_archive(self.client, self.config,
                                     {'path': '/music/Converted/a.zip', 'target': '/restored',
                                      'remove_archive': True})
        self.assertEqual(self.client.deleted, [])


class TagToolGatingTests(unittest.TestCase):
    """Without both tag API settings the tools are hidden and refuse calls."""

    def setUp(self):
        self.client = music_client()
        self.config = make_config()
        self.calls = {
            'nextcloud_read_music_tags':
                lambda: mcp.tool_read_music_tags(self.client, self.config, {'path': '/music/song.mp3'}),
            'nextcloud_search_musicbrainz':
                lambda: mcp.tool_search_musicbrainz(self.client, self.config, {'artist': 'x'}),
            'nextcloud_write_music_tags':
                lambda: mcp.tool_write_music_tags(self.client, self.config,
                                                  {'path': '/music/song.mp3', 'tags': {'title': 'x'}}),
        }

    def test_every_tag_tool_reports_the_missing_settings(self):
        for name, call in self.calls.items():
            with self.assertRaises(mcp.ToolError, msg=name) as caught:
                call()
            message = str(caught.exception)
            self.assertIn('NEXTCLOUD_MCP_TAG_API_URL', message, name)
            self.assertIn('NEXTCLOUD_MCP_TAG_API_TOKEN', message, name)

    def test_calling_a_disabled_tag_tool_over_jsonrpc_is_a_tool_error(self):
        for name in sorted(TAG_TOOLS):
            response = mcp.handle_message(
                {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                 'params': {'name': name, 'arguments': {'path': '/music/song.mp3', 'artist': 'x',
                                                         'tags': {'title': 'x'}}}},
                self.client, self.config)
            self.assertTrue(response['result']['isError'], name)

    def test_a_token_without_a_url_is_also_refused(self):
        config = make_config(NEXTCLOUD_MCP_TAG_API_TOKEN='token')
        with self.assertRaises(mcp.ToolError):
            mcp.tool_read_music_tags(self.client, config, {'path': '/music/song.mp3'})


class TagApiBridgeTests(unittest.TestCase):
    class Handler(BaseHTTPRequestHandler):
        requests = []

        def log_message(self, *args):
            pass

        def _reply(self):
            type(self).requests.append(self.path)
            if self.headers.get('Authorization') != 'Bearer secret-token':
                self.send_response(401)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            length = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(length) if length else b''
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            # GET carries the path in the query, POST in the JSON body. The MCP
            # server must send the library-relative path (no leading slash).
            if self.command == 'POST':
                try:
                    received = json.loads(body or b'{}').get('path')
                except ValueError:
                    received = None
            else:
                received = (query.get('path') or [''])[0]
            payload = json.dumps({'ok': True, 'received_path': received,
                                  'body': body.decode('utf-8', 'replace')}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            self._reply()

        def do_POST(self):
            self._reply()

    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.Handler.requests = []
        port = self.server.server_address[1]
        self.config = make_config(NEXTCLOUD_MCP_TAG_API_URL=f'http://127.0.0.1:{port}',
                                  NEXTCLOUD_MCP_TAG_API_TOKEN='secret-token')
        self.client = music_client()

    def test_read_music_tags_stats_the_file_before_calling_the_tag_api(self):
        with self.assertRaises(mcp.NextcloudError):
            mcp.tool_read_music_tags(self.client, self.config, {'path': '/music/missing.mp3'})
        self.assertEqual(self.Handler.requests, [])

    def test_read_music_tags_reaches_the_bridged_tag_api(self):
        result = mcp.tool_read_music_tags(self.client, self.config, {'path': '/music/song.mp3'})
        self.assertTrue(result['ok'])

    def test_read_music_tags_sends_the_library_relative_path(self):
        # The tag API works inside its own music root, so the absolute
        # Nextcloud path must become a library-relative one before the call.
        result = mcp.tool_read_music_tags(self.client, self.config, {'path': '/music/song.mp3'})
        self.assertEqual(result['received_path'], 'song.mp3')

    def test_read_music_tags_returns_the_absolute_path_not_the_tag_apis_one(self):
        # Regression: the tag API answers with a path relative to its music
        # root, which used to leak through unchanged instead of matching every
        # other tool's absolute-path convention.
        result = mcp.tool_read_music_tags(self.client, self.config, {'path': '/music/song.mp3'})
        self.assertEqual(result['path'], '/music/song.mp3')

    def test_a_custom_music_root_is_stripped_before_the_call(self):
        config = make_config(NEXTCLOUD_MCP_MUSIC_ROOT='/audio',
                             NEXTCLOUD_MCP_TAG_API_URL=self.config.tag_api_url,
                             NEXTCLOUD_MCP_TAG_API_TOKEN='secret-token')
        result = mcp.tool_read_music_tags(music_client('/audio'), config,
                                          {'path': '/audio/song.mp3'})
        self.assertEqual(result['received_path'], 'song.mp3')

    def test_write_music_tags_reaches_the_bridged_tag_api_after_the_permission_check(self):
        result = mcp.tool_write_music_tags(self.client, self.config,
                                           {'path': '/music/song.mp3', 'tags': {'title': 'New'}})
        self.assertTrue(result['ok'])
        self.assertEqual(result['received_path'], 'song.mp3')

    def test_a_missing_token_is_refused_before_any_network_call(self):
        config = make_config(NEXTCLOUD_MCP_TAG_API_URL=self.config.tag_api_url)
        with self.assertRaises(mcp.ToolError):
            mcp.tool_read_music_tags(self.client, config, {'path': '/music/song.mp3'})


class JsonRpcDispatchTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config()
        self.client = FakeNextcloudClient()

    def test_a_notification_without_an_id_returns_nothing(self):
        self.assertIsNone(mcp.handle_message({'jsonrpc': '2.0', 'method': 'ping'}, self.client, self.config))

    def test_a_message_with_an_id_but_no_method_is_an_invalid_request(self):
        response = mcp.handle_message({'jsonrpc': '2.0', 'id': 1}, self.client, self.config)
        self.assertEqual(response['error']['code'], -32600)
        self.assertEqual(response['id'], 1)

    def test_a_message_with_nor_id_nor_method_is_treated_as_a_notification(self):
        self.assertIsNone(mcp.handle_message({'jsonrpc': '2.0', 'params': {}},
                                             self.client, self.config))

    def test_initialize_echoes_a_supported_protocol_version(self):
        response = mcp.handle_message(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
             'params': {'protocolVersion': '2025-03-26'}}, self.client, self.config)
        self.assertEqual(response['result']['protocolVersion'], '2025-03-26')

    def test_initialize_falls_back_to_the_default_for_an_unknown_version(self):
        response = mcp.handle_message(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
             'params': {'protocolVersion': '1999-01-01'}}, self.client, self.config)
        self.assertEqual(response['result']['protocolVersion'], mcp.DEFAULT_PROTOCOL)

    def test_ping_returns_an_empty_result(self):
        response = mcp.handle_message({'jsonrpc': '2.0', 'id': 2, 'method': 'ping'}, self.client, self.config)
        self.assertEqual(response['result'], {})

    def test_tools_list_matches_available_tools(self):
        response = mcp.handle_message({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'},
                                      self.client, self.config)
        self.assertEqual(response['result']['tools'], mcp.available_tools(self.config))

    def test_an_unknown_method_is_a_jsonrpc_error_not_a_crash(self):
        response = mcp.handle_message({'jsonrpc': '2.0', 'id': 4, 'method': 'nope'},
                                      self.client, self.config)
        self.assertEqual(response['error']['code'], -32601)

    def test_calling_an_unknown_tool_reports_a_tool_error_without_raising(self):
        response = mcp.handle_message(
            {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call',
             'params': {'name': 'nextcloud_does_not_exist', 'arguments': {}}},
            self.client, self.config)
        self.assertTrue(response['result']['isError'])

    def test_a_write_tool_is_refused_by_a_read_only_server_without_raising(self):
        config = make_config(NEXTCLOUD_MCP_READ_ONLY='1')
        response = mcp.handle_message(
            {'jsonrpc': '2.0', 'id': 6, 'method': 'tools/call',
             'params': {'name': 'nextcloud_write_file',
                        'arguments': {'path': '/a.txt', 'content': 'x'}}},
            self.client, config)
        self.assertTrue(response['result']['isError'])

    def test_non_dict_arguments_are_reported_without_raising(self):
        response = mcp.handle_message(
            {'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call',
             'params': {'name': 'nextcloud_whoami', 'arguments': ['not', 'a', 'dict']}},
            self.client, self.config)
        self.assertTrue(response['result']['isError'])

    def test_a_successful_tool_call_is_not_marked_as_an_error(self):
        response = mcp.handle_message(
            {'jsonrpc': '2.0', 'id': 8, 'method': 'tools/call',
             'params': {'name': 'nextcloud_whoami', 'arguments': {}}},
            self.client, self.config)
        self.assertFalse(response['result']['isError'])


class HealthPayloadTests(unittest.TestCase):
    def test_the_health_payload_uses_the_documented_keys(self):
        payload = mcp.health_payload(make_config())
        self.assertEqual(set(payload), {'status', 'server', 'version',
                                        'read_only', 'tag_tools'})
        self.assertEqual(payload['status'], 'ok')
        self.assertEqual(payload['version'], mcp.VERSION)
        self.assertFalse(payload['read_only'])
        self.assertFalse(payload['tag_tools'])

    def test_tag_tools_is_true_when_both_settings_are_present(self):
        payload = mcp.health_payload(make_config(
            NEXTCLOUD_MCP_TAG_API_URL='http://127.0.0.1:5810',
            NEXTCLOUD_MCP_TAG_API_TOKEN='token',
            NEXTCLOUD_MCP_READ_ONLY='1'))
        self.assertTrue(payload['tag_tools'])
        self.assertTrue(payload['read_only'])


class FakeNextcloudServer(BaseHTTPRequestHandler):
    """A tiny stand-in for Nextcloud's whoami OCS endpoint, used over real HTTP."""

    protocol_version = 'HTTP/1.1'
    fail_whoami = False
    unreachable = False

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith('/ocs/v2.php/cloud/user'):
            if self.fail_whoami:
                # Real Nextcloud rejects bad Basic Auth at the HTTP layer
                # (genuine 401), not with an OCS-level meta.statuscode.
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="Nextcloud"')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            self._ocs(200, {'id': 'alice', 'displayname': 'Alice'})
            return
        self.send_response(404)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _ocs(self, statuscode, data):
        payload = json.dumps({'ocs': {'meta': {'statuscode': statuscode}, 'data': data}}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class HttpEndToEndTests(unittest.TestCase):
    """Drive the real McpServer over a socket against a fake Nextcloud."""

    def setUp(self):
        FakeNextcloudServer.fail_whoami = False
        self.nc_server = ThreadingHTTPServer(('127.0.0.1', 0), FakeNextcloudServer)
        self.nc_thread = threading.Thread(target=self.nc_server.serve_forever, daemon=True)
        self.nc_thread.start()
        self.addCleanup(self.nc_server.shutdown)
        self.addCleanup(self.nc_server.server_close)

        self.config = make_config(
            NEXTCLOUD_MCP_BASE_URL=f'http://127.0.0.1:{self.nc_server.server_address[1]}')
        self.mcp_server = mcp.build_server(('127.0.0.1', 0), self.config)
        self.mcp_thread = threading.Thread(target=self.mcp_server.serve_forever, daemon=True)
        self.mcp_thread.start()
        self.addCleanup(self.mcp_server.shutdown)
        self.addCleanup(self.mcp_server.server_close)
        self.port = self.mcp_server.server_address[1]

    def start_mcp_server(self, **env):
        """Start a second MCP server with its own environment overrides."""
        config = make_config(
            NEXTCLOUD_MCP_BASE_URL=f'http://127.0.0.1:{self.nc_server.server_address[1]}', **env)
        server = mcp.build_server(('127.0.0.1', 0), config)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return server.server_address[1]

    def get(self, route, port=None):
        connection = http.client.HTTPConnection('127.0.0.1', port or self.port, timeout=5)
        try:
            connection.request('GET', route)
            response = connection.getresponse()
            body = response.read()
            return response.status, (json.loads(body) if body else None)
        finally:
            connection.close()

    def post(self, message, authorization='Basic dGVzdDp0ZXN0', headers=None, port=None):
        connection = http.client.HTTPConnection('127.0.0.1', port or self.port, timeout=5)
        try:
            request_headers = {'Content-Type': 'application/json'}
            if authorization is not None:
                request_headers['Authorization'] = authorization
            request_headers.update(headers or {})
            connection.request('POST', '/mcp', body=json.dumps(message), headers=request_headers)
            response = connection.getresponse()
            body = response.read()
            return response.status, (json.loads(body) if body else None)
        finally:
            connection.close()

    def test_healthz_reports_the_server_state(self):
        status, payload = self.get('/healthz')
        self.assertEqual(status, 200)
        self.assertEqual(payload, {'status': 'ok', 'server': 'nextcloud',
                                   'version': mcp.VERSION, 'read_only': False,
                                   'tag_tools': False})

    def test_a_request_without_authorization_is_refused(self):
        status, _body = self.post({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}, authorization=None)
        self.assertEqual(status, 401)

    def test_initialize_succeeds_when_nextcloud_accepts_the_credentials(self):
        status, body = self.post({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}})
        self.assertEqual(status, 200)
        self.assertEqual(body['result']['serverInfo']['name'], 'nextcloud')

    def test_initialize_is_refused_when_nextcloud_rejects_the_credentials(self):
        FakeNextcloudServer.fail_whoami = True
        status, _body = self.post({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}})
        self.assertEqual(status, 401)

    def test_tools_list_hides_the_tag_tools_by_default(self):
        status, body = self.post({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        self.assertEqual(status, 200)
        names = {tool['name'] for tool in body['result']['tools']}
        for name in TAG_TOOLS:
            self.assertNotIn(name, names)

    def test_calling_an_unknown_tool_over_http_does_not_break_the_connection(self):
        # Regression for the call_tool early-raise bug: this used to leave the
        # request thread with an unhandled exception instead of a response.
        status, body = self.post({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                  'params': {'name': 'nope', 'arguments': {}}})
        self.assertEqual(status, 200)
        self.assertTrue(body['result']['isError'])
        # The server must still be alive for the next request on this connection.
        status, body = self.post({'jsonrpc': '2.0', 'id': 2, 'method': 'ping'})
        self.assertEqual(status, 200)

    def test_tools_call_whoami_reaches_the_fake_nextcloud(self):
        status, body = self.post({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                  'params': {'name': 'nextcloud_whoami', 'arguments': {}}})
        self.assertEqual(status, 200)
        text = body['result']['content'][0]['text']
        self.assertIn('alice', text)

    def test_a_batch_request_is_rejected_cleanly(self):
        status, body = self.post([{'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}])
        self.assertEqual(status, 400)
        self.assertEqual(body['error']['code'], -32600)

    def test_a_message_with_an_id_but_no_method_answers_minus_32600(self):
        status, body = self.post({'jsonrpc': '2.0', 'id': 11})
        self.assertEqual(status, 200)
        self.assertEqual(body['error']['code'], -32600)
        self.assertEqual(body['id'], 11)

    def test_a_rejected_credential_is_a_tool_error_not_an_http_401(self):
        FakeNextcloudServer.fail_whoami = True
        status, body = self.post({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                  'params': {'name': 'nextcloud_whoami', 'arguments': {}}})
        self.assertEqual(status, 200)
        self.assertTrue(body['result']['isError'])
        self.assertIn('401', body['result']['content'][0]['text'])

    def test_an_origin_matching_the_host_is_allowed(self):
        status, _body = self.post({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
                                  headers={'Origin': f'http://127.0.0.1:{self.port}'})
        self.assertEqual(status, 200)

    def test_an_origin_from_another_host_is_refused(self):
        status, body = self.post({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
                                 headers={'Origin': 'http://evil.example'})
        self.assertEqual(status, 403)
        self.assertIn('origin', body['error'])

    def test_a_request_without_an_origin_is_still_allowed(self):
        status, _body = self.post({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
        self.assertEqual(status, 200)

    def test_an_allowlisted_origin_is_accepted_even_when_it_differs_from_host(self):
        port = self.start_mcp_server(
            NEXTCLOUD_MCP_ALLOWED_ORIGINS='https://allowed.example')
        status, _body = self.post({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
                                  headers={'Origin': 'https://allowed.example'}, port=port)
        self.assertEqual(status, 200)

    def test_an_origin_outside_the_allowlist_is_refused(self):
        port = self.start_mcp_server(
            NEXTCLOUD_MCP_ALLOWED_ORIGINS='https://allowed.example')
        status, _body = self.post({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
                                  headers={'Origin': 'https://evil.example'}, port=port)
        self.assertEqual(status, 403)


class StandardLibraryOnlyTests(unittest.TestCase):
    def test_the_server_imports_only_standard_library_modules(self):
        tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split('.')[0])
        third_party = sorted(roots - set(sys.stdlib_module_names))
        self.assertEqual(third_party, [], f'third-party imports: {third_party}')


class DocumentationTests(unittest.TestCase):
    def test_every_environment_variable_is_documented(self):
        variables = set(re.findall(r'NEXTCLOUD_MCP_[A-Z_]+',
                                   SOURCE.read_text(encoding='utf-8')))
        env_example = read('.env.example')
        readme = read('README.md')
        self.assertTrue(variables)
        for name in sorted(variables):
            self.assertIn(name, env_example, name)
            self.assertIn(name, readme, name)

    def test_the_readme_ends_with_a_japanese_summary(self):
        readme = read('README.md')
        self.assertIn('日本語', readme)
        self.assertTrue(re.search(r'[ぁ-んァ-ン一-龥]', readme.rsplit('日本語', 1)[1]))

    def test_the_version_constant_matches_the_changelog(self):
        changelog = read('CHANGELOG.md')
        self.assertIn(f'[{mcp.VERSION}]', changelog)


class PackageLayoutTests(unittest.TestCase):
    def test_the_dockerfile_runs_as_a_non_root_user(self):
        dockerfile = read('Dockerfile')
        self.assertIn('FROM python:3.13-alpine', dockerfile)
        self.assertIn('EXPOSE 5811', dockerfile)
        self.assertIn('ENV NEXTCLOUD_MCP_BIND=0.0.0.0', dockerfile)
        self.assertIn('USER mcp', dockerfile)
        self.assertIn('ENTRYPOINT ["python3", "/app/nextcloud_mcp.py"]', dockerfile)

    def test_compose_reads_the_env_file_and_keeps_the_port_on_loopback(self):
        compose = read('compose.yaml')
        self.assertIn('env_file: .env', compose)
        self.assertIn('127.0.0.1:5811:5811', compose)
        self.assertIn('read_only: true', compose)
        self.assertIn('cap_drop: [ALL]', compose)
        self.assertIn('restart: unless-stopped', compose)

    def test_the_compose_stores_the_scratch_dir_on_a_named_volume(self):
        compose = read('compose.yaml')
        self.assertIn('- nextcloud-mcp-tmp:/var/tmp/nextcloud-mcp', compose)
        self.assertIn('nextcloud-mcp-tmp:', compose)
        self.assertIn('tmpfs:', compose)
        self.assertIn('- /tmp', compose)

    def test_the_ci_workflow_tests_and_builds(self):
        ci = read('.github/workflows/ci.yml')
        self.assertIn("python-version: ['3.10', '3.11', '3.12', '3.13']", ci)
        self.assertIn('ruff check', ci)
        self.assertIn('python3 -m unittest discover -s tests -v', ci)
        self.assertIn('py_compile', ci)
        self.assertIn('docker build', ci)

    def test_the_image_workflow_publishes_multi_arch_on_version_tags(self):
        image = read('.github/workflows/image.yml')
        self.assertIn("tags: ['v*']", image)
        self.assertIn('packages: write', image)
        self.assertIn('docker/login-action', image)
        self.assertIn('docker/build-push-action', image)
        self.assertIn('linux/amd64,linux/arm64', image)
        self.assertIn('ghcr.io/ruruthegeek/nextcloud-mcp:latest', image)

    def test_dependabot_watches_github_actions_weekly(self):
        dependabot = read('.github/dependabot.yml')
        self.assertIn('package-ecosystem: github-actions', dependabot)
        self.assertIn('interval: weekly', dependabot)

    def test_the_security_policy_points_at_github_advisories(self):
        security = read('SECURITY.md')
        self.assertIn('GitHub Security Advisories', security)
        self.assertIn('security/advisories/new', security)
        self.assertIn('Scope', security)

    def test_the_ruff_lint_configuration_selects_the_documented_rules(self):
        ruff = read('pyproject.toml')
        self.assertIn('line-length', ruff)
        self.assertIn("['E', 'F', 'W', 'I']", ruff)

    def test_the_license_is_the_agpl(self):
        self.assertIn('GNU AFFERO GENERAL PUBLIC LICENSE', read('LICENSE'))


class RepositoryHygieneTests(unittest.TestCase):
    def test_no_private_identifiers_leak_into_the_repository(self):
        # Assembled at runtime so this test file does not match itself.
        forbidden = ['apex' + 'tox', '192' + '.168.', 'shake' + '-cloud',
                     '/home/' + 'ruru']
        skipped = {'.git', '__pycache__', '.ruff_cache', '.mypy_cache', '.pytest_cache'}
        for path in sorted(ROOT.rglob('*')):
            if not path.is_file() or skipped.intersection(path.parts):
                continue
            text = path.read_text(encoding='utf-8', errors='ignore')
            for pattern in forbidden:
                self.assertNotIn(pattern, text, f'{path}: {pattern}')


if __name__ == '__main__':
    unittest.main()
