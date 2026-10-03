FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    WRISTCALL_CONFIG=/config/wristcall.yaml

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install . \
 && useradd --uid 10001 --create-home wristcall \
 && mkdir -p /data /config \
 && chown wristcall /data

USER wristcall
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/v1/health')" || exit 1
ENTRYPOINT ["wristcall"]
CMD ["serve"]
