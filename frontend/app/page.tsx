'use client';

import { FormEvent, useEffect, useRef, useState } from 'react';

type Result = { id:string|number; url:string; title?:string; description?:string; content?:string; domain?:string; source?:string };

function getDomain(url:string) {
  try { return new URL(url).hostname.replace(/^www\./,''); } catch { return url; }
}

function getSnippet(r:Result) {
  const text = (r.description || r.content || '').replace(/\s+/g,' ').trim();
  return text.length > 260 ? `${text.slice(0,260)}…` : text || 'Open this result to view the page.';
}

export default function Home() {
  const [query,setQuery]=useState('');
  const [results,setResults]=useState<Result[]>([]);
  const [suggestions,setSuggestions]=useState<string[]>([]);
  const [total,setTotal]=useState(0);
  const [loading,setLoading]=useState(false);
  const [searched,setSearched]=useState(false);
  const [page,setPage]=useState(0);
  const [error,setError]=useState('');
  const [preview,setPreview]=useState<Result|null>(null);
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

  useEffect(() => {
    if (!preview) return;
    const old = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => { document.body.style.overflow = old; };
  }, [preview]);

  function handleQueryChange(value:string) {
    setQuery(value); setResults([]); setTotal(0); setSearched(false); setError(''); setPage(0); setPreview(null);
  }

  async function runSearch(e?:FormEvent, nextPage=0, selectedQuery?:string) {
    e?.preventDefault();
    const q=(selectedQuery ?? query).trim(); if(!q) return;
    if (selectedQuery) { skipSuggest.current=true; setQuery(selectedQuery); }
    setSuggestions([]); setLoading(true); setSearched(true); setPage(nextPage); setError(''); setPreview(null);
    try {
      const res=await fetch(`/api/search?q=${encodeURIComponent(q)}&limit=10&offset=${nextPage*10}`, { cache: 'no-store' });
      const data=await res.json().catch(()=>({}));
      if(!res.ok) throw new Error(data.detail || `Search API returned ${res.status}`);
      setResults(Array.isArray(data.results) ? data.results : []);
      setTotal(Number(data.total) || 0);
    } catch (err) {
      setResults([]); setTotal(0); setError(err instanceof Error ? err.message : 'Unable to reach search API');
    } finally { setLoading(false); }
  }

  return <main className="page"><div className="shell">
    <section className={`hero ${searched ? 'compact' : ''}`}>
      <h1 className="brand">Nisaan<span>SearchEngine</span></h1>
      <p className="tagline">Search the web with NisaanSearchEngine.</p>
    </section>

    <form className="search" onSubmit={runSearch}>
      <div className="searchbox">
        <span className="searchIcon">⌕</span>
        <input value={query} onChange={e=>handleQueryChange(e.target.value)} placeholder="Search the web..." aria-label="Search" autoComplete="off" />
        {query && <button className="clear" type="button" aria-label="Clear search" onClick={()=>handleQueryChange('')}>×</button>}
        {suggestions.length>0 && <div className="suggestions" role="listbox">
          {suggestions.map((s)=><button type="button" key={s} onMouseDown={(e)=>e.preventDefault()} onClick={()=>runSearch(undefined,0,s)}>⌕ {s}</button>)}
        </div>}
      </div>
      <button className="searchButton" type="submit">Search</button>
    </form>

    {searched && <div className="status"><span>{loading ? 'Searching the web…' : error ? `Search error: ${error}` : `${total.toLocaleString()} results`}</span><span className="statusHint">{!loading && !error && results.length ? 'Tap a result to preview it here' : ''}</span></div>}
    {!loading && searched && !error && results.length===0 && <div className="empty"><div className="emptyIcon">⌕</div><h2>No results found</h2><p>Try different keywords or a more specific question.</p></div>}

    {!loading && searched && !error && results.map((r,i)=>{
      const domain = r.domain || getDomain(r.url);
      return <article className="result" key={`${r.id}-${r.url}`}>
        <button className="resultButton" type="button" onClick={()=>setPreview(r)} aria-label={`Preview ${r.title || domain}`}>
          <div className="resultTop"><span className="favicon">{domain.slice(0,1).toUpperCase()}</span><div><div className="domain">{domain}</div><div className="resultUrl">{r.url}</div></div></div>
          <h2>{r.title || r.url}</h2>
          <p className="snippet">{getSnippet(r)}</p>
        </button>
        <div className="resultActions"><button type="button" onClick={()=>setPreview(r)}>Preview</button><a href={r.url} target="_blank" rel="noopener noreferrer">Open in new tab ↗</a><span>#{page*10+i+1}</span></div>
      </article>;
    })}

    {searched && !error && total>10 && <nav className="pager">
      <button type="button" disabled={page===0} onClick={()=>runSearch(undefined,page-1)}>Previous</button><span>Page {page+1}</span><button type="button" disabled={(page+1)*10>=total} onClick={()=>runSearch(undefined,page+1)}>Next</button>
    </nav>}
  </div>

  {preview && <div className="previewOverlay" role="dialog" aria-modal="true" aria-label="Web page preview" onMouseDown={e=>{if(e.target===e.currentTarget)setPreview(null)}}>
    <section className="previewWindow">
      <header className="previewHeader"><div className="previewTitle"><strong>{preview.title || getDomain(preview.url)}</strong><span>{preview.url}</span></div><div className="previewControls"><a href={preview.url} target="_blank" rel="noopener noreferrer">Open ↗</a><button type="button" onClick={()=>setPreview(null)} aria-label="Close preview">×</button></div></header>
      <div className="previewBody"><iframe src={preview.url} title={preview.title || 'Web page preview'} referrerPolicy="no-referrer" /></div>
    </section>
  </div>}
  </main>;
}
