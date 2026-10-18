FROM python:3.13-alpine

RUN adduser -D -H -u 10001 mcp

WORKDIR /app
# The OAuth packages are imported only with NEXTCLOUD_MCP_AUTH=oauth|both.
COPY requirements-oauth.txt /app/requirements-oauth.txt
RUN pip install --no-cache-dir -r /app/requirements-oauth.txt
COPY nextcloud_mcp.py nextcloud_mcp_oauth.py /app/
RUN chown -R mcp:mcp /app \
 && install -d -o mcp -g mcp -m 0700 /var/lib/nextcloud-mcp

USER mcp
EXPOSE 5811
ENV NEXTCLOUD_MCP_BIND=0.0.0.0 \
    NEXTCLOUD_MCP_PORT=5811 \
    NEXTCLOUD_MCP_TMP=/var/tmp/nextcloud-mcp \
    NEXTCLOUD_MCP_STATE_DIR=/var/lib/nextcloud-mcp \
    PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["python3", "/app/nextcloud_mcp.py"]
