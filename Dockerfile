FROM python:3.12-slim

# Installation de FFmpeg et des utilitaires nécessaires
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copie et installation des dépendances
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copie du code source
COPY . .

# Création du dossier temporaire de téléchargement
RUN mkdir -p downloads && chmod 777 downloads

# Port d'écoute pour les hébergeurs cloud (Render, Railway, Koyeb, HuggingFace)
ENV PORT=8000
EXPOSE 8000

# Démarrage de l'application FastAPI
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
