FROM python:3.12-slim
WORKDIR /srv/market
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
ARG WITH_WHISPER=0
RUN if [ "$WITH_WHISPER" = "1" ]; then pip install --no-cache-dir faster-whisper; fi
COPY app app
COPY config config
RUN useradd -m market && mkdir data && chown market:market data && chmod 700 data
USER market
CMD ["python","-m","app.main"]

