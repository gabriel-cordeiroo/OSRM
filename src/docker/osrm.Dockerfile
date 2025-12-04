FROM ubuntu:latest

ENV DEBIAN_FRONTEND=noninteractive
ENV CXX=clang++
ENV CC=clang

ADD ./osrm-brazil-files /

RUN apt-get update \
    && apt-get install -y build-essential cmakeclang git g++ pkg-config libboost-all-dev libprotobuf-dev \
        protobuf-compiler liblua5.3-dev libstxxl-dev libosmpbf-dev libtbb-dev libxml2-dev zlib1g-dev libbz2-dev \
        libz-dev libpng-dev libosmpbf-dev libsqlite3-dev libgdal-de python3-pip python3-venv wget

RUN git clone https://github.com/Project-OSRM/osrm-backend.git \
    && cd osrm-backend
    && mkdir build \
    && cd build \
    && cmake .. \
    && cmake --build . \
    && cmake --build . --target install \
    && cd ../..

RUN python3 -m venv .venv \
    && source .venv/bin/activate \
    && pip3 install awscli \
    && aws s3 cp s3://20-ze-datalake-landing/territory_osrm/osrm-brazil-files/ . --recursive

EXPOSE 5000

ENTRYPOINT ["osrm-routed", "--algorithm", "mld", "brazil-latest.osrm"]