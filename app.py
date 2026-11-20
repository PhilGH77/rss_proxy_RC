import asyncio, html, json, os, re, threading, time
from datetime import datetime, timezone
from email.utils import format_datetime
from flask import Flask, Response, jsonify, request
from playwright.async_api import async_playwright

APP = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_EPISODES = int(os.getenv('MAX_EPISODES', '25'))
CACHE_SECONDS = int(os.getenv('CACHE_SECONDS', '1800'))
PUBLIC_URL = os.getenv('PUBLIC_URL', '').rstrip('/')
PROGRAMS_FILE = os.getenv('PROGRAMS_FILE', os.path.join(BASE_DIR, 'programs.json'))
CACHE, LOCKS = {}, {}
GUARD = threading.Lock()
AUDIO_RE = re.compile(r'https?://[^\"\'\\\s<>]+(?:\.mp3|\.m4a|\.aac|\.m3u8)(?:\?[^\"\'\\\s<>]*)?', re.I)
OHIDO_RE = re.compile(r'^https://ici\.radio-canada\.ca/ohdio/balados/(\d+)/([^/?#]+)(?:/)?$', re.I)

def esc(v): return html.escape(str(v or ''), quote=True)
def clean(v): return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', v or '')).strip()

def programs():
    try:
        with open(PROGRAMS_FILE, encoding='utf-8') as f:
            data=json.load(f)
    except FileNotFoundError:
        data=[{
            'slug':'la-journee',
            'title':'La journée (est encore jeune)',
            'url':'https://ici.radio-canada.ca/ohdio/balados/9887/la-journee-est-encore-jeune',
            'description':'Flux RSS personnel généré depuis OHdio.'
        }]
    if not isinstance(data, list):
        raise ValueError('programs.json doit contenir une liste JSON')
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
    candidates = []
    diagnostics = []

    def add_candidate(audio_url, content_type="", source="network"):
        if not audio_url:
            return

        audio_url = audio_url.replace("\\u002F", "/")
        audio_url = audio_url.replace("\\/", "/")
        content_type = (content_type or "").lower()

        is_audio = (
            content_type.startswith("audio/")
            or "mpegurl" in content_type
            or "application/vnd.apple.mpegurl" in content_type
            or "application/x-mpegurl" in content_type
            or re.search(
                r"\.(mp3|m4a|aac|m3u8|mp4)(?:\?|$)",
                audio_url,
                re.I
            )
            or any(
                marker in audio_url.lower()
                for marker in [
                    "/media/",
                    "/audio/",
                    "/stream/",
                    "manifest",
                    "playlist",
                    "master.m3u8"
                ]
            )
        )

        diagnostics.append({
            "url": audio_url,
            "contentType": content_type,
            "source": source,
            "accepted": bool(is_audio)
        })

        if is_audio and audio_url not in candidates:
            candidates.append(audio_url)

    async def handle_response(response):
        try:
            add_candidate(
                response.url,
                response.headers.get("content-type", ""),
                "response"
            )
        except Exception:
            pass

    async def handle_request(request):
        try:
            resource_type = request.resource_type

            if resource_type in [
                "media",
                "fetch",
                "xhr"
            ]:
                add_candidate(
                    request.url,
                    "",
                    f"request:{resource_type}"
                )
        except Exception:
            pass

    page.on(
        "response",
        lambda response: asyncio.create_task(
            handle_response(response)
        )
    )

    page.on(
        "request",
        lambda req: asyncio.create_task(
            handle_request(req)
        )
    )

    await page.goto(
        url,
        wait_until="networkidle",
        timeout=360000
    )

    # Accepter les fenêtres de consentement éventuelles
    for selector in [
        'button:has-text("Accepter")',
        'button:has-text("Tout accepter")',
        'button:has-text("J’accepte")',
        'button:has-text("Continuer")'
    ]:
        try:
            button = page.locator(selector).first

            if await button.count():
                await button.click(timeout=3000)
                await page.wait_for_timeout(1000)
        except Exception:
            pass

    # Examiner le HTML et les scripts intégrés
    body = (
        await page.content()
    ).replace("\\u002F", "/").replace("\\/", "/")

    embedded_patterns = [
        r'https?://[^"\'\s<>]+\.mp3(?:\?[^"\'\s<>]*)?',
        r'https?://[^"\'\s<>]+\.m4a(?:\?[^"\'\s<>]*)?',
        r'https?://[^"\'\s<>]+\.aac(?:\?[^"\'\s<>]*)?',
        r'https?://[^"\'\s<>]+\.m3u8(?:\?[^"\'\s<>]*)?',
        r'"(?:url|src|audioUrl|mediaUrl|streamUrl)"\s*:\s*"([^"]+)"'
    ]

    for pattern in embedded_patterns:
        for match in re.findall(
            pattern,
            body,
            flags=re.I
        ):
            value = match if isinstance(match, str) else match[0]
            add_candidate(value, "", "html")

    # Cliquer sur les contrôles possibles du lecteur OHdio
    play_selectors = [
        'button[aria-label*="écouter" i]',
        'button[aria-label*="lecture" i]',
        'button[aria-label*="lancer" i]',
        'button[title*="écouter" i]',
        'button:has-text("Lancer l’écoute")',
        'button:has-text("Écouter")',
        '[data-testid*="play" i]',
        '[class*="playButton" i]',
        '[class*="play-button" i]',
        'audio'
    ]

    for selector in play_selectors:
        try:
            controls = page.locator(selector)
            count = min(await controls.count(), 5)

            for index in range(count):
                try:
                    await controls.nth(index).scroll_into_view_if_needed()
                    await controls.nth(index).click(
                        timeout=5000,
                        force=True
                    )
                    await page.wait_for_timeout(8000)

                    if candidates:
                        break
                except Exception:
                    continue

            if candidates:
                break

        except Exception:
            continue

    # Examiner les ressources chargées par le navigateur
    try:
        resources = await page.evaluate("""
            () => performance.getEntriesByType("resource").map(entry => ({
                name: entry.name,
                initiatorType: entry.initiatorType
            }))
        """)

        for resource in resources:
            add_candidate(
                resource.get("name"),
                "",
                "performance:" +
                resource.get("initiatorType", "")
            )
    except Exception:
        pass

    # Vérifier les éléments audio/vidéo du DOM
    try:
        media_sources = await page.evaluate("""
            () => {
                const values = [];

                document.querySelectorAll(
                    "audio, video, sourcece"
                ).forEach(element => {
                    if (element.src) {
                        valuesush(element.src);
                    }

                    if (element.currentSrc) {
                        values.push(element.currentSrc);
                    }
                });

                return values;
            }
        """)

        for source in media_sources:
            add_candidate(source, "", "dom-media")
    except Exception:
        pass

    title = clean(
        await page.locator("h1").first.inner_text()
    ) if await page.locator("h1").count() else clean(
        await page.title()
    )

    description = ""

    for selector in [
        'meta[name="description"]',
        'meta[property="og:description"]'
    ]:
        try:
            description = (
                await page.locator(selector).get_attribute(
                    "content"
                )
                or ""
            )

            if description:
                break
        except Exception:
            pass

    visible_text = await page.locator("body").inner_text()

    date_match = re.search(
        r"\d{1,2}\s+"
        r"(?:janvier|février|mars|avril|mai|juin|"
        r"juillet|août|septembre|octobre|novembre|"
        r"décembre)\s+\d{4}",
        visible_text,
        re.I
    )

    # Préférer un fichier audio direct, sinon un manifeste HLS
    audio = next(
        (
            candidate
            for candidate in candidates
            if re.search(
                r"\.(mp3|m4a|aac)(?:\?|$)",
                candidate,
                re.I
            )
        ),
        ""
    )

    if not audio:
        audio = next(
            (
                candidate
                for candidate in candidates
                if ".m3u8" in candidate.lower()
            ),
            ""
        )

    if not audio and candidates:
        audio = candidates[0]

    if not audio:
        APP.logger.warning(
            "Aucun audio pour %s. Requêtes examinées: %s",
            url,
            json.dumps(
                diagnostics[-30:],
                ensure_ascii=False
            )
        )

    return {
        "url": url,
        "title": title,
        "description": clean(description),
        "date": (
            date_match.group(0)
            if date_match
            else ""
        ),
        "audio": audio
    }

async def scrape_async(p):
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        ctx=await browser.new_context(locale='fr-CA',user_agent='Mozilla/5.0 (compatible; OHdioRSS/3.0)')
        page=await ctx.new_page(); await page.goto(p['url'],wait_until='networkidle',timeout=90000)
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


@APP.errorhandler(Exception)
def unhandled_error(error):
    APP.logger.exception('Erreur non gérée')
    return jsonify(status='error', type=type(error).__name__, error=str(error)), 500

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
