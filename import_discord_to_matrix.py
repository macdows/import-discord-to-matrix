#!/usr/bin/env python3
"""
Import a Discord channel export (DiscordChatExporter JSON) into a Matrix
Synapse room using the Application Service API.

Usage:
    # 1. Generate the appservice YAML template
    python import_discord_to_matrix.py --generate-config

    # 2. Dry-run to verify parsing
    python import_discord_to_matrix.py --dry-run

    # 3. Real import
    python import_discord_to_matrix.py

Environment variables:
    MATRIX_AS_TOKEN   - Application service access token (required for real run)
    HOMESERVER_URL    - Matrix homeserver URL (default: http://localhost:8008)
    OWNER_MXID        - Your Matrix user ID (e.g. @user:example.com)
    SERVER_NAME       - Matrix server name (e.g. example.com)
    SENDER_MAP        - JSON mapping Discord display names → Matrix MXIDs
    CHAT_DIR          - Path to Discord export folder (default: script directory)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sys
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    from nio import AsyncClient, AsyncClientConfig, LocalProtocolError, SyncError
    from nio.crypto.attachments import encrypt_attachment
    HAS_NIO = True
except ImportError:
    HAS_NIO = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent

# Discord markdown → HTML
FORMAT_RULES = [
    # **bold**
    (re.compile(r"\*\*(.+?)\*\*", re.DOTALL), r"<strong>\1</strong>"),
    # *italic*
    (re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", re.DOTALL), r"<em>\1</em>"),
    # ~~strikethrough~~
    (re.compile(r"~~(.+?)~~", re.DOTALL), r"<del>\1</del>"),
    # __underline__
    (re.compile(r"__(.+?)__", re.DOTALL), r"<u>\1</u>"),
    # `code`
    (re.compile(r"`([^`]+?)`"), r"<code>\1</code>"),
]

# Message types to skip
SKIP_TYPES = {"ChannelPinnedMessage", "46"}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Import Discord channel export into Matrix via appservice API"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse and display messages without sending to Matrix"
    )
    parser.add_argument(
        "--fresh", action="store_true",
        help="Delete import_progress.json and start a fresh import"
    )
    parser.add_argument(
        "--generate-config", action="store_true",
        help="Print appservice YAML and setup instructions, then exit"
    )
    parser.add_argument(
        "--homeserver-url",
        default=os.environ.get("HOMESERVER_URL", "http://localhost:8008"),
        help="Matrix homeserver URL"
    )
    parser.add_argument(
        "--as-token",
        default=os.environ.get("MATRIX_AS_TOKEN"),
        help="Appservice access token (prefer MATRIX_AS_TOKEN env var)"
    )
    parser.add_argument(
        "--owner-mxid",
        default=os.environ.get("OWNER_MXID"),
        help="Your Matrix user ID, e.g. @user:example.com"
    )
    parser.add_argument(
        "--server-name",
        default=os.environ.get("SERVER_NAME"),
        help="Matrix server name, e.g. example.com"
    )
    parser.add_argument(
        "--sender-map",
        default=os.environ.get("SENDER_MAP"),
        help='JSON mapping Discord names → Matrix MXIDs, e.g. \'{"Malo": "@malo:example.com"}\''
    )
    parser.add_argument(
        "--room-id",
        default=os.environ.get("MATRIX_ROOM_ID"),
        help="Existing room ID to import into (skip room creation)"
    )
    parser.add_argument(
        "--chat-dir",
        default=os.environ.get("CHAT_DIR", str(SCRIPT_DIR)),
        help="Path to Discord export folder (default: script directory)"
    )
    parser.add_argument(
        "--no-encryption", action="store_true",
        help="Send messages as plaintext (default: end-to-end encrypted)"
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Discord export parsing
# ---------------------------------------------------------------------------

def parse_discord_export(export_path: Path) -> tuple[dict, list[dict]]:
    """Parse a DiscordChatExporter JSON file.

    Returns (channel_info, messages) where channel_info has guild/channel
    metadata and messages is a list of parsed message dicts.
    """
    with open(export_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    channel_info = {
        "guild_name": data.get("guild", {}).get("name", ""),
        "channel_name": data.get("channel", {}).get("name", ""),
        "channel_id": data.get("channel", {}).get("id", ""),
    }

    messages = []
    for msg in data.get("messages", []):
        msg_type = msg.get("type", "")

        # Skip system/meta message types
        if msg_type in SKIP_TYPES:
            continue

        content = msg.get("content", "") or ""
        attachments = msg.get("attachments", [])
        embeds = msg.get("embeds", [])

        # Skip empty messages (no content, no attachments)
        if not content and not attachments:
            continue

        author = msg.get("author", {})
        sender_name = author.get("nickname") or author.get("name", "Unknown")
        sender_username = author.get("name", "unknown")
        sender_id = author.get("id", "")

        # Parse ISO 8601 timestamp
        ts_str = msg.get("timestamp", "")
        dt = datetime.fromisoformat(ts_str)
        ts_ms = int(dt.timestamp() * 1000)

        parsed_attachments = []
        for att in attachments:
            parsed_attachments.append({
                "fileName": att.get("fileName", "file"),
                "url": att.get("url", ""),
                "fileSizeBytes": att.get("fileSizeBytes", 0),
            })

        messages.append({
            "id": msg.get("id", ""),
            "sender_name": sender_name,
            "sender_username": sender_username,
            "sender_id": sender_id,
            "timestamp_ms": ts_ms,
            "timestamp_dt": dt.isoformat(),
            "body": content,
            "attachments": parsed_attachments,
            "embeds": embeds,
            "is_reply": msg_type == "Reply",
            "reference_id": msg.get("reference", {}).get("messageId") if msg_type == "Reply" else None,
        })

    return channel_info, messages


def format_to_html(text: str) -> str | None:
    """Convert Discord markdown to HTML. Returns None if no formatting."""
    html = text
    changed = False
    for pattern, replacement in FORMAT_RULES:
        new_html = pattern.sub(replacement, html)
        if new_html != html:
            changed = True
            html = new_html

    if not changed:
        return None

    # Convert newlines to <br> for HTML
    html = html.replace("\n", "<br>\n")
    return html


# ---------------------------------------------------------------------------
# Matrix API helpers
# ---------------------------------------------------------------------------

class MatrixAPI:
    def __init__(self, homeserver_url: str, as_token: str):
        self.base = homeserver_url.rstrip("/")
        self.as_token = as_token
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {as_token}"
        self.txn_counter = int(time.time() * 1000)

    def _url(self, path: str) -> str:
        return f"{self.base}/_matrix/client/v3{path}"

    def _request(self, method: str, path: str, params: dict = None,
                 json_body: dict = None, data=None, headers=None,
                 max_retries: int = 5) -> requests.Response:
        url = self._url(path)
        for attempt in range(max_retries):
            try:
                resp = self.session.request(
                    method, url, params=params, json=json_body,
                    data=data, headers=headers, timeout=30
                )
                if resp.status_code == 429:
                    retry_ms = resp.json().get("retry_after_ms", 2000 * (attempt + 1))
                    print(f"  Rate limited, waiting {retry_ms}ms...")
                    time.sleep(retry_ms / 1000)
                    continue
                if resp.status_code >= 500:
                    wait = min(2 ** attempt, 30)
                    print(f"  Server error {resp.status_code}, retrying in {wait}s...")
                    time.sleep(wait)
                    continue
                return resp
            except requests.exceptions.RequestException as e:
                wait = min(2 ** attempt, 30)
                print(f"  Request error: {e}, retrying in {wait}s...")
                time.sleep(wait)

        # Final attempt without retry
        return self.session.request(
            method, url, params=params, json=json_body,
            data=data, headers=headers, timeout=30
        )

    def _next_txn(self) -> str:
        self.txn_counter += 1
        return str(self.txn_counter)

    def register_ghost(self, localpart: str) -> None:
        """Register an appservice ghost user (idempotent)."""
        resp = self._request("POST", "/register", json_body={
            "type": "m.login.application_service",
            "username": localpart,
        })
        if resp.status_code in (200, 409):
            print(f"  Ghost user @{localpart} registered (or already exists)")
        else:
            print(f"  Warning: register ghost returned {resp.status_code}: {resp.text}")

    def set_displayname(self, user_id: str, name: str) -> None:
        resp = self._request(
            "PUT", f"/profile/{user_id}/displayname",
            params={"user_id": user_id},
            json_body={"displayname": name}
        )
        if resp.status_code == 200:
            print(f"  Set display name for {user_id} → {name}")
        else:
            print(f"  Warning: set displayname returned {resp.status_code}: {resp.text}")

    def create_room(self, creator_user_id: str, name: str | None = None,
                    invite: list[str] = None, encrypted: bool = False) -> str:
        """Create a room as the given user. Returns room_id."""
        body = {
            "visibility": "private",
            "preset": "private_chat",
            "creation_content": {
                "m.federate": False,
            },
        }
        if name:
            body["name"] = name
        if invite:
            body["invite"] = invite
        if encrypted:
            body["initial_state"] = [{
                "type": "m.room.encryption",
                "state_key": "",
                "content": {"algorithm": "m.megolm.v1.aes-sha2"},
            }]

        resp = self._request(
            "POST", "/createRoom",
            params={"user_id": creator_user_id},
            json_body=body
        )
        resp.raise_for_status()
        room_id = resp.json()["room_id"]
        print(f"  Created room: {room_id}")
        return room_id

    def join_room(self, room_id: str, user_id: str) -> None:
        resp = self._request(
            "POST", f"/join/{room_id}",
            params={"user_id": user_id},
        )
        if resp.status_code == 200:
            print(f"  {user_id} joined {room_id}")
        else:
            print(f"  Warning: join returned {resp.status_code}: {resp.text}")

    def upload_file(self, file_path: Path, user_id: str) -> str:
        """Upload a file and return the mxc:// URI."""
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        file_data = file_path.read_bytes()

        url = f"{self.base}/_matrix/media/v3/upload"
        resp = self.session.post(
            url,
            params={
                "filename": file_path.name,
                "user_id": user_id,
            },
            data=file_data,
            headers={
                "Content-Type": content_type,
                "Authorization": f"Bearer {self.as_token}",
            },
            timeout=60,
        )
        resp.raise_for_status()
        mxc_uri = resp.json()["content_uri"]
        print(f"  Uploaded {file_path.name} → {mxc_uri}")
        return mxc_uri

    def send_message(self, room_id: str, user_id: str, ts_ms: int,
                     content: dict) -> str:
        """Send a message event with a specific timestamp. Returns event_id."""
        txn_hash = hashlib.sha256(
            f"{room_id}:{user_id}:{ts_ms}:{json.dumps(content, sort_keys=True)}".encode()
        ).hexdigest()[:16]

        resp = self._request(
            "PUT",
            f"/rooms/{room_id}/send/m.room.message/{txn_hash}",
            params={
                "user_id": user_id,
                "ts": str(ts_ms),
            },
            json_body=content,
        )
        resp.raise_for_status()
        return resp.json()["event_id"]

    def send_encrypted_message(self, room_id: str, user_id: str, ts_ms: int,
                               encrypted_content: dict) -> str:
        """Send an m.room.encrypted event with a specific timestamp."""
        txn_hash = hashlib.sha256(
            f"{room_id}:{user_id}:{ts_ms}:{json.dumps(encrypted_content, sort_keys=True)}".encode()
        ).hexdigest()[:16]

        resp = self._request(
            "PUT",
            f"/rooms/{room_id}/send/m.room.encrypted/{txn_hash}",
            params={
                "user_id": user_id,
                "ts": str(ts_ms),
            },
            json_body=encrypted_content,
        )
        resp.raise_for_status()
        return resp.json()["event_id"]

    def upload_data(self, data: bytes, filename: str, content_type: str,
                    user_id: str) -> str:
        """Upload raw bytes and return the mxc:// URI."""
        url = f"{self.base}/_matrix/media/v3/upload"
        resp = self.session.post(
            url,
            params={"filename": filename, "user_id": user_id},
            data=data,
            headers={
                "Content-Type": content_type,
                "Authorization": f"Bearer {self.as_token}",
            },
            timeout=60,
        )
        resp.raise_for_status()
        mxc_uri = resp.json()["content_uri"]
        print(f"  Uploaded {filename} → {mxc_uri}")
        return mxc_uri

    def ensure_room_encrypted(self, room_id: str, user_id: str) -> None:
        """Send m.room.encryption state event if room isn't already encrypted."""
        resp = self._request(
            "GET", f"/rooms/{room_id}/state/m.room.encryption",
            params={"user_id": user_id},
        )
        if resp.status_code == 200:
            print(f"  Room {room_id} already has encryption enabled")
            return

        resp = self._request(
            "PUT", f"/rooms/{room_id}/state/m.room.encryption/",
            params={"user_id": user_id},
            json_body={"algorithm": "m.megolm.v1.aes-sha2"},
        )
        resp.raise_for_status()
        print(f"  Enabled encryption for room {room_id}")


