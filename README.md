# 🚀 StreamDrop - Téléchargeur Vidéo & Audio Universel (FastAPI + yt-dlp)

StreamDrop est une application web complète inspirée de *notube*, permettant de télécharger et convertir facilement des vidéos et audios depuis de nombreuses plateformes (YouTube, TikTok, Twitter/X, Instagram, Facebook, Twitch, etc.).

---

## 📁 Architecture du Projet

```text
video-downloader/
│
├── main.py                  # Backend FastAPI & logique yt-dlp asynchrone
├── requirements.txt         # Dépendances Python
├── README.md                # Documentation et guide de lancement
│
├── templates/
│   └── index.html           # Interface SPA Frontend (Tailwind CSS + JS Vanilla)
│
└── temp_downloads/          # Dossier temporaire auto-nettoyé après chaque téléchargement
```

---

## 🛠️ Prérequis

1. **Python 3.9+** installé sur votre machine.
2. *(Fortement recommandé pour le MP3 & la fusion 1080p+)* **FFmpeg** installé :
   - **Windows** : `winget install Gyan.FFmpeg` ou via [ffmpeg.org](https://ffmpeg.org/download.html)
   - **macOS** : `brew install ffmpeg`
   - **Linux (Ubuntu/Debian)** : `sudo apt update && sudo apt install ffmpeg`

> 💡 **Note :** Si FFmpeg n'est pas encore installé, l'application fonctionnera tout de même en mode **MP4** direct (qualité native streamée).

---

## ⚡ Installation & Démarrage en Local

### 1. Ouvrir un terminal dans le dossier du projet
```bash
cd C:\Users\mardo\.gemini\antigravity\scratch\video-downloader
```

### 2. (Optionnel mais recommandé) Créer et activer un environnement virtuel
- **Sous Windows (PowerShell) :**
  ```powershell
  python -m venv venv
  .\venv\Scripts\Activate.ps1
  ```
- **Sous Linux / macOS :**
  ```bash
  python3 -m venv venv
  source venv/bin/activate
  ```

### 3. Installer les dépendances
```bash
pip install -r requirements.txt
```

### 4. Lancer le serveur
```bash
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```
*Ou directement avec Python :*
```bash
python main.py
```

### 5. Accéder à l'application
Ouvrez votre navigateur web sur :
👉 **[http://127.0.0.1:8000](http://127.0.0.1:8000)**

---

## 🌟 Fonctionnalités Incluses

- **Interface Dark Mode moderne & épurée** conçue avec Tailwind CSS.
- **Support multi-plateformes** grâce à la puissance de `yt-dlp`.
- **Choix du format** : Vidéo MP4 (meilleure qualité) ou Audio MP3 (192 kbps).
- **Déclenchement automatique du téléchargement** dès que le traitement est terminé.
- **Nettoyage automatique du serveur** : les fichiers temporaires sont purgés immédiatement après l'envoi au navigateur afin de ne jamais encombrer le disque.
- **Gestion asynchrone non bloquante** (`asyncio.to_thread`) pour supporter les requêtes simultanées sans bloquer l'Event Loop de FastAPI.
