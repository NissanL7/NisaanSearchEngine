import { NextRequest, NextResponse } from 'next/server';

const API_URL = (process.env.NEXT_PUBLIC_API_URL || 'https://nisaan-searchengine-api.onrender.com').replace(/\/$/, '');

export async function GET(request: NextRequest) {
  const q = request.nextUrl.searchParams.get('q')?.trim() || '';
  const limit = request.nextUrl.searchParams.get('limit') || '10';
  const offset = request.nextUrl.searchParams.get('offset') || '0';

  if (!q) return NextResponse.json({ query: '', total: 0, results: [] }, { status: 400 });

  try {
    const upstream = await fetch(`${API_URL}/search?q=${encodeURIComponent(q)}&limit=${encodeURIComponent(limit)}&offset=${encodeURIComponent(offset)}`, {
      cache: 'no-store',
      headers: { accept: 'application/json' },
    });
    const body = await upstream.text();
    return new NextResponse(body, {
      status: upstream.status,
      headers: { 'content-type': upstream.headers.get('content-type') || 'application/json' },
    });
  } catch {
    return NextResponse.json({ detail: 'Search API unavailable' }, { status: 503 });
  }
}
