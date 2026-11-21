FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl build-essential \
    tesseract-ocr \
    fonts-dejavu-core \
    libjpeg62-turbo \
    zlib1g \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY packages ./packages
COPY apps ./apps
COPY alembic ./alembic
COPY alembic.ini ./
COPY scripts ./scripts
COPY fixtures ./fixtures

RUN pip install --no-cache-dir -e .

# Generate clear / blurred / blacked-out KYC fixture images at build time
RUN python -m scripts.generate_fixtures

RUN mkdir -p /app/.data/docs

EXPOSE 8000 8001

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
