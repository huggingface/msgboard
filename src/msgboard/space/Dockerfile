# Built from what msgboard.space.deploy() uploads: this file, requirements.txt, LICENSE and the
# installed msgboard package (msgboard/), so the Space runs the deployer's msgboard revision.
FROM python:3.12-slim
RUN useradd -m -u 1000 user
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
USER user
ENV PYTHONUNBUFFERED=1 HF_HUB_DISABLE_PROGRESS_BARS=1 MSGBOARD_DATA_DIR=/home/user/board-data
EXPOSE 7860
# exec form: SIGTERM reaches Python, which pushes a final snapshot before exiting
CMD ["python", "-m", "msgboard.space.app"]
