"""Native document, FAQ and Wiki management within the THERE knowledge ACL.

Mount with /api/v1/there. Every mutation shares THERE's durable operation
journal; knowledge-engine credentials and engine IDs are resolved server-side.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query
from pydantic import ConfigDict, Field, field_validator

from open_webui.routers.there import (
    Form,
    begin_operation,
    engine_data,
    engine_items,
    get_async_session,
    get_binding,
    get_verified_user,
    no_store,
    read_engine,
    require_workspace,
    write_engine,
)
from open_webui.there_integration.weknora import WeKnoraClient

router = APIRouter(dependencies=[Depends(no_store)])
ResourceID = Annotated[str, Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")]
FAQID = Annotated[str, Path(pattern=r"^[1-9][0-9]{0,18}$")]
Revision = Annotated[int, Field(strict=True, ge=0, le=2147483647)]
Answer = Annotated[str, Field(min_length=1, max_length=32768)]
Question = Annotated[str, Field(min_length=1, max_length=8192)]


class ChunkForm(Form):
    # Preserve indentation/newlines in Markdown and code-bearing chunks.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    content: str = Field(min_length=1, max_length=200000)
    expected_revision: Revision

    @field_validator("content")
    @classmethod
    def valid_content(cls, value):
        if not value.strip() or "\0" in value:
            raise ValueError("Content must be non-empty text")
        return value


class ChunkRevertForm(Form):
    version: Revision
    expected_revision: Revision


class FAQForm(Form):
    question: Question
    answers: list[Answer] = Field(min_length=1, max_length=20)
    similar_questions: list[Question] = Field(default_factory=list, max_length=20)

    @field_validator("question", "answers", "similar_questions")
    @classmethod
    def valid_text(cls, value):
        values = value if isinstance(value, list) else [value]
        if any(not item.strip() or "\0" in item for item in values):
            raise ValueError("Questions and answers must be non-empty text")
        return value


class WikiEditForm(Form):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    content: str = Field(min_length=1, max_length=200000)
    expected_version: int = Field(strict=True, ge=1, le=2147483647)

    @field_validator("content")
    @classmethod
    def valid_content(cls, value):
        return ChunkForm.valid_content(value)


class WikiRevertForm(Form):
    slug: str = Field(min_length=1, max_length=512)
    version: int = Field(strict=True, ge=1, le=2147483647)

    @field_validator("slug")
    @classmethod
    def valid_slug(cls, value):
        return validate_slug(value)


class WikiCreateForm(Form):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    slug: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=512)
    content: str = Field(min_length=1, max_length=200000)
    page_type: Literal["entity", "concept", "summary", "index", "synthesis", "comparison"] = "concept"
    status: Literal["draft", "published", "archived"] = "draft"

    @field_validator("slug")
    @classmethod
    def valid_slug(cls, value):
        return validate_slug(value)

    @field_validator("title", "content")
    @classmethod
    def valid_text(cls, value):
        return ChunkForm.valid_content(value)


def validate_slug(value: str) -> str:
    if (
        any(part in {"", ".", ".."} for part in value.split("/"))
        or any(char in value for char in ("\\", "%", "?", "#"))
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError("Invalid Wiki page slug")
    return value


def page_slug(slug: str = Query(min_length=1, max_length=512)) -> str:
    try:
        return validate_slug(slug)
    except ValueError:
        raise HTTPException(422, "Wiki 页面标识无效。") from None


def numeric_entry_id(value: str) -> int:
    identifier = int(value)
    if not 1 <= identifier <= 9223372036854775807:
        raise HTTPException(422, "FAQ 条目标识无效。")
    return identifier


def faq_ids(value: Any) -> Any:
    """Keep WeKnora's int64 sequence IDs exact in JavaScript clients."""
    if isinstance(value, list):
        return [faq_ids(item) for item in value]
    if isinstance(value, dict):
        return {
            key: str(item) if key in {"id", "seq_id", "tag_id"} and type(item) is int else faq_ids(item)
            for key, item in value.items()
        }
    return value


def result_data(envelope):
    # Wiki endpoints return their typed object directly; ordinary APIs wrap data.
    return engine_data(envelope) if "data" in envelope else envelope


def paginated(envelope, *, page=1, page_size=20, collection_keys=()):
    items = engine_items(envelope)
    data = result_data(envelope)
    if not items and isinstance(data, dict):
        for key in (*collection_keys, "items", "data", "list", "results"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
    total = envelope.get("total")
    if not isinstance(total, int) or isinstance(total, bool):
        total = data.get("total") if isinstance(data, dict) else None
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        total = None
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": page * page_size < total if total is not None else len(items) == page_size,
    }


