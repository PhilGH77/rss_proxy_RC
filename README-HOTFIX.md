# Correctif Railway

Remplacer les fichiers du dépôt par ceux du paquet, puis pousser le commit. `railway.json` force Railway à utiliser le Dockerfile. Le chemin de `programs.json` est maintenant absolu et une configuration de secours est intégrée.

Variables Railway :
- `PUBLIC_URL=https://votre-service.up.railway.app`
- `MAX_EPISODES=10`
- `CACHE_SECONDS=3600`

Après le redéploiement, tester dans cet ordre :
1. `/health` : doit retourner la liste des émissions sans lancer Chromium.
2. `/` : doit afficher le catalogue.
3. `/health/la-journee` : lance l'extraction et peut prendre plusieurs minutes au premier appel.
4. `/feed/la-journee` : retourne le RSS.

En cas d'erreur 500, la réponse affiche maintenant le type et le message d'erreur. Les mêmes détails apparaissent dans Railway > Deployments > View Logs.