# ---------------------------------------------------------------------------
# E2EE helper (matrix-nio crypto engine)
# ---------------------------------------------------------------------------

class E2EEHelper:
    """Manages matrix-nio crypto clients for Megolm encryption.

    Creates an AsyncClient per user (owner + ghosts + mapped real users)
    used only for their crypto engine — actual events are sent via the
    appservice MatrixAPI.
    """

    def __init__(self, homeserver_url: str, as_token: str,
                 owner_mxid: str, all_mxids: list[str], chat_dir: str | Path):
        self.homeserver_url = homeserver_url
        self.as_token = as_token
        self.owner_mxid = owner_mxid
        self.all_mxids = all_mxids  # all unique Matrix users (owner + ghosts + mapped)
        self.chat_dir = Path(chat_dir)
        self.store_dir = self.chat_dir / ".e2ee_store"
        self.creds_file = self.chat_dir / "nio_credentials.json"
        self.clients: dict[str, AsyncClient] = {}
        self._loop = asyncio.new_event_loop()

    # -- internal helpers ---------------------------------------------------

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def _load_credentials(self) -> dict:
        if self.creds_file.exists():
            return json.loads(self.creds_file.read_text())
        return {}

    def _save_credentials(self, creds: dict):
        self.creds_file.write_text(json.dumps(creds, indent=2))

    def _appservice_login(self, user_id: str) -> tuple[str, str]:
        """POST /login with m.login.application_service → (access_token, device_id)."""
        resp = requests.post(
            f"{self.homeserver_url}/_matrix/client/v3/login",
            json={
                "type": "m.login.application_service",
                "identifier": {"type": "m.id.user", "user": user_id},
            },
            headers={"Authorization": f"Bearer {self.as_token}"},
            timeout=30,
        )
        if not resp.ok:
            print(f"  [DEBUG] Login as {user_id} failed: {resp.status_code} {resp.text}")
        resp.raise_for_status()
        data = resp.json()
        return data["access_token"], data["device_id"]

    async def _init_client(self, user_id: str) -> AsyncClient:
        """Create or restore a nio AsyncClient for one user."""
        localpart = user_id.split(":")[0].lstrip("@")
        store_path = str(self.store_dir / localpart)
        os.makedirs(store_path, exist_ok=True)

        config = AsyncClientConfig(
            encryption_enabled=True,
            store_sync_tokens=True,
        )
        client = AsyncClient(
            self.homeserver_url,
            user=user_id,
            store_path=store_path,
            config=config,
        )

        creds = self._load_credentials()
        if user_id in creds:
            client.restore_login(
                user_id=user_id,
                device_id=creds[user_id]["device_id"],
                access_token=creds[user_id]["access_token"],
            )
            print(f"  Restored nio login for {user_id} "
                  f"(device {creds[user_id]['device_id']})")
        else:
            access_token, device_id = self._appservice_login(user_id)
            client.restore_login(
                user_id=user_id,
                device_id=device_id,
                access_token=access_token,
            )
            creds[user_id] = {
                "access_token": access_token,
                "device_id": device_id,
            }
            self._save_credentials(creds)
            print(f"  Logged in {user_id} via appservice (device {device_id})")

        # Upload identity + one-time keys
        try:
            keys_resp = await client.keys_upload()
            print(f"  Keys upload for {user_id}: {type(keys_resp).__name__}")
        except LocalProtocolError:
            print(f"  Keys already uploaded for {user_id}, skipping")

        self.clients[user_id] = client
        return client

    # -- public synchronous API ---------------------------------------------

    def initialize(self, room_id: str):
        """Login all clients, sync, share group sessions."""
        self._run(self._initialize(room_id))

    async def _initialize(self, room_id: str):
        print("  Initializing E2EE crypto engine...")

        for mxid in self.all_mxids:
            await self._init_client(mxid)

        # Minimal sync to discover room state & members
        sync_filter = json.dumps({
            "room": {
                "rooms": [room_id],
                "timeline": {"limit": 1},
            },
            "presence": {"not_types": ["*"]},
            "account_data": {"not_types": ["*"]},
        })

        for mxid, client in self.clients.items():
            resp = await client.sync(timeout=30000, sync_filter=sync_filter)
            print(f"  Sync for {mxid}: {type(resp).__name__}")
            if isinstance(resp, SyncError):
                print(f"  Sync failed for {mxid}, re-logging in...")
                # Clear stale credentials and re-login
                creds = self._load_credentials()
                creds.pop(mxid, None)
                self._save_credentials(creds)
                access_token, device_id = self._appservice_login(mxid)
                client.restore_login(
                    user_id=mxid,
                    device_id=device_id,
                    access_token=access_token,
                )
                creds[mxid] = {
                    "access_token": access_token,
                    "device_id": device_id,
                }
                self._save_credentials(creds)
                print(f"  Re-logged in {mxid} (device {device_id})")
                # Upload keys for the new device
                try:
                    await client.keys_upload()
                except LocalProtocolError:
                    pass
                # Retry sync
                resp = await client.sync(timeout=30000, sync_filter=sync_filter)
                print(f"  Retry sync for {mxid}: {type(resp).__name__}")

        # Ensure device keys for room members are available
        for mxid, client in self.clients.items():
            try:
                resp = await client.keys_query()
                print(f"  Keys query for {mxid}: {type(resp).__name__}")
            except LocalProtocolError:
                print(f"  No key query needed for {mxid}, skipping")

        # Create outbound Megolm sessions & share inbound keys via to-device
        for mxid, client in self.clients.items():
            try:
                resp = await client.share_group_session(
                    room_id, ignore_unverified_devices=True,
                )
                print(f"  Shared group session for {mxid}: "
                      f"{type(resp).__name__}")
            except Exception as e:
                print(f"  Warning: share_group_session for {mxid}: {e}")

        # Sync again so each client receives the others' shared keys
        for mxid, client in self.clients.items():
            await client.sync(timeout=10000, sync_filter=sync_filter)

        print("  E2EE initialized")

    def encrypt_message(self, room_id: str, sender_mxid: str,
                        content: dict) -> dict:
        """Encrypt a plaintext m.room.message content dict → m.room.encrypted payload."""
        return self._run(self._encrypt_message(room_id, sender_mxid, content))

    async def _encrypt_message(self, room_id: str, sender_mxid: str,
                               content: dict) -> dict:
        client = self.clients[sender_mxid]

        plaintext = {
            "type": "m.room.message",
            "content": content,
            "room_id": room_id,
        }

        try:
            encrypted = client.olm.group_encrypt(room_id, plaintext)
        except Exception:
            # Outbound session may be missing — create & retry
            await client.share_group_session(
                room_id, ignore_unverified_devices=True,
            )
            encrypted = client.olm.group_encrypt(room_id, plaintext)

        return encrypted

    def encrypt_file(self, file_data: bytes) -> tuple[bytes, dict]:
        """Encrypt file bytes for upload. Returns (ciphertext, file_keys)."""
        return encrypt_attachment(file_data)

    def export_keys(self, output_path: str | Path, passphrase: str = "import-discord"):
        """Export all inbound Megolm session keys (for import into Element)."""
        self._run(self._export_keys(output_path, passphrase))

    async def _export_keys(self, output_path, passphrase):
        Path(output_path).unlink(missing_ok=True)
        client = self.clients[self.owner_mxid]
        await client.export_keys(str(output_path), passphrase)
        print(f"  Exported Megolm session keys → {output_path}")

    def close(self, delete_devices: bool = False):
        if delete_devices:
            self._delete_import_devices()
        for client in self.clients.values():
            self._run(client.close())
        self._loop.close()

    def _delete_import_devices(self):
        """Delete temporary import devices from the server and clean up local state."""
        creds = self._load_credentials()
        for user_id, info in creds.items():
            device_id = info["device_id"]
            access_token = info["access_token"]
            try:
                resp = requests.post(
                    f"{self.homeserver_url}/_matrix/client/v3/delete_devices",
                    json={
                        "devices": [device_id],
                        "auth": {
                            "type": "m.login.application_service",
                            "identifier": {"type": "m.id.user", "user": user_id},
                        },
                    },
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=30,
                )
                if resp.status_code in (200, 401):
                    if resp.status_code == 401:
                        session = resp.json().get("session")
                        resp = requests.post(
                            f"{self.homeserver_url}/_matrix/client/v3/delete_devices",
                            json={
                                "devices": [device_id],
                                "auth": {
                                    "type": "m.login.application_service",
                                    "identifier": {"type": "m.id.user", "user": user_id},
                                    "session": session,
                                },
                            },
                            headers={"Authorization": f"Bearer {access_token}"},
                            timeout=30,
                        )
                    resp.raise_for_status()
                    print(f"  Deleted import device {device_id} for {user_id}")
                else:
                    resp.raise_for_status()
            except Exception as e:
                print(f"  Warning: could not delete device {device_id} "
                      f"for {user_id}: {e}")

        # Clean up local crypto state (devices no longer exist)
        self.creds_file.unlink(missing_ok=True)
        if self.store_dir.exists():
            shutil.rmtree(self.store_dir)
        print("  Cleaned up local crypto state")


