FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Cloud Run sets $PORT; server.py reads it (defaults to 8080).
ENV PORT=8080
EXPOSE 8080

CMD ["python", "server.py"]
