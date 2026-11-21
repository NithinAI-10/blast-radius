# Blast Radius entry.
#
# Same base image the organizers labeled with, so running sqlglot's tests here
# behaves like their run. The build installs the labeled package set from PyPI,
# fetches sqlglot at the pinned commit from GitHub, checks it byte for byte
# against the manifest the map was built from, and precompiles it.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa

COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --disable-pip-version-check -r /tmp/requirements.lock

COPY selector/fetch_sqlglot.py /tmp/fetch_sqlglot.py
RUN python /tmp/fetch_sqlglot.py ${SQLGLOT_SHA}

COPY selector /app/selector
RUN cd /app/sqlglot && sha256sum --quiet -c /app/selector/base.sha256 \
 && python -m compileall -q -j 0 /app/sqlglot/sqlglot /app/sqlglot/tests /app/selector \
 && chmod -R a+rX /app

ENV PYTHONUNBUFFERED=1
CMD ["python", "/app/selector/selector.py"]
