FROM python:3.11-slim

WORKDIR /usr/src/app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# will remove that once published
RUN pip install restate_sdk-0.1.0-cp311-cp311-manylinux_2_34_x86_64.whl

EXPOSE 9000

ENV PYTHONPATH="/usr/src/app/src"
CMD ["hypercorn", "example:app", "--config", "hypercorn-config.toml"]
