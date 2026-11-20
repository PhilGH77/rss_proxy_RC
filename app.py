import asyncio, html, json, logging, os, re, sqlite3, threading, time
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, request
from playwright.async_api import async_playwright

BASE = Path(__file__).resolve().parent
PROGRAMS_FILE = Path(os.getenv('PROGRAMS_FILE', BASE / 'programs.json'))
DATA_DIR = Path(os.getenv('DATA_DIR', BASE / 'data'))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = DATA_DIR / 'feeds.sqlite3'
PUBLIC_URL = os.getenv('PUBLIC_URL', '').rstrip('/')
MAX_EPISODES = max(1, int(os.getenv('MAX_EPISODES', '10')))
CACHE_SECONDS = max(300, int(os.getenv('CACHE_SECONDS', '21600')))
PAGE_TIMEOUT_MS = max(15000, int(os.getenv('PAGE_TIMEOUT_MS', '60000')))
PLAY_WAIT_MS = max(5000, int(os.getenv('PLAY_WAIT_MS', '15000')))
AUTO_REFRESH = os.getenv('AUTO_REFRESH', 'true').lower() in {'1','true','yes'}

APP = Flask(__name__)
logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'))
LOG = logging.getLogger('rss-proxy-rc')
REFRESH_LOCK = threading.Lock()
RUNNING = set()

AUDIO_EXT = re.compile(r'\.(mp3|m4a|aac|ogg|oga|opus|wav)(?:\?|$)', re.I)
HLS_EXT = re.compile(r'\.m3u8(?:\?|$)', re.I)
IMAGE_EXT = re.compile(r'\.(jpe?g|png|gif|webp|svg|avif)(?:\?|$)', re.I)
URL_RE = re.compile(r'https?://[^"\'\s<>{}\\]+', re.I)
DATE_RE = re.compile(r'\d{1,2}\s+(?:janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre)\s+\d{4}', re.I)


def db():
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('''CREATE TABLE IF NOT EXISTS episodes (
        programme TEXT NOT NULL, guid TEXT NOT NULL, title TEXT NOT NULL,
        page_url TEXT NOT NULL, published TEXT, description TEXT,
        audio_url TEXT NOT NULL, mime TEXT NOT NULL, cover TEXT,
        updated REAL NOT NULL, PRIMARY KEY(programme,guid))''')
    con.execute('''CREATE TABLE IF NOT EXISTS state (
        programme TEXT PRIMARY KEY, status TEXT, message TEXT,
        started REAL, finished REAL, discovered INTEGER DEFAULT 0,
        accepted INTEGER DEFAULT 0)''')
    return con


def esc(value): return html.escape(str(value or ''), quote=True)
def clean(value): return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', value or '')).strip()


def load_programs():
    with PROGRAMS_FILE.open(encoding='utf-8-sig') as f:
        raw = json.load(f)
    if isinstance(raw, dict):
        raw = raw.get('programs') or raw.get('programmes') or [raw]
    if not isinstance(raw, list):
        raise ValueError('programs.json doit contenir une liste, ou un objet avec une clé programs/programmes')
    result = {}
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f'Entrée {i+1}: objet JSON attendu')
        url = str(item.get('url') or item.get('ohdio_url') or item.get('programme_url') or '').rstrip('/')
        m = re.match(r'https://ici\.radio-canada\.ca/ohdio/balados/(\d+)/([^/?#]+)', url, re.I)
        if not m:
            raise ValueError(f'Entrée {i+1}: URL OHdio invalide: {url}')
        slug = str(item.get('slug') or item.get('key') or item.get('name') or m.group(2)).strip().lower()
        slug = re.sub(r'[^a-z0-9-]+', '-', slug).strip('-')
        title = str(item.get('title') or item.get('titre') or slug)
        result[slug] = {**item, 'slug': slug, 'title': title, 'url': url,
                        'programme_id': m.group(1), 'ohdio_slug': m.group(2),
                        'description': str(item.get('description') or 'Flux RSS généré depuis OHdio.')}
    return result


def is_rejected(url, content_type=''):
    low = url.lower(); ctype = content_type.lower()
    return (not url.startswith(('http://','https://')) or IMAGE_EXT.search(low)
            or 'images.radio-canada.ca' in low or ctype.startswith(('image/','font/','text/css')))


