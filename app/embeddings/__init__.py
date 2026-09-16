# Marks `app.embeddings` as a package.
# embedder.py: wraps the OpenAI embeddings API (text -> vector(1536)).
# search.py: hybrid (vector similarity + metadata filter) search against the
# gmail_cache / gcal_cache / gdrive_cache tables using pgvector's cosine distance operator.
