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
