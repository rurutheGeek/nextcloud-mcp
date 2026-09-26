# Security policy

## Reporting a vulnerability

Please report security issues through the repository's
[GitHub Security Advisories](https://github.com/rurutheGeek/nextcloud-mcp/security/advisories/new)
(private report). Do not open a public issue for a suspected vulnerability.

Include the version (`GET /healthz` or the release tag), the deployment mode
(Docker/systemd, reverse proxy) and a minimal reproduction if possible. We aim
to acknowledge reports within 7 days and to publish a fix or a mitigation
before any public disclosure.

## Scope

In scope:

- Path traversal, write-deny bypass, archive escapes (zip-slip, symlinks) or
  permission checks that let a caller reach files their Nextcloud user cannot.
- Authentication/authorization flaws in the MCP or tag API bridge, including
  credential forwarding, Origin/DNS-rebinding handling and JSON-RPC parsing.
- Denial of service through uploads, archives or tag payloads within the
  documented limits.
- Container/CI issues in `Dockerfile`, `compose.yaml` and
  `.github/workflows/`.

Out of scope:

- Vulnerabilities in Nextcloud itself or in an external tag API; report those
  to the respective project.
- Misconfiguration of the reverse proxy or of a `NEXTCLOUD_MCP_*` value that
  weakens security (for example exposing the port beyond loopback).
- Findings that require an already-compromised host or valid credentials with
  broader access than the tested user's own Nextcloud account.

## Supported versions

The latest release on `main` receives security fixes. Older tags are not
maintained.
