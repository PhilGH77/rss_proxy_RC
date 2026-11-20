import asyncio, html, json, os, re, threading, time
from datetime import datetime, timezone
from email.utils import format_datetime
from flask import Flask, Response, jsonify, request
from playwright.async_api import async_playwright

APP = Flask(__name__)
MAX_EPISODES = int(os.getenv('MAX_EPISODES', '25'))
CACHE_SECONDS = int(os.getenv('CACHE_SECONDS', '1800'))
PUBLIC_URL = os.getenv('PUBLIC_URL', '').rstrip('/')
PROGRAMS_FILE = os.getenv('PROGRAMS_FILE', 'programs.json')
CACHE, LOCKS = {}, {}
GUARD = threading.Lock()
AUDIO_RE = re.compile(r'https?://[^\"\'\\\s<>]+(?:\.mp3|\.m4a|\.aac|\.m3u8)(?:\?[^\"\'\\\s<>]*)?', re.I)
OHIDO_RE = re.compile(r'^https://ici\.radio-canada\.ca/ohdio/balados/(\d+)/([^/?#]+)(?:/)?$', re.I)

def esc(v): return html.escape(str(v or ''), quote=True)
def clean(v): return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', v or '')).strip()

def programs():
    with open(PROGRAMS_FILE, encoding='utf-8') as f: data=json.load(f)
    output={}
    for p in data:
        slug=p['slug'].strip().lower()
        m=OHIDO_RE.match(p['url'].strip())
        if not m: raise ValueError(f"URL OHdio invalide pour {slug}")
        output[slug]={**p, 'slug':slug, 'id':m.group(1), 'ohdio_slug':m.group(2), 'url':p['url'].rstrip('/')}
    return output

def parse_date(text):
    months={'janvier':1,'février':2,'fevrier':2,'mars':3,'avril':4,'mai':5,'juin':6,'juillet':7,'août':8,'aout':8,'septembre':9,'octobre':10,'novembre':11,'décembre':12,'decembre':12}
    m=re.search(r'(\d{1,2})\s+([a-zéû]+)\s+(\d{4})',(text or '').lower())
    try: return format_datetime(datetime(int(m[3]),months[m[2]],int(m[1]),17,tzinfo=timezone.utc)) if m else format_datetime(datetime.now(timezone.utc))
    except: return format_datetime(datetime.now(timezone.utc))

async def extract_episode(page, url):
    found=[]
    def on_response(resp):
        u=resp.url; c=(resp.headers.get('content-type') or '').lower()
        if re.search(r'\.(mp3|m4a|aac|m3u8)(\?|$)',u,re.I) or c.startswith('audio/') or 'mpegurl' in c: found.append(u)
    page.on('response',on_response)
    await page.goto(url,wait_until='domcontentloaded',timeout=60000)
    body=(await page.content()).replace('\\u002F','/').replace('\\/','/')
    found += AUDIO_RE.findall(body)
    if not found:
        for sel in ['button:has-text("Lancer l’écoute")','button:has-text("Écouter")','[aria-label*="écoute" i]','audio']:
            try:
                el=page.locator(sel).first
                if await el.count():
                    await el.click(timeout=3000); await page.wait_for_timeout(5000)
                    if found: break
            except: pass
    title=clean(await page.locator('h1').first.inner_text()) if await page.locator('h1').count() else clean(await page.title())
    desc=''
    for sel in ['meta[name="description"]','meta[property="og:description"]']:
        try:
            desc=await page.locator(sel).get_attribute('content') or ''
            if desc: break
        except: pass
    text=await page.locator('body').inner_text()
    dm=re.search(r'\d{1,2}\s+(?:janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre)\s+\d{4}',text,re.I)
    audio=next((u for u in found if re.search(r'\.(mp3|m4a|aac)(\?|$)',u,re.I)),found[0] if found else '')
    return {'url':url,'title':title,'description':clean(desc),'date':dm.group(0) if dm else '', 'audio':audio}

