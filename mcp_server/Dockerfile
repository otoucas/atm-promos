FROM python:3.12-slim

WORKDIR /app

COPY mcp_server/requirements.txt ./mcp_server/requirements.txt
RUN pip install --no-cache-dir -r mcp_server/requirements.txt

# Réutilise directement les modèles/la config de l'appli principale (app/) —
# ni fastapi ni pyzbar/pillow n'y sont requis pour models.py/database.py/
# config.py, donc aucun conflit avec les dépendances propres à mcp_server.
COPY app ./app
COPY mcp_server ./mcp_server

VOLUME ["/app/data"]
EXPOSE 8000

CMD ["python", "-m", "mcp_server.server"]
