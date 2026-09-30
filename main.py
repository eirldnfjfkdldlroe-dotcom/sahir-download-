import asyncio
import json
import os
import re
import shutil
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel
import yt_dlp

# --- 1. CONFIGURATION DU SYSTÈME & CHEMINS ---

# Auto-détection et injection de FFmpeg dans le PATH système
POSSIBLE_FFMPEG_PATHS = [
    Path(r"C:\Users\mardo\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.2-full_build\bin"),
    Path(os.path.expanduser(r"~\AppData\Local\Microsoft\WinGet\Links")),
]
for p in POSSIBLE_FFMPEG_PATHS:
    if p.exists() and str(p) not in os.environ.get("PATH", ""):
        os.environ["PATH"] = str(p) + os.pathsep + os.environ.get("PATH", "")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
TEMP_DIR = BASE_DIR / "temp_downloads"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# Headers simulant un navigateur Chrome récent
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# Extensions d'images à exclure absolument des flux vidéo
IMAGE_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".bmp", ".ico", ".avif", ".tiff"
)

# Extensions multimédia réelles (Vidéo & Audio)
MEDIA_EXTENSIONS = {
    ".mp4", ".webm", ".mkv", ".mov", ".ts", ".avi", ".flv",
    ".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".opus"
}

# Mémoire globale pour le suivi en temps réel de la progression (SSE)
PROGRESS_DATA: Dict[str, dict] = {}

app = FastAPI(
    title="sahir-dowload",
    description="Moteur de téléchargement vidéo & audio ultra-robuste avec SSE et Fallback intelligent",
    version="3.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- 2. MODÈLES PYDANTIC ---

class AnalyzeRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    media_type: str = "video"  # "video" ou "audio"
    format: str = "mp4"  # Vidéo: "mp4", "webm" | Audio: "mp3", "m4a", "wav"
    quality: str = "best"  # "2160", "1440", "1080", "720", "480", "best" / "320", "256", "192", "128"


# --- 3. UTILITAIRES SYSTÈME & RÉSEAU ---

def check_ffmpeg_installed() -> bool:
    """Vérifie la présence de FFmpeg dans le PATH."""
    return shutil.which("ffmpeg") is not None


def get_dynamic_referer(url: str) -> str:
    """Génère un header Referer basé sur le nom de domaine de la cible."""
    try:
        parsed = urllib.parse.urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}/"
    except Exception:
        return "https://www.google.com/"


def is_image_url(url: str) -> bool:
    """Vérifie si une URL pointe vers un fichier image."""
    try:
        clean_path = urllib.parse.urlparse(url).path.lower()
        return any(clean_path.endswith(ext) for ext in IMAGE_EXTENSIONS)
    except Exception:
        return False


def is_direct_media_url(url: str) -> bool:
    """Détecte si l'URL pointe directement vers un flux vidéo/audio brut."""
    try:
        clean_path = urllib.parse.urlparse(url).path.lower()
        direct_exts = (".mp4", ".webm", ".m3u8", ".mpd", ".mov", ".ts", ".mkv", ".mp3", ".m4a")
        return any(clean_path.endswith(ext) for ext in direct_exts)
    except Exception:
        return False


def get_base_ytdlp_opts(url: str) -> dict:
    """Configuration de base sécurisée et anonymisée pour yt-dlp."""
    referer = get_dynamic_referer(url)
    return {
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "noplaylist": True,
        "ignoreerrors": False,
        "restrictfilenames": True,
        # Sécurité critique : Ne jamais générer la miniature lors du téléchargement vidéo
        "writethumbnail": False,
        "write_all_thumbnails": False,
        "hls_prefer_native": False,  # FFmpeg gère les flux HLS m3u8 pour un assemblage parfait
        "http_headers": {
            "User-Agent": CHROME_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": referer,
            "Sec-Fetch-Mode": "navigate",
        },
    }


# --- 4. LE MOTEUR "ULTIMATE FALLBACK" (BeautifulSoup4) ---