# ---------------------------------------------------------------------------
# Appservice config generation
# ---------------------------------------------------------------------------

def generate_appservice_config(server_name: str, owner_mxid: str | None = None,
                               sender_map: dict[str, str] | None = None):
    import secrets
    as_token = secrets.token_hex(32)
    hs_token = secrets.token_hex(32)

    escaped_server = server_name.replace(".", "\\\\.")
    owner_localpart = owner_mxid.split(":")[0].lstrip("@") if owner_mxid else "USER"

    # Build user namespace entries
    user_entries = []
    # Owner (non-exclusive)
    user_entries.append(
        f"    - exclusive: false\n      regex: '@{owner_localpart}:{escaped_server}'"
    )
    # Mapped real users (non-exclusive)
    if sender_map:
        seen = {owner_mxid}
        for mxid in sender_map.values():
            if mxid not in seen:
                lp = mxid.split(":")[0].lstrip("@")
                user_entries.append(
                    f"    - exclusive: false\n      regex: '@{lp}:{escaped_server}'"
                )
                seen.add(mxid)
    # Ghost wildcard (exclusive)
    user_entries.append(
        f"    - exclusive: true\n      regex: '@discord_.*:{escaped_server}'"
    )

    users_yaml = "\n".join(user_entries)

    yaml_content = f"""# Application Service registration for Discord import
# Place this file on your server and register it with Synapse

id: discord-import
url: ''
as_token: {as_token}
hs_token: {hs_token}
sender_localpart: _discord_import
namespaces:
  users:
{users_yaml}
  rooms: []
  aliases: []
rate_limited: false
"""

    print("=" * 60)
    print("APPSERVICE REGISTRATION YAML")
    print("=" * 60)
    print(yaml_content)
    print("=" * 60)
    print()
    print("SETUP INSTRUCTIONS (matrix-docker-ansible-deploy playbook)")
    print("=" * 60)
    print()
    print("1. Save the YAML above to a file on your server, e.g.:")
    print("     /matrix/synapse/config/appservice-discord-import.yaml")
    print()
    print("2. In your playbook's inventory/host_vars/matrix.DOMAIN/vars.yml, add:")
    print()
    print("   matrix_synapse_configuration_extension_yaml: |")
    print("     app_service_config_files:")
    print("       - /data/appservice-discord-import.yaml")
    print()
    print("   Or if you already have app_service_config_files, append to the list.")
    print()
    print("   Alternatively, if your playbook version supports it:")
    print("   matrix_synapse_app_service_config_files_auto: []")
    print("   matrix_synapse_app_service_config_files_custom:")
    print("     - /matrix/synapse/config/appservice-discord-import.yaml")
    print()
    print("3. Re-run the playbook:")
    print("     just run-tags setup-synapse,start")
    print()
    print("4. Install E2EE dependencies (for encrypted import):")
    print("     brew install libolm          # macOS")
    print("     # apt install libolm-dev     # Debian/Ubuntu")
    print('     pip install "matrix-nio[e2e]"')
    print()
    print("5. Set the environment variable and run this script:")
    print(f"     export MATRIX_AS_TOKEN='{as_token}'")
    print(f"     export HOMESERVER_URL='https://matrix.YOURDOMAIN.com'")
    print(f"     export OWNER_MXID='@{owner_localpart}:{server_name}'")
    print(f"     export SERVER_NAME='{server_name}'")
    print(f"     export SENDER_MAP='{{\"DiscordNick\": \"@user:{server_name}\"}}'")
    print()
    print(f"   Then:  python import_discord_to_matrix.py --dry-run")
    print(f"   Then:  python import_discord_to_matrix.py")
    print(f"   (add --no-encryption to skip E2EE)")
    print()


