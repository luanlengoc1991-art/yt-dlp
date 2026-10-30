// latest.json va file exe lay tu GitHub Release moi nhat (do .github/workflows/studio-release.yml tao),
// nen moi lan push thay doi app len nhanh studio la cac may tu cap nhat, khong can deploy lai Worker.
const RELEASE = 'https://github.com/luanlengoc1991-art/yt-dlp/releases/latest/download';

export default {
  async fetch(request) {
    const { pathname } = new URL(request.url);
    if (pathname === '/latest.json') {
      const r = await fetch(`${RELEASE}/latest.json`, { cf: { cacheTtl: 60, cacheEverything: true } });
      if (!r.ok) return new Response('Khong lay duoc thong tin phien ban', { status: 502 });
      return new Response(r.body, {
        headers: {
          'Content-Type': 'application/json; charset=utf-8',
          'Cache-Control': 'no-store',
          'Access-Control-Allow-Origin': '*',
        },
      });
    }
    if (pathname === '/yt-dlp-studio.exe') return Response.redirect(`${RELEASE}/yt-dlp-studio.exe`, 302);
    return new Response('Not found', { status: 404 });
  },
};
