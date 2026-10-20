# Actions SEO/Perf à faire côté infra (hors code applicatif)

Ce fichier liste les correctifs identifiés lors de l'audit SEO/GEO/Perf (Limova.ai,
août 2026) qui ne se règlent pas dans ce dépôt — ce sont des réglages Cloudflare / DNS
OVH. À traiter par la personne ayant accès aux dashboards.

## 1. Chaîne de redirections sur le domaine apex — RÉSOLU (déjà correct)

**Constat initial :** Google PageSpeed Insights (Mobile) chiffrait à 1.11s la perte due à
des "redirections multiples de pages". Hypothèse de départ : une chaîne à 2 sauts
(`http://retroplanning.eu → https://retroplanning.eu → https://www.retroplanning.eu`) à
corriger via un compte Cloudflare.

**Vérification (11/08/2026) :** cette hypothèse était fausse. Le domaine n'est géré par
aucun compte Cloudflare accessible (le compte `SI@omnipub.net` n'a aucun domaine
enregistré) — la détection "Cloudflare" de l'audit vient de l'infrastructure interne de
**Render**, pas d'une configuration côté client.

Dans le dashboard Render (**Settings → Custom Domains** du service `retroplanning`) :
- `retroplanning.eu` redirige déjà vers `https://www.retroplanning.eu` en **un seul saut**
  (badge *"redirects to www.retroplanning.eu"*).
- Les deux domaines sont **Verified** + **Certificate Issued**.

C'est donc déjà configuré correctement — rien à changer. Les 1.11s mesurés par PageSpeed
sont probablement le coût incompressible d'un aller-retour HTTP + négociation TLS pour ce
seul saut, pas une chaîne à raccourcir davantage.

## 2. Enregistrement DMARC absent

**Constat :** le SPF est en place (`v=spf1 include:mx.ovh.com -all`) mais aucun
enregistrement DMARC n'existe. Ça pèse sur la délivrabilité des emails transactionnels
(confirmations de paiement, codes de vérification) et laisse le domaine exposé à
l'usurpation.

**Correctif (zone DNS OVH) :** ajouter un enregistrement TXT sur
`_dmarc.retroplanning.eu`, par exemple pour démarrer en mode observation :

```
v=DMARC1; p=none; rua=mailto:contact@retroplanning.eu
```

Une fois les rapports analysés et la délivrabilité confirmée propre, passer
progressivement à `p=quarantine` puis `p=reject`.

## 3. Netlinking (priorité la plus élevée du rapport)

**Constat :** 0 backlink, 0 domaine référent, force de domaine = 0. C'est le plus gros
écart avec les sites leaders du secteur (audit noté F sur ce critère). Chantier
marketing continu, hors dépôt :
- Articles invités / partenariats avec des blogs événementiel, wedding planning,
  agences de com, gestion de projet.
- Inscription sur des annuaires métier et SaaS (Product Hunt, SaaSHub, annuaires
  French Tech).
- Créer une fiche Google Business Profile et la lier au site (impacte aussi le SEO
  local et les avis).
- Lier les réseaux sociaux de la marque (LinkedIn, Instagram, Facebook, X, YouTube)
  une fois créés.

## 4. Bing Webmaster Tools + IndexNow (visibilité ChatGPT / Copilot) — octobre 2026

Bing alimente la recherche de ChatGPT et de Copilot. Le site n'apparaissait pas dans son index.
- Créer le site dans Bing Webmaster Tools (bing.com/webmasters) via « Importer depuis Google Search Console ».
- Soumettre https://www.retroplanning.eu/sitemap.xml.
- IndexNow est en place dans le code : la clé est servie à la racine (/<clé>.txt).
  Après chaque déploiement ou nouvel article : `python scripts/indexnow_ping.py`.

## 5. Contenus à publier via l'admin

Voir docs/articles/ : articles rédigés au format des champs de /admin/blog/nouveau.
Le brouillon « délais de production » contient des délais marqués [À VALIDER] à corriger avant publication.
