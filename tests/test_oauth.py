"""Guard the OAuth front end (nextcloud_mcp_oauth.py).

A bug here either lets a request reach Nextcloud with someone else's (or no)
credential, or breaks the discovery that MCP clients need to log in. Runs
the real Starlette app under uvicorn against a fake Nextcloud that plays
both the OAuth2 app (authorize/token) and the OCS user endpoint. Skipped when
the optional OAuth dependencies are not installed.
"""
import base64
import hashlib
import html
import http.client
import importlib.util
import json
import re
import secrets
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HAVE_OAUTH = all(importlib.util.find_spec(name) for name in ('fastmcp', 'uvicorn'))

sys.path.insert(0, str(ROOT))
import nextcloud_mcp as core  # noqa: E402

if HAVE_OAUTH:
    import uvicorn

    import nextcloud_mcp_oauth as oauth

NC_TOKEN = 'nc-access-token-alice'
CLIENT_ID = 'mcp-client-id'
CLIENT_SECRET = 'mcp-client-secret'
BASIC = 'Basic ' + base64.b64encode(b'alice:app-password').decode()


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class FakeNextcloud(BaseHTTPRequestHandler):
    """Nextcloud's OAuth2 app and OCS user endpoint, nothing else."""

    codes = {}
    token_requests = []

    def log_message(self, *_args):
        pass

    def send(self, status, payload=None, headers=None):
        body = json.dumps(payload).encode() if payload is not None else b''
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == '/ocs/v2.php/cloud/user':
            if self.headers.get('Authorization') in ('Bearer ' + NC_TOKEN, BASIC):
                self.send(200, {'ocs': {'meta': {'statuscode': 200},
                                        'data': {'id': 'alice', 'displayname': 'Alice'}}})
            else:
                self.send(401, {'ocs': {'meta': {'statuscode': 401}}})
            return
        if parsed.path == oauth.AUTHORIZE_PATH:
            # Nextcloud logs the user in and redirects to the registered
            # redirect URI with a code and the caller's state.
            query = urllib.parse.parse_qs(parsed.query)
            code = secrets.token_hex(8)
            FakeNextcloud.codes[code] = query['client_id'][0]
            target = (query['redirect_uri'][0] + '?' +
                      urllib.parse.urlencode({'code': code, 'state': query['state'][0]}))
            self.send(302, headers={'Location': target})
            return
        self.send(404)

    def do_POST(self):
        if self.path != oauth.TOKEN_PATH:
            self.send(404)
            return
        length = int(self.headers.get('Content-Length') or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        FakeNextcloud.token_requests.append(form)
        ok = (form.get('client_id') == [CLIENT_ID] and form.get('client_secret') == [CLIENT_SECRET]
              and FakeNextcloud.codes.pop(form.get('code', [''])[0], None) == CLIENT_ID)
        if not ok:
            self.send(400, {'error': 'invalid_request'})
            return
        self.send(200, {'access_token': NC_TOKEN, 'token_type': 'Bearer', 'expires_in': 3600,
                        'refresh_token': 'nc-refresh', 'user_id': 'alice'})


@unittest.skipUnless(HAVE_OAUTH, 'OAuth dependencies (fastmcp, uvicorn) are not installed')
class OAuthFrontEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.nc = ThreadingHTTPServer(('127.0.0.1', 0), FakeNextcloud)
        threading.Thread(target=cls.nc.serve_forever, daemon=True).start()
        cls.nc_url = f'http://127.0.0.1:{cls.nc.server_address[1]}'
        cls.state = tempfile.TemporaryDirectory(prefix='nc-mcp-oauth-')
        cls.servers = []

    @classmethod
    def tearDownClass(cls):
        for server in cls.servers:
            server.should_exit = True
        cls.nc.shutdown()
        cls.nc.server_close()
        cls.state.cleanup()

    def setUp(self):
        self.cookies = {}

    def start(self, auth):
        port = free_port()
        config = core.Config({
            'NEXTCLOUD_MCP_AUTH': auth,
            'NEXTCLOUD_MCP_BASE_URL': self.nc_url,
            'NEXTCLOUD_MCP_PUBLIC_URL': f'http://127.0.0.1:{port}',
            'NEXTCLOUD_MCP_OAUTH_CLIENT_ID': CLIENT_ID,
            'NEXTCLOUD_MCP_OAUTH_CLIENT_SECRET': CLIENT_SECRET,
            'NEXTCLOUD_MCP_STATE_DIR': str(Path(self.state.name) / auth),
            'NEXTCLOUD_MCP_TMP': str(Path(self.state.name) / 'tmp'),
        })
        app = oauth.build_app(core, config)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        self.servers.append(server)
        return port

    def request(self, port, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def post_mcp(self, port, message, authorization=None):
        headers = {'Content-Type': 'application/json'}
        if authorization:
            headers['Authorization'] = authorization
        status, headers, body = self.request(port, 'POST', '/mcp', json.dumps(message), headers)
        return status, headers, (json.loads(body) if body else None)

    def test_an_unauthenticated_request_points_at_the_resource_metadata(self):
        port = self.start('oauth')
        status, headers, _ = self.post_mcp(port, {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
        self.assertEqual(status, 401)
        self.assertIn('resource_metadata="', headers['www-authenticate'])
        self.assertTrue(headers['www-authenticate'].startswith('Bearer '))

    def test_discovery_advertises_pkce_and_registration(self):
        port = self.start('oauth')
        status, headers, _ = self.post_mcp(port, {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
        metadata_url = re.search(r'resource_metadata="([^"]+)"', headers['www-authenticate']).group(1)
        status, _, body = self.request(port, 'GET', urllib.parse.urlsplit(metadata_url).path)
        self.assertEqual(status, 200)
        resource = json.loads(body)
        self.assertEqual(resource['resource'], f'http://127.0.0.1:{port}/mcp')
        issuer = resource['authorization_servers'][0].rstrip('/')
        status, _, body = self.request(port, 'GET', '/.well-known/oauth-authorization-server')
        self.assertEqual(status, 200)
        server = json.loads(body)
        self.assertEqual(server['issuer'].rstrip('/'), issuer)
        self.assertIn('S256', server['code_challenge_methods_supported'])
        self.assertIn('registration_endpoint', server)

    def test_basic_is_refused_in_oauth_mode(self):
        port = self.start('oauth')
        status, _, _ = self.post_mcp(port, {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}, BASIC)
        self.assertEqual(status, 401)

    def test_basic_still_works_in_both_mode(self):
        port = self.start('both')
        status, _, body = self.post_mcp(port, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                               'params': {'name': 'nextcloud_whoami', 'arguments': {}}},
                                        BASIC)
        self.assertEqual(status, 200)
        self.assertIn('alice', body['result']['content'][0]['text'])

    def test_a_raw_nextcloud_token_is_not_accepted_as_a_bearer(self):
        # Only tokens this server issued are valid here; a Nextcloud token
        # (or anything else) presented directly must not be passed through.
        port = self.start('oauth')
        status, headers, _ = self.post_mcp(port, {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
                                           'Bearer ' + NC_TOKEN)
        self.assertEqual(status, 401)
        self.assertIn('invalid_token', headers['www-authenticate'])

    def browse(self, port, path):
        """Follow this server's pages like a browser (cookies, consent form).

        Returns the first redirect that leaves this server.
        """
        method, body, extra = 'GET', None, {}
        for _ in range(10):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
            headers = {'Cookie': '; '.join(f'{k}={v}' for k, v in self.cookies.items())}
            headers.update(extra)
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            page = response.read().decode('utf-8', 'replace')
            for key, value in response.getheaders():
                if key.lower() == 'set-cookie':
                    name, _, rest = value.partition('=')
                    self.cookies[name] = rest.split(';', 1)[0]
            connection.close()
            if response.status in (301, 302, 303, 307):
                location = response.getheader('Location')
                target = urllib.parse.urlsplit(urllib.parse.urljoin(f'http://127.0.0.1:{port}{path}', location))
                if target.port != port:
                    return location
                method, body, extra = 'GET', None, {}
                path = target.path + ('?' + target.query if target.query else '')
                continue
            self.assertEqual(response.status, 200, page[:500])
            form = {key: html.unescape(value) for key, value in
                    re.findall(r'name="([^"]+)"\s+value="([^"]*)"', page)}
            form['action'] = 'approve'
            action = html.unescape(re.search(r'<form[^>]*action="([^"]*)"', page).group(1)) or path
            method, body = 'POST', urllib.parse.urlencode(form)
            extra = {'Content-Type': 'application/x-www-form-urlencoded',
                     'Origin': f'http://127.0.0.1:{port}', 'Sec-Fetch-Site': 'same-origin'}
            path = urllib.parse.urlsplit(action).path or path
        self.fail('too many redirects')

    def test_the_full_login_reaches_nextcloud_as_the_caller(self):
        port = self.start('oauth')
        base = f'http://127.0.0.1:{port}'
        redirect_uri = 'http://127.0.0.1:19876/mcp/oauth/callback'
        status, _, body = self.request(port, 'POST', '/register', json.dumps({
            'redirect_uris': [redirect_uri], 'client_name': 'test',
            'grant_types': ['authorization_code', 'refresh_token'],
            'response_types': ['code'], 'token_endpoint_auth_method': 'none'}),
            {'Content-Type': 'application/json'})
        self.assertEqual(status, 201, body)
        client_id = json.loads(body)['client_id']

        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        query = urllib.parse.urlencode({
            'response_type': 'code', 'client_id': client_id, 'redirect_uri': redirect_uri,
            'state': 'client-state', 'code_challenge': challenge, 'code_challenge_method': 'S256',
            'resource': base + '/mcp'})
        location = self.browse(port, '/authorize?' + query)
        upstream = urllib.parse.urlsplit(location)
        self.assertEqual(upstream.path, oauth.AUTHORIZE_PATH)
        upstream_query = urllib.parse.parse_qs(upstream.query)
        self.assertEqual(upstream_query['client_id'], [CLIENT_ID])
        self.assertNotIn('code_challenge', upstream_query)

        # Nextcloud logs the user in and calls back.
        _, headers, _ = self.request(self.nc.server_address[1], 'GET', upstream.path + '?' + upstream.query)
        callback = urllib.parse.urlsplit(headers['Location'])
        location = self.browse(port, callback.path + '?' + callback.query)
        final = urllib.parse.urlsplit(location)
        self.assertEqual(final.scheme + '://' + final.netloc + final.path, redirect_uri)
        final_query = urllib.parse.parse_qs(final.query)
        self.assertEqual(final_query['state'], ['client-state'])

        status, _, body = self.request(port, 'POST', '/token', urllib.parse.urlencode({
            'grant_type': 'authorization_code', 'code': final_query['code'][0],
            'redirect_uri': redirect_uri, 'client_id': client_id, 'code_verifier': verifier}),
            {'Content-Type': 'application/x-www-form-urlencoded'})
        self.assertEqual(status, 200, body)
        token = json.loads(body)['access_token']
        self.assertNotEqual(token, NC_TOKEN)

        status, _, body = self.post_mcp(port, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                               'params': {'name': 'nextcloud_whoami', 'arguments': {}}},
                                        'Bearer ' + token)
        self.assertEqual(status, 200)
        self.assertIn('alice', body['result']['content'][0]['text'])

    def test_a_wrong_pkce_verifier_gets_no_token(self):
        port = self.start('oauth')
        status, _, body = self.request(port, 'POST', '/token', urllib.parse.urlencode({
            'grant_type': 'authorization_code', 'code': 'nope', 'client_id': 'nope',
            'code_verifier': 'x' * 43}), {'Content-Type': 'application/x-www-form-urlencoded'})
        self.assertIn(status, (400, 401))


if __name__ == '__main__':
    unittest.main()
