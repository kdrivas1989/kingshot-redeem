FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data

EXPOSE 5015

CMD ["gunicorn", "--bind", "0.0.0.0:5015", "--workers", "1", "--threads", "4", "app:app"]
