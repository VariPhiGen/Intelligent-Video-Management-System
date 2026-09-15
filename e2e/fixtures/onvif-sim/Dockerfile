# stdlib only, deliberately: the simulator has no dependencies to drift, and a
# fixture that needs a package index is a fixture that fails on an air-gapped
# build host.
FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534
COPY sim.py /sim.py
EXPOSE 8080/tcp 3702/udp
ENTRYPOINT ["python3", "-u", "/sim.py"]
