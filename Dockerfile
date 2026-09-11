# syntax=docker/dockerfile:1
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN dpkg --add-architecture i386 && apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg xvfb xauth tini fonts-liberation cabextract \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://dl.winehq.org/wine-builds/winehq.key -o /etc/apt/keyrings/winehq-archive.key \
    && curl -fsSL https://dl.winehq.org/wine-builds/ubuntu/dists/noble/winehq-noble.sources -o /etc/apt/sources.list.d/winehq.sources \
    && apt-get update && apt-get install -y --install-recommends winehq-stable \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 trader && mkdir -p /app /state && chown trader:trader /app /state
USER trader
ENV WINEPREFIX=/home/trader/.wine WINEARCH=win64 WINEDEBUG=-all \
    WINEDLLOVERRIDES=mscoree,mshtml= PYTHONUNBUFFERED=1
WORKDIR /app
ARG PYTHON_VERSION=3.12.10
RUN curl -fsSL "https://www.python.org/ftp/python/${PYTHON_VERSION}/python-${PYTHON_VERSION}-amd64.exe" -o /tmp/python.exe \
    && xvfb-run -a sh -c 'wineboot --init && wineserver -w && wine /tmp/python.exe /quiet InstallAllUsers=0 TargetDir=C:\\Python312 Include_pip=1 Include_test=0 Include_launcher=0 && wineserver -w' \
    && rm /tmp/python.exe
COPY --chown=trader:trader pyproject.toml ./
COPY --chown=trader:trader signal_capture ./signal_capture
RUN xvfb-run -a wine 'C:\Python312\python.exe' -m pip install --no-cache-dir .
# Supply a clean broker terminal installation; no installer/UI at container boot.
COPY --chown=trader:trader vendor/mt5/ /opt/mt5-template/
RUN test -f /opt/mt5-template/terminal64.exe
COPY --chown=trader:trader config.toml /app/config.toml
COPY --chown=trader:trader docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh
ENV CONFIG_PATH=Z:\\app\\config.toml LEDGER_PATH=Z:\\state\\ledger.db \
    MT5_PATH=C:\\mt5\\terminal64.exe MT5_PORTABLE=true
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s CMD curl -fsS http://localhost:8080/health/live || exit 1
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/app/entrypoint.sh"]
