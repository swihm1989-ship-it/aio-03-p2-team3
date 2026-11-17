"""실제 pgvector PostgreSQL·생산 검색 SQL·가짜 임베딩으로 공용 카탈로그 연결을 검증한다."""
from hashlib import sha256
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text

from app.auth.deps import current_user_id
from app.catalog.operations import PROFILE, enrich_document, index_catalog, retrieve
from app.catalog.repository import SqlCatalog
from app.config import Settings
from app.db.models import User
from app.knowledge.schemas import EmbeddingBatch, KnowledgeError
from app.main import create_app
from app.trade.pi.operations import create_pi, revise_pi
from app.trade.pi.models import PIInput
from app.trade.pi.repository import pi_repository
from tests.fakes.storage import FakeStorage
from tests.test_pi_domain import pi_input

ONE = UUID("10000000-0000-4000-8000-000000000001")
TWO = UUID("10000000-0000-4000-8000-000000000002")
SOURCE = b"synthetic catalog source"
DIGEST = sha256(SOURCE).hexdigest()


@pytest.fixture
def catalog(database):
    # Supabase's auth schema/roles are compatibility fixtures in this temporary DB.
    # They contain synthetic identities only and are never applied to production.
    with database.engine.begin() as connection:
        connection.exec_driver_sql("""do $$ begin
          if not exists(select 1 from pg_roles where rolname='anon') then create role anon; end if;
          if not exists(select 1 from pg_roles where rolname='authenticated') then create role authenticated; end if;
          if not exists(select 1 from pg_roles where rolname='service_role') then create role service_role; end if;
        end $$;
        create schema auth;
        create table auth.users (id uuid primary key);
        create function auth.uid() returns uuid language sql stable as
        $$ select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
        """)
        connection.exec_driver_sql(Path(__file__).with_name("fixtures").joinpath("aio3_catalog.sql").read_text())
    with database.session() as session:
        session.get(User, 1).supabase_uid = ONE
        session.add(User(id=2, supabase_uid=TWO, email="outside@example.invalid"))
        session.execute(text("insert into auth.users values (:one),(:two)"), {"one": ONE, "two": TWO})
        session.execute(text("insert into aio3_catalog_members(user_id,grant_reason) values (:one,'test member')"), {"one": ONE})
        session.execute(text("""insert into aio3_catalog_imports
            (source_sha256,company_name,source_file,source_sheet,product_count,stock_qty,inventory_value_usd)
            values (:hash,'AIO3 주식회사','synthetic.xlsx','Inventory',3,90,900)"""), {"hash": DIGEST})
        products = [("SYN-MOUSE", "Virtual Mouse Black", "mouse", "", 9, 30),
                    ("SYN-TACTILE", "Virtual Keyboard Tactile Black", "keyboard", "Tactile", 12, 20),
                    ("SYN-LINEAR", "Virtual Keyboard Linear Black", "keyboard", "Linear", 12, 40)]
        for number, (sku, name, category, switch, price, stock) in enumerate(products, 4):
            session.execute(text("""insert into aio3_catalog_products
                (sku,company_name,brand,product_name_original,manufacturer_model_code,category,
                 color,switch_type,stock_qty,unit,currency,unit_price,price_role,source_sha256,
                 source_file,source_sheet,source_row)
                values (:sku,'AIO3 주식회사','VIRTUAL',:name,:sku,:category,'Black',:switch,:stock,
                 'pcs','USD',:price,'unspecified_inventory_list_price',:hash,'synthetic.xlsx','Inventory',:row)"""),
                {"sku": sku, "name": name, "category": category, "switch": switch, "stock": stock,
                 "price": price, "hash": DIGEST, "row": number})
            content = name + " 검정 " + ("마우스" if category == "mouse" else "키보드")
            session.execute(text("""insert into aio3_catalog_chunks
                (sku,content,content_sha256,metadata) values (:sku,:content,:hash,'{}')"""),
                {"sku": sku, "content": content, "hash": sha256(content.encode()).hexdigest()})
        session.execute(text("""insert into aio3_catalog_sources
            (source_sha256,logical_path,file_name,mime_type,content,data_links)
            values (:hash,'synthetic/synthetic.xlsx','synthetic.xlsx',
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',:data,'{}')"""),
            {"hash": DIGEST, "data": SOURCE})
    return SqlCatalog(database)


