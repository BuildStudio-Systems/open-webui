"""Server-side sharing policy for the private There deployment.

Device receipts and ordinary model replies may contain internal information.
Do not attempt to infer safety from an IP/secret regular expression. Publishing
chat snapshots requires an explicit operator opt-in, independent of UI grants.
"""
import os


def chat_sharing_enabled() -> bool:
    return os.getenv('THERE_ENABLE_CHAT_SHARING', 'false').strip().lower() == 'true'


def require_chat_sharing() -> None:
    if not chat_sharing_enabled():
        from fastapi import HTTPException
        raise HTTPException(403, 'Chat sharing is disabled for this private deployment.')


def can_bypass_private_content_access(role: str) -> bool:
    """Attachments must not circumvent the deployment's private-chat policy."""
    from open_webui.config import BYPASS_ADMIN_ACCESS_CONTROL, ENABLE_ADMIN_CHAT_ACCESS

    return role == 'admin' and BYPASS_ADMIN_ACCESS_CONTROL and ENABLE_ADMIN_CHAT_ACCESS