def scrape_page_metadata_and_streams(page_url: str) -> dict:
    """
    Scraper expert pour les sites non supportés nativement :
    1. Récupère le titre via OpenGraph (og:title, twitter:title, <title>).
    2. Récupère la miniature via OpenGraph (og:image, twitter:image, <video poster>).
    3. Scanne les balises <video src>, <source src>, <iframe> et les scripts pour flux .m3u8/.mp4.
    """
    headers = {
        "User-Agent": CHROME_USER_AGENT,
        "Referer": get_dynamic_referer(page_url),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    result = {
        "title": None,
        "thumbnail": None,
        "candidates": [],
    }

    try:
        response = requests.get(page_url, headers=headers, timeout=12)
        if response.status_code >= 400:
            return result

        soup = BeautifulSoup(response.text, "html.parser")

        # 1. Extraction Titre & Miniature depuis les balises Meta
        for meta in soup.find_all("meta"):
            prop = meta.get("property", "").lower()
            name = meta.get("name", "").lower()
            content = meta.get("content", "").strip()
            if not content:
                continue

            # Titre
            if prop in ("og:title", "twitter:title") or name in ("title", "twitter:title"):
                if not result["title"]:
                    result["title"] = content

            # Miniature
            if prop in ("og:image", "og:image:url", "og:image:secure_url") or name in ("twitter:image", "twitter:image:src"):
                if not result["thumbnail"]:
                    result["thumbnail"] = urllib.parse.urljoin(page_url, content)

        # Fallback Titre via balise <title>
        if not result["title"] and soup.title and soup.title.string:
            result["title"] = soup.title.string.strip()

        # Fallback Miniature via poster de la vidéo
        if not result["thumbnail"]:
            for video in soup.find_all("video"):
                poster = video.get("poster")
                if poster:
                    result["thumbnail"] = urllib.parse.urljoin(page_url, poster)
                    break

        # 2. Détection des flux vidéo réels (og:video, <video>, <source>, <iframe>, regex)
        candidates = []

        # Balises Open Graph vidéo
        for meta in soup.find_all("meta"):
            prop = meta.get("property", "").lower()
            name = meta.get("name", "").lower()
            content = meta.get("content", "").strip()
            if not content:
                continue
            if prop in ("og:video", "og:video:url", "og:video:secure_url") or name in ("twitter:player:stream"):
                full_cand = urllib.parse.urljoin(page_url, content)
                if not is_image_url(full_cand):
                    candidates.append(full_cand)

        # Balises <video> directes
        for video in soup.find_all("video"):
            src = video.get("src")
            if src:
                full_cand = urllib.parse.urljoin(page_url, src)
                if not is_image_url(full_cand):
                    candidates.append(full_cand)

            # Balises <source> enfants
            for source in video.find_all("source"):
                s_src = source.get("src")
                if s_src:
                    full_cand = urllib.parse.urljoin(page_url, s_src)
                    if not is_image_url(full_cand):
                        candidates.append(full_cand)

        # Balises <iframe> (lecteurs intégrés)
        for iframe in soup.find_all("iframe"):
            i_src = iframe.get("src")
            if i_src:
                full_src = urllib.parse.urljoin(page_url, i_src)
                if not is_image_url(full_src) and any(k in full_src.lower() for k in ("embed", "player", "video", "watch")):
                    candidates.append(full_src)

        # Détection Regex dans le script JavaScript (.m3u8, .mpd, .mp4)
        matches = re.findall(r'(https?://[^"\'\s<>]+\.(?:m3u8|mpd|mp4)[^"\'\s<>]*)', response.text)
        for match in matches:
            clean_match = match.replace(r"\/", "/")
            if not is_image_url(clean_match):
                candidates.append(clean_match)

        # Déduplication ordonnée
        seen = set()
        for cand in candidates:
            if cand not in seen and cand.startswith("http"):
                seen.add(cand)
                result["candidates"].append(cand)

    except Exception as exc:
        print(f"[Ultimate Fallback] Erreur de lecture sur {page_url} : {exc}")

    return result


def perform_analysis(url: str) -> dict:
    """
    Analyse rapide sans téléchargement :
    Tente yt-dlp, puis le scraper BeautifulSoup en fallback.
    """
    clean_url = url.strip()
    target_url = clean_url
    info = None
    scraped_data = None

    opts = get_base_ytdlp_opts(target_url)

    # Flux média direct (.mp4, .m3u8...)
    if is_direct_media_url(clean_url):
        clean_path = urllib.parse.urlparse(clean_url).path
        filename = Path(clean_path).name or "video_directe.mp4"
        clean_title = urllib.parse.unquote(filename)

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(clean_url, download=False)
        except Exception:
            info = None

        if not info:
            return {
                "title": clean_title,
                "thumbnail": None,
                "duration": None,
                "uploader": "Flux Direct",
                "webpage_url": clean_url,
                "resolutions": [{"value": "best", "label": "⭐ Flux Direct Original"}],
                "has_ffmpeg": check_ffmpeg_installed(),
            }

    # 1. Tentative avec yt-dlp directement
    if not info:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(clean_url, download=False)
        except Exception as initial_err:
            print(f"[Analyse] yt-dlp direct a échoué ({initial_err}). Activation du Fallback...")

            # 2. Scraper BeautifulSoup
            scraped_data = scrape_page_metadata_and_streams(clean_url)
            candidates = scraped_data.get("candidates", [])

            for cand in candidates:
                try:
                    cand_opts = get_base_ytdlp_opts(cand)
                    cand_opts["http_headers"]["Referer"] = clean_url
                    with yt_dlp.YoutubeDL(cand_opts) as ydl:
                        cand_info = ydl.extract_info(cand, download=False)
                        if cand_info:
                            info = cand_info
                            target_url = cand
                            print(f"[Analyse] Flux validé avec succès : {cand}")
                            break
                except Exception:
                    continue

            # Si yt-dlp n'a pas pu extraire de format mais qu'on a un lien .m3u8/.mp4
            if not info and candidates:
                target_url = candidates[0]
                return {
                    "title": scraped_data.get("title") or "Vidéo Web détectée",
                    "thumbnail": scraped_data.get("thumbnail"),
                    "duration": None,
                    "uploader": urllib.parse.urlparse(clean_url).netloc,
                    "webpage_url": target_url,
                    "resolutions": [{"value": "best", "label": "⭐ Meilleure qualité disponible"}],
                    "has_ffmpeg": check_ffmpeg_installed(),
                }

    if not info:
        raise ValueError("Aucune vidéo détectée sur cette page publique.")

    final_title = info.get("title")
    final_thumb = info.get("thumbnail")
    if scraped_data:
        if not final_title or final_title.lower() in ("manifest", "index", "master", "playlist", "video"):
            if scraped_data.get("title"):
                final_title = scraped_data["title"]
        if not final_thumb and scraped_data.get("thumbnail"):
            final_thumb = scraped_data["thumbnail"]

    # Organiser les résolutions disponibles
    raw_formats = info.get("formats", [])
    detected_heights = set()
    for f in raw_formats:
        h = f.get("height")
        vcodec = f.get("vcodec", "none")
        if h and isinstance(h, int) and vcodec != "none":
            detected_heights.add(h)

    known_presets = [
        {"height": 2160, "label": "4K Ultra HD (2160p)"},
        {"height": 1440, "label": "2K Quad HD (1440p)"},
        {"height": 1080, "label": "Full HD (1080p)"},
        {"height": 720, "label": "HD (720p)"},
        {"height": 480, "label": "SD (480p)"},
        {"height": 360, "label": "Basse (360p)"},
    ]

    resolutions = []
    if detected_heights:
        for preset in known_presets:
            if any(h >= preset["height"] - 30 and h <= preset["height"] + 30 for h in detected_heights):
                resolutions.append({"value": str(preset["height"]), "label": preset["label"]})

        if not resolutions:
            for h in sorted(list(detected_heights), reverse=True):
                resolutions.append({"value": str(h), "label": f"{h}p"})

    resolutions.insert(0, {"value": "best", "label": "⭐ Meilleure qualité disponible"})

    return {
        "title": final_title or "Vidéo détectée",
        "thumbnail": final_thumb,
        "duration": info.get("duration"),
        "uploader": info.get("uploader") or info.get("channel") or info.get("extractor", "Web"),
        "webpage_url": target_url,
        "resolutions": resolutions,
        "has_ffmpeg": check_ffmpeg_installed(),
    }


# --- 5. TÉLÉCHARGEMENT AVEC PROGRESSION EN TEMPS RÉEL (SSE) ---

def make_ytdlp_progress_hook(task_id: str):
    """Hook de progression transmettant l'état pour les Server-Sent Events (SSE)."""
    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            percent = (downloaded / total * 100) if total > 0 else 0

            speed_bytes = d.get("speed") or 0
            eta_sec = d.get("eta") or 0

            speed_str = f"{speed_bytes / (1024 * 1024):.1f} Mo/s" if speed_bytes else "-- Mo/s"
            eta_str = f"{int(eta_sec)}s" if eta_sec else "--"

            PROGRESS_DATA[task_id] = {
                "status": "downloading",
                "percent": round(min(percent, 95.0), 1),
                "speed": speed_str,
                "eta": eta_str,
                "phase": "Téléchargement des segments...",
            }
        elif d.get("status") == "finished":
            PROGRESS_DATA[task_id] = {
                "status": "processing",
                "percent": 96.0,
                "speed": "",
                "eta": "",
                "phase": "Assemblage et conversion (FFmpeg)...",
            }
    return hook


def execute_download_worker(task_id: str, req: DownloadRequest):
    """
    Processus de téléchargement exécuté en tâche de fond.
    Gère yt-dlp + FFmpeg, le fallback BeautifulSoup et la sécurité anti faux-fichiers.
    """
    task_dir = TEMP_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    has_ffmpeg = check_ffmpeg_installed()
    output_template = str(task_dir / "%(title).100s.%(ext)s")

    opts = get_base_ytdlp_opts(req.url)
    opts.update({
        "outtmpl": output_template,
        "noplaylist": True,
        "progress_hooks": [make_ytdlp_progress_hook(task_id)],
    })

    media_type = req.media_type.lower()
    fmt = req.format.lower()
    quality = req.quality.lower()

    if media_type == "audio":
        if not has_ffmpeg and fmt in ["mp3", "wav"]:
            PROGRESS_DATA[task_id] = {"status": "error", "error": "FFmpeg est requis pour la conversion MP3/WAV."}
            cleanup_directory(task_dir)
            return

        audio_quality = quality if quality in ["320", "256", "192", "128"] else "192"
        opts.update({
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": fmt if fmt in ["mp3", "m4a", "wav"] else "mp3",
                    "preferredquality": audio_quality,
                }
            ],
        })
    else:  # Vidéo
        out_ext = "webm" if fmt == "webm" else "mp4"

        if has_ffmpeg:
            if quality != "best" and quality.isdigit():
                h = int(quality)
                if out_ext == "mp4":
                    format_str = (
                        f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
                        f"bestvideo[height<={h}]+bestaudio/"
                        f"best[height<={h}][ext=mp4]/best[height<={h}]/best"
                    )
                else:
                    format_str = (
                        f"bestvideo[height<={h}][ext=webm]+bestaudio[ext=webm]/"
                        f"bestvideo[height<={h}]+bestaudio/"
                        f"best[height<={h}]/best"
                    )
            else:
                if out_ext == "mp4":
                    format_str = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best"
                else:
                    format_str = "bestvideo+bestaudio/best"

            opts.update({
                "format": format_str,
                "merge_output_format": out_ext,
            })
        else:
            opts.update({"format": f"best[ext={out_ext}]/best"})

    # Lancement de yt-dlp (+ Fallback si nécessaire)
    info = None
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(req.url, download=True)
    except Exception as dl_err:
        print(f"[Téléchargement] Échec initial sur {req.url} : {dl_err}. Recherche de flux caché...")
        scraped_data = scrape_page_metadata_and_streams(req.url)
        candidates = scraped_data.get("candidates", [])
        download_success = False

        for cand in candidates:
            try:
                cand_opts = dict(opts)
                cand_opts["http_headers"]["Referer"] = req.url
                with yt_dlp.YoutubeDL(cand_opts) as cand_ydl:
                    info = cand_ydl.extract_info(cand, download=True)
                    download_success = True
                    print(f"[Téléchargement] Succès yt-dlp avec flux candidat : {cand}")
                    break
            except Exception:
                continue

        if not download_success:
            cleanup_directory(task_dir)
            PROGRESS_DATA[task_id] = {
                "status": "error",
                "error": "Échec : La vidéo est protégée contre le téléchargement direct ou le lien est invalide.",
            }
            return

    # Sécurité et sélection du vrai fichier multimédia
    media_files = [f for f in task_dir.glob("*") if f.is_file() and f.suffix.lower() in MEDIA_EXTENSIONS]
    if not media_files:
        media_files = [
            f for f in task_dir.glob("*")
            if f.is_file() and f.suffix.lower() not in IMAGE_EXTENSIONS and not f.name.endswith((".json", ".part", ".ytdl", ".temp", ".txt"))
        ]

    if not media_files:
        cleanup_directory(task_dir)
        PROGRESS_DATA[task_id] = {
            "status": "error",
            "error": "Échec : La vidéo est protégée contre le téléchargement direct ou le lien est invalide.",
        }
        return

    target_file = max(media_files, key=lambda f: f.stat().st_size)
    filesize = target_file.stat().st_size

    # RÈGLE OBLIGATOIRE ANTI FAUX-FICHIERS (200 Ko / 200 000 octets minimum)
    MIN_VALID_SIZE = 200 * 1024
    if filesize < MIN_VALID_SIZE:
        cleanup_directory(task_dir)
        PROGRESS_DATA[task_id] = {
            "status": "error",
            "error": "Échec : La vidéo est protégée contre le téléchargement direct ou le lien est invalide.",
        }
        return

    # Succès total : mise à jour de l'état SSE pour déclencher le téléchargement client
    PROGRESS_DATA[task_id] = {
        "status": "completed",
        "percent": 100.0,
        "speed": "",
        "eta": "",
        "phase": "Fichier prêt pour le téléchargement !",
        "title": info.get("title") if info else target_file.stem,
        "filename": target_file.name,
        "filesize": filesize,
        "download_url": f"/api/file/{task_id}",
    }


