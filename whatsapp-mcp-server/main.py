import signal
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from whatsapp import (
    download_media as whatsapp_download_media,
)
from whatsapp import (
    get_chat as whatsapp_get_chat,
)
from whatsapp import (
    get_contact_chats as whatsapp_get_contact_chats,
)
from whatsapp import (
    get_direct_chat_by_contact as whatsapp_get_direct_chat_by_contact,
)
from whatsapp import (
    get_last_interaction as whatsapp_get_last_interaction,
)
from whatsapp import (
    get_message_context as whatsapp_get_message_context,
)
from whatsapp import (
    get_sender_name as whatsapp_get_sender_name,
)
from whatsapp import (
    list_chats as whatsapp_list_chats,
)
from whatsapp import (
    list_messages as whatsapp_list_messages,
)
from whatsapp import (
    msg_to_dict,
)
from whatsapp import (
    search_contacts as whatsapp_search_contacts,
)
# Anchr fork: send_audio_message / send_file / send_message / send_reaction
# imports + their @mcp.tool() registrations have been removed deliberately
# to close the "lethal trifecta" — untrusted message content + agent read
# access + an agent-controlled outbound WhatsApp channel is a prompt-
# injection-to-exfil attack surface. See ANCHR.md. If you ever merge
# from upstream verygoodplugins/main and they reappear, strip them again.

# Anchr fork: chat allowlist (belt-and-suspenders with the bridge's
# allowlist.go). See chat-allowlist.example.txt and ANCHR.md.
from allowlist import ALLOWLIST, ChatNotAllowed, enforce  # noqa: F401  (re-export for handlers)


def _chat_jid(obj):
    """Extract a chat JID from a Chat dataclass, dict, or None."""
    if obj is None:
        return None
    if hasattr(obj, "jid"):
        return obj.jid
    if isinstance(obj, dict):
        return obj.get("jid")
    return None


# Initialize FastMCP server
mcp = FastMCP("whatsapp")


@mcp.tool()
def search_contacts(query: str) -> list[dict[str, Any]]:
    """Search WhatsApp contacts by name or phone number.

    Args:
        query: Search term to match against contact names or phone numbers
    """
    contacts = whatsapp_search_contacts(query)
    return contacts


@mcp.tool()
def get_contact(
    identifier: str | None = None,
    phone_number: str | None = None,
    phone: str | None = None,
) -> dict[str, Any]:
    """Look up a WhatsApp contact by phone number, LID, or full JID.

    Automatically detects the identifier type and queries appropriately.

    Args:
        identifier: Phone number, LID, or full JID. Examples:
                    - "12025551234" (phone number)
                    - "35047067385985" (LID - numeric)
                    - "12025551234@s.whatsapp.net" (phone JID)
                    - "184125298348272@lid" (LID JID)
        phone_number: Backward-compatible alias for `identifier`.
        phone: Backward-compatible alias for `identifier` (matches README parameter name).

    Returns:
        Dictionary with jid, name, display_name, is_lid, and resolved status
    """
    if identifier is None:
        identifier = phone_number
    if identifier is None:
        identifier = phone
    if identifier is None:
        raise ValueError("Missing required argument: identifier (or phone_number / phone)")

    identifier = identifier.strip()
    if not identifier:
        raise ValueError("identifier must be non-empty")

    # Detect identifier type and normalize to JID.
    bare_numeric_digits: str | None = None
    if "@" in identifier:
        # Already a JID - use as-is
        jid = identifier
        is_lid = jid.endswith("@lid") or jid.split("@", 1)[-1] == "lid"
    else:
        digits = "".join(c for c in identifier if c.isdigit())
        if digits:
            # LIDs can overlap phone-number lengths, so bare numeric inputs try phone first.
            jid = f"{digits}@s.whatsapp.net"
            is_lid = False
            if identifier.isdigit():
                bare_numeric_digits = digits
        else:
            # Non-numeric and not a JID; try as-is.
            jid = identifier
            is_lid = False

    jid_user = jid.split("@", 1)[0]

    display_name: str | None = None
    resolved = False

    # Prefer chats table lookup via get_chat (works for both phone and LID contacts).
    candidates: list[tuple[str, bool]] = [(jid, is_lid)]
    if bare_numeric_digits:
        candidates.append((f"{bare_numeric_digits}@lid", True))

    chat = None
    for candidate_jid, candidate_is_lid in candidates:
        chat = whatsapp_get_chat(candidate_jid, include_last_message=False)
        if chat:
            jid = candidate_jid
            is_lid = candidate_is_lid
            jid_user = jid.split("@", 1)[0]
            break

    if chat and chat.get("name"):
        display_name = chat["name"]
        resolved = display_name not in (jid, jid_user)
    else:
        # Fallback: best-effort sender-name resolution (may use fuzzy LIKE lookup).
        display_name = whatsapp_get_sender_name(jid)
        resolved = display_name not in (jid, jid_user, identifier)

    return {
        "identifier": identifier,
        "jid": jid,
        "phone_number": jid_user if not is_lid else None,
        "lid": jid_user if is_lid else None,
        "name": display_name if resolved else jid_user,
        "display_name": display_name,
        "is_lid": is_lid,
        "resolved": resolved,
    }


