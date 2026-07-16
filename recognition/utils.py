"""
attendance_system/recognition/utils.py
Utility functions for recognition: math, image IO, path helpers.
"""

import os
import io
import pickle
import glob
import logging

import cv2
import numpy as np
import requests
from PIL import Image
from io import BytesIO

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

from config import Config

logger = logging.getLogger(__name__)

# Supported image extensions
PREFERRED_IMAGE_EXTS = [".jpg", ".jpeg", ".png", ".JPG", ".JPEG", ".PNG", ".heic", ".HEIC"]


# ============================================
# Math Utilities
# ============================================
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io
from PIL import Image
import cv2
import numpy as np

def download_drive_image(file_id, service_account_file):
    try:
        creds = Credentials.from_service_account_file(
            service_account_file,
            scopes=["https://www.googleapis.com/auth/drive.readonly"]
        )

        drive = build("drive", "v3", credentials=creds)

        request = drive.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)

        done = False
        while not done:
            _, done = downloader.next_chunk()

        fh.seek(0)

        pil_img = Image.open(fh).convert("RGB")
        return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)

    except Exception as e:
        print("Drive download error:", e)
        return None
    
def normalize_embedding(emb):
    """L2-normalize an embedding vector."""
    emb = np.asarray(emb, dtype=np.float32).flatten()
    norm = np.linalg.norm(emb)
    if norm < 1e-9:
        return emb
    return emb / norm


def cosine_similarity(a, b):
    """Cosine similarity between two embeddings."""
    a = normalize_embedding(a)
    b = normalize_embedding(b)
    return float(np.dot(a, b))


