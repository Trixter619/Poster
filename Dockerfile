FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 VK_POSTER_DATA=/data VK_POSTER_BIND=0.0.0.0
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home vkposter && mkdir /data && chown vkposter:vkposter /data
COPY *.py ./
COPY templates/ templates/
COPY static/ static/
USER vkposter
VOLUME /data
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/api/health', timeout=3)"
CMD ["python", "app.py"]
