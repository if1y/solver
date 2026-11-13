FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DISPLAY=:99

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fonts-liberation \
        gnupg \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcups2 \
        libdrm2 \
        libgbm1 \
        libgl1 \
        libglib2.0-0 \
        libnss3 \
        libpango-1.0-0 \
        libsm6 \
        libx11-6 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        libxrender1 \
        libxshmfence1 \
        tini \
        unzip \
        xauth \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

# Google Chrome (stable) from the official apt repository.
RUN curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
        | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && chmod a+r /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Best-effort: SeleniumBase can also fetch a matching driver at runtime.
RUN sbase install chromedriver latest || true

COPY solver ./solver

# The opt-in real-browser smoke test needs pytest inside the image:
#   docker compose run --rm solver pytest tests/test_solver_smoke.py -m smoke
RUN pip install --no-cache-dir pytest
COPY pyproject.toml .
COPY tests/__init__.py tests/conftest.py tests/test_solver_smoke.py ./tests/

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["xvfb-run", "-a", "python", "-m", "solver"]