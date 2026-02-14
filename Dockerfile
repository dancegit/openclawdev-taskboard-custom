FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY app.py .
COPY static/ static/

# Data volume for SQLite
VOLUME /app/data

# Run
EXPOSE 8080
# DEV MODE: --reload for hot reloading (remove for production)
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--reload"]
