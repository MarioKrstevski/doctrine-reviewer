FROM python:3.12-slim

WORKDIR /app
COPY server/ /app/
COPY addon/ /app/addon-src/

# Package the STUDENT add-on at build time. build_student_package refuses a
# DEV build, so the served file can never carry the pipeline key.
RUN python3 -c "import server; server.build_student_package('/app/addon-src', '/app/doctrine_editor.ankiaddon')" \
    && rm -rf /app/addon-src

ENV HOST=0.0.0.0 \
    PORT=8080 \
    DB_PATH=/data/doctrine.db

EXPOSE 8080
CMD ["python3", "server.py"]