def cleanup_directory(path: Path):
    """Supprime un dossier temporaire après transmission du fichier."""
    time.sleep(1)
    if path.exists() and path.is_dir():
        try:
            shutil.rmtree(path, ignore_errors=True)
            print(f"[Nettoyage] Dossier purgé : {path.name}")
        except Exception as exc:
            print(f"[Nettoyage] Erreur sur {path} : {exc}")


def purge_old_temp_files(max_age_seconds: int = 1800):
    """Nettoie les téléchargements oubliés de plus de 30 min."""
    now = time.time()
    for item in TEMP_DIR.iterdir():
        try:
            if item.is_dir() and (now - item.stat().st_mtime > max_age_seconds):
                shutil.rmtree(item, ignore_errors=True)
        except Exception:
            pass


# --- 6. ROUTES API ---

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """Sert l'interface Sahir Download."""
    index_file = TEMPLATES_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="Interface introuvable.")
    return HTMLResponse(content=index_file.read_text(encoding="utf-8"))


@app.get("/api/status")
async def get_system_status():
    """Vérifie l'état de l'API et de FFmpeg."""
    return {
        "status": "online",
        "ffmpeg_available": check_ffmpeg_installed(),
        "app_name": "sahir-dowload",
    }


@app.post("/api/analyze")
async def analyze_url(req: AnalyzeRequest, background_tasks: BackgroundTasks):
    """Étape 1 : Analyse rapide et extraction des métadonnées."""
    url_str = req.url.strip()
    if not url_str.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400,
            detail="URL invalide. L'adresse doit commencer par http:// ou https://",
        )

    background_tasks.add_task(purge_old_temp_files)

    try:
        metadata = await asyncio.to_thread(perform_analysis, url_str)
        return {"success": True, "data": metadata}
    except ValueError as ve:
        raise HTTPException(status_code=404, detail=str(ve))
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Erreur lors de l'analyse : {str(exc)}"
        )


