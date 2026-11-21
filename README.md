# BR Terminal

Eigenes Trading-Terminal für NQ: Level, Daily Bias, News, 3D-Grafiken mit Erklärung.
Läuft kostenlos auf GitHub Pages, Daten aktualisieren sich alle 15 Min von selbst.

## Einrichten (einmalig)
1. Auf github.com neues Repository **br-terminal** anlegen → **Public**.
2. Alle Dateien aus diesem Ordner hochladen („uploading an existing file“, Ordner reinziehen).
   Dann **Add file → Create new file**, Name `.github/workflows/update.yml`, Inhalt aus `workflow-update.yml` reinkopieren → Commit.
3. **Settings → Pages** → Source: *Deploy from a branch* → Branch `main` / `(root)` → Save.
4. **Actions** → „Daten-Update“ → *Run workflow* (erstes Laden).
5. Seite öffnen: `https://DEINNAME.github.io/br-terminal/` → am Handy „Zum Home-Bildschirm“.

## Telegram (optional)
1. @BotFather → `/newbot` → Token kopieren. Dem Bot einmal schreiben.
2. Im Browser öffnen: `https://api.telegram.org/bot<TOKEN>/getUpdates` → Zahl bei `"chat":{"id":…}` kopieren.
3. Repo → **Settings → Secrets and variables → Actions** → `TG_TOKEN` und `TG_CHAT` anlegen.
4. Kommt dann Mo–Fr 15:05 automatisch. Test: Actions → Run workflow → Haken bei Telegram.

## Dateien
- `fetch_data.py` – holt alle Daten (CBOE, Yahoo, ForexFactory, RSS), rechnet GEX, Vol-Surface, Bias
- `index.html` – die Website
- `.github/workflows/update.yml` – Automatik
