"""Legacy cache delivery must have the same ownership proof as uploaded files."""
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select

from open_webui.internal.db import get_async_db_context
from open_webui.models.files import File
from open_webui.utils.access_control.files import has_access_to_file
from open_webui.utils.chat_privacy import can_bypass_private_content_access


async def private_cache_response(root: Path, path: str, user):
    missing = HTTPException(status_code=404, detail='File not found')
    root = Path(root).resolve()
    requested = root / path
    resolved = requested.resolve()
    if not resolved.is_relative_to(root) or resolved == root or not resolved.is_file():
        raise missing
    # Refuse symlink aliases rather than granting access based on a path that
    # can subsequently resolve to a different file.
    if any(p.is_symlink() for p in (requested, *requested.parents) if p != root and p.is_relative_to(root)):
        raise missing
    async with get_async_db_context() as db:
        rows = (await db.execute(select(File.id, File.user_id).where(File.path == str(resolved)))).all()
        authorized = False
        for file_id, owner_id in rows:
            if owner_id == user.id or can_bypass_private_content_access(user.role) or await has_access_to_file(file_id, 'read', user, db=db):
                authorized = True
                break
    if not authorized:
        # An opaque filename or an occurrence in a user-editable chat is not
        # ownership evidence. Do not infer owners for legacy unregistered files.
        raise missing
    return FileResponse(resolved, filename=resolved.name, headers={
        'Cache-Control': 'private, no-store',
        'Content-Security-Policy': 'sandbox',
        'Referrer-Policy': 'no-referrer',
        'X-Content-Type-Options': 'nosniff',
    })
