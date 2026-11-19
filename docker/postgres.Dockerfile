# 延用 postgres:17 的作業系統與資料目錄，只加入同一 PostgreSQL major 的 pgvector。
FROM pgvector/pgvector:0.8.1-pg17 AS pgvector
FROM postgres:17
COPY --from=pgvector /usr/lib/postgresql/17/lib/vector.so /usr/lib/postgresql/17/lib/vector.so
COPY --from=pgvector /usr/share/postgresql/17/extension/vector* /usr/share/postgresql/17/extension/
