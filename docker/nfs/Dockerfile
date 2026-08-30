FROM debian:13-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates dbus netbase nfs-ganesha nfs-ganesha-vfs rpcbind && \
    rm -rf /var/lib/apt/lists/*
COPY ganesha.conf.template /etc/ganesha/ganesha.conf.template
COPY entrypoint.sh /usr/local/bin/modelshelf-nfs-entrypoint
RUN chmod 0755 /usr/local/bin/modelshelf-nfs-entrypoint
EXPOSE 2049/tcp
ENTRYPOINT ["/usr/local/bin/modelshelf-nfs-entrypoint"]
