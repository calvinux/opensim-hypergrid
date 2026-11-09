ARG DOTNET_VERSION=8.0

# ---------- 1) Compilation depuis les sources git ----------
FROM mcr.microsoft.com/dotnet/sdk:${DOTNET_VERSION} AS build
ARG OPENSIM_GIT=git://opensimulator.org/git/opensim
ARG OPENSIM_REF=master
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${OPENSIM_REF}" "${OPENSIM_GIT}" /src
WORKDIR /src
RUN chmod +x runprebuild.sh && ./runprebuild.sh \
    && dotnet build --configuration Release OpenSim.sln

# ---------- 2) Image d'exécution ----------
FROM mcr.microsoft.com/dotnet/runtime:${DOTNET_VERSION}
RUN apt-get update \
    && apt-get install -y --no-install-recommends gettext-base ca-certificates python3 \
    && (apt-get install -y --no-install-recommends libgdiplus || echo "libgdiplus indisponible : ignoré") \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/bin /opt/opensim/bin
COPY config/ /opt/config/
COPY webconsole/ /opt/webconsole/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
WORKDIR /opt/opensim/bin
VOLUME /data
ENTRYPOINT ["/entrypoint.sh"]
