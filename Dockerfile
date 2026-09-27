FROM python:3.12-slim
WORKDIR /srv/opsagent
COPY pyproject.toml uv.lock ./
COPY app ./app
COPY scripts ./scripts
RUN pip install --no-cache-dir uv==0.8.22 && uv sync --frozen --no-dev
ENV PATH="/srv/opsagent/.venv/bin:$PATH" PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 10001 opsagent
USER opsagent
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
