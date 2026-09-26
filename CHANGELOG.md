# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
