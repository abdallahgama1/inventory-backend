# Use official Python image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Create upload directory
RUN mkdir -p uploaded_inventory

# Expose the port Flask runs on
EXPOSE 5000

# Run the app
CMD ["python", "app.py"]
