FROM python:3.10-slim

# Install system dependencies
# libreoffice for docx->pdf, libgl1/libglib2.0 for opencv/rembg
# Fix for slim images: create missing man directory for Java installation
RUN mkdir -p /usr/share/man/man1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer \
    libreoffice-java-common \
    default-jre \
    libgl1 \
    libglib2.0-0 \
    tesseract-ocr \
    libtesseract-dev \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create directories
RUN mkdir -p logs output

# Expose port
ENV PORT=10000
EXPOSE 10000

# Run the bot
CMD ["python", "bot.py"]
