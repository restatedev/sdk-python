FROM python:3.11-slim

WORKDIR /usr/src/app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 9000

ENV PYTHONPATH="/usr/src/app/src"
CMD ["hypercorn", "interpreter:app", "--config", "hypercorn-config.toml"]
