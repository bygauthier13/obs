# OBS - soundscout (scanner de niches sonores)

Entrée : une industrie. Sortie : un rapport de concepts de produit musicaux/sonores, chacun avec preuves citées mot pour mot (vérifiées par le code), concurrents, prix sourcés et verdict.

## Mise en route (une fois)

1. Téléverse ces fichiers à la racine de ton dépôt privé (glisser-déposer sur la page GitHub), en gardant le dossier `.github/workflows`.
2. Dépôt > Settings > Secrets and variables > Actions > onglet **Secrets** : crée `ANTHROPIC_API_KEY` et `EXA_API_KEY`.
3. Même page, onglet **Variables** : crée `MODEL_FAST` (modèle bon marché pour l'extraction) et `MODEL_STRONG` (modèle pour idées, concurrence et verdicts). Prends les identifiants exacts dans la liste des modèles de la console Anthropic.
4. Onglet **Actions** > **Scan** > **Run workflow** > tape l'industrie. Au bout de quelques minutes, télécharge l'artefact `rapport-scan` : `rapport.md` est le rapport.

Premier lancement conseillé : plafond de tokens à 300000 pour mesurer le coût réel avant de laisser tourner plus large. Le plafond arrête le scan proprement et le rapport indique ce qui n'a pas été fait.

## En local

    ANTHROPIC_API_KEY=... EXA_API_KEY=... MODEL_FAST=... MODEL_STRONG=... python scout.py "podcasteurs indépendants" --max-tokens 300000

Test hors ligne, sans clés : `SCOUT_MOCK=1 python scout.py "test" --out runs/mock`

## Ce que le code garantit, et ce qu'il ne garantit pas

- Garanti par le code : une citation est rejetée si elle n'est pas retrouvée mot pour mot dans le texte renvoyé par le moteur de recherche ; les documents hors fenêtre de dates ou sans date sont écartés ; un prix de concurrent n'apparaît que si sa citation est vérifiée ; un concept ne peut pas être « candidate » avec moins de 2 sources indépendantes ou sans prix de concurrent vérifié.
- Non garanti : le tri (idées, menaces, verdicts) est fait par un modèle de langage. Reddit, LinkedIn et les communautés privées ne sont pas lus. Seul un test client prouve la demande.
- Non testé en conditions réelles au moment de l'écriture : les appels aux API Anthropic et Exa n'ont été testés qu'avec des simulations.
