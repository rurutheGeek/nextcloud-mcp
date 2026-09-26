# Nextcloud MCP server

A small [MCP](https://modelcontextprotocol.io/) server that lets AI agents
(opencode, Claude, ...) read and write your Nextcloud files, edit MP3 tags and
pack or unpack archives — **as the connecting user**. It implements the
Streamable HTTP transport (`POST /mcp`) with the Python standard library only:
no dependencies, no database, no state.

## What it does

- **Files**: list, stat, read (text or base64), search, create, overwrite,
  move/rename, copy, delete (to the Nextcloud trash).
- **Archives**: list zip/tar contents, create a zip from files/folders, extract
  a zip/tar with file-count, size, symlink and path-escape checks.
- **Music tags**: read/write ID3 tags and query MusicBrainz, through an
  optional tag API bridge (disabled unless configured).

## Design

- **Pass-through authentication.** The `Authorization` header of the MCP
  request is forwarded to Nextcloud unchanged (Basic with a user/app password
  today, a Bearer token with the same header later). Every operation is
  executed by Nextcloud as that user, so ACLs, shares, quotas, versions and the
  trash keep applying. The server never stores, caches or logs credentials.
- **No sessions.** Each JSON-RPC message is answered by a single JSON response;
  `initialize`, `ping`, `tools/list` and `tools/call` are implemented.
  `initialize` verifies the credentials against Nextcloud (`whoami`) and
  answers `401` when Nextcloud rejects them.
- **Small attack surface.** Standard library only; paths are normalized and
  `..` is refused; archive members are validated before anything is written;
  `{music_root}/Converted` and everything listed in `NEXTCLOUD_MCP_WRITE_DENY`
  are refused by every write tool.
- **Stateless on disk.** Archive work happens in `NEXTCLOUD_MCP_TMP` (a
  directory on disk, not tmpfs, so multi-GB archives do not eat RAM).

## Tools

Read tools:

| Tool | Description |
| --- | --- |
| `nextcloud_whoami` | Connected user (ID and display name). |
| `nextcloud_list_files` | Direct children of a folder, with size/etag/fileid/writable. |
| `nextcloud_file_info` | Metadata for one file or folder. |
| `nextcloud_read_file` | File content as text or base64, truncated at the limit. |
| `nextcloud_search_files` | Nextcloud unified search (names and contents). |
| `nextcloud_read_music_tags` | ID3 tags of an `.mp3` under the music root. Tag tools only. |
| `nextcloud_search_musicbrainz` | MusicBrainz recording candidates. Tag tools only. |
| `nextcloud_list_archive` | Zip/tar member list before extracting. |

Write tools:

| Tool | Description |
| --- | --- |
| `nextcloud_write_file` | Create/overwrite (text or base64, optional `If-Match`). |
| `nextcloud_create_folder` | Create a folder (idempotent). |
| `nextcloud_move_file` | Move/rename; WebDAV MOVE keeps the fileid and share links. |
| `nextcloud_copy_file` | Copy a file or folder. |
| `nextcloud_delete_file` | Delete to the Nextcloud trash. |
| `nextcloud_write_music_tags` | Write ID3 tags; the tag API keeps a backup. Tag tools only. |
| `nextcloud_create_zip` | Pack files/folders into a zip on Nextcloud. |
| `nextcloud_extract_archive` | Extract a zip/tar into a folder. |

The three "tag tools only" entries are hidden from `tools/list` and answer a
clear error while the tag API is not configured. With
`NEXTCLOUD_MCP_READ_ONLY=1` every write tool is hidden and refused.

Input/output schemas are the JSON Schema returned by `tools/list`. The exact
tool names and schemas are stable; they are not changed between minor versions.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `NEXTCLOUD_MCP_PORT` | `5811` | Listening port. |
| `NEXTCLOUD_MCP_BIND` | `127.0.0.1` | Bind address. Keep the loopback and use a reverse proxy. |
| `NEXTCLOUD_MCP_BASE_URL` | `http://127.0.0.1:8080` | Nextcloud base URL. |
| `NEXTCLOUD_MCP_TAG_API_URL` | *(empty)* | Tag API base URL. The tag tools need this **and** the token. |
| `NEXTCLOUD_MCP_TAG_API_TOKEN` | *(empty)* | Tag API bearer token. |
| `NEXTCLOUD_MCP_MUSIC_ROOT` | `/music` | Music root (DAV path). Limits the MP3 tag tools and defines `{root}/Converted`. |
| `NEXTCLOUD_MCP_WRITE_DENY` | *(empty)* | Extra write-deny prefixes, comma-separated absolute paths. |
| `NEXTCLOUD_MCP_TMP` | `/var/tmp/nextcloud-mcp` | Scratch directory for archive downloads/extractions. Use disk, not tmpfs. |
| `NEXTCLOUD_MCP_READ_ONLY` | `0` | `1` hides every write tool and refuses write calls. |
| `NEXTCLOUD_MCP_MAX_READ_BYTES` | `262144` | Maximum bytes returned by `nextcloud_read_file`. |
| `NEXTCLOUD_MCP_MAX_WRITE_BYTES` | `8388608` | Maximum bytes per `nextcloud_write_file`. |
| `NEXTCLOUD_MCP_MAX_BODY_BYTES` | `33554432` | Maximum HTTP request body size. |
| `NEXTCLOUD_MCP_MAX_EXTRACT_FILES` | `10000` | Maximum files extracted from one archive. |
| `NEXTCLOUD_MCP_MAX_EXTRACT_BYTES` | `4294967296` | Maximum total extracted size (4 GiB). |
| `NEXTCLOUD_MCP_MAX_ZIP_BYTES` | `4294967296` | Maximum total size packed into a zip (4 GiB). |
| `NEXTCLOUD_MCP_TIMEOUT` | `120` | Nextcloud/tag API HTTP timeout in seconds. |
| `NEXTCLOUD_MCP_ARCHIVE_TIMEOUT` | `1800` | Timeout in seconds for archive downloads/uploads. |

`GET /healthz` returns
`{"status":"ok","server":"nextcloud","version":"1.0.0","read_only":false,"tag_tools":false}`.

## Running with Docker

```bash
cp .env.example .env
$EDITOR .env

docker compose up -d --build
curl -s http://127.0.0.1:5811/healthz
```

The compose file publishes `127.0.0.1:5811` only, runs read-only with
`cap_drop: [ALL]` and `no-new-privileges`, and mounts tmpfs at `/tmp` and
`/var/tmp/nextcloud-mcp`.

## Running with systemd

```ini
# /etc/systemd/system/nextcloud-mcp.service
[Unit]
Description=Nextcloud MCP server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=nextcloud-mcp
ExecStart=/usr/bin/python3 /opt/nextcloud-mcp/nextcloud_mcp.py
EnvironmentFile=/opt/nextcloud-mcp/nextcloud-mcp.env
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/tmp/nextcloud-mcp

[Install]
WantedBy=multi-user.target
```

```bash
sudo install -d -m 0700 -o nextcloud-mcp -g nextcloud-mcp /var/tmp/nextcloud-mcp
sudo install -m 0400 -o nextcloud-mcp -g nextcloud-mcp .env /opt/nextcloud-mcp/nextcloud-mcp.env
sudo systemctl daemon-reload
sudo systemctl enable --now nextcloud-mcp
curl -s http://127.0.0.1:5811/healthz
```

Use an application password, not the account password, in the clients below.

## Connecting an MCP client

Any MCP client that speaks Streamable HTTP works. The endpoint is
`http://<host>:5811/mcp` and the `Authorization` header must carry the
Nextcloud credentials of the user whose files the agent may touch.

opencode (`opencode.json`):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "nextcloud": {
      "type": "remote",
      "url": "https://nextcloud-mcp.example.net/mcp",
      "headers": {
        "Authorization": "Basic <base64(user:app-password)>"
      }
    }
  }
}
```

`<base64(user:app-password)>` is the standard Basic credential, i.e.
`printf '%s' 'alice:app-password' | base64`.

Smoke test without a client:

```bash
curl -s -u 'alice:app-password' -X POST http://127.0.0.1:5811/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Security notes