# ---------------------------------------------------------------------------
# Progress tracking
# ---------------------------------------------------------------------------

def load_progress(progress_file: Path) -> dict:
    if progress_file.exists():
        return json.loads(progress_file.read_text())
    return {"sent_indices": [], "room_id": None}


def save_progress(progress: dict, progress_file: Path):
    progress_file.write_text(json.dumps(progress, indent=2))


# ---------------------------------------------------------------------------
# Sender mapping
# ---------------------------------------------------------------------------

def build_sender_map(sender_map_json: str | None, messages: list[dict],
                     server_name: str) -> dict[str, str]:
    """Build a mapping from Discord display name → Matrix MXID.

    Configured names map to real users; unmapped authors get ghost MXIDs.
    """
    configured = {}
    if sender_map_json:
        configured = json.loads(sender_map_json)

    # Discover all unique senders
    all_senders = {}
    for msg in messages:
        name = msg["sender_name"]
        if name not in all_senders:
            all_senders[name] = msg["sender_username"]

    # Build final map
    result = {}
    for name, username in all_senders.items():
        if name in configured:
            result[name] = configured[name]
        else:
            # Auto-generate ghost MXID from username
            safe_username = re.sub(r"[^a-z0-9._=-]", "_", username.lower())
            result[name] = f"@discord_{safe_username}:{server_name}"

    return result