async def mutate(id, user, db, action, key, invoke: Callable[[str], Awaitable]):
    await require_workspace(user, "knowledge", db)
    binding, _ = await get_binding(id, user, "write", db=db)
    operation = await begin_operation(db, user, action, id, key)
    result = await write_engine(db, operation, invoke(binding.engine_id))
    await db.commit()
    return {"data": result_data(result), "operation_id": operation.id}


@router.get("/knowledge/{id}/documents/{document_id}/chunks")
async def chunks(
    id: ResourceID,
    document_id: ResourceID,
    page: int = Query(1, ge=1, le=100000),
    page_size: int = Query(20, ge=1, le=100),
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
):
    binding, _ = await get_binding(id, user, "read", db=db)
    result = await read_engine(
        WeKnoraClient().list_chunks(binding.engine_id, document_id, page=page, page_size=page_size)
    )
    return paginated(result, page=page, page_size=page_size, collection_keys=("chunks",))


@router.post("/knowledge/{id}/documents/{document_id}/reparse")
async def reparse_document(
    id: ResourceID,
    document_id: ResourceID,
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
    idempotency_key: str | None = Header(None),
):
    return await mutate(
        id,
        user,
        db,
        "document.reparse",
        idempotency_key,
        lambda kb: WeKnoraClient().reparse_document(kb, document_id),
    )


@router.put("/knowledge/{id}/documents/{document_id}/chunks/{chunk_id}")
async def update_chunk(
    id: ResourceID,
    document_id: ResourceID,
    chunk_id: ResourceID,
    form: ChunkForm,
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
    idempotency_key: str | None = Header(None),
):
    return await mutate(
        id,
        user,
        db,
        "chunk.update",
        idempotency_key,
        lambda kb: WeKnoraClient().update_chunk(
            kb, document_id, chunk_id, content=form.content, expected_revision=form.expected_revision
        ),
    )


@router.get("/knowledge/{id}/documents/{document_id}/chunks/{chunk_id}/revisions")
async def chunk_revisions(
    id: ResourceID,
    document_id: ResourceID,
    chunk_id: ResourceID,
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
):
    binding, _ = await get_binding(id, user, "read", db=db)
    result = await read_engine(WeKnoraClient().list_chunk_revisions(binding.engine_id, document_id, chunk_id))
    items = paginated(result, collection_keys=("revisions",))["items"]
    return {"items": items, "total": len(items)}


@router.post("/knowledge/{id}/documents/{document_id}/chunks/{chunk_id}/revert")
async def revert_chunk(
    id: ResourceID,
    document_id: ResourceID,
    chunk_id: ResourceID,
    form: ChunkRevertForm,
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
    idempotency_key: str | None = Header(None),
):
    return await mutate(
        id,
        user,
        db,
        "chunk.revert",
        idempotency_key,
        lambda kb: WeKnoraClient().revert_chunk(
            kb, document_id, chunk_id, revision=form.version, expected_revision=form.expected_revision
        ),
    )


@router.get("/knowledge/{id}/faq")
async def faq_entries(
    id: ResourceID,
    page: int = Query(1, ge=1, le=100000),
    page_size: int = Query(20, ge=1, le=100),
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
):
    binding, _ = await get_binding(id, user, "read", db=db)
    result = await read_engine(WeKnoraClient().list_faq_entries(binding.engine_id, page=page, page_size=page_size))
    return faq_ids(paginated(result, page=page, page_size=page_size, collection_keys=("entries",)))


@router.post("/knowledge/{id}/faq", status_code=201)
async def create_faq_entry(
    id: ResourceID,
    form: FAQForm,
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
    idempotency_key: str | None = Header(None),
):
    return faq_ids(
        await mutate(
            id,
            user,
            db,
            "faq.create",
            idempotency_key,
            lambda kb: WeKnoraClient().create_faq_entry(kb, **form.model_dump()),
        )
    )


@router.get("/knowledge/{id}/faq/{entry_id}")
async def get_faq_entry(
    id: ResourceID,
    entry_id: FAQID,
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
):
    identifier = numeric_entry_id(entry_id)
    binding, _ = await get_binding(id, user, "read", db=db)
    result = await read_engine(WeKnoraClient().get_faq_entry(binding.engine_id, identifier))
    return {"data": faq_ids(engine_data(result))}


@router.put("/knowledge/{id}/faq/{entry_id}")
async def update_faq_entry(
    id: ResourceID,
    entry_id: FAQID,
    form: FAQForm,
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
    idempotency_key: str | None = Header(None),
):
    identifier = numeric_entry_id(entry_id)
    return faq_ids(
        await mutate(
            id,
            user,
            db,
            "faq.update",
            idempotency_key,
            lambda kb: WeKnoraClient().update_faq_entry(kb, identifier, **form.model_dump()),
        )
    )


