FROM python:3.12-slim
RUN useradd -m -u 1000 user
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[hf]"
COPY hfspace/app.py hfspace/demo_bots.py hfspace/accounts.py src/msgboard/dashboard.html ./
USER user
ENV PYTHONUNBUFFERED=1 HF_HUB_DISABLE_PROGRESS_BARS=1 MSGBOARD_DATA_DIR=/home/user/board-data
EXPOSE 7860
# exec form: SIGTERM reaches Python, which pushes a final snapshot before exiting
CMD ["python", "app.py"]