class FakeEmbedder:
    profile = PROFILE

    def __init__(self, *, fail_after=None, mutate=None, invalid=None):
        self.calls, self.fail_after, self.mutate, self.invalid = [], fail_after, mutate, invalid

    def embed_documents(self, texts):
        self.calls.append(texts)
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise KnowledgeError("provider_unavailable", "synthetic unavailable", 503)
        vectors = [[1.0] + [0.0] * 1535 for _ in texts]
        if self.invalid == "nan": vectors[0][0] = float("nan")
        if self.invalid == "zero": vectors[0] = [0.0] * 1536
        if self.mutate:
            self.mutate()
        return EmbeddingBatch(profile=self.profile, vectors=vectors)


def test_catalog_is_optional_when_no_catalog_was_configured(database):
    assert SqlCatalog(database).available(1) is False
    with pytest.raises(KnowledgeError, match="구성되지"):
        SqlCatalog(database).status(1)


def test_explicit_membership_still_applies_to_privileged_server_connections(catalog):
    assert catalog.status(1)["products"] == 3
    assert catalog.available(2) is False
    for operation in (lambda: catalog.search(2, "Virtual Mouse Black"),
                      lambda: catalog.pending(2), lambda: catalog.source(2)):
        with pytest.raises(KnowledgeError) as error:
            operation()
        assert error.value.status == 403


def test_exact_code_korean_alias_ambiguity_and_unknown_product(catalog):
    exact = catalog.search(1, "SYN-MOUSE")
    assert len(exact) == 1 and exact[0]["retrieval_method"] == "exact_identifier"
    assert exact[0]["unit_price"].startswith("9.")
    assert len(catalog.search(1, "Virtual Keyboard Black")) == 2
    assert len(catalog.search(1, "마우스 검정")) == 1
    assert catalog.search(1, "no-such-usb") == []


def test_source_bytes_are_checked_and_returned_only_to_members(catalog):
    assert catalog.source(1)["content"] == SOURCE


def test_real_pgvector_publication_is_atomic_and_idempotent(catalog):
    embedder = FakeEmbedder()
    result = index_catalog(catalog, 1, embedder, batch_size=2)
    assert result["indexed"] == result["newly_indexed"] == 3
    assert len(embedder.calls) == 2
    assert index_catalog(catalog, 1, embedder)["newly_indexed"] == 0
    assert len(embedder.calls) == 2
    assert len(retrieve(catalog, 1, "typing accessory", embedder=FakeEmbedder())) == 3


@pytest.mark.parametrize("invalid", ["nan", "zero"])
def test_invalid_provider_vector_is_never_published(catalog, invalid):
    with pytest.raises(KnowledgeError):
        index_catalog(catalog, 1, FakeEmbedder(invalid=invalid))
    assert catalog.status(1)["indexed"] == 0


def test_failure_after_first_batch_leaves_no_partial_index(catalog):
    with pytest.raises(KnowledgeError):
        index_catalog(catalog, 1, FakeEmbedder(fail_after=1), batch_size=2)
    assert catalog.status(1)["indexed"] == 0


@pytest.mark.parametrize("revoke", [False, True])
def test_changed_source_or_revoked_member_cannot_publish_late_result(catalog, revoke):
    def mutate():
        with catalog.database.session() as session:
            if revoke:
                session.execute(text("delete from aio3_catalog_members"))
            else:
                session.execute(text("update aio3_catalog_chunks set content_sha256=:hash"), {"hash": "b" * 64})
    with pytest.raises(KnowledgeError):
        index_catalog(catalog, 1, FakeEmbedder(mutate=mutate))
    with catalog.database.session() as session:
        assert session.scalar(text("select count(*) from aio3_catalog_chunks where embedding is not null")) == 0


