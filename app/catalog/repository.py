"""공용 DB의 기존 AIO3 테이블만 읽는다. 서버 DB 권한과 별개로 멤버십을 검사한다."""
from hashlib import sha256
import json

from sqlalchemy import text

from app.knowledge.schemas import KnowledgeError

MODEL, DIMENSIONS = "text-embedding-3-small", 1536
MEMBER = """exists (select 1 from public.aio3_catalog_members m
    join public.users u on u.supabase_uid=m.user_id where u.id=:owner)"""


class SqlCatalog:
    def __init__(self, database):
        self.database = database

    @staticmethod
    def _installed(session):
        return bool(session.scalar(text("""select
            to_regclass('public.aio3_catalog_products') is not null and
            to_regclass('public.aio3_catalog_chunks') is not null and
            to_regclass('public.aio3_catalog_members') is not null and
            to_regclass('public.aio3_catalog_sources') is not null and
            to_regprocedure('public.search_aio3_catalog(text,integer,vector,double precision)') is not null
        """)))

    def _require(self, session, owner):
        if not self._installed(session):
            raise KnowledgeError("catalog_not_configured", "공용 상품 저장소가 이 DB에 구성되지 않았습니다.", 503)
        if not session.scalar(text("select " + MEMBER), {"owner": owner}):
            raise KnowledgeError("catalog_access_denied", "공용 상품 저장소의 팀 조회 권한이 필요합니다.", 403)

    def available(self, owner):
        with self.database.session() as session:
            return self._installed(session) and bool(session.scalar(
                text("select " + MEMBER), {"owner": owner}))

    def status(self, owner):
        with self.database.session() as session:
            self._require(session, owner)
            return dict(session.execute(text("""select
                (select count(*) from public.aio3_catalog_products) as products,
                (select count(*) from public.aio3_catalog_chunks) as chunks,
                (select count(*) from public.aio3_catalog_chunks where embedding is not null) as indexed,
                (select count(*) from public.aio3_catalog_sources) as source_files
                where """ + MEMBER), {"owner": owner}).mappings().one())

    def search(self, owner, query, *, vector=None, limit=50):
        if not isinstance(query, str) or not query.strip() or len(query) > 500 or not 1 <= limit <= 50:
            raise KnowledgeError("invalid_catalog_query", "상품 검색어와 결과 개수를 확인하세요.")
        with self.database.session() as session:
            self._require(session, owner)
            # The final predicate remains inside the SELECT even though the server
            # may connect as postgres/service_role and therefore bypass table RLS.
            rows = session.execute(text("""select r.* from public.search_aio3_catalog(
                :query,:limit,cast(:vector as vector),0.80) r where """ + MEMBER),
                {"owner": owner, "query": query, "limit": limit,
                 "vector": json.dumps(vector) if vector is not None else None}).mappings().all()
            return [{**dict(row), "unit_price": str(row["unit_price"])} for row in rows]

    def pending(self, owner):
        with self.database.session() as session:
            self._require(session, owner)
            return [dict(row) for row in session.execute(text("""select c.sku,c.content,c.content_sha256
                from public.aio3_catalog_chunks c where c.embedding is null and """ + MEMBER +
                " order by c.sku"), {"owner": owner}).mappings()]

    def publish(self, owner, snapshots, vectors):
        with self.database.session() as session:
            self._require(session, owner)
            current = {r["sku"]: r["content_sha256"] for r in session.execute(text("""select
                c.sku,c.content_sha256 from public.aio3_catalog_chunks c
                where c.sku = any(:skus) and """ + MEMBER + " order by c.sku for update of c"),
                {"owner": owner, "skus": [r["sku"] for r in snapshots]}).mappings()}
            if current != {r["sku"]: r["content_sha256"] for r in snapshots}:
                raise KnowledgeError("catalog_changed", "상품 원문이 변경되었습니다. 다시 인덱싱해 주세요.", 409)
            for row, vector in zip(snapshots, vectors, strict=True):
                changed = session.execute(text("""update public.aio3_catalog_chunks c set
                    embedding=cast(:vector as vector),embedding_model=:model,
                    embedding_content_sha256=:digest,embedded_at=now()
                    where c.sku=:sku and c.content_sha256=:digest and """ + MEMBER),
                    {"owner": owner, "sku": row["sku"], "digest": row["content_sha256"],
                     "model": MODEL, "vector": json.dumps(vector)}).rowcount
                if changed != 1:
                    raise KnowledgeError("catalog_changed", "상품 원문이나 팀 권한이 변경되었습니다.", 409)

    def source(self, owner):
        with self.database.session() as session:
            self._require(session, owner)
            row = session.execute(text("""select s.file_name,s.mime_type,s.content,s.source_sha256
                from public.aio3_catalog_sources s where """ + MEMBER +
                " order by s.stored_at desc limit 1"), {"owner": owner}).mappings().first()
            if row is None:
                raise KnowledgeError("catalog_source_not_found", "보관된 상품 원본이 없습니다.", 404)
            data = bytes(row["content"])
            if sha256(data).hexdigest() != row["source_sha256"]:
                raise KnowledgeError("catalog_source_corrupted", "상품 원본 파일의 해시가 맞지 않습니다.", 503)
            return {**dict(row), "content": data}