@app.post("/api/download")
async def start_download(req: DownloadRequest, background_tasks: BackgroundTasks):
    """
    Étape 2 : Démarre le téléchargement asynchrone et renvoie l'ID de suivi SSE.
    """
    url_str = req.url.strip()
    if not url_str.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL invalide.")

    task_id = str(uuid.uuid4())
    PROGRESS_DATA[task_id] = {
        "status": "starting",
        "percent": 0.0,
        "speed": "-- Mo/s",
        "eta": "--",
        "phase": "Initialisation du moteur de téléchargement...",
    }

    # Lancement du worker dans un thread séparé
    asyncio.create_task(asyncio.to_thread(execute_download_worker, task_id, req))

    return {
        "success": True,
        "task_id": task_id,
        "progress_url": f"/api/progress/{task_id}",
    }


@app.get("/api/progress/{task_id}")
async def stream_progress(task_id: str):
    """
    Communication SSE (Server-Sent Events) pour diffuser la progression en direct.
    """
    async def event_generator():
        while True:
            if task_id not in PROGRESS_DATA:
                yield f"data: {json.dumps({'status': 'not_found'})}\n\n"
                break

            data = PROGRESS_DATA[task_id]
            yield f"data: {json.dumps(data)}\n\n"

            # Terminer le flux quand le téléchargement est achevé ou en erreur
            if data.get("status") in ("completed", "error"):
                break

            await asyncio.sleep(0.35)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/file/{task_id}")
