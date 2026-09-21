"""NisaanSearchEngine V1 HTTP API."""
from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .search_index import search

origins = [
    origin.strip()
    for origin in os.getenv("API_CORS_ORIGINS", "http://localhost:3000").split(",")
    if origin.strip()
]

app = FastAPI(title="NisaanSearchEngine API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
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
