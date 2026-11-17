"""공용 상품의 인증된 조회·원본 다운로드·명시적 임베딩 생성."""
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app.auth.deps import current_user_id
from app.catalog.operations import index_catalog, retrieve
from app.catalog.repository import SqlCatalog
from app.catalog.service import make_embedder
from app.files.validation import safe_disposition
from app.knowledge.schemas import KnowledgeError
from app.llm.client import OpenAIKeyError
from app.trade.common.api import require_write


def no_store(response: Response):
    response.headers["Cache-Control"] = "private, no-store"


router = APIRouter(prefix="/api/catalog", tags=["shared-catalog"], dependencies=[Depends(no_store)])


@router.get("/status")
def status(request: Request, owner: int = Depends(current_user_id)):
    return SqlCatalog(request.app.state.database).status(owner)


@router.get("/search")
def search(request: Request, q: str = Query(min_length=1, max_length=500),
           owner: int = Depends(current_user_id)):
    return {"matches": SqlCatalog(request.app.state.database).search(owner, q)}


class SemanticQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    q: str = Field(min_length=1, max_length=500)


@router.post("/search")
def semantic_search(body: SemanticQuery, request: Request, owner: int = Depends(require_write)):
    database = request.app.state.database
    repository = SqlCatalog(database)
    status = repository.status(owner)  # Authorize before reading keys or calling an embedding provider.
    matches = repository.search(owner, body.q)
    if status["indexed"] and not any(r["retrieval_method"] == "exact_identifier" for r in matches):
        try:
            embedder = make_embedder(database, owner, request.app.state.settings)
            matches = retrieve(repository, owner, body.q, embedder=embedder)
        except OpenAIKeyError as error:
            raise KnowledgeError(error.code, error.message, error.status_code) from None
    return {"matches": matches}


@router.post("/index")
def index(request: Request, owner: int = Depends(require_write)):
    database = request.app.state.database
    repository = SqlCatalog(database)
    repository.status(owner)
    if not repository.pending(owner):
        return {**repository.status(owner), "newly_indexed": 0}
    try:
        embedder = make_embedder(database, owner, request.app.state.settings)
        return index_catalog(repository, owner, embedder)
    except OpenAIKeyError as error:
        raise KnowledgeError(error.code, error.message, error.status_code) from None


@router.get("/source")
def source(request: Request, owner: int = Depends(current_user_id)):
    file = SqlCatalog(request.app.state.database).source(owner)
    return Response(file["content"], media_type=file["mime_type"], headers={
        "Content-Disposition": safe_disposition(file["file_name"]),
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
    })