def classify(url, content_type='', resource_type='', source=''):
    if is_rejected(url, content_type): return None
    low = url.lower(); ctype = content_type.lower()
    if AUDIO_EXT.search(low):
        ext = AUDIO_EXT.search(low).group(1).lower()
        return {'mp3':'audio/mpeg','m4a':'audio/mp4','aac':'audio/aac','ogg':'audio/ogg','oga':'audio/ogg','opus':'audio/opus','wav':'audio/wav'}[ext]
    if HLS_EXT.search(low) or 'mpegurl' in ctype: return 'application/vnd.apple.mpegurl'
    if ctype.startswith('audio/'): return ctype.split(';')[0]
    # URLs sans extension: uniquement si le navigateur confirme une ressource media
    if resource_type == 'media' and source in {'request','response','performance'}: return 'audio/mpeg'
    return None


def extract_urls(value):
    found=[]
    def walk(v):
        if isinstance(v, dict):
            for x in v.values(): walk(x)
        elif isinstance(v, list):
            for x in v: walk(x)
        elif isinstance(v, str):
            s=v.replace('\\u002F','/').replace('\\/','/').replace('\\u0026','&').replace('&amp;','&')
            found.extend(URL_RE.findall(s))
    walk(value)
    return found


async def inspect_episode(context, url):
    page = await context.new_page()
    candidates=[]; seen=set(); tasks=[]
    def add(u, ctype='', rtype='', source=''):
        u=(u or '').replace('\\u002F','/').replace('\\/','/').replace('\\u0026','&').replace('&amp;','&')
        mime=classify(u,ctype,rtype,source)
        if mime and u not in seen:
            seen.add(u); candidates.append((u,mime,source))
    async def inspect_response(resp):
        try:
            ctype=(await resp.all_headers()).get('content-type','')
            add(resp.url,ctype,resp.request.resource_type,'response')
            if resp.request.resource_type in {'xhr','fetch'} or 'json' in ctype or 'javascript' in ctype:
                if int((await resp.all_headers()).get('content-length','0') or 0) < 5_000_000:
                    body=await resp.text()
                    try: values=extract_urls(json.loads(body))
                    except Exception: values=URL_RE.findall(body.replace('\\/','/').replace('\\u002F','/'))
                    for u in values: add(u,'','','body')
        except Exception: pass
    def on_response(resp): tasks.append(asyncio.create_task(inspect_response(resp)))
    def on_request(req): add(req.url,'',req.resource_type,'request')
    page.on('response',on_response); page.on('request',on_request)
    try:
        await page.goto(url,wait_until='domcontentloaded',timeout=PAGE_TIMEOUT_MS)
        await page.wait_for_timeout(3500)
        # Consent, then play controls in every frame
        for frame in page.frames:
            for sel in ['button:has-text("Tout accepter")','button:has-text("Accepter")','button:has-text("Continuer")']:
                try:
                    if await frame.locator(sel).count(): await frame.locator(sel).first.click(force=True,timeout=1500)
                except Exception: pass
        clicked=False
        selectors=['button:has-text("Lancer l’écoute")','button:has-text("Lancer l\'écoute")','button:has-text("Écouter")',
                   '[role="button"]:has-text("Lancer l’écoute")','button[aria-label*="écoute" i]',
                   'button[aria-label*="lecture" i]','button[aria-label*="play" i]','[data-testid*="play" i]']
        for frame in page.frames:
            for sel in selectors:
                try:
                    loc=frame.locator(sel)
                    if await loc.count():
                        await loc.first.scroll_into_view_if_needed(); await loc.first.click(force=True,timeout=4000)
                        clicked=True; break
                except Exception: pass
            if clicked: break
        if not clicked:
            try:
                clicked=await page.evaluate("""() => {const a=[...document.querySelectorAll('button,[role=button]')]; const e=a.find(x=>/lancer.*[ée]coute|[ée]couter|lecture|play/i.test(x.innerText||x.textContent||x.getAttribute('aria-label')||'')); if(e){e.click();return true} return false}""")
            except Exception: pass
        await page.wait_for_timeout(PLAY_WAIT_MS)
        try:
            entries=await page.evaluate("() => performance.getEntriesByType('resource').map(e=>({url:e.name,type:e.initiatorType}))")
            for x in entries: add(x.get('url',''),'',x.get('type',''),'performance')
            media=await page.evaluate("() => [...document.querySelectorAll('audio,video,source')].flatMap(e=>[e.currentSrc,e.src]).filter(Boolean)")
            for u in media: add(u,'','media','performance')
        except Exception: pass
        if tasks: await asyncio.gather(*tasks,return_exceptions=True)
        title=clean(await page.locator('h1').first.inner_text()) if await page.locator('h1').count() else clean(await page.title())
        desc=''
        for sel in ['meta[name="description"]','meta[property="og:description"]']:
            try:
                desc=await page.locator(sel).get_attribute('content') or ''
                if desc: break
            except Exception: pass
        bodytext=await page.locator('body').inner_text()
        dm=DATE_RE.search(bodytext)
        cover=''
        for sel in ['meta[property="og:image"]','meta[name="twitter:image"]']:
            try:
                cover=await page.locator(sel).get_attribute('content') or ''
                if cover: break
            except Exception: pass
        # Prefer direct audio over HLS and ignore suspicious generic URLs
        candidates.sort(key=lambda x: (0 if AUDIO_EXT.search(x[0]) else 1 if HLS_EXT.search(x[0]) else 2))
        audio,mime,source=candidates[0] if candidates else ('','','')
        LOG.info('episode=%s clicked=%s candidates=%s selected=%s source=%s',url,clicked,len(candidates),audio,source)
        return {'guid':url,'title':title,'page_url':url,'published':dm.group(0) if dm else '',
                'description':clean(desc),'audio_url':audio,'mime':mime,'cover':cover}
    finally:
        await page.close()


