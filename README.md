# Discord to Matrix Importer

Import a Discord channel export ([DiscordChatExporter](https://github.com/Tyrrrz/DiscordChatExporter) JSON) into a Matrix room using the Application Service (AS) API. Messages are sent with their original timestamps and sender identities preserved.

## Features

- **End-to-end encrypted** by default (Megolm via matrix-nio's crypto engine)
- Parses DiscordChatExporter's `export.json` format (guild channels with multiple authors)
- Converts Discord markdown (\*\*bold\*\*, \*italic\*, \_\_underline\_\_, \~\~strikethrough\~\~, \`code\`) to HTML
- Downloads attachments from Discord CDN and re-uploads to Matrix (`m.image` for images, `m.file` for others)
- **Configurable sender mapping**: map Discord nicknames to existing Matrix users; unmapped authors get ghost users automatically
- Creates a named group room (e.g. "guild-updates (Clique)")
- Resumable: tracks progress in `import_progress.json` so interrupted imports can continue
- Idempotent message sending via deterministic transaction IDs
- Built-in rate-limit and retry handling

## Docker (recommended)

The Docker image bundles libolm and all Python dependencies, avoiding native build issues (especially on macOS).

```bash
# Build the image
docker compose build

# Place your Discord export in ./chat/ then run:
docker compose run import --dry-run

# Real import (set env vars in your shell or a .env file)
export MATRIX_AS_TOKEN='...'
export HOMESERVER_URL='https://matrix.example.com'
export OWNER_MXID='@user:example.com'
export SERVER_NAME='example.com'
export SENDER_MAP='{"Malo": "@malo:example.com", "Aahsoka": "@nereyde:example.com"}'
docker compose run import
```

The `./chat` directory is mounted into the container at `/data`. Put your `export.json` there.

You can also pass flags directly:

```bash
docker compose run import --no-encryption --fresh
```

## Prerequisites (native)

- Python 3.10+
- A Matrix homeserver running [Synapse](https://github.com/element-hq/synapse)
- An application service registered with the homeserver
- `libolm` 3.x (required for E2EE)

```bash
# System dependency (for E2EE)
brew install libolm          # macOS
# apt install libolm-dev     # Debian/Ubuntu

# Python dependencies
pip install requests Pillow "matrix-nio[e2e]"
```

`Pillow` is optional (only needed for image dimension metadata). `matrix-nio[e2e]` is optional if you use `--no-encryption`.

## Quick start

### 1. Prepare the Discord export

Export a Discord channel using [DiscordChatExporter](https://github.com/Tyrrrz/DiscordChatExporter) in **JSON** format. Place the `export.json` file in the `./chat/` directory (or use `--chat-dir` to point elsewhere).

```bash
python import_discord_to_matrix.py --chat-dir /path/to/export/folder --dry-run
```

### 2. Generate the appservice registration

```bash
python import_discord_to_matrix.py --generate-config --server-name example.com \
  --owner-mxid '@user:example.com' \
  --sender-map '{"Malo": "@malo:example.com", "Aahsoka": "@nereyde:example.com"}'
```

This prints a YAML registration file and setup instructions for your homeserver. Save the YAML to your server and register it with Synapse.

### 3. Set environment variables

```bash
export MATRIX_AS_TOKEN='<as_token from the generated YAML>'
export HOMESERVER_URL='https://matrix.example.com'
export OWNER_MXID='@user:example.com'
export SERVER_NAME='example.com'
export SENDER_MAP='{"Malo": "@malo:example.com", "Aahsoka": "@nereyde:example.com"}'
```

### 4. Dry run

Verify that the export parses correctly without sending anything:

```bash
python import_discord_to_matrix.py --dry-run
```

The dry run lists all messages, shows sender mappings (which Discord authors map to which Matrix users), and flags any unmapped authors that will get ghost users.

### 5. Import

```bash
python import_discord_to_matrix.py
```

The script will:

1. Register ghost users for any unmapped Discord authors (e.g. `@discord_raid-helper:example.com`)
2. Create an encrypted group room named after the channel (or reuse an existing one)
3. Set up E2EE (login crypto devices for all users, share Megolm sessions)
4. Send all messages encrypted with original timestamps, downloading and re-uploading attachments
5. Export Megolm session keys to `megolm_keys.txt`

After import, import the keys into Element: **Settings > Security & Privacy > Import E2E room keys** (passphrase: `import-discord`).

To skip encryption: `python import_discord_to_matrix.py --no-encryption`

## Sender mapping

The `--sender-map` flag (or `SENDER_MAP` env var) is a JSON object mapping Discord display names to Matrix MXIDs:

```json
{"Malo": "@malo:example.com", "Aahsoka": "@nereyde:example.com"}
```

- **Mapped authors** send messages as the specified real Matrix user
- **Unmapped authors** automatically get a ghost user created: `@discord_<username>:server`
- The `OWNER_MXID` must appear as one of the values in the sender map — it's used for room creation

Use `--dry-run` to see all unique authors in the export and plan your mapping.

## Configuration

All options can be set via CLI flags or environment variables:

| Flag | Env var | Default | Description |
|------|---------|---------|-------------|
| `--homeserver-url` | `HOMESERVER_URL` | `http://localhost:8008` | Matrix homeserver URL |
| `--as-token` | `MATRIX_AS_TOKEN` | — | Appservice access token (required) |
| `--owner-mxid` | `OWNER_MXID` | — | Your Matrix user ID (required) |
| `--server-name` | `SERVER_NAME` | — | Matrix server name (required) |
| `--sender-map` | `SENDER_MAP` | — | JSON mapping Discord names to Matrix MXIDs |
| `--room-id` | `MATRIX_ROOM_ID` | — | Import into an existing room |
| `--chat-dir` | `CHAT_DIR` | Script directory | Path to Discord export folder |
| `--no-encryption` | — | — | Send plaintext instead of E2EE |
| `--dry-run` | — | — | Parse only, don't send |
| `--fresh` | — | — | Delete progress and start a fresh import |
| `--generate-config` | — | — | Print appservice YAML and exit |

## Resuming an interrupted import

Progress is saved to `import_progress.json` after each message. Re-running the script will skip already-sent messages and continue where it left off.

To start fresh, re-run with `--fresh` to delete the progress file and create a new room.

## End-to-end encryption

By default, messages are **end-to-end encrypted** using Megolm (the same algorithm Matrix/Element uses). The script uses matrix-nio's crypto engine to encrypt message content client-side, then sends the encrypted payloads through the appservice API with original timestamps.

**How it works:**

1. A temporary crypto device is created for each Matrix user (owner + mapped users + ghosts) and logged in via appservice auth
2. Device keys are uploaded and Megolm group sessions are shared among all participants
3. Each message is encrypted with the sender's Megolm session before being sent
4. Attachments are encrypted client-side (AES-CTR) and uploaded as `application/octet-stream`
5. Session keys are exported to `megolm_keys.txt` for import into Element

**Runtime artifacts** (created in the chat directory):

- `.e2ee_store/` — nio crypto state (Olm accounts, Megolm sessions)
- `nio_credentials.json` — device IDs and access tokens for the crypto devices
- `megolm_keys.txt` — exported Megolm session keys

**Re-runs** reuse the existing crypto state. To fully reset E2EE state, delete `.e2ee_store/` and `nio_credentials.json`.

Use `--no-encryption` to send plaintext messages instead (no `libolm` or `matrix-nio` needed).
