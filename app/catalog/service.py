"""현재 사용자 키와 명시적 공용 카탈로그 권한의 조립 경계."""
from app.catalog.operations import retrieve
from app.catalog.repository import DIMENSIONS, MODEL, SqlCatalog
from app.config import get_settings
from app.knowledge.embeddings import OpenAIEmbedder
from app.knowledge.schemas import KnowledgeError
from app.llm.client import OpenAIKeyError
from app.services.auth import make_cipher
from app.services.openai_keys import UserKeyAccess


def make_embedder(database, owner, settings):
    access = UserKeyAccess(database, owner, make_cipher(settings), settings)
    access.read()  # Fail before a paid request; never use another user's/server key.
    return OpenAIEmbedder(access.read, MODEL, DIMENSIONS)


def optional_lookup(database, owner):
    repository = SqlCatalog(database)
    if not repository.available(owner):
        return None

    def lookup(query):
        # No key lookup or provider call for an exact identifier or an empty index.
        rows = repository.search(owner, query)
        if any(r["retrieval_method"] == "exact_identifier" for r in rows) or not repository.status(owner)["indexed"]:
            return rows
        try:
            embedder = make_embedder(database, owner, get_settings())
            return retrieve(repository, owner, query, embedder=embedder)
        except OpenAIKeyError:
            return rows
        except KnowledgeError as error:
            if error.code.startswith("provider_"):
                return rows
            raise
    return lookup
