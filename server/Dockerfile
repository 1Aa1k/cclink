FROM python:3.12-alpine
WORKDIR /app
COPY app.py .
# Runs as nobody, not root: this box holds keys to Nate's other machines, and this service must not be a path to them.
USER 65534:65534
EXPOSE 8080
CMD ["python", "-u", "app.py"]