- **Reverse proxy first.** The server speaks plain HTTP and trusts the
  `Authorization` header as-is. Expose it through an HTTPS reverse proxy
  (Caddy, nginx, ...) and keep `NEXTCLOUD_MCP_BIND=127.0.0.1` (or the Docker
  loopback port). The tag API token is a bearer secret: never expose the tag
  API port either.
- **Per-user credentials.** MCP credentials equal Nextcloud credentials. Hand
  each user their own app password, and revoke it in Nextcloud when it leaks.
  Users can never see more than their own account allows.
- **Read-only deployments.** Set `NEXTCLOUD_MCP_READ_ONLY=1` for agents that
  should only read. Add `NEXTCLOUD_MCP_WRITE_DENY` for folders that must stay
  untouched even then.
- **Rate and body limits** (`NEXTCLOUD_MCP_MAX_BODY_BYTES`, per-tool byte
  limits) protect the service from oversized payloads and zip bombs.

## Development

The implementation and the tests use the standard library only (Python 3.13 or
newer; `ast` verifies the imports in CI).

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile nextcloud_mcp.py
docker build -t nextcloud-mcp:dev .
```

The test suite covers path normalization and write guards, archive safety
(including zip-slip and symlinks), tag API gating, JSON-RPC dispatch, and real
HTTP round trips against fake Nextcloud/tag API servers.

## 日本語の概要

Nextcloud MCPサーバーは、AIエージェント（opencode等）にNextcloudの
ファイル操作・MP3タグ編集・圧縮/解凍をMCPツールとして渡す、標準ライブラリ
だけで動く小さなサーバーです。呼び出し元が送った `Authorization` ヘッダーを
そのままNextcloudへ転送するので、権限・共有・クォータ・ゴミ箱はすべて
Nextcloud側のACLに従い、サーバーは資格情報を保存しません。

タグ編集ツール（`nextcloud_read_music_tags`・`nextcloud_search_musicbrainz`・
`nextcloud_write_music_tags`）は `NEXTCLOUD_MCP_TAG_API_URL` と
`NEXTCLOUD_MCP_TAG_API_TOKEN` の両方が設定されているときだけ `tools/list`
に現れます。音楽ルートは `NEXTCLOUD_MCP_MUSIC_ROOT`（既定 `/music`）、
書き込み禁止パスは `{music_root}/Converted` と
`NEXTCLOUD_MCP_WRITE_DENY` で決まり、`NEXTCLOUD_MCP_READ_ONLY=1` にすると
書き込み系ツールがすべて隠されます。

想定配備はDocker（`compose.yaml`、ホストの `127.0.0.1:5811` のみ公開）か
systemdユニットで、どちらも前段にHTTPSリバースプロキシを置く前提です。
テストは `python3 -m unittest discover -s tests -v` で実行できます。
