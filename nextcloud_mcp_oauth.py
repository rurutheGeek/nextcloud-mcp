"""OAuth 2.1 front end for the Nextcloud MCP server (NEXTCLOUD_MCP_AUTH=oauth|both).

MCP clients expect an authorization server with discovery, PKCE and dynamic
client registration. Nextcloud's built-in OAuth2 app has none of these, so
FastMCP's OAuthProxy plays that role: MCP clients register with and log in
through this server, which sends the user to Nextcloud's own login and
consent page and keeps the Nextcloud token it receives. Each request then
reaches Nextcloud with the caller's own token, so the caller's Nextcloud ACLs
still apply exactly as with Basic authentication.

Only the authorization part comes from FastMCP. The MCP endpoint itself is
the same JSON-RPC handling as the standard-library server (check_post and
answer_post in nextcloud_mcp.py).

Needs: pip install fastmcp uvicorn (see requirements-oauth.txt).
"""
import logging

import anyio
import fastmcp
import uvicorn
from fastmcp.server.auth import AccessToken, OAuthProxy, TokenVerifier
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

log = logging.getLogger('nextcloud-mcp')

AUTHORIZE_PATH = '/index.php/apps/oauth2/authorize'
TOKEN_PATH = '/index.php/apps/oauth2/api/v1/token'
MCP_PATH = '/mcp'
CALLBACK_PATH = '/auth/callback'


def check_oauth_config(config):
    """Fail at startup, not at the first login, when OAuth is misconfigured."""
    missing = [name for name, value in (
        ('NEXTCLOUD_MCP_PUBLIC_URL', config.public_url),
        ('NEXTCLOUD_MCP_OAUTH_CLIENT_ID', config.oauth_client_id),
        ('NEXTCLOUD_MCP_OAUTH_CLIENT_SECRET', config.oauth_client_secret),
    ) if not value]
    if missing:
        raise SystemExit(f'NEXTCLOUD_MCP_AUTH={config.auth} needs {", ".join(missing)}')


class NextcloudTokenVerifier(TokenVerifier):
    """Accept a Nextcloud OAuth2 access token when Nextcloud accepts it.

    Nextcloud's OAuth2 access tokens are opaque app tokens, so the only
    authority is Nextcloud itself: ask it who the token belongs to.
    """

    def __init__(self, core, config, client_factory):
        super().__init__(base_url=config.public_url)
        self.core = core
        self.client_factory = client_factory

    async def verify_token(self, token):
        client = self.client_factory('Bearer ' + token)
        try:
            user = await anyio.to_thread.run_sync(client.whoami)
        except self.core.NextcloudError as error:
            if error.status not in (401, 403):
                log.warning('token check: Nextcloud answered HTTP %s', error.status)
            return None
        except self.core.ToolError as error:
            log.warning('token check: Nextcloud did not answer: %s', error)
            return None
        return AccessToken(token=token, client_id=user['id'], scopes=[], subject=user['id'])


def build_proxy(core, config, client_factory):
    # FastMCP keeps client registrations and the (encrypted) upstream tokens
    # under its home directory; keep them in the state directory instead.
    config.state_dir.mkdir(parents=True, exist_ok=True)
    fastmcp.settings.home = config.state_dir
    return OAuthProxy(
        upstream_authorization_endpoint=config.nextcloud_public_url + AUTHORIZE_PATH,
        upstream_token_endpoint=config.base_url + TOKEN_PATH,
        upstream_client_id=config.oauth_client_id,
        upstream_client_secret=config.oauth_client_secret,
        token_verifier=NextcloudTokenVerifier(core, config, client_factory),
        base_url=config.public_url,
        redirect_path=CALLBACK_PATH,
        allowed_client_redirect_uris=list(config.oauth_redirect_uris) or None,
        # Nextcloud's OAuth2 app supports neither PKCE nor resource
        # indicators. PKCE still protects the MCP client -> this server leg;
        # this server -> Nextcloud is a confidential client (client secret).
        forward_pkce=False,
        forward_resource=False,
        token_endpoint_auth_method='client_secret_post',
    )


def build_app(core, config, client_factory=None, proxy=None):
    if client_factory is None:
        client_factory = lambda authorization: core.NextcloudClient(authorization, config)  # noqa: E731
    if proxy is None:
        proxy = build_proxy(core, config, client_factory)
    routes = proxy.get_routes(mcp_path=MCP_PATH)
    metadata_path = next(route.path for route in routes
                         if route.path.startswith('/.well-known/oauth-protected-resource'))
    metadata_url = config.public_url + metadata_path

    def challenge(error=None):
        value = f'Bearer resource_metadata="{metadata_url}"'
        if error:
            value = f'Bearer error="{error}", resource_metadata="{metadata_url}"'
        return {'WWW-Authenticate': value}

    async def health(_request):
        return JSONResponse(core.health_payload(config))

    async def mcp(request):
        if request.method != 'POST':
            return Response(status_code=405, headers={'Allow': 'POST'})
        header = request.headers.get('authorization', '')
        scheme = header.split(' ', 1)[0].lower()
        authorization = ''
        if scheme == 'basic' and config.auth == 'both':
            authorization = header
        elif scheme == 'bearer' and isinstance(request.user, AuthenticatedUser):
            # The access token here is the caller's Nextcloud token (the
            # verifier above), never this server's own JWT.
            authorization = 'Bearer ' + request.user.access_token.token
        try:
            length = int(request.headers.get('content-length') or 0)
        except ValueError:
            length = 0
        result = core.check_post(request.headers.get('origin'), request.headers.get('host'),
                                 length, authorization, config)
        if result is None:
            raw = await request.body()
            client = client_factory(authorization)
            result = await anyio.to_thread.run_sync(
                core.answer_post, raw, client, config, challenge('invalid_token'))
        status, headers, payload = result
        if status == 401:
            headers = challenge('invalid_token' if header else None)
        if payload is None:
            return Response(status_code=status, headers=headers)
        return JSONResponse(payload, status_code=status, headers=headers)

    routes = routes + [
        Route('/healthz', health, methods=['GET']),
        Route(MCP_PATH, mcp, methods=['GET', 'POST', 'DELETE']),
    ]
    return Starlette(routes=routes, middleware=proxy.get_middleware())


def serve(config, core):
    check_oauth_config(config)
    app = build_app(core, config)
    log.info('OAuth (%s) listening on %s:%s, public URL %s, Nextcloud login %s',
             config.auth, config.bind, config.port, config.public_url,
             config.nextcloud_public_url + AUTHORIZE_PATH)
    uvicorn.run(app, host=config.bind, port=config.port, log_level='info')
