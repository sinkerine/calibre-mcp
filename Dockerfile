# calibre-mcp. Build: docker build -t calibre-mcp .   (--build-arg WITH_CALIBRE=0 for a slim image
# without ebook-convert: books lacking an EPUB then get no epub_url)
FROM python:3.13-slim

ARG WITH_CALIBRE=1
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 QT_QPA_PLATFORM=offscreen \
    CALIBRE_MCP_CACHE_DIR=/cache

RUN if [ "$WITH_CALIBRE" = 1 ]; then \
      apt-get update && apt-get install -y --no-install-recommends calibre \
      && rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
COPY tests ./tests
RUN pip install . && python -m unittest discover -s tests && rm -rf /src

WORKDIR /cache
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"
ENTRYPOINT ["calibre-mcp"]
CMD ["serve"]