@router.delete("/knowledge/{id}/faq/{entry_id}")
async def delete_faq_entry(
    id: ResourceID,
    entry_id: FAQID,
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
    idempotency_key: str | None = Header(None),
):
    identifier = numeric_entry_id(entry_id)
    result = await mutate(
        id,
        user,
        db,
        "faq.delete",
        idempotency_key,
        lambda kb: WeKnoraClient().delete_faq_entries(kb, [identifier]),
    )
    return {"deleted": True, "operation_id": result["operation_id"]}


@router.get("/knowledge/{id}/wiki/pages")
async def wiki_pages(
    id: ResourceID,
    page: int = Query(1, ge=1, le=100000),
    page_size: int = Query(20, ge=1, le=100),
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
):
    binding, _ = await get_binding(id, user, "read", db=db)
    result = await read_engine(WeKnoraClient().list_wiki_pages(binding.engine_id, page=page, page_size=page_size))
    return paginated(result, page=page, page_size=page_size, collection_keys=("pages",))


@router.get("/knowledge/{id}/wiki/page")
async def wiki_page(
    id: ResourceID,
    slug: str = Depends(page_slug),
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
):
    binding, _ = await get_binding(id, user, "read", db=db)
    result = await read_engine(WeKnoraClient().get_wiki_page(binding.engine_id, slug))
    return {"data": result_data(result)}


@router.post("/knowledge/{id}/wiki/pages", status_code=201)
async def create_wiki_page(
    id: ResourceID,
    form: WikiCreateForm,
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
    idempotency_key: str | None = Header(None),
):
    return await mutate(
        id, user, db, "wiki.create", idempotency_key,
        lambda kb: WeKnoraClient().create_wiki_page(kb, **form.model_dump()),
    )


@router.delete("/knowledge/{id}/wiki/page")
async def delete_wiki_page(
    id: ResourceID,
    slug: str = Depends(page_slug),
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
    idempotency_key: str | None = Header(None),
):
    result = await mutate(
        id, user, db, "wiki.delete", idempotency_key,
        lambda kb: WeKnoraClient().delete_wiki_page(kb, slug),
    )
    return {"deleted": True, "operation_id": result["operation_id"]}


@router.get("/knowledge/{id}/wiki/search")
async def search_wiki(
    id: ResourceID,
    q: str = Query(min_length=1, max_length=2000),
    limit: int = Query(10, ge=1, le=50),
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
):
    if not q.strip() or "\0" in q:
        raise HTTPException(422, "请输入有效的检索内容。")
    binding, _ = await get_binding(id, user, "read", db=db)
    result = await read_engine(WeKnoraClient().search_wiki(binding.engine_id, q, limit=limit))
    return {"items": paginated(result, collection_keys=("pages",))["items"]}


@router.get("/knowledge/{id}/wiki/index")
async def wiki_index(
    id: ResourceID,
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
):
    binding, _ = await get_binding(id, user, "read", db=db)
    result = await read_engine(WeKnoraClient().get_wiki_index(binding.engine_id))
    return {"data": result_data(result)}


@router.put("/knowledge/{id}/wiki/page")
async def update_wiki_page(
    id: ResourceID,
    form: WikiEditForm,
    slug: str = Depends(page_slug),
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
    idempotency_key: str | None = Header(None),
):
    return await mutate(
        id,
        user,
        db,
        "wiki.update",
        idempotency_key,
        lambda kb: WeKnoraClient().update_wiki_page(kb, slug, **form.model_dump()),
    )


@router.get("/knowledge/{id}/wiki/revisions")
async def wiki_revisions(
    id: ResourceID,
    slug: str = Depends(page_slug),
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
):
    binding, _ = await get_binding(id, user, "read", db=db)
    result = await read_engine(WeKnoraClient().list_wiki_revisions(binding.engine_id, slug))
    data = result_data(result)
    response = paginated(result, collection_keys=("revisions",))
    response["current_version"] = data.get("current_version") if isinstance(data, dict) else None
    return response


@router.post("/knowledge/{id}/wiki/revert")
async def revert_wiki_page(
    id: ResourceID,
    form: WikiRevertForm,
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
    idempotency_key: str | None = Header(None),
):
    return await mutate(
        id,
        user,
        db,
        "wiki.revert",
        idempotency_key,
        lambda kb: WeKnoraClient().revert_wiki_page(kb, **form.model_dump()),
    )


@router.get("/knowledge/{id}/wiki/graph")
async def wiki_graph(
    id: ResourceID,
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
):
    binding, _ = await get_binding(id, user, "read", db=db)
    result = await read_engine(WeKnoraClient().get_wiki_graph(binding.engine_id))
    return {"data": result_data(result), "kind": "wiki_page_links"}
