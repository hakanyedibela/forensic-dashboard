FROM python:3.12-alpine
WORKDIR /app
COPY app.py /app/app.py
EXPOSE 8080
USER 65534:65534
ENTRYPOINT ["python", "-u", "/app/app.py"]
