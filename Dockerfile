FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt clouddrive.proto ./
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. clouddrive.proto

COPY *.py ./

CMD ["python", "-m", "main"]