async def discover(program):
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage','--disable-gpu','--disable-extensions','--no-zygote'])
        context=await browser.new_context(locale='fr-CA',user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36')
        page=await context.new_page()
        try:
            await page.goto(program['url'],wait_until='domcontentloaded',timeout=PAGE_TIMEOUT_MS)
            await page.wait_for_timeout(3500)
            hrefs=await page.locator('a[href]').evaluate_all('(els)=>els.map(e=>e.href)')
            pattern=re.compile(rf"/ohdio/balados/{re.escape(program['programme_id'])}/{re.escape(program['ohdio_slug'])}/\d+/[^?#]+",re.I)
            links=[]
            for u in hrefs:
                if pattern.search(u) and u not in links: links.append(u)
                if len(links)>=MAX_EPISODES: break
        finally: await page.close()
        episodes=[]
        for u in links:
            try:
                ep=await inspect_episode(context,u)
                if ep['audio_url']: episodes.append(ep)
            except Exception as e: LOG.exception('Échec épisode %s: %s',u,e)
        await browser.close()
        return links,episodes


def save_state(slug,status,message='',started=None,finished=None,discovered=0,accepted=0):
    with db() as con:
        con.execute('''INSERT INTO state(programme,status,message,started,finished,discovered,accepted) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(programme) DO UPDATE SET status=excluded.status,message=excluded.message,started=COALESCE(excluded.started,state.started),finished=excluded.finished,discovered=excluded.discovered,accepted=excluded.accepted''',
        (slug,status,message,started,finished,discovered,accepted))


def refresh_worker(slug):
    try:
        programs=load_programs(); p=programs.get(slug)
        if not p: return
        started=time.time(); save_state(slug,'running','',started,None,0,0)
        links,episodes=asyncio.run(discover(p))
        with db() as con:
            for e in episodes:
                con.execute('''INSERT INTO episodes(programme,guid,title,page_url,published,description,audio_url,mime,cover,updated)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(programme,guid) DO UPDATE SET title=excluded.title,page_url=excluded.page_url,published=excluded.published,description=excluded.description,audio_url=excluded.audio_url,mime=excluded.mime,cover=excluded.cover,updated=excluded.updated''',
                (slug,e['guid'],e['title'],e['page_url'],e['published'],e['description'],e['audio_url'],e['mime'],e['cover'],time.time()))
        status='ready' if episodes else 'error'; msg='' if episodes else 'Aucun fichier audio détecté; consulter les logs candidates/selected.'
        save_state(slug,status,msg,started,time.time(),len(links),len(episodes))
    except Exception as e:
        LOG.exception('Rafraîchissement %s',slug); save_state(slug,'error',f'{type(e).__name__}: {e}',None,time.time(),0,0)
    finally:
        with REFRESH_LOCK: RUNNING.discard(slug)


def start_refresh(slug):
    with REFRESH_LOCK:
        if slug in RUNNING: return False
        RUNNING.add(slug)
    threading.Thread(target=refresh_worker,args=(slug,),daemon=True,name=f'refresh-{slug}').start()
    return True


def parse_date(fr):
    months={'janvier':1,'février':2,'fevrier':2,'mars':3,'avril':4,'mai':5,'juin':6,'juillet':7,'août':8,'aout':8,'septembre':9,'octobre':10,'novembre':11,'décembre':12,'decembre':12}
    m=re.search(r'(\d{1,2})\s+([a-zéû]+)\s+(\d{4})',(fr or '').lower())
    try: return format_datetime(datetime(int(m[3]),months[m[2]],int(m[1]),17,tzinfo=timezone.utc)) if m else format_datetime(datetime.now(timezone.utc))
    except Exception: return format_datetime(datetime.now(timezone.utc))


def make_rss(p,rows):
    self_url=f'{PUBLIC_URL}/feed/{p["slug"]}' if PUBLIC_URL else request.url_root.rstrip('/')+f'/feed/{p["slug"]}'
    cover=next((r['cover'] for r in rows if r['cover']),str(p.get('image') or p.get('cover') or ''))
    items=[]
    for r in rows:
        image=f'<itunes:image href="{esc(r["cover"] or cover)}"/>' if (r['cover'] or cover) else ''
        items.append(f'<item><title>{esc(r["title"])}</title><link>{esc(r["page_url"])}</link><guid isPermaLink="false">{esc(r["guid"])}</guid><pubDate>{parse_date(r["published"])}</pubDate><description>{esc(r["description"])}</description>{image}<enclosure url="{esc(r["audio_url"])}" length="0" type="{esc(r["mime"]) }"/></item>')
    channel_image=f'<itunes:image href="{esc(cover)}"/>' if cover else ''
    return f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel><title>{esc(p["title"])}</title><link>{esc(p["url"])}</link><description>{esc(p["description"])}</description><language>fr-ca</language><itunes:author>Radio-Canada</itunes:author><itunes:explicit>false</itunes:explicit>{channel_image}<atom:link href="{esc(self_url)}" rel="self" type="application/rss+xml"/>{"".join(items)}</channel></rss>'


@APP.get('/ready')
def ready(): return jsonify(status='ready'),200

@APP.get('/health')
def health():
    try:
        ps=load_programs(); return jsonify(status='ok',programmes=[{'slug':x['slug'],'title':x['title']} for x in ps.values()])
    except Exception as e: return jsonify(status='error',type=type(e).__name__,error=str(e)),500

@APP.get('/')
def index():
    ps=load_programs(); base=PUBLIC_URL or request.url_root.rstrip('/')
    li=''.join(f'<li><b>{esc(p["title"])}</b>: <a href="{base}/feed/{esc(s)}">RSS</a> · <a href="{base}/status/{esc(s)}">état</a> · <a href="{base}/refresh/{esc(s)}">rafraîchir</a></li>' for s,p in ps.items())
    return f'<!doctype html><meta charset="utf-8"><title>RSS Proxy RC</title><h1>RSS Proxy RC</h1><ul>{li}</ul>'

@APP.get('/status/<slug>')
def status(slug):
    if slug not in load_programs(): return jsonify(status='error',error='Émission inconnue'),404
    with db() as con:
        state=con.execute('SELECT * FROM state WHERE programme=?',(slug,)).fetchone()
        count=con.execute('SELECT COUNT(*) n,MAX(updated) updated FROM episodes WHERE programme=?',(slug,)).fetchone()
    return jsonify(programme=slug,state=dict(state) if state else {'status':'not_started'},cachedItems=count['n'],cacheUpdated=count['updated'])

@APP.get('/refresh/<slug>')
def refresh(slug):
    if slug not in load_programs(): return jsonify(status='error',error='Émission inconnue'),404
    started=start_refresh(slug)
    return jsonify(status='started' if started else 'running',statusUrl=f'/status/{slug}',feedUrl=f'/feed/{slug}'),202

@APP.get('/feed/<slug>')
def feed(slug):
    p=load_programs().get(slug)
    if not p: return jsonify(status='error',error='Émission inconnue'),404
    with db() as con:
        rows=con.execute('SELECT * FROM episodes WHERE programme=? ORDER BY updated DESC LIMIT ?',(slug,MAX_EPISODES)).fetchall()
        state=con.execute('SELECT * FROM state WHERE programme=?',(slug,)).fetchone()
    if not rows:
        if AUTO_REFRESH: start_refresh(slug)
        return jsonify(status='not_ready',message='Génération démarrée en arrière-plan.',statusUrl=f'/status/{slug}',refreshUrl=f'/refresh/{slug}'),202
    # Serve stale cache immediately and refresh in background
    newest=max(r['updated'] for r in rows)
    if AUTO_REFRESH and time.time()-newest>CACHE_SECONDS: start_refresh(slug)
    return Response(make_rss(p,rows),headers={'Content-Type':'application/rss+xml; charset=utf-8','Cache-Control':'public, max-age=900','X-RSS-Items':str(len(rows))})

@APP.errorhandler(Exception)
def error(e):
    LOG.exception('Erreur HTTP'); return jsonify(status='error',type=type(e).__name__,error=str(e)),500

if __name__=='__main__':
    with db(): pass
    APP.run(host='0.0.0.0',port=int(os.getenv('PORT','8080')))