async def scrape_async(p):
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        ctx=await browser.new_context(locale='fr-CA',user_agent='Mozilla/5.0 (compatible; OHdioRSS/3.0)')
        page=await ctx.new_page(); await page.goto(p['url'],wait_until='domcontentloaded',timeout=60000)
        hrefs=await page.locator('a[href]').evaluate_all('(els)=>els.map(e=>e.href)')
        ep_re=re.compile(rf"/ohdio/balados/{re.escape(p['id'])}/{re.escape(p['ohdio_slug'])}/\d+/[^?#]+",re.I)
        links=[]
        for u in hrefs:
            if ep_re.search(u) and u not in links: links.append(u)
            if len(links)>=MAX_EPISODES: break
        episodes=[]
        for u in links:
            ep=await ctx.new_page()
            try:
                item=await extract_episode(ep,u)
                if item['audio']: episodes.append(item)
            except Exception: pass
            finally: await ep.close()
        await browser.close()
        return episodes

def get_feed(p,force=False):
    slug=p['slug']; existing=CACHE.get(slug)
    if existing and not force and time.time()-existing['at']<CACHE_SECONDS: return existing
    with GUARD: lock=LOCKS.setdefault(slug,threading.Lock())
    with lock:
        existing=CACHE.get(slug)
        if existing and not force and time.time()-existing['at']<CACHE_SECONDS: return existing
        eps=asyncio.run(scrape_async(p))
        data={'at':time.time(),'episodes':eps,'error':None if eps else 'Aucun fichier audio détecté'}
        CACHE[slug]=data; return data

def make_rss(p,data):
    self_url=f"{PUBLIC_URL}/feed/{p['slug']}" if PUBLIC_URL else request.url_root.rstrip('/')+f"/feed/{p['slug']}"
    items=[]
    for e in data['episodes']:
        typ='application/vnd.apple.mpegurl' if '.m3u8' in e['audio'].lower() else 'audio/mpeg'
        items.append(f'<item><title>{esc(e["title"])}</title><link>{esc(e["url"])}</link><guid isPermaLink="false">{esc(e["url"])}</guid><pubDate>{parse_date(e["date"])}</pubDate><description>{esc(e["description"])}</description><enclosure url="{esc(e["audio"])}" length="0" type="{typ}"/></item>')
    return f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel><title>{esc(p["title"])}</title><link>{esc(p["url"])}</link><description>{esc(p.get("description","Flux RSS généré depuis OHdio."))}</description><language>fr-ca</language><itunes:author>Radio-Canada</itunes:author><itunes:explicit>false</itunes:explicit><atom:link href="{esc(self_url)}" rel="self" type="application/rss+xml"/>{"".join(items)}</channel></rss>'

@APP.get('/')
def index():
    base=PUBLIC_URL or request.url_root.rstrip('/')
    rows=''.join(f'<li><a href="{base}/feed/{esc(p["slug"])}">{esc(p["title"])}</a> · <a href="{base}/health/{esc(p["slug"])}">diagnostic</a></li>' for p in programs().values())
    return f'<!doctype html><meta charset="utf-8"><title>Flux OHdio</title><h1>Flux OHdio disponibles</h1><ul>{rows}</ul>'

@APP.get('/feed/<slug>')
def feed(slug):
    p=programs().get(slug.lower())
    if not p: return Response('Émission inconnue',404,content_type='text/plain; charset=utf-8')
    try:
        data=get_feed(p,request.args.get('refresh')=='1')
        if not data['episodes']: return Response(data['error'],502,content_type='text/plain; charset=utf-8')
        return Response(make_rss(p,data),headers={'Content-Type':'application/rss+xml; charset=utf-8','Cache-Control':f'public, max-age={CACHE_SECONDS}','X-RSS-Items':str(len(data['episodes']))})
    except Exception as e: return Response(f'Erreur: {e}',502,content_type='text/plain; charset=utf-8')

@APP.get('/health')
def all_health():
    return jsonify(programmes=[{'slug':p['slug'],'title':p['title'],'cachedItems':len(CACHE.get(p['slug'],{}).get('episodes',[]))} for p in programs().values()])

@APP.get('/health/<slug>')
def health(slug):
    p=programs().get(slug.lower())
    if not p: return jsonify(status='error',error='Émission inconnue'),404
    try:
        data=get_feed(p,False); n=len(data['episodes'])
        return jsonify(status='ok' if n else 'error',programme=p['title'],items=n,cachedAt=data['at'],error=data['error']),(200 if n else 502)
    except Exception as e: return jsonify(status='error',error=str(e)),502

if __name__=='__main__': APP.run(host='0.0.0.0',port=int(os.getenv('PORT','8080')))
