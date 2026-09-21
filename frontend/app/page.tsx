'use client';

import { FormEvent, useState } from 'react';

const API_URL = (process.env.NEXT_PUBLIC_API_URL || 'https://nisaan-searchengine-api.onrender.com').replace(/\/$/, '');

type Result = { id:number; url:string; title?:string; description?:string; content?:string; domain?:string };

export default function Home() {
  const [query,setQuery]=useState('');
  const [results,setResults]=useState<Result[]>([]);
  const [total,setTotal]=useState(0);
  const [loading,setLoading]=useState(false);
  const [searched,setSearched]=useState(false);
  const [page,setPage]=useState(0);
  const [error,setError]=useState('');

  async function runSearch(e?:FormEvent, nextPage=0) {
    e?.preventDefault();
    const q=query.trim(); if(!q) return;
    setLoading(true); setSearched(true); setPage(nextPage); setError('');
    try {
      const res=await fetch(`${API_URL}/search?q=${encodeURIComponent(q)}&limit=10&offset=${nextPage*10}`, { cache: 'no-store' });
      if(!res.ok) throw new Error(`API returned ${res.status}`);
      const data=await res.json(); setResults(data.results||[]); setTotal(data.total||0);
    } catch (err) {
      setResults([]); setTotal(0);
      setError(err instanceof Error ? err.message : 'Unable to reach search API');
    } finally { setLoading(false); }
  }

  return <main className="page"><div className="shell">
    <section className="hero">
      <h1 className="brand">Nisaan<span>SearchEngine</span></h1>
      <p className="tagline">A search engine built by NissanL7.</p>
    </section>
    <form className="search" onSubmit={runSearch}>
      <input value={query} onChange={e=>setQuery(e.target.value)} placeholder="Search the web..." aria-label="Search" />
      <button type="submit">Search</button>
    </form>
    {searched && <div className="status">{loading ? 'Searching…' : error ? `Search error: ${error}` : `${total.toLocaleString()} results`}</div>}
    {!loading && searched && !error && results.length===0 && <div className="empty">No results found. Try another search.</div>}
    {!loading && results.map(r=><article className="result" key={r.id}>
      <h2><a href={r.url} target="_blank" rel="noreferrer">{r.title || r.url}</a></h2>
      <div className="url">{r.url}</div>
      <p className="snippet">{r.description || (r.content||'').slice(0,240)}</p>
    </article>)}
    {searched && !error && total>10 && <nav className="pager">
      <button disabled={page===0} onClick={()=>runSearch(undefined,page-1)}>Previous</button>
      <span>Page {page+1}</span>
      <button disabled={(page+1)*10>=total} onClick={()=>runSearch(undefined,page+1)}>Next</button>
    </nav>}
  </div></main>;
}
