FROM python:3.12-slim
RUN useradd --create-home --uid 1000 collector
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py /app/server.py
USER collector
ENV PORT=7860
EXPOSE 7860
CMD ["python", "server.py"]
