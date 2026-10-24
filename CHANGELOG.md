# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.3.0] - 2026-10-24

### Added

- Deck tools: `nextcloud_deck_boards`, `nextcloud_deck_cards`,
  `nextcloud_deck_create_card`, `nextcloud_deck_update_card`,
  `nextcloud_deck_move_card`, `nextcloud_deck_comments` and
  `nextcloud_deck_add_comment`. They speak Deck's JSON API with the caller's
  own credential, so board shares and edit permissions keep applying. Board
  and stack arguments accept an id or a title; cards keep their description
  (truncated at 16 KiB) so an agent can turn what a person wrote into an issue.
- A clear error when the Nextcloud Deck app is missing: the Deck tools check
  the `deck` capability on `/ocs/v2.php/cloud/capabilities`.

### Changed

- The capability response is fetched once per client and shared by the
  calendar and Deck checks.

## [1.2.0] - 2026-10-18

### Added

- OAuth 2.1 login for MCP clients (`NEXTCLOUD_MCP_AUTH=oauth`, or `both` to
  keep accepting Basic). Uses Nextcloud's built-in OAuth2 app as the upstream
  identity provider; FastMCP's OAuth proxy supplies the protected resource
  and authorization server metadata, dynamic client registration, PKCE and a
  consent page. Each request reaches Nextcloud with the caller's own
  Nextcloud token, so the caller's ACLs keep applying.
- `NEXTCLOUD_MCP_PUBLIC_URL`, `NEXTCLOUD_MCP_NEXTCLOUD_PUBLIC_URL`,
  `NEXTCLOUD_MCP_OAUTH_CLIENT_ID`, `NEXTCLOUD_MCP_OAUTH_CLIENT_SECRET`,
  `NEXTCLOUD_MCP_OAUTH_REDIRECT_URIS` and `NEXTCLOUD_MCP_STATE_DIR`.
- `requirements-oauth.txt` (pinned `fastmcp` and `uvicorn`), installed in the
  Docker image.

### Changed

- `basic` stays the default and keeps running on the standard library only;
  the OAuth packages are imported only when OAuth is enabled.
- The POST `/mcp` handling is shared by the standard-library server and the
  OAuth server (`check_post`, `answer_post`).

## [1.1.0] - 2026-09-28

### Added

- Calendar tools: `nextcloud_list_calendars`, `nextcloud_list_events` and
  `nextcloud_create_event`, speaking CalDAV as the connecting user so calendar
  sharing and read-only privileges keep applying. Calendars that do not accept
  `VEVENT` and read-only calendars are refused for new events.
- `nextcloud_create_event` expands recurring series (`DAILY`, `WEEKLY`,
  `MONTHLY`, `YEARLY` with `INTERVAL`, `COUNT`, `UNTIL`, `BYDAY`, `BYMONTHDAY`,
  `BYMONTH`, `EXDATE` and `RECURRENCE-ID` overrides), skips
  `TRANSP:TRANSPARENT` / `STATUS:CANCELLED` events and refuses a time that
  overlaps an existing busy event unless `allow_overlap=true` (the refusal
  lists the conflicting events, so an agent can skip conflicting candidates).
- A clear error when the Nextcloud Calendar app is missing: the calendar tools
  check the `calendar` capability on `/ocs/v2.php/cloud/capabilities`.
- `NEXTCLOUD_MCP_TIMEZONE` (default: the host's local timezone) for times
  without an offset, all-day events and the default listing range.

### Changed

- README documents the calendar tools, their overlap semantics, recurrence
  support and the new configuration (English and Japanese).

## [1.0.1] - 2026-09-26

### Security

- Every `tools/call` now verifies the caller with `whoami` before the tool
  runs; a `401`/`403` (revoked or invalid credentials) becomes a tool error
  instead of being ignored. `nextcloud_read_music_tags` also stats the target
  file before calling the tag API.
- Write-deny prefixes now protect their ancestors as well, so a move, copy,
  delete or extraction can no longer take out `/music` when
  `/music/Converted` is protected. `nextcloud_move_file` and
  `nextcloud_copy_file` check source and destination, and
  `nextcloud_extract_archive` checks every member's final path (and the
  archive itself when `remove_archive=true`).
- Optional `Origin` validation: `NEXTCLOUD_MCP_ALLOWED_ORIGINS` (comma
  separated) rejects mismatching origins with `403`; without the list, a
  present `Origin` must match the `Host` header (DNS-rebinding protection).

### Fixed

- `nextcloud_read_file` clamps `max_bytes` to at least 1 and answers a tool
  error (not "Internal error") for non-integer values.
- `nextcloud_list_archive` and `nextcloud_create_zip` check sizes with `stat`
  before downloading, instead of after reading everything.
- A JSON-RPC message with an `id` but no `method` is answered with `-32600`
  (notifications without `id` are still accepted with `202`).

### Changed

- The compose file stores `NEXTCLOUD_MCP_TMP` on the named volume
  `nextcloud-mcp-tmp` instead of tmpfs; only `/tmp` stays a tmpfs.
- The README documents `inputSchema`-only support (no `outputSchema` /
  `structuredContent`), the single-user tag API premise, partial extraction
  behavior, and Claude Code / opencode HTTP examples with TLS termination.

### Added

- Multi-arch (`linux/amd64`, `linux/arm64`) image workflow publishing
  `ghcr.io/ruruthegeek/nextcloud-mcp` on `v*` tags (`:latest` included).
- `SECURITY.md` (GitHub Security Advisories) and weekly Dependabot updates
  for GitHub Actions.
- CI test matrix (Python 3.10–3.13) and `ruff` lint (E, F, W, I).
- Regression tests for the whoami check, tag-read stat, ancestor write-deny,
  per-member extraction checks, `max_bytes`, pre-download size checks, Origin
  validation and the `-32600` answer.

## [1.0.0] - 2026-09-26

### Added

- Streamable HTTP MCP server (`POST /mcp`: `initialize`, `ping`, `tools/list`,
  `tools/call`) implemented with the Python standard library only.
- Sixteen tools: eight read tools (`nextcloud_whoami`, `nextcloud_list_files`,
  `nextcloud_file_info`, `nextcloud_read_file`, `nextcloud_search_files`,
  `nextcloud_read_music_tags`, `nextcloud_search_musicbrainz`,
  `nextcloud_list_archive`) and eight write tools (`nextcloud_write_file`,
  `nextcloud_create_folder`, `nextcloud_move_file`, `nextcloud_copy_file`,
  `nextcloud_delete_file`, `nextcloud_write_music_tags`, `nextcloud_create_zip`,
  `nextcloud_extract_archive`).
- Pass-through authentication: the caller's `Authorization` header is
  forwarded to Nextcloud unchanged, so ACLs, shares, quotas and the trash keep
  applying. No credentials are stored or logged.
- Optional music tag API bridge, enabled only when both
  `NEXTCLOUD_MCP_TAG_API_URL` and `NEXTCLOUD_MCP_TAG_API_TOKEN` are set.
- Archive tools with file-count, size, symlink and path-escape checks.
- Configurable music root (`NEXTCLOUD_MCP_MUSIC_ROOT`) and extra write-deny
  prefixes (`NEXTCLOUD_MCP_WRITE_DENY`); `{music_root}/Converted` is always
  write-protected.
- Read-only mode (`NEXTCLOUD_MCP_READ_ONLY=1`) that hides every write tool.
- `GET /healthz` reporting `status`, `read_only`, `tag_tools` and `version`.
- Dockerfile (python:3.13-alpine, non-root) and a hardened compose file, plus
  a systemd example in the README.
- Self-contained unit test suite (standard library only).
