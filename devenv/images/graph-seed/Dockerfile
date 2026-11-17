# The loader uses stdlib Python to stream COPY data through PostgreSQL's psql.
# Package both at build time so seeding never installs dependencies.
FROM postgres:18

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*