def test_enrichment_records_source_and_requires_price_confirmation(catalog):
    draft = {"document_type": "PI", "currency": "USD", "items": [
        {"product_name": "Virtual Mouse Black", "product_code": None, "quantity": "31", "unit_price": None}]}
    enrich_document(draft, lambda q: catalog.search(1, q))
    assert draft["items"][0]["product_code"] == "SYN-MOUSE"
    assert draft["items"][0]["unit_price"].startswith("9.")
    assert any("price role is unspecified" in s for s in draft["missing_fields"])
    assert any("stock shortage" in s for s in draft["missing_fields"])
    assert draft["catalog_evidence"][0]["candidates"][0]["source"]["row"] == 4


def test_ambiguous_variant_never_selects_a_price(catalog):
    draft = {"document_type": "PO", "currency": "USD", "items": [
        {"product_name": "Virtual Keyboard Black", "unit_price": None}]}
    enrich_document(draft, lambda q: catalog.search(1, q))
    assert draft["items"][0]["unit_price"] is None
    assert "select the exact model/options" in draft["missing_fields"][0]


def test_user_price_and_ci_pi_source_prices_are_not_overwritten(catalog):
    for kind in ("PI", "CI", "PL"):
        draft = {"document_type": kind, "items": [
            {"product_name": "Virtual Mouse Black", "unit_price": "8.50"}]}
        enrich_document(draft, lambda q: catalog.search(1, q))
        assert draft["items"][0]["unit_price"] == "8.50"


def test_direct_and_revised_pi_use_shared_catalog_context(catalog):
    repository = pi_repository(catalog.database, 1)
    command = pi_input(items=[{"product_name": "Virtual Mouse Black", "quantity": "2", "unit": "pcs"}])
    result = create_pi(command, repository=repository)
    assert result.document["catalog_evidence"]
    assert result.document["items"][0]["unit_price"].startswith("9.")
    values = command.model_dump(mode="json")
    values["items"][0]["unit_price"] = "8.50"
    changed = PIInput.model_validate(values)
    updated = revise_pi(result.document_id, result.revision, changed, repository=repository)
    assert updated.document["items"][0]["unit_price"] == "8.50"
    assert updated.document["catalog_evidence"]


def test_authenticated_catalog_http_requires_csrf_for_index_and_handles_missing_key(catalog):
    app = create_app(Settings(database_url=str(catalog.database.engine.url), session_secret="synthetic-session"),
                     storage=FakeStorage())
    app.dependency_overrides[current_user_id] = lambda: 1
    with TestClient(app) as client:
        assert client.get("/api/catalog/status").json()["products"] == 3
        result = client.get("/api/catalog/search", params={"q": "SYN-MOUSE"})
        assert result.status_code == 200 and result.headers["cache-control"] == "private, no-store"
        assert len(result.json()["matches"]) == 1
        assert client.get("/api/catalog/search", params={"q": "SYN-MOUSE", "semantic": True}).status_code == 200
        download = client.get("/api/catalog/source")
        assert download.content == SOURCE
        assert client.post("/api/catalog/index").status_code == 403
        client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf_token"]
        missing_key = client.post("/api/catalog/index")
        assert missing_key.status_code == 409 and missing_key.json()["error"]["code"] == "OPENAI_KEY_REQUIRED"
        app.dependency_overrides[current_user_id] = lambda: 2
        assert client.get("/api/catalog/status").status_code == 403
        assert client.get("/api/catalog/source").status_code == 403


def test_pure_ci_and_pl_never_call_shared_lookup():
    def forbidden(query):
        pytest.fail("CI/PL must preserve the approved source rather than query catalog prices")
    for kind in ("CI", "PL"):
        document = {"document_type": kind, "items": [{"unit_price": "7.50"}]}
        enrich_document(document, forbidden)
        assert document["items"][0]["unit_price"] == "7.50"


def test_pure_missing_catalog_does_not_change_unrelated_documents():
    document = {"document_type": "PI", "items": [{"product_name": "Unrelated material", "unit_price": "18"}]}
    enrich_document(document, lambda query: [])
    assert document["items"][0]["unit_price"] == "18"
    assert "catalog_evidence" not in document
