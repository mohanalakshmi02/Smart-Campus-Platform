FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 DATABASE=/app/data/campus.db
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /app/data
EXPOSE 5000
CMD ["gunicorn", "-w", "1", "--threads", "4", "-b", "0.0.0.0:5000", "app:app"]
