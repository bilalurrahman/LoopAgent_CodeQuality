# Quality-ratchet loop image: .NET SDK (to build + count warnings) + python3
# (the loop agent) + git/gh (branch + PR). Runs as a non-root user.
FROM mcr.microsoft.com/dotnet/sdk:10.0

# git, python3, gh CLI, bash
RUN apt-get update \
 && apt-get install -y --no-install-recommends git python3 ca-certificates curl bash \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY agent/            /app/agent/
COPY seed/             /app/seed/
COPY skills/           /app/skills/
COPY ui/               /app/ui/
COPY entrypoint.sh     /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh /app/seed/.glm-loop/gate.sh

# Non-root user with a writable HOME and workspace.
RUN useradd -m -u 10001 loop \
 && mkdir -p /work && chown -R loop:loop /work /app
USER loop
ENV HOME=/home/loop \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_NOLOGO=1 \
    PYTHONIOENCODING=utf-8 \
    UI_PORT=8787
EXPOSE 8787
ENTRYPOINT ["/app/entrypoint.sh"]
