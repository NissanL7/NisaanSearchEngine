"""NisaanSearchEngine V1 HTTP API."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from search_index.indexer import search

app = FastAPI(title="NisaanSearchEngine API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "NisaanSearchEngine API"}


@app.get("/search")
def search_endpoint(
    q: str = Query(min_length=1, max_length=300),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    try:
        result = search(q.strip(), limit=limit, offset=offset)
        return {
            "query": q.strip(),
            "total": result.get("estimatedTotalHits", 0),
            "results": result.get("hits", []),
            "limit": limit,
            "offset": offset,
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Search service unavailable") from exc