def get_ghost_users(sender_map: dict[str, str], server_name: str) -> dict[str, str]:
    """Return {mxid: display_name} for all ghost users in the sender map."""
    ghosts = {}
    prefix = f"@discord_"
    for display_name, mxid in sender_map.items():
        if mxid.startswith(prefix):
            ghosts[mxid] = display_name
    return ghosts


# ---------------------------------------------------------------------------
# Attachment helpers
# ---------------------------------------------------------------------------

def download_discord_attachment(url: str) -> bytes:
    """Download a file from Discord CDN."""
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content


def get_file_info(filename: str, data: bytes) -> dict:
    """Get file metadata for Matrix upload."""
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    info = {
        "size": len(data),
        "mimetype": content_type,
    }
    if content_type.startswith("image/") and HAS_PIL:
        try:
            import io
            with Image.open(io.BytesIO(data)) as img:
                info["w"], info["h"] = img.size
        except Exception:
            pass
    return info


def is_image(filename: str) -> bool:
    content_type = mimetypes.guess_type(filename)[0] or ""
    return content_type.startswith("image/")


# ---------------------------------------------------------------------------
# Main import logic
# ---------------------------------------------------------------------------

def do_dry_run(channel_info: dict, messages: list[dict],
               sender_map: dict[str, str]):
    """Print parsed messages without sending to Matrix."""
    channel = channel_info["channel_name"]
    guild = channel_info["guild_name"]
    print(f"\nChannel: {channel} ({guild})")
    print(f"Parsed {len(messages)} messages:\n")

    for i, msg in enumerate(messages):
        mxid = sender_map.get(msg["sender_name"], f"?{msg['sender_name']}")
        ts = msg["timestamp_dt"]
        body = msg["body"]

        # Truncate long bodies for display
        if len(body) > 120:
            body_display = body[:120] + "..."
        else:
            body_display = body

        print(f"[{i:3d}] {ts}  {mxid}")
        if body:
            lines = body_display.split("\n")
            print(f"      text: {lines[0]}")
            if len(lines) > 1:
                print(f"            (+ {len(body.split(chr(10))) - 1} more lines)")
        for att in msg["attachments"]:
            print(f"      attachment: {att['fileName']} ({att['fileSizeBytes']} bytes)")

        html = format_to_html(msg["body"])
        if html:
            print(f"      (has HTML formatting)")
        print()

    # Summary
    senders = sorted(set(m["sender_name"] for m in messages))
    attach_count = sum(len(m["attachments"]) for m in messages)
    ghosts = get_ghost_users(sender_map, "")
    print(f"Summary: {len(messages)} messages, {len(senders)} senders, "
          f"{attach_count} attachments")
    print(f"\nSender mapping:")
    for name in senders:
        mxid = sender_map.get(name, "???")
        label = " (ghost)" if mxid.startswith("@discord_") else " (mapped)"
        print(f"  {name} → {mxid}{label}")