async def get_downloaded_file(task_id: str, background_tasks: BackgroundTasks):
    """
    Envoie le fichier final au navigateur et supprime immédiatement le fichier du serveur.
    """
    task_dir = TEMP_DIR / task_id
    if not task_dir.exists() or not task_dir.is_dir():
        raise HTTPException(status_code=404, detail="Fichier expiré ou déjà téléchargé.")

    media_files = [f for f in task_dir.glob("*") if f.is_file() and f.suffix.lower() in MEDIA_EXTENSIONS]
    if not media_files:
        media_files = [
            f for f in task_dir.glob("*")
            if f.is_file() and f.suffix.lower() not in IMAGE_EXTENSIONS and not f.name.endswith((".json", ".part", ".ytdl", ".temp", ".txt"))
        ]

    if not media_files:
        raise HTTPException(status_code=404, detail="Aucun fichier vidéo ou audio disponible.")

    file_to_send = max(media_files, key=lambda f: f.stat().st_size)

    # Validation anti faux-fichiers
    MIN_VALID_SIZE = 200 * 1024
    if file_to_send.stat().st_size < MIN_VALID_SIZE:
        background_tasks.add_task(cleanup_directory, task_dir)
        raise HTTPException(
            status_code=400,
            detail="Échec : La vidéo est protégée contre le téléchargement direct ou le lien est invalide.",
        )

    background_tasks.add_task(cleanup_directory, task_dir)

    ext = file_to_send.suffix.lower()
    media_type = "video/mp4"
    if ext == ".webm":
        media_type = "video/webm"
    elif ext == ".mp3":
        media_type = "audio/mpeg"
    elif ext == ".m4a":
        media_type = "audio/mp4"
    elif ext == ".wav":
        media_type = "audio/wav"

    return FileResponse(
        path=str(file_to_send),
        filename=file_to_send.name,
        media_type=media_type,
    )


@app.get("/api/download-thumbnail")
async def download_thumbnail(url: str, title: Optional[str] = "miniature"):
    """Télécharge la miniature d'une vidéo et la renvoie en pièce jointe."""
    clean_url = url.strip()
    if not clean_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL de miniature invalide.")

    headers = {
        "User-Agent": CHROME_USER_AGENT,
        "Referer": get_dynamic_referer(clean_url),
    }

    try:
        resp = requests.get(clean_url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Impossible de télécharger la miniature : {str(exc)}")

    content_type = resp.headers.get("content-type", "image/jpeg").lower()
    ext = "jpg"
    if "png" in content_type:
        ext = "png"
    elif "webp" in content_type:
        ext = "webp"
    elif "gif" in content_type:
        ext = "gif"

    safe_title = re.sub(r'[^a-zA-Z0-9_\- ]+', '', title or "miniature").strip().replace(" ", "_") or "miniature"
    safe_filename = f"{safe_title[:50]}_thumbnail.{ext}"

    return Response(
        content=resp.content,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{safe_filename}"'},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
