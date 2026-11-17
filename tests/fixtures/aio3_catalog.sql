-- AIO3-only additive schema. Does not change existing app tables or policies.
create table public.aio3_catalog_members (
  user_id uuid primary key references auth.users(id) on delete cascade,
  access_level text not null default 'reader' check (access_level = 'reader'),
  granted_at timestamptz not null default now(),
  grant_reason text not null
);

create table public.aio3_catalog_imports (
  source_sha256 text primary key check (source_sha256 ~ '^[a-f0-9]{64}$'),
  company_name text not null check (company_name = 'AIO3 주식회사'),
  source_file text not null,
  source_sheet text not null,
  product_count integer not null check (product_count >= 0),
  stock_qty bigint not null check (stock_qty >= 0),
  inventory_value_usd numeric(20,6) not null check (inventory_value_usd >= 0),
  imported_at timestamptz not null default now(),
  source_as_of timestamptz,
  notes jsonb not null default '[]'::jsonb
);

create table public.aio3_catalog_products (
  sku text primary key,
  company_name text not null check (company_name = 'AIO3 주식회사'),
  brand text not null,
  product_name_original text not null,
  manufacturer_model_code text,
  category text not null check (category in ('laptop','mouse','keyboard','headset')),
  category_inferred boolean not null default true,
  color text,
  switch_type text,
  stock_qty integer not null check (stock_qty >= 0),
  unit text not null check (unit = 'pcs'),
  currency text not null check (currency = 'USD'),
  unit_price numeric(18,6) not null check (unit_price >= 0),
  price_role text not null check (price_role = 'unspecified_inventory_list_price'),
  line_total numeric(20,6) generated always as (stock_qty::numeric * unit_price) stored,
  source_sha256 text not null references public.aio3_catalog_imports(source_sha256),
  source_file text not null,
  source_sheet text not null,
  source_row integer not null check (source_row >= 4),
  imported_at timestamptz not null default now(),
  unique (source_sha256, source_row)
);

create table public.aio3_catalog_chunks (
  sku text primary key references public.aio3_catalog_products(sku) on delete cascade,
  content text not null check (length(content) > 0),
  aliases text[] not null default '{}',
  content_sha256 text not null check (content_sha256 ~ '^[a-f0-9]{64}$'),
  metadata jsonb not null,
  search_tsv tsvector generated always as (to_tsvector('simple'::regconfig, content)) stored,
  embedding public.vector(1536),
  embedding_model text,
  embedding_content_sha256 text,
  embedded_at timestamptz,
  constraint aio3_embedding_profile check (
    (embedding is null and embedding_model is null and embedding_content_sha256 is null and embedded_at is null)
    or
    (embedding is not null and embedding_model = 'text-embedding-3-small'
     and embedding_content_sha256 = content_sha256 and embedded_at is not null)
  )
);
create index aio3_catalog_products_model_idx on public.aio3_catalog_products(manufacturer_model_code);
create index aio3_catalog_chunks_search_idx on public.aio3_catalog_chunks using gin(search_tsv);

alter table public.aio3_catalog_members enable row level security;
alter table public.aio3_catalog_imports enable row level security;
alter table public.aio3_catalog_products enable row level security;
alter table public.aio3_catalog_chunks enable row level security;

create policy aio3_member_reads_own_membership on public.aio3_catalog_members
  for select to authenticated using (user_id = (select auth.uid()));
create policy aio3_member_reads_imports on public.aio3_catalog_imports
  for select to authenticated using (
    exists (select 1 from public.aio3_catalog_members m where m.user_id = (select auth.uid()))
  );
create policy aio3_member_reads_products on public.aio3_catalog_products
  for select to authenticated using (
    exists (select 1 from public.aio3_catalog_members m where m.user_id = (select auth.uid()))
  );
create policy aio3_member_reads_chunks on public.aio3_catalog_chunks
  for select to authenticated using (
    exists (select 1 from public.aio3_catalog_members m where m.user_id = (select auth.uid()))
  );

revoke all on public.aio3_catalog_members, public.aio3_catalog_imports,
  public.aio3_catalog_products, public.aio3_catalog_chunks from public, anon, authenticated;
grant select on public.aio3_catalog_members, public.aio3_catalog_imports,
  public.aio3_catalog_products, public.aio3_catalog_chunks to authenticated;
grant select, insert, update, delete on public.aio3_catalog_members, public.aio3_catalog_imports,
  public.aio3_catalog_products, public.aio3_catalog_chunks to service_role;

create function public.aio3_normalize_product(value text)
returns text language sql immutable strict security invoker set search_path = ''
as $$ select regexp_replace(lower(value), '[^a-z0-9가-힣]+', '', 'g') $$;
revoke all on function public.aio3_normalize_product(text) from public, anon;
grant execute on function public.aio3_normalize_product(text) to authenticated, service_role;

