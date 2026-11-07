FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt && playwright install --with-deps chromium

COPY app.py ./
COPY jobfinder ./jobfinder
COPY assets ./assets
COPY .streamlit ./.streamlit
COPY static ./static

# offers, CVs and letters; mount a volume here to keep CVs and settings between restarts
VOLUME /app/data
EXPOSE 8501
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')"
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
