# Same base image the tests were labeled on.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
COPY select_tests.py main.py index.pkl.gz test_struct.json src.pkl.gz collect_map.pkl.gz tpcds_gate.json.gz tpcds_gate_child.py prep_index.py /app/
RUN python /app/prep_index.py && chmod 644 /app/*.py /app/*.pkl /app/*.gz /app/*.json && python -c "import compileall; compileall.compile_dir('/app', quiet=1)" && chmod -R a+rX /app
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-S", "/app/main.py"]
