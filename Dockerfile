FROM python:3.12-slim

WORKDIR /app
COPY server/ /app/

ENV HOST=0.0.0.0 \
    PORT=8080 \
    DB_PATH=/data/doctrine.db

EXPOSE 8080
CMD ["python3", "server.py"]
