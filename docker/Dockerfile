# Hex TCG private server — container image for AWS Fargate.
#
# Runs the game services and optional campaign editor on one task:
#   hconnect_server.py  -> TCP 9933  (HConnect game protocol)
#   proxy.py            -> TCP 8081  (Steam auth / collection HTTP / news)
#   campaign editor     -> HTTP 8765 (campaign editing UI)
#   gamemodes/tournament_server.py  (tournament pool/refill scheduler)
#   replay_server.py                 (replay artifact worker)
#
# Build from the repository root:
#   docker build -f docker/Dockerfile -t hex-server .
#
# The client (config.ini / UnityConfig.json) points GameServerIP at the
# Fargate task's public IP or a load-balancer hostname.
FROM python:3.12-slim

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
COPY campaign/ /hex/campaign/
# The campaign editor lives at the project root; it reads and writes the
# campaign above through campaign_editor/paths.py (HEX_CAMPAIGN_ROOT overrides).
COPY campaign_editor/ /hex/campaign_editor/
COPY domain/ /hex/domain/
COPY gamedata/ /hex/gamedata/
COPY gamemodes/ /hex/gamemodes/
COPY rules_port/ /hex/rules_port/
COPY services/ /hex/services/
COPY tests/ /hex/tests/
# The root wildcard above includes replay_server.py; this package includes the
# tournament scheduler used by the running services.
COPY docker/docker_bootstrap.py /hex/docker/docker_bootstrap.py
COPY docker/healthcheck.py /hex/docker/healthcheck.py
COPY docker/campaign_editor_launcher.sh /hex/docker/campaign_editor_launcher.sh
COPY supervisord.conf /hex/supervisord.conf
COPY run_hconnect.sh /hex/run_hconnect.sh
# hex-private/Records and generated starter-deck data are materialized into
# the writable container filesystem at startup when a mounted HEX_GAMEDATA
# file is available. They are intentionally not copied into the image.
COPY news/ /hex/news/
COPY LICENSE /hex/LICENSE
COPY VERSION /hex/VERSION

# Editor art is generated at startup from the client install. Mount the Hex
# install root (the folder that holds Hex_Data and AssetBundles) at /client-data.
# The container runs as UID 1001.
# The documented invocation runs the container as UID 1001. Bootstrap may
# materialize Records and starter decks, and the editor writes campaign data
# and rules, so these image-local directories must be writable by that user.
RUN mkdir -p /hex/hex-private/Records /hex/generated \
        /hex/campaign_editor/generated \
    && chown -R 1001:1001 /hex/hex-private/Records /hex/generated \
        /hex/campaign_editor/generated \
    && chown -R 1001:1001 /hex/campaign/campaign_data \
        /hex/campaign/campaign_rules

# Fail the image build early if a copied server, extractor, or test module has
# a syntax error.
RUN python3 -m compileall -q /hex

# Ports: 9933 = HConnect, 8081 = HTTP proxy, 8765 = campaign editor.
EXPOSE 9933 8081 8765

# Docker/ECS can replace the task when a public service stops accepting
# connections. Supervisor still handles ordinary process exits internally.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python3", "/hex/docker/healthcheck.py"]

# Supervisor entrypoint: keep all services under one process supervisor.
COPY docker/docker_entrypoint.sh /hex/docker_entrypoint.sh
RUN chmod +x /hex/docker_entrypoint.sh

ENTRYPOINT ["/hex/docker_entrypoint.sh"]