@mcp.tool()
def list_messages(
    after: str | None = None,
    before: str | None = None,
    sender_phone_number: str | None = None,
    chat_jid: str | None = None,
    query: str | None = None,
    limit: int = 50,
    page: int = 0,
    include_context: bool = True,
    context_before: int = 1,
    context_after: int = 1,
    sort_by: str = "newest",
) -> list[dict[str, Any]]:
    """Get WhatsApp messages matching specified criteria with optional context.

    Each message includes sender_display showing "Name (phone)" for easy identification.

    Args:
        after: ISO-8601 date string (e.g., "2026-01-01" or "2026-01-01T09:00:00")
        before: ISO-8601 date string (e.g., "2026-01-09" or "2026-01-09T18:00:00")
        sender_phone_number: Phone number to filter by sender (e.g., "12025551234")
        chat_jid: Chat JID to filter by (e.g., "12025551234@s.whatsapp.net" or group JID)
        query: Search term to filter messages by content
        limit: Max messages to return (default 50, max 500)
        page: Page number for pagination (default 0)
        include_context: Include surrounding messages for context (default True)
        context_before: Messages to include before each match (default 1)
        context_after: Messages to include after each match (default 1)
        sort_by: "newest" (default, most recent first) or "oldest" (chronological)
    """
    # Cap limit at 500 to prevent excessive queries
    limit = min(limit, 500)

    # Anchr fork: if caller passed a specific chat_jid, it must be allowed.
    # If they didn't, we let whatsapp_list_messages run unfiltered (so it
    # can still page across all messages) and post-filter the results by
    # chat_jid against the allowlist before returning.
    if chat_jid is not None:
        enforce(chat_jid)

    messages = whatsapp_list_messages(
        after=after,
        before=before,
        sender_phone_number=sender_phone_number,
        chat_jid=chat_jid,
        query=query,
        limit=limit,
        page=page,
        include_context=include_context,
        context_before=context_before,
        context_after=context_after,
        sort_by=sort_by,
    )
    if ALLOWLIST.enabled and chat_jid is None:
        messages = [m for m in messages if ALLOWLIST.is_allowed(m.get("chat_jid"))]
    return messages


@mcp.tool()
def list_chats(
    query: str | None = None,
    limit: int = 50,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active",
) -> list[dict[str, Any]]:
    """Get WhatsApp chats matching specified criteria.

    Args:
        query: Search term to filter chats by name or JID
        limit: Max chats to return (default 50, max 200)
        page: Page number for pagination (default 0)
        include_last_message: Include the last message in each chat (default True)
        sort_by: "last_active" (default, most recent first) or "name" (alphabetical)
    """
    # Cap limit at 200 to prevent excessive queries
    limit = min(limit, 200)
    chats = whatsapp_list_chats(
        query=query, limit=limit, page=page, include_last_message=include_last_message, sort_by=sort_by
    )
    if ALLOWLIST.enabled:
        chats = [c for c in chats if ALLOWLIST.is_allowed(_chat_jid(c))]
    return chats


@mcp.tool()
def get_chat(chat_jid: str, include_last_message: bool = True) -> dict[str, Any]:
    """Get WhatsApp chat metadata by JID.

    Args:
        chat_jid: The JID of the chat to retrieve
        include_last_message: Whether to include the last message (default True)
    """
    enforce(chat_jid)
    chat = whatsapp_get_chat(chat_jid, include_last_message)
    return chat


@mcp.tool()
def get_direct_chat_by_contact(sender_phone_number: str) -> dict[str, Any]:
    """Get WhatsApp chat metadata by sender phone number.

    Args:
        sender_phone_number: The phone number to search for
    """
    chat = whatsapp_get_direct_chat_by_contact(sender_phone_number)
    if ALLOWLIST.enabled and not ALLOWLIST.is_allowed(_chat_jid(chat)):
        return {}
    return chat


@mcp.tool()
def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> list[dict[str, Any]]:
    """Get all WhatsApp chats involving the contact.

    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    chats = whatsapp_get_contact_chats(jid, limit, page)
    if ALLOWLIST.enabled:
        chats = [c for c in chats if ALLOWLIST.is_allowed(_chat_jid(c))]
    return chats


@mcp.tool()
def get_last_interaction(jid: str) -> dict[str, Any]:
    """Get most recent WhatsApp message involving the contact.

    Args:
        jid: The JID of the contact to search for

    Returns:
        Message dictionary with id, timestamp, sender, content, etc. or empty dict if not found.
    """
    message = whatsapp_get_last_interaction(jid)
    return message if message else {}


@mcp.tool()
def get_message_context(message_id: str, before: int = 5, after: int = 5) -> dict[str, Any]:
    """Get context around a specific WhatsApp message.

    Args:
        message_id: The ID of the message to get context for
        before: Number of messages to include before the target message (default 5)
        after: Number of messages to include after the target message (default 5)
    """
    context = whatsapp_get_message_context(message_id, before, after)
    return {
        "message": msg_to_dict(context.message),
        "before": [msg_to_dict(message) for message in context.before],
        "after": [msg_to_dict(message) for message in context.after],
    }


# Anchr fork: send_message / send_reaction / send_file / send_audio_message
# @mcp.tool() registrations removed deliberately — see comment near the
# top of this file and ANCHR.md. Read-only tool surface only.


@mcp.tool()
def download_media(message_id: str, chat_jid: str) -> dict[str, Any]:
    """Download media from a WhatsApp message and get the local file path.

    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message

    Returns:
        A dictionary containing success status, a status message, and the file path if successful
    """
    enforce(chat_jid)
    file_path = whatsapp_download_media(message_id, chat_jid)

    if file_path:
        return {"success": True, "message": "Media downloaded successfully", "file_path": file_path}
    else:
        return {"success": False, "message": "Failed to download media"}


def shutdown_handler(signum, frame):
    """Handle shutdown signals gracefully to prevent zombie processes."""
    sys.exit(0)


if __name__ == "__main__":
    # Register signal handlers for clean shutdown
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    # Initialize and run the server
    mcp.run(transport="stdio")
