FROM mlikiowa/napcat-docker:latest

# The upstream entrypoint copies the release into /app/napcat on first boot.
# Its current image does not create that destination directory.
RUN mkdir -p /app/napcat/config

ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]
