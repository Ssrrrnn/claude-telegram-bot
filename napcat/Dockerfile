FROM mlikiowa/napcat-docker:latest

# The upstream entrypoint copies the release into /app/napcat on first boot.
RUN mkdir -p /app/napcat/config

COPY napcat/entrypoint-railway.sh /app/entrypoint-railway.sh

ENTRYPOINT ["/bin/bash", "/app/entrypoint-railway.sh"]
