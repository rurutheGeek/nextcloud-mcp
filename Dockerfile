FROM python:3.13-alpine

RUN adduser -D -H -u 10001 mcp

WORKDIR /app
COPY nextcloud_mcp.py /app/nextcloud_mcp.py
RUN chown -R mcp:mcp /app

USER mcp
EXPOSE 5811
ENV NEXTCLOUD_MCP_BIND=0.0.0.0 \
    NEXTCLOUD_MCP_PORT=5811 \
    NEXTCLOUD_MCP_TMP=/var/tmp/nextcloud-mcp \
    PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["python3", "/app/nextcloud_mcp.py"]
