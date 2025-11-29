FROM python:3.11-slim

# ================================
# 📦 System setup
# ================================
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TOKENIZERS_PARALLELISM=false

WORKDIR /app

# Install system dependencies (if later you need build tools, add here)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
 && rm -rf /var/lib/apt/lists/*


# ================================
# 📚 Python dependencies
# ================================
# Copy only requirements first for better Docker layer caching
# NOTE: build context는 프로젝트 루트여야 합니다:
#   docker build -f deploy/Dockerfile -t trading-advisor .
# 여기서는 deploy/requirements.txt를 사용합니다.
COPY deploy/requirements.txt ./requirements.txt

RUN pip install --upgrade pip && \
    pip install -r requirements.txt


# ================================
# 📁 Application code
# ================================
# 필요한 폴더만 슬림하게 복사
# - deploy/: 앱 코드 (Streamlit, advisor, pipeline, config 등)
# - docs/: RAG용 문서
COPY deploy/ ./deploy
COPY docs/ ./docs

# Streamlit will listen on port 8501
EXPOSE 8501


# ================================
# ▶️ Run Streamlit app
# ================================
# Switch into the deploy directory inside the container, then run Streamlit.
WORKDIR /app/deploy

# Expect OPENAI_API_KEY to be provided at runtime (e.g. --env-file deploy/.env)
CMD ["streamlit", "run", "app_streamlit.py", "--server.port=8501", "--server.address=0.0.0.0"]
