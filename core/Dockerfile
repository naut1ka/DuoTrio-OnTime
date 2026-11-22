FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && mkdir /data && chown 65534:65534 /data
COPY *.py openapi.json ./
USER 65534:65534
CMD ["gunicorn","--config","gunicorn_config.py","--bind","0.0.0.0:8000","--workers","1","--threads","3","app:app"]