create function public.search_aio3_catalog(
  query_text text,
  match_count integer default 10,
  query_embedding public.vector default null,
  min_similarity double precision default 0.65
)
returns table (
  sku text, product_name text, brand text, manufacturer_model_code text,
  category text, color text, switch_type text, stock_qty integer,
  unit_price numeric, currency text, price_role text, unit text,
  retrieval_text text, source jsonb, score double precision,
  retrieval_method text, embedding_ready boolean
)
language plpgsql stable security invoker set search_path = ''
as $$
begin
  if query_text is null or length(btrim(query_text)) = 0 then return; end if;
  if length(query_text) > 500 then
    raise exception 'query_text must be at most 500 characters' using errcode = '22023';
  end if;
  if match_count is null or match_count < 1 or match_count > 50 then
    raise exception 'match_count must be between 1 and 50' using errcode = '22023';
  end if;
  if min_similarity is null or min_similarity < -1 or min_similarity > 1 then
    raise exception 'min_similarity must be between -1 and 1' using errcode = '22023';
  end if;
  if query_embedding is not null and public.vector_dims(query_embedding) <> 1536 then
    raise exception 'query_embedding must use text-embedding-3-small, 1536 dimensions' using errcode = '22023';
  end if;
  return query
  with q as (
    select btrim(query_text) as raw,
           public.aio3_normalize_product(query_text) as normalized,
           websearch_to_tsquery('simple'::regconfig, query_text) as tsq
  ), scored as (
    select p.*, c.content, c.embedding is not null as ready,
      (q.normalized <> '' and (
        public.aio3_normalize_product(p.sku) = q.normalized
        or public.aio3_normalize_product(p.manufacturer_model_code) = q.normalized
        or public.aio3_normalize_product(p.product_name_original) = q.normalized
        or public.aio3_normalize_product(p.brand || ' ' || p.product_name_original) = q.normalized
      )) as exact_match,
      c.search_tsv @@ q.tsq as keyword_match,
      ts_rank_cd(c.search_tsv, q.tsq)::double precision as keyword_score,
      case when query_embedding is not null and c.embedding is not null
           then 1 - (c.embedding operator(public.<=>) query_embedding)
           else null end as similarity
    from public.aio3_catalog_products p
    join public.aio3_catalog_chunks c on c.sku = p.sku
    cross join q
  ), eligible as (
    select s.* from scored s
    where case when exists (select 1 from scored e where e.exact_match)
          then s.exact_match
          else s.keyword_match or s.similarity >= min_similarity end
  )
  select e.sku, e.product_name_original, e.brand, e.manufacturer_model_code,
    e.category, e.color, e.switch_type, e.stock_qty, e.unit_price, e.currency,
    e.price_role, e.unit, e.content,
    jsonb_build_object('file', e.source_file, 'sheet', e.source_sheet,
                       'row', e.source_row, 'sha256', e.source_sha256,
                       'imported_at', e.imported_at, 'source_as_of', null),
    (case when e.exact_match then 1000.0 else 0.0 end
       + coalesce(e.keyword_score, 0) + coalesce(e.similarity, 0))::double precision,
    case when e.exact_match then 'exact_identifier'
         when e.keyword_match and e.similarity >= min_similarity then 'hybrid'
         when e.keyword_match then 'keyword' else 'vector' end,
    e.ready
  from eligible e
  order by 15 desc, e.sku
  limit match_count;
end;
$$;
revoke all on function public.search_aio3_catalog(text, integer, public.vector, double precision) from public, anon;
grant execute on function public.search_aio3_catalog(text, integer, public.vector, double precision) to authenticated, service_role;

comment on table public.aio3_catalog_products is 'AIO3 team demonstration catalog. Exact Excel values; inventory-list price role is unspecified. Isolated from KE KOREA app tables.';
comment on table public.aio3_catalog_members is 'Explicit read allowlist. No self-enrollment and no public writes. Dashboard organization membership is managed separately.';
comment on table public.aio3_catalog_chunks is 'Product retrieval text with source-row provenance. Numeric price and stock are returned from aio3_catalog_products. Null embeddings are pending, never synthetic vectors.';
comment on function public.search_aio3_catalog(text, integer, public.vector, double precision) is 'Read-only RAG/LangGraph retrieval RPC respecting member RLS; exact identifier first, keyword search available, real OpenAI embeddings optional.';

create table public.aio3_catalog_sources (
 source_sha256 text primary key references public.aio3_catalog_imports(source_sha256),
 repository_name text not null default 'AIO3 컴퓨터부품',
 logical_path text not null,
 file_name text not null,
 mime_type text not null check (mime_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
 content bytea not null,
 byte_size integer generated always as (octet_length(content)) stored,
 data_links jsonb not null,
 stored_at timestamptz not null default now(),
 constraint aio3_source_hash_matches check (encode(sha256(content),'hex')=source_sha256),
 constraint aio3_source_size_limit check (octet_length(content) between 1 and 52428800)
);
alter table public.aio3_catalog_sources enable row level security;
create policy aio3_member_reads_source_files on public.aio3_catalog_sources
 for select to authenticated using (
  exists (select 1 from public.aio3_catalog_members m where m.user_id=(select auth.uid()))
 );
revoke all on public.aio3_catalog_sources from public,anon,authenticated;
grant select on public.aio3_catalog_sources to authenticated;
grant select,insert,update,delete on public.aio3_catalog_sources to service_role;
comment on table public.aio3_catalog_sources is 'KE KOREA shared database / AIO3 computer-parts source repository. Original workbook bytes with verified SHA-256, separate from ECLADO and contract records. SQL source archive, not a Supabase Storage bucket.';
