from flask import Flask, request, render_template, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_limiter.errors import RateLimitExceeded
from werkzeug.utils import secure_filename
from functools import wraps
from PIL import Image
import torch
from transformers import SiglipImageProcessor, SiglipForImageClassification
from production_features import create_detection_record, register_production_routes
import numpy as np
import os
import time
import hashlib
import logging
import secrets

# ===============================
# App initialization
# ===============================
app = Flask(__name__)

app.config.update(
    SECRET_KEY=secrets.token_hex(32),
    MAX_CONTENT_LENGTH=30 * 1024 * 1024,  # 30 MB
    UPLOAD_FOLDER="uploads"
)

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["100 per hour", "20 per minute"]
)
limiter.init_app(app)

# ===============================
# Logging
# ===============================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("security.log"), logging.StreamHandler()]
)

# ===============================
# Load Model from Hugging Face
# ===============================
MODEL_NAME = "waleeyd/deepfake-image"
MODEL_DISPLAY_NAME = "AttEq + SigLIP"

device = "cuda" if torch.cuda.is_available() else "cpu"

try:
    logging.info(f"Loading model: {MODEL_NAME}...")
    processor = SiglipImageProcessor.from_pretrained(MODEL_NAME)
    model = SiglipForImageClassification.from_pretrained(
        MODEL_NAME,
        torch_dtype=torch.float32,
        trust_remote_code=True
    ).to(device)
    model.eval()
    
    # Optimize for multi-core CPU inference (8 vCPUs)
    torch.set_num_threads(8) 
    logging.info(f"Torch threads set to 8 to match vCPUs")
    logging.info(f"Model loaded successfully on {device}")
except Exception as e:
    logging.error(f"Model load failed: {e}")
    model = None
    processor = None

# ===============================
# Constants
# ===============================
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"}
MAX_FILE_SIZE = 30 * 1024 * 1024

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

# ===============================
# Helpers
# ===============================
def security_validate(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        logging.info(f"{request.remote_addr} → {request.endpoint}")
        return f(*args, **kwargs)
    return wrapper

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def sanitize_filename(filename):
    name, ext = os.path.splitext(secure_filename(filename))
    return f"{name}_{int(time.time())}{ext}"

def validate_file_security(file):
    if not file or file.filename == "":
        return ["No file provided"]

    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)

    errors = []

    if size > MAX_FILE_SIZE:
        errors.append("File exceeds 30MB limit")

    if not allowed_file(file.filename):
        errors.append("Invalid file extension")

    try:
        img = Image.open(file)
        img.verify()
    except Exception:
        errors.append("Invalid or corrupted image file")
    finally:
        file.seek(0)

    return errors

def generate_file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            h.update(chunk)
    return h.hexdigest()

def predict_image(path):
    """
    Predict using the SigLIP model.
    """
    if not model or not processor:
        raise Exception("Model not loaded")

    img = Image.open(path).convert("RGB")
    
    inputs = processor(images=img, return_tensors="pt").to(device)
    with torch.no_grad():
        outputs = model(**inputs)
    logits = outputs.logits
    idx = logits.argmax(-1).item()
    id2label = {int(k): v for k, v in model.config.id2label.items()}
    label_raw = id2label[idx]
    probs = torch.softmax(logits, dim=-1)
    conf = float(probs[0, idx])
    
    # Map labels (AI -> fake, hum -> real)
    final_label = "fake" if label_raw.lower() == "ai" else "real"
    
    logging.info(f"Prediction: {final_label} ({conf:.4f})")
    
    return final_label, round(conf * 100, 2), MODEL_DISPLAY_NAME

# ===============================
# Routes
# ===============================
@app.route("/", methods=["GET"])
@limiter.limit("30 per minute")
@security_validate
def index():
    return render_template("index.html")

@app.route("/predict", methods=["POST"])
@limiter.limit("60 per minute")
@security_validate
def predict():
    if "image" not in request.files:
        return jsonify({"error": "No image uploaded"}), 400

    file = request.files["image"]
    original_filename = secure_filename(file.filename)
    file_type = file.content_type
    errors = validate_file_security(file)

    if errors:
        return jsonify({"error": "; ".join(errors)}), 400

    file.seek(0, os.SEEK_END)
    uploaded_file_size = file.tell()
    file.seek(0)
    filename = sanitize_filename(file.filename)
    path = os.path.join(app.config["UPLOAD_FOLDER"], filename)

    try:
        img = Image.open(file).convert("RGB")
        img.save(path, "JPEG", quality=95)

        image_width, image_height = img.size
        label, confidence, model_used = predict_image(path)
        file_hash = generate_file_hash(path)
        record_id = None

        try:
            record_id = create_detection_record(
                result=label,
                confidence=confidence,
                model_used=model_used,
                original_filename=original_filename,
                file_type=file_type,
                file_size=uploaded_file_size,
                file_hash=file_hash,
                image_width=image_width,
                image_height=image_height,
                extra_metadata={"model_repository": MODEL_NAME, "inference_device": device},
            )
        except Exception as e:
            logging.error(f"Could not store detection history: {e}")

        return jsonify({
            "label": label,
            "confidence": confidence,
            "hash": file_hash,
            "record_id": record_id,
            "model_used": model_used
        })

    except Exception as e:
        logging.error(f"Prediction failed: {e}")
        return jsonify({"error": "Prediction failed"}), 500

    finally:
        if os.path.exists(path):
            os.remove(path)

register_production_routes(app, os.path.join(app.static_folder, "Logo.jpg"))

# ===============================
# Security headers
# ===============================
@app.after_request
def security_headers(res):
    res.headers["X-Content-Type-Options"] = "nosniff"
    res.headers["X-Frame-Options"] = "DENY"
    res.headers["X-XSS-Protection"] = "1; mode=block"
    res.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    res.headers["Content-Security-Policy"] = (
        "default-src 'self' https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://fonts.googleapis.com; "
        "script-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "font-src 'self' https://cdnjs.cloudflare.com https://fonts.gstatic.com; "
        "frame-src https://www.youtube.com https://www.youtube-nocookie.com;"
    )
    return res

# ===============================
# JSON Error Handlers
# ===============================
@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({"error": "File too large. Maximum allowed size is 30 MB."}), 413

@app.errorhandler(RateLimitExceeded)
def rate_limit_handler(e):
    return jsonify({"error": "Too many requests. Please slow down."}), 429

@app.errorhandler(400)
def bad_request(error):
    return jsonify({"error": "Invalid request or image file."}), 400

@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "Internal server error during prediction."}), 500

# ===============================
# Run
# ===============================
if __name__ == "__main__":
    app.run(
        debug=False,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
    )