def do_import(channel_info: dict, messages: list[dict],
              sender_map: dict[str, str], args, chat_dir: Path,
              progress_file: Path):
    """Send all messages to Matrix."""
    if not HAS_REQUESTS:
        sys.exit("Missing dependency: requests\n  pip install requests")
    if not args.as_token:
        sys.exit("Error: MATRIX_AS_TOKEN is required for import.\n"
                 "Set it via --as-token or the MATRIX_AS_TOKEN env var.\n"
                 "Run with --generate-config to create the appservice registration.")

    use_encryption = not args.no_encryption
    if use_encryption and not HAS_NIO:
        sys.exit("Missing dependency for E2EE: matrix-nio[e2e]\n"
                 '  pip install "matrix-nio[e2e]"\n'
                 "  Also requires libolm 3.x (brew install libolm)\n"
                 "  Or run with --no-encryption to skip E2EE.")

    server_name = args.server_name
    api = MatrixAPI(args.homeserver_url, args.as_token)
    progress = load_progress(progress_file)
    e2ee = None

    # Collect all unique Matrix users
    all_mxids = sorted(set(sender_map.values()))
    ghosts = get_ghost_users(sender_map, server_name)
    owner_mxid = args.owner_mxid

    total_steps = 5 if use_encryption else 4

    # Step 1: Register ghost users
    print(f"\n[1/{total_steps}] Registering ghost users...")
    for ghost_mxid, display_name in ghosts.items():
        localpart = ghost_mxid.split(":")[0].lstrip("@")
        api.register_ghost(localpart)
        api.set_displayname(ghost_mxid, display_name)

    # Step 2: Create or reuse room
    room_id = args.room_id or progress.get("room_id")
    channel = channel_info["channel_name"]
    guild = channel_info["guild_name"]
    # Clean emoji prefixes from channel name (e.g. "📣│guild-updates" → "guild-updates")
    clean_channel = re.sub(r"^[^\w]+", "", channel)
    room_name = f"{clean_channel} ({guild})" if guild else clean_channel

    if room_id:
        print(f"\n[2/{total_steps}] Using existing room: {room_id}")
        if use_encryption:
            api.ensure_room_encrypted(room_id, owner_mxid)
    else:
        print(f"\n[2/{total_steps}] Creating room '{room_name}'...")
        other_mxids = [m for m in all_mxids if m != owner_mxid]
        room_id = api.create_room(
            creator_user_id=owner_mxid,
            name=room_name,
            invite=other_mxids,
            encrypted=use_encryption,
        )
        for mxid in other_mxids:
            api.join_room(room_id, mxid)
        progress["room_id"] = room_id
        save_progress(progress, progress_file)

    # Step 3: Initialize E2EE (if enabled)
    if use_encryption:
        print(f"\n[3/{total_steps}] Setting up end-to-end encryption...")
        e2ee = E2EEHelper(
            args.homeserver_url, args.as_token,
            owner_mxid, all_mxids, chat_dir,
        )
        e2ee.initialize(room_id)

    # Step N: Send messages
    msg_step = 4 if use_encryption else 3
    mode = "encrypted" if use_encryption else "plaintext"
    print(f"\n[{msg_step}/{total_steps}] Sending {len(messages)} messages ({mode})...")
    sent_set = set(progress.get("sent_indices", []))
    event_count = 0

    for i, msg in enumerate(messages):
        if i in sent_set:
            continue

        mxid = sender_map.get(msg["sender_name"])
        if not mxid:
            print(f"  Warning: unknown sender '{msg['sender_name']}', skipping")
            continue

        ts_ms = msg["timestamp_ms"]

        # Send text if present
        if msg["body"]:
            content = {
                "msgtype": "m.text",
                "body": msg["body"],
            }
            html = format_to_html(msg["body"])
            if html:
                content["format"] = "org.matrix.custom.html"
                content["formatted_body"] = html

            if e2ee:
                encrypted = e2ee.encrypt_message(room_id, mxid, content)
                event_id = api.send_encrypted_message(
                    room_id, mxid, ts_ms, encrypted)
            else:
                event_id = api.send_message(room_id, mxid, ts_ms, content)
            event_count += 1
            print(f"  [{i}] text from {msg['sender_name'][:20]} → {event_id}")

        # Send attachments
        for att in msg["attachments"]:
            filename = att["fileName"]
            url = att["url"]
            try:
                print(f"  [{i}] Downloading {filename}...")
                file_data = download_discord_attachment(url)
                file_info = get_file_info(filename, file_data)
                content_type = file_info.get("mimetype", "application/octet-stream")
                msgtype = "m.image" if is_image(filename) else "m.file"

                if e2ee:
                    ciphertext, file_keys = e2ee.encrypt_file(file_data)
                    mxc_uri = api.upload_data(
                        ciphertext, filename,
                        "application/octet-stream", mxid,
                    )
                    content = {
                        "msgtype": msgtype,
                        "body": filename,
                        "info": file_info,
                        "file": {
                            "url": mxc_uri,
                            "mimetype": content_type,
                            **file_keys,
                        },
                    }
                    encrypted = e2ee.encrypt_message(room_id, mxid, content)
                    event_id = api.send_encrypted_message(
                        room_id, mxid, ts_ms, encrypted)
                else:
                    mxc_uri = api.upload_data(
                        file_data, filename, content_type, mxid)
                    content = {
                        "msgtype": msgtype,
                        "body": filename,
                        "url": mxc_uri,
                        "info": file_info,
                    }
                    event_id = api.send_message(room_id, mxid, ts_ms, content)
                event_count += 1
                print(f"  [{i}] {msgtype} from {msg['sender_name'][:20]} → {event_id}")
            except Exception as e:
                print(f"  [{i}] Warning: failed to send attachment {filename}: {e}")

        sent_set.add(i)
        progress["sent_indices"] = sorted(sent_set)
        save_progress(progress, progress_file)

    # Final step: Done (+ key export for E2EE)
    done_step = total_steps
    print(f"\n[{done_step}/{total_steps}] Import complete!")
    print(f"  Room: {room_id}")
    print(f"  Events sent: {event_count}")
    print(f"  Progress saved to: {progress_file}")

    if e2ee:
        keys_file = chat_dir / "megolm_keys.txt"
        passphrase = "import-discord"
        e2ee.export_keys(keys_file, passphrase)
        print("\n  Deleting temporary import devices...")
        e2ee.close(delete_devices=True)
        print()
        print("  E2EE KEY EXPORT")
        print("  " + "-" * 40)
        print(f"  Keys file: {keys_file}")
        print(f"  Passphrase: {passphrase}")
        print()
        print("  To decrypt messages in Element:")
        print("  1. Open Element → Settings → Security & Privacy")
        print("  2. Click 'Import E2E room keys'")
        print(f"  3. Select: {keys_file}")
        print(f"  4. Enter passphrase: {passphrase}")
        print("  5. Messages should now show with a lock icon")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    if args.generate_config:
        server_name = args.server_name
        if not server_name:
            server_name = input("Enter your Matrix server name (e.g. example.com): ").strip()
        sender_map_parsed = None
        if args.sender_map:
            sender_map_parsed = json.loads(args.sender_map)
        generate_appservice_config(server_name, args.owner_mxid, sender_map_parsed)
        return

    # Validate required args for non-dry-run
    if not args.dry_run:
        missing = []
        if not args.owner_mxid:
            missing.append("OWNER_MXID (--owner-mxid)")
        if not args.server_name:
            missing.append("SERVER_NAME (--server-name)")
        if missing:
            sys.exit(f"Error: missing required config: {', '.join(missing)}\n"
                     f"Set via CLI args or environment variables.")

    # Resolve chat directory and derived paths
    chat_dir = Path(args.chat_dir).resolve()
    export_file = chat_dir / "export.json"
    progress_file = chat_dir / "import_progress.json"

    if not export_file.exists():
        sys.exit(f"Error: Discord export not found at {export_file}\n"
                 f"Place a DiscordChatExporter export.json in {chat_dir}")

    if args.fresh and progress_file.exists():
        progress_file.unlink()
        print("  Cleared previous import progress (--fresh)")

    # Parse export
    print(f"Parsing {export_file}...")
    channel_info, messages = parse_discord_export(export_file)
    print(f"Found {len(messages)} messages from "
          f"#{channel_info['channel_name']} ({channel_info['guild_name']})")

    # Build sender map
    server_name = args.server_name or "example.com"
    sender_map = build_sender_map(args.sender_map, messages, server_name)

    if args.dry_run:
        do_dry_run(channel_info, messages, sender_map)
    else:
        do_import(channel_info, messages, sender_map, args, chat_dir,
                  progress_file)


if __name__ == "__main__":
    main()
