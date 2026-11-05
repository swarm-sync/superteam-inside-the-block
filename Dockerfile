# Inside the Block -- reproducible measurement container.
# Build:  docker build -t inside-the-block .
# Run:    docker compose up            (dry-run by default; sends nothing)
# Live:   ITB_ALLOW_MAINNET_LIVE=1 docker compose -f docker-compose.live.yml up
#         (live compose file is NOT in this repo: create it yourself from
#         docker-compose.yml, adding --live and a keypair volume.)
FROM python:3.12-slim

WORKDIR /app

# No build tools needed: solders ships wheels.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY README.md REPORT.md ./

# Data outlives the container via this volume.
VOLUME ["/app/data"]

# Default: mainnet DRY-RUN (builds txs, simulates, sends nothing).
ENTRYPOINT ["python", "-m", "src.run"]
CMD ["measure", "--network", "mainnet", "--out", "data/mainnet/dry-run"]
