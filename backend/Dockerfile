FROM python:3.11-slim

WORKDIR /app

# Install git (to clone submissions) and the docker CLI (the docker-py
# library talks to the daemon over the socket, but having the CLI too
# makes debugging inside the container easier).
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    nodejs \
    npm \
    php-cli \
    && curl -fsSL https://get.docker.com | sh \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
