# Search index

NisaanSearchEngine V1 uses Meilisearch as the retrieval index.

## Environment

Set:

```text
MEILISEARCH_URL=http://localhost:7700
MEILISEARCH_MASTER_KEY=your-key
MEILISEARCH_INDEX=pages
```

`indexer.py` configures searchable, filterable, sortable and displayed fields and provides document indexing and query functions.

The PostgreSQL/Supabase database remains the source of truth; this index can be rebuilt from stored pages.
