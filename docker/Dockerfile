# Hex TCG private server — container image for AWS Fargate.
#
# Runs four processes on one task:
#   hconnect_server.py  -> TCP 9933  (HConnect game protocol)
#   proxy.py            -> TCP 8081  (Steam auth / collection HTTP / news)
#   gamemodes/tournament_server.py  (tournament pool/refill scheduler)
#   replay_server.py                 (replay artifact worker)
#
# Build from the repository root:
#   docker build -f docker/Dockerfile -t hex-server .
#
# The client (config.ini / UnityConfig.json) points GameServerIP at the
# Fargate task's public IP or a load-balancer hostname.
FROM python:3.11-slim

WORKDIR /hex

# The gamedata contains relaxed YAML-compatible conversation records.  Keep
# this runtime dependency explicit so clean containers seed the full campaign
# catalog rather than silently skipping those records.
COPY requirements.txt /hex/requirements.txt
RUN python3 -m pip install --no-cache-dir -r /hex/requirements.txt

# Server sources and data.  The build context is the repository root even
# though this Dockerfile lives under docker/.
COPY *.py /hex/
COPY AssetExtraction/ /hex/AssetExtraction/
COPY abilities/ /hex/abilities/
COPY application/ /hex/application/
COPY campaign_chains/ /hex/campaign_chains/
COPY domain/ /hex/domain/
COPY gamemodes/ /hex/gamemodes/
COPY services/ /hex/services/
COPY tests/ /hex/tests/
# The root wildcard above includes replay_server.py; this package includes the
# tournament scheduler used by the running services.
COPY docker/docker_bootstrap.py /hex/docker/docker_bootstrap.py
# Records and generated starter-deck data are materialized into the writable
# container filesystem at startup when a mounted HEX_GAMEDATA file is
# available. They are intentionally not copied into the image.
COPY news/ /hex/news/

# Fail the image build early if a copied server, extractor, or test module has
# a syntax error.
RUN python3 -m compileall -q /hex

# Ports: 9933 = HConnect game server, 8081 = HTTP proxy.
EXPOSE 9933 8081

# Supervisord-style entrypoint: launch all services. Each writes to a log
# that we tail on stdout for `docker logs`.
COPY docker/docker_entrypoint.sh /hex/docker_entrypoint.sh
RUN chmod +x /hex/docker_entrypoint.sh

ENTRYPOINT ["/hex/docker_entrypoint.sh"]
