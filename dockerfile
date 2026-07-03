FROM python:3.11-slim

# Set working directory
WORKDIR /app

ARG PROJECT_NAME=.
ARG PROJECT_PORT=8000
ARG ROOT_PATH=/procurement
ARG NEED_FFMPEG=false
ARG NEED_OCR=false
ARG NEED_AI_LIBS=false
ARG ENTRY_FILE=server.py

# ---------- Copy project folder ----------
COPY ${PROJECT_NAME} /app

# ---------- Install system dependencies ----------
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ unixodbc unixodbc-dev \
&& if [ "${NEED_FFMPEG}" = "true" ]; then apt-get install -y ffmpeg; fi \
&& if [ "${NEED_OCR}" = "true" ]; then apt-get install -y libgl1 libglib2.0-0 poppler-utils; fi \
&& rm -rf /var/lib/apt/lists/*

# ---------- Install Python dependencies ----------
RUN if [ -f requirements_final.txt ]; then pip install --no-cache-dir -r requirements_final.txt; \
    elif [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi

# ---------- Install optional AI libraries ----------
RUN if [ "${NEED_AI_LIBS}" = "true" ]; then \
        pip install --no-cache-dir \
            "sentence-transformers==2.2.2" \
            "transformers==4.30.2" \
            "huggingface-hub==0.16.4" \
            "accelerate==0.20.3" \
            easyocr docx2txt; \
    fi

# ---------- Expose project port ----------
EXPOSE ${PROJECT_PORT}

# ---------- Environment Variables ----------
ENV PROJECT_PORT=${PROJECT_PORT}
ENV ROOT_PATH=${ROOT_PATH}
ENV ENTRY_FILE=${ENTRY_FILE}

# ---------- Start FastAPI ----------
CMD ["sh", "-c", "uvicorn ${ENTRY_FILE%.*}:app --host 0.0.0.0 --port ${PROJECT_PORT} --root-path ${ROOT_PATH}"]