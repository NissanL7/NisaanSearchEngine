'use client';

import { FormEvent, useEffect, useRef, useState } from 'react';

type Result = { id:string|number; url:string; title?:string; description?:string; content?:string; domain?:string; source?:string };

export default function Home() {
  const [query,setQuery]=useState('');
  const [results,setResults]=useState<Result[]>([]);
  const [suggestions,setSuggestions]=useState<string[]>([]);
  const [total,setTotal]=useState(0);
  const [loading,setLoading]=useState(false);
  const [searched,setSearched]=useState(false);
  const [page,setPage]=useState(0);
  const [error,setError]=useState('');
  const skipSuggest = useRef(false);

  useEffect(() => {
    const q = query.trim();
    if (skipSuggest.current) { skipSuggest.current = false; return; }
    if (q.length < 2) { setSuggestions([]); return; }
    const timer = setTimeout(async () => {
      try {
        const res = await fetch(`/api/suggest?q=${encodeURIComponent(q)}`, { cache: 'no-store' });
        const data = await res.json();
        setSuggestions(Array.isArray(data.suggestions) ? data.suggestions : []);
      } catch { setSuggestions([]); }
    }, 220);
    return () => clearTimeout(timer);
  }, [query]);

  async function runSearch(e?:FormEvent, nextPage=0, selectedQuery?:string) {
    e?.preventDefault();
    const q=(selectedQuery ?? query).trim(); if(!q) return;
    if (selectedQuery) { skipSuggest.current=true; setQuery(selectedQuery); }
    setSuggestions([]); setLoading(true); setSearched(true); setPage(nextPage); setError('');
    try {
      const res=await fetch(`/api/search?q=${encodeURIComponent(q)}&limit=10&offset=${nextPage*10}`, { cache: 'no-store' });
      const data=await res.json().catch(()=>({}));
      if(!res.ok) throw new Error(data.detail || `Search API returned ${res.status}`);
      setResults(Array.isArray(data.results) ? data.results : []);
      setTotal(Number(data.total) || 0);
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
      <div className="searchbox">
        <input value={query} onChange={e=>setQuery(e.target.value)} placeholder="Search the web..." aria-label="Search" autoComplete="off" />
        {suggestions.length>0 && <div className="suggestions" role="listbox">
          {suggestions.map((s)=><button type="button" key={s} onMouseDown={(e)=>e.preventDefault()} onClick={()=>runSearch(undefined,0,s)}>{s}</button>)}
        </div>}
      </div>
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
      <button type="button" disabled={page===0} onClick={()=>runSearch(undefined,page-1)}>Previous</button>
      <span>Page {page+1}</span>
      <button type="button" disabled={(page+1)*10>=total} onClick={()=>runSearch(undefined,page+1)}>Next</button>
    </nav>}
  </div></main>;
}
