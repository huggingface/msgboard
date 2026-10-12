FROM python:3.12-slim
RUN useradd -m -u 1000 user
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . fastapi==0.142.4 uvicorn==0.54.0 httpx==0.28.1 aiohttp==3.13.2
COPY space_app.py loadtest.py aio_loadtest.py attachments.py unicode_boundary.py ./
USER user
ENV PYTHONUNBUFFERED=1 FLOCK_DATA_DIR=/home/user/board-data
EXPOSE 7860
CMD ["python", "space_app.py"]
