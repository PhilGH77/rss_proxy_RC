# Convertisseur OHdio multi-émissions

Une seule instance produit un flux RSS séparé pour chaque émission déclarée dans `programs.json`.

## Ajouter une émission
Copier l'URL de sa page OHdio et ajouter un objet dans `programs.json` :

```json
{
  "slug": "nom-court-unique",
  "title": "Titre affiché",
  "url": "https://ici.radio-canada.ca/ohdio/balados/1234/nom-de-lemission",
  "description": "Description facultative"
}
```

Le `slug` devient l'adresse du flux : `/feed/nom-court-unique`.

## Déploiement Railway
Déployer les cinq fichiers à la racine du dépôt. Variables recommandées :

```text
PUBLIC_URL=https://votre-service.up.railway.app
MAX_EPISODES=25
CACHE_SECONDS=1800
```

## Adresses
- Catalogue : `/`
- Tous les diagnostics : `/health`
- Diagnostic ciblé : `/health/<slug>`
- Flux RSS : `/feed/<slug>`
- Rafraîchissement forcé : `/feed/<slug>?refresh=1`

## Capacité recommandée
Commencer avec 10 à 25 épisodes par émission. Chaque émission possède son propre cache de 30 minutes. Pour 5 émissions à 25 épisodes, prévoir environ 125 ouvertures de pages lors d'un rafraîchissement complet. Éviter de forcer tous les rafraîchissements simultanément.
