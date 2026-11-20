# RSS Proxy RC — reconstruction robuste

Cette version conserve `programs.json` et remplace le reste.

## Fichiers à remplacer
Gardez votre `programs.json` actuel. Remplacez `app.py`, `requirements.txt`, `Dockerfile`, `railway.json`, `.dockerignore`.

## Variables Railway
```text
PUBLIC_URL=https://rssproxyrc-production.up.railway.app
MAX_EPISODES=5
CACHE_SECONDS=21600
AUTO_REFRESH=true
PAGE_TIMEOUT_MS=60000
PLAY_WAIT_MS=15000
DATA_DIR=/app/data
```
Ne définissez pas `PORT`. Railway le fournit.

## Volume Railway requis
Montez un volume persistant sur `/app/data`. Sans volume, le cache SQLite disparaît à chaque déploiement.

## Endpoints
- `/ready` : healthcheck Railway, aucune extraction
- `/health` : validation de programs.json
- `/` : catalogue
- `/refresh/<slug>` : démarre l'extraction en arrière-plan et répond immédiatement 202
- `/status/<slug>` : état et nombre d'épisodes en cache
- `/feed/<slug>` : sert toujours le cache existant; si vide, démarre l'extraction et répond 202

## Déploiement
1. Copiez les nouveaux fichiers dans le dépôt, sans modifier `programs.json`.
2. Ajoutez le volume `/app/data`.
3. Déployez avec l'option Clear build cache.
4. Testez `/ready`, puis `/health`.
5. Ouvrez `/refresh/la-journee` une seule fois.
6. Consultez `/status/la-journee` jusqu'à `ready`.
7. Ajoutez `/feed/la-journee` dans Pocket Casts.

## Diagnostic audio
Les images sont explicitement rejetées. Seules les extensions audio, les manifestes HLS, un Content-Type audio, ou une ressource navigateur de type media sont acceptés. Les réponses JSON XHR/fetch sont inspectées récursivement. Les logs indiquent `clicked`, le nombre de `candidates`, l'URL `selected` et sa `source`.