def bbox_iou(a, b):
    """Intersection over Union for two bboxes (x1,y1,x2,y2)."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)
    iw = max(0, inter_x2 - inter_x1)
    ih = max(0, inter_y2 - inter_y1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    a_area = max(1, (ax2 - ax1) * (ay2 - ay1))
    b_area = max(1, (bx2 - bx1) * (by2 - by1))
    return inter / (a_area + b_area - inter + 1e-6)


def bbox_center(bbox):
    """Center point of a bbox."""
    x1, y1, x2, y2 = bbox
    return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)


def point_inside_box(pt, box):
    """Check if a point (x,y) is inside a box (x1,y1,x2,y2)."""
    x, y = pt
    x1, y1, x2, y2 = box
    return (x1 <= x <= x2) and (y1 <= y <= y2)


def sharpness_score(img, bbox, resize_to=128):
    """Compute Laplacian-based sharpness score for a face crop."""
    x1, y1, x2, y2 = [int(v) for v in bbox]
    x1 = max(0, x1)
    y1 = max(0, y1)
    x2 = min(img.shape[1], x2)
    y2 = min(img.shape[0], y2)
    crop = img[y1:y2, x1:x2]
    if crop.size == 0:
        return 0.0
    try:
        h, w = crop.shape[:2]
        if max(h, w) > resize_to:
            scale = resize_to / max(h, w)
            crop = cv2.resize(crop, (int(w * scale), int(h * scale)))
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        val = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        return min(1.0, val / 200.0)
    except Exception:
        return 0.0


# ============================================
# Image IO Utilities
# ============================================

def safe_name(s):
    """Sanitize a string for use as a filename."""
    return "".join([c if c.isalnum() or c in ("-", "_") else "_" for c in str(s)])


def dataset_image_path(student_id, usn):
    """Get the image file path for a student in the dataset."""
    fn = f"{safe_name(usn)}.jpg"
    return os.path.join(Config.DATASET_IMG_DIR, fn)


def dataset_embedding_path(student_id):
    """Get the embedding file path for a student."""
    fn = f"{safe_name(str(student_id))}.npz"
    return os.path.join(Config.DATASET_EMB_DIR, fn)


def save_face_image(student_id, usn, pil_image):
    """Save a PIL face crop image to dataset/images/."""
    path = dataset_image_path(student_id, usn)
    if isinstance(pil_image, np.ndarray):
        # Convert BGR numpy to PIL
        pil_image = Image.fromarray(cv2.cvtColor(pil_image, cv2.COLOR_BGR2RGB))
    pil_image.save(path, "JPEG", quality=95)
    return path


def save_embedding_npz(student_id, emb_full):
    """Save embedding to dataset/embeddings/ as .npz."""
    path = dataset_embedding_path(student_id)
    np.savez_compressed(
        path,
        full=np.asarray(emb_full, dtype=np.float32),
    )
    return path


def load_embedding_npz(student_id):
    """Load embedding from dataset/embeddings/."""
    try:
        path = dataset_embedding_path(student_id)
        if os.path.isfile(path):
            data = np.load(path)
            return normalize_embedding(data["full"])
    except Exception as e:
        logger.debug(f"load_embedding_npz error for {student_id}: {e}")
    return None


def safe_load_image(raw_path):
    """Load an image from disk path, returns BGR numpy array or None."""
    if raw_path is None:
        return None

    path = str(raw_path).strip().strip("'\"")

    # Try multiple locations
    candidates = [path]
    if not os.path.isabs(path):
        candidates.append(os.path.join(Config.DATASET_IMG_DIR, path))
        candidates.append(os.path.join(Config.DATASET_DIR, path))

    # Try with different extensions
    all_candidates = []
    for c in candidates:
        root, ext = os.path.splitext(c)
        if ext:
            all_candidates.append(c)
            for e in PREFERRED_IMAGE_EXTS:
                if e.lower() != ext.lower():
                    all_candidates.append(root + e)
        else:
            for e in PREFERRED_IMAGE_EXTS:
                all_candidates.append(c + e)

    for p in all_candidates:
        try:
            if os.path.exists(p):
                data = np.fromfile(p, dtype=np.uint8)
                img = cv2.imdecode(data, cv2.IMREAD_COLOR)
                if img is not None:
                    return img
                img = cv2.imread(p, cv2.IMREAD_COLOR)
                return img
        except Exception:
            continue

    return None


import requests
import cv2
from PIL import Image
import numpy as np
import io
import logging

logger = logging.getLogger(__name__)

def download_image_from_url(url, timeout=15, max_retries=3):
    """
    Download image from URL → BGR numpy array.
    FIXED: Handles Google Drive direct links + common image hosts.
    """
    if not url or not url.startswith(('http://', 'https://')):
        logger.warning(f"Invalid URL: {url}")
        return None
    
    # Handle Google Drive direct links
    if 'drive.google.com' in url:
        file_id = None

        if "id=" in url:
            file_id = url.split("id=")[-1].split("&")[0]
        elif "/d/" in url:
            file_id = url.split("/d/")[1].split("/")[0]

        if not file_id:
            return None

        # 🔥 USE DRIVE API (LIKE JUPYTER)
        return download_drive_image(file_id, "credentials.json")
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'image/*,*/*;q=0.8',
        'Referer': 'https://drive.google.com/',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive'
    }
    
    for attempt in range(max_retries):
        try:
            logger.info(f"Downloading [{attempt+1}/{max_retries}]: {url[:100]}...")
            
            resp = requests.get(url, headers=headers, timeout=timeout, stream=True)
            resp.raise_for_status()
            
            # PIL Image → BGR
            pil_img = Image.open(io.BytesIO(resp.content)).convert('RGB')
            bgr_img = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
            
            logger.info(f"✅ Downloaded: {pil_img.size} → {bgr_img.shape}")
            return bgr_img
            
        except Exception as e:
            logger.error(f"Attempt {attempt+1} failed: {e}")
            if attempt == max_retries - 1:
                logger.error(f"❌ ALL RETRIES FAILED: {url}")
                return None
            import time
            time.sleep(1)
    
    return None

def decode_image_bytes(content):
    import numpy as np
    import cv2
    from PIL import Image
    from io import BytesIO

    try:
        # 🔥 Try OpenCV decode first
        img_array = np.frombuffer(content, np.uint8)
        img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)

        if img is not None:
            return img

        # 🔥 FALLBACK → PIL (handles HEIC/HEIF)
        try:
            pil_img = Image.open(BytesIO(content)).convert("RGB")
        except Exception:
            import pillow_heif
            heif_file = pillow_heif.read_heif(BytesIO(content))
            pil_img = Image.frombytes(
                heif_file.mode,
                heif_file.size,
                heif_file.data,
                "raw"
            )

        return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)

    except Exception as e:
        print("Decode error:", e)
        return None

def bgr_to_pil(bgr_img):
    """BGR → PIL RGB."""
    if bgr_img is None:
        return None
    return Image.fromarray(cv2.cvtColor(bgr_img, cv2.COLOR_BGR2RGB))

def pil_to_bgr(pil_img):
    """PIL RGB → BGR."""
    if pil_img is None:
        return None
    return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)


def serialize_embedding(emb):
    """Serialize embedding to bytes for DB storage."""
    return pickle.dumps(np.asarray(emb, dtype=np.float32))


def deserialize_embedding(blob):
    """Deserialize embedding from DB blob."""
    if blob is None:
        return None
    try:
        arr = pickle.loads(blob)
        return normalize_embedding(arr)
    except Exception:
        try:
            arr = np.frombuffer(blob, dtype=np.float32)
            return normalize_embedding(arr)
        except Exception:
            return None
   

# ============================================
# Grid Helpers (for focused recognition)
# ============================================

def compute_grid_cells(width, height, grid=None):
    """Compute grid cell bounding boxes for focus-based recognition."""
    if grid is None:
        grid = Config.FOCUS_GRID
    gx, gy = grid
    w = width // gx
    h = height // gy
    cells = []
    for iy in range(gy):
        for ix in range(gx):
            x1 = ix * w
            y1 = iy * h
            x2 = width if ix == gx - 1 else (ix + 1) * w
            y2 = height if iy == gy - 1 else (iy + 1) * h
            cells.append(((ix, iy), (x1, y1, x2, y2)))
    return cells