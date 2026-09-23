import { NextResponse } from 'next/server';

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const q = (searchParams.get('q') || '').trim();
  if (q.length < 2) return NextResponse.json({ suggestions: [] });

  try {
    const language = /[\u0900-\u097F]/.test(q) ? 'hi' : 'en';
    const url = new URL(`https://${language}.wikipedia.org/w/api.php`);
    url.searchParams.set('action', 'opensearch');
    url.searchParams.set('search', q);
    url.searchParams.set('limit', '8');
    url.searchParams.set('namespace', '0');
    url.searchParams.set('format', 'json');

    const response = await fetch(url, {
      headers: { 'User-Agent': 'NisaanSearchEngine/1.0' },
      cache: 'no-store',
    });
    if (!response.ok) return NextResponse.json({ suggestions: [] });
    const data = await response.json();
    return NextResponse.json({ suggestions: Array.isArray(data?.[1]) ? data[1] : [] });
  } catch {
    return NextResponse.json({ suggestions: [] });
  }
}
