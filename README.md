# Nextcloud MCP server

A small [MCP](https://modelcontextprotocol.io/) server that lets AI agents
(opencode, Claude, ...) read and write your Nextcloud files, edit MP3 tags,
pack or unpack archives and manage CalDAV calendar events — **as the
connecting user**. It implements the Streamable HTTP transport (`POST /mcp`)
with the Python standard library only: no dependencies, no database, no state.

## What it does

- **Files**: list, stat, read (text or base64), search, create, overwrite,
  move/rename, copy, delete (to the Nextcloud trash).
- **Archives**: list zip/tar contents, create a zip from files/folders, extract
  a zip/tar with file-count, size, symlink and path-escape checks.
- **Calendars**: list the caller's calendars, list events in a time range
  (recurring events expanded), and create events that are refused when they
  overlap an existing busy event unless `allow_overlap=true`. CalDAV follows
  the caller's own calendar collections and sharing privileges. If the
  Nextcloud Calendar app is missing, the tools answer a clear error instead of
  a generic failure.
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
  are refused by every write tool, together with their ancestors (so a
  protected subtree cannot be moved or deleted along with its parent).
- **Origin checks.** With `NEXTCLOUD_MCP_ALLOWED_ORIGINS` set, a request that
  carries an `Origin` header must match the allow-list; without the list, a
  present `Origin` must equal the request's `Host` (DNS-rebinding protection).
  Server-to-server clients that send no `Origin` are unaffected.
- **Stateless on disk.** Archive work happens in `NEXTCLOUD_MCP_TMP` (a
  directory on disk, not tmpfs, so multi-GB archives do not eat RAM).
- **Single-user tag bridge.** The optional tag API uses one shared bearer
  token and rewrites files through the shared music library, not through the
  caller's Nextcloud ACL. Run it for a single trusted user for now; per-user
  tag API access is planned.

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
| `nextcloud_list_calendars` | Calendars of the account (id, name, color, writable, components). |
| `nextcloud_list_events` | Events overlapping a time range (recurring events expanded). |

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
| `nextcloud_create_event` | Create a VEVENT; refuses overlaps unless `allow_overlap=true`. |

The three "tag tools only" entries are hidden from `tools/list` and answer a
clear error while the tag API is not configured. With
`NEXTCLOUD_MCP_READ_ONLY=1` every write tool is hidden and refused.

Calendar notes:

- `nextcloud_list_calendars` needs the Nextcloud Calendar app. When the
  `calendar` capability is missing from `/ocs/v2.php/cloud/capabilities`, the
  calendar tools answer that the Calendar app (extension) is not available,
  instead of a generic CalDAV failure.
- Calendar ids are the CalDAV collection names (`personal`, a UUID, ...).
  Event tools accept an id or a display name, and only calendars that accept
  `VEVENT` and are writable can receive events.
- `nextcloud_create_event` checks the target range for busy events (recurring
  series are expanded, overrides and `EXDATE` applied, `TRANSP:TRANSPARENT` and
  `STATUS:CANCELLED` skipped) and refuses to create an overlapping event unless
  `allow_overlap=true`; the refusal lists the conflicting events.
- Times are ISO 8601. Offsets are kept as given and stored as UTC; times
  without an offset, all-day events and the default listing range use
  `NEXTCLOUD_MCP_TIMEZONE` (default: the host's local timezone). Recurrence
  rules are expanded for `DAILY`, `WEEKLY`, `MONTHLY` and `YEARLY`; other
  frequencies are reported as a warning and not expanded.

Each tool carries the JSON Schema returned as `inputSchema` by `tools/list`.
`outputSchema`/`structuredContent` are **not implemented**: results are
returned as a text content block. The exact tool names and input schemas are
stable; they are not changed between minor versions.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `NEXTCLOUD_MCP_PORT` | `5811` | Listening port. |
| `NEXTCLOUD_MCP_BIND` | `127.0.0.1` | Bind address. Keep the loopback and use a reverse proxy. |
| `NEXTCLOUD_MCP_BASE_URL` | `http://127.0.0.1:8080` | Nextcloud base URL. |
| `NEXTCLOUD_MCP_TAG_API_URL` | *(empty)* | Tag API base URL. The tag tools need this **and** the token. |
| `NEXTCLOUD_MCP_TAG_API_TOKEN` | *(empty)* | Tag API bearer token. |
| `NEXTCLOUD_MCP_MUSIC_ROOT` | `/music` | Music root (DAV path). Limits the MP3 tag tools and defines `{root}/Converted`. |
| `NEXTCLOUD_MCP_TIMEZONE` | *(empty)* | IANA timezone for calendar times without an offset, all-day events and the default listing range. Empty uses the host's local timezone. |
| `NEXTCLOUD_MCP_WRITE_DENY` | *(empty)* | Extra write-deny prefixes, comma-separated absolute paths. Their ancestors are refused too. |
| `NEXTCLOUD_MCP_ALLOWED_ORIGINS` | *(empty)* | Origin allow-list, comma-separated (for example `https://nextcloud-mcp.example.net`). Recommended for browser clients. |
| `NEXTCLOUD_MCP_TMP` | `/var/tmp/nextcloud-mcp` | Scratch directory for archive downloads/extractions. Use disk (or a named volume), not tmpfs. |
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
`{"status":"ok","server":"nextcloud","version":"1.1.0","read_only":false,"tag_tools":false}`.

## Running with Docker

```bash
cp .env.example .env
$EDITOR .env

docker compose up -d --build
curl -s http://127.0.0.1:5811/healthz
```

The compose file publishes `127.0.0.1:5811` only, runs read-only with
`cap_drop: [ALL]` and `no-new-privileges`, mounts tmpfs at `/tmp`, and mounts
the named volume `nextcloud-mcp-tmp` at `/var/tmp/nextcloud-mcp`
(`NEXTCLOUD_MCP_TMP`). The volume keeps multi-GB archive work off RAM and out
of the container filesystem; remove it with `docker volume rm
nextcloud-mcp_nextcloud-mcp-tmp` if you want to clear leftover scratch files.

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

**Use a Nextcloud application password, not the account password.** It can be
revoked in *Settings → Security* without touching the account, and it survives
2FA. `<base64(user:app-password)>` below is the standard Basic credential:
`printf '%s' 'alice:app-password' | base64`.

## Connecting an MCP client

Any MCP client that speaks Streamable HTTP works. The endpoint is
`http://<host>:5811/mcp` (or the HTTPS host in front of it) and the
`Authorization` header must carry the Nextcloud credentials of the user whose
files the agent may touch. Server-to-server clients send no `Origin` header;
if one is sent, it must match `NEXTCLOUD_MCP_ALLOWED_ORIGINS` or the `Host`.

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

Claude Code (`.mcp.json` in the project, or `claude mcp add --transport http`):

```json
{
  "mcpServers": {
    "nextcloud": {
      "type": "http",
      "url": "https://nextcloud-mcp.example.net/mcp",
      "headers": {
        "Authorization": "Basic <base64(user:app-password)>"
      }
    }
  }
}
```

Smoke test without a client:

```bash
curl -s -u 'alice:app-password' -X POST http://127.0.0.1:5811/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

### TLS termination

The server itself speaks plain HTTP; terminate TLS in front of it. Caddy:

```caddyfile
nextcloud-mcp.example.net {
    reverse_proxy 127.0.0.1:5811
}
```

nginx:

```nginx
server {
    listen 443 ssl;
    server_name nextcloud-mcp.example.net;
    # ssl_certificate /etc/letsencrypt/live/.../fullchain.pem;
    # ssl_certificate_key /etc/letsencrypt/live/.../privkey.pem;
    location / {
        proxy_pass http://127.0.0.1:5811;
        proxy_set_header Host $host;
        proxy_set_header Authorization $http_authorization;
        proxy_set_header Origin $http_origin;
        proxy_http_version 1.1;
    }
}
```

Set `NEXTCLOUD_MCP_ALLOWED_ORIGINS=https://nextcloud-mcp.example.net` when a
browser-based MCP client (or an attacker page trying DNS rebinding) may send
an `Origin` header; the reverse proxy must forward the header.

## Security notes

- **Reverse proxy first.** The server speaks plain HTTP and trusts the
  `Authorization` header as-is. Expose it through an HTTPS reverse proxy
  (Caddy, nginx, ...) and keep `NEXTCLOUD_MCP_BIND=127.0.0.1` (or the Docker
  loopback port). The tag API token is a bearer secret: never expose the tag
  API port either.
- **Origin checks.** Requests with an `Origin` header must match
  `NEXTCLOUD_MCP_ALLOWED_ORIGINS`, or the `Host` header when the list is empty;
  others get `403` (DNS-rebinding protection). Requests without `Origin` stay
  allowed for server-to-server compatibility.
- **Per-user credentials.** MCP credentials equal Nextcloud credentials. Hand
  each user their own app password, and revoke it in Nextcloud when it leaks.
  Users can never see more than their own account allows.
- **Tag API is single-user.** The tag API is reached with one shared bearer
  token and edits files in the shared music library directly, bypassing the
  caller's Nextcloud ACL. Only run the tag tools in a single-user (or fully
  trusted) deployment until per-user tag ACLs exist.
- **Read-only deployments.** Set `NEXTCLOUD_MCP_READ_ONLY=1` for agents that
  should only read. Add `NEXTCLOUD_MCP_WRITE_DENY` for folders that must stay
  untouched even then; ancestors of a denied prefix are refused as well.
- **Partial extractions.** `nextcloud_extract_archive` uploads file by file
  with no rollback: if an upload fails halfway, the already-extracted files
  stay in the target folder. Check the `files`/`bytes` result and clean up the
  target before retrying.
- **Rate and body limits** (`NEXTCLOUD_MCP_MAX_BODY_BYTES`, per-tool byte
  limits) protect the service from oversized payloads and zip bombs.

## Development

The implementation and the tests use the standard library only (Python 3.10 or
newer; `ast` verifies the imports, CI runs 3.10–3.13). `ruff` (E, F, W, I) is
used for lint and is not a runtime dependency.

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile nextcloud_mcp.py
ruff check .
docker build -t nextcloud-mcp:dev .
```

The test suite covers path normalization and write guards, archive safety
(including zip-slip and symlinks), tag API gating, JSON-RPC dispatch, Origin
validation, and real HTTP round trips against fake Nextcloud/tag API servers.

## Future work

- **Calendar updates and deletions** (`nextcloud_update_event` /
  `nextcloud_delete_event`) and free/busy-aware suggestions.
- **stdio / `uvx` transport** for local, single-user agents (no reverse proxy).
- **OAuth** so MCP clients can obtain per-user tokens instead of forwarding
  Basic credentials, and a per-user tag API ACL.

Calendar write support is create-only in 1.1; the other items are out of scope
for 1.x. HTTP + Basic/app password is the supported transport today.

## 日本語の概要

Nextcloud MCPサーバーは、AIエージェント（opencode等）にNextcloudの
ファイル操作・MP3タグ編集・圧縮/解凍・カレンダー予定の一覧/追加をMCPツール
として渡す、標準ライブラリだけで動く小さなサーバーです。呼び出し元が送った
`Authorization` ヘッダーをそのままNextcloudへ転送するので、権限・共有・
クォータ・ゴミ箱はすべてNextcloud側のACLに従い、サーバーは資格情報を保存
しません。

カレンダーは `nextcloud_list_calendars`・`nextcloud_list_events`・
`nextcloud_create_event` の3ツールです。CalDAVのカレンダーは呼び出し元
自身のコレクションと共有権限に従い、`VEVENT` を受け取れて書き込み可能な
カレンダーにだけ予定を追加できます。Nextcloudのカレンダー拡張（Calendar
アプリ）が有効でない場合（`/ocs/v2.php/cloud/capabilities` に `calendar` が
無い場合）は、汎用の失敗ではなく「Calendarアプリが有効ではない」と明確な
エラーを返します。`nextcloud_create_event` は指定時間帯の既存予定（繰り返し
予定は展開し、上書き・EXDATE・`TRANSP:TRANSPARENT`・`STATUS:CANCELLED` を
考慮）と重なる場合、かぶった予定を列挙して追加を拒否します。`allow_overlap=true`
で強制追加でき、かぶりを避けたいAIは候補ごとに1件ずつ試してスキップできます。
時刻はISO 8601で、オフセット付きはUTCとして保存、オフセット無し・終日予定・
一覧の既定範囲は `NEXTCLOUD_MCP_TIMEZONE`（既定はホストのローカルタイム）を
使います。繰り返し規則は `DAILY`・`WEEKLY`・`MONTHLY`・`YEARLY` を展開し、
それ以外は警告として報告します。

タグ編集ツール（`nextcloud_read_music_tags`・`nextcloud_search_musicbrainz`・
`nextcloud_write_music_tags`）は `NEXTCLOUD_MCP_TAG_API_URL` と
`NEXTCLOUD_MCP_TAG_API_TOKEN` の両方が設定されているときだけ `tools/list`
に現れます。音楽ルートは `NEXTCLOUD_MCP_MUSIC_ROOT`（既定 `/music`）、
書き込み禁止パスは `{music_root}/Converted` と
`NEXTCLOUD_MCP_WRITE_DENY` で決まり、その祖先（保護対象を含む親フォルダー）も
拒否されます。`NEXTCLOUD_MCP_READ_ONLY=1` にすると書き込み系ツールがすべて
隠されます。

1.0.1では、全ツール呼び出しの前に `whoami` で資格情報を検証し（401/403は
ツールエラー）、タグ読み出し前の `stat`、祖先を含む書き込み禁止判定、
展開メンバーごとの書き込み判定、`max_bytes` の下限1バイト化、アーカイブの
ダウンロード前サイズ検査、`Origin` 検証（`NEXTCLOUD_MCP_ALLOWED_ORIGINS`）を
追加しました。タグAPIは共有ライブラリを直接書き換えるため、当面は単一
ユーザー運用が前提です。アプリパスワードの利用を推奨します。

1.1.0ではカレンダー3ツールを追加しました。カレンダー書き込みは追加のみで、
更新・削除は今後の対応です。

想定配備はDocker（`compose.yaml`、ホストの `127.0.0.1:5811` のみ公開）か
systemdユニットで、どちらも前段にHTTPSリバースプロキシを置く前提です。
一時領域 `/var/tmp/nextcloud-mcp` はDockerでは名前付きボリューム
`nextcloud-mcp-tmp` に置かれます（`/tmp` のみtmpfs）。テストは
`python3 -m unittest discover -s tests -v` で実行できます。
