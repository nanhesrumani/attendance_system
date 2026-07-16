"""
attendance_system/recognition/liveness.py
Anti-spoofing using MiniFASNet ONNX models (Silent-Face-Anti-Spoofing).
Detects photo/screen attacks without requiring PyTorch.

Required models in anti_spoof_models/:
  - 2.7_80x80_MiniFASNetV2.onnx
  - 4_0_0_80x80_MiniFASNetV1SE.onnx

Download from:
  https://github.com/minivision-ai/Silent-Face-Anti-Spoofing
  Convert .pth to .onnx or use pre-converted ONNX files.
"""

import os
import logging
import math

import cv2
import numpy as np

from config import Config

logger = logging.getLogger(__name__)


class LivenessChecker:
    """
    Anti-spoofing liveness detector using MiniFASNet ONNX models.
    Runs inference on face crops to determine real vs spoof.
    """

    # Model configs: (model_filename, input_size, scale)
    MODEL_CONFIGS = [
        ("2.7_80x80_MiniFASNetV2.onnx", (80, 80), 2.7),
        ("4_0_0_80x80_MiniFASNetV1SE.onnx", (80, 80), 4.0),
    ]

    def __init__(self):
        self.sessions = []  # list of (ort.InferenceSession, input_size, scale)
        self.is_initialized = False

    def initialize(self):
        """Load ONNX anti-spoof models."""
        try:
            import onnxruntime as ort

            providers = ort.get_available_providers()
            # Prefer GPU
            if "CUDAExecutionProvider" in providers:
                exec_providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
            else:
                exec_providers = ["CPUExecutionProvider"]

            loaded = 0
            for model_file, input_size, scale in self.MODEL_CONFIGS:
                model_path = os.path.join(Config.ANTI_SPOOF_MODEL_DIR, model_file)
                if not os.path.isfile(model_path):
                    logger.warning(f"Anti-spoof model not found: {model_path}")
                    continue

                session = ort.InferenceSession(model_path, providers=exec_providers)
                self.sessions.append((session, input_size, scale))
                loaded += 1
                logger.info(f"Loaded anti-spoof model: {model_file}")

            if loaded > 0:
                self.is_initialized = True
                logger.info(f"LivenessChecker ready: {loaded} model(s) loaded.")
            else:
                logger.warning(
                    "No anti-spoof models loaded. Liveness checking disabled. "
                    "Place ONNX models in anti_spoof_models/ folder."
                )

            return self.is_initialized

        except ImportError:
            logger.error("onnxruntime not installed. Liveness checking disabled.")
            return False
        except Exception as e:
            logger.error(f"LivenessChecker init error: {e}")
            return False

    def _crop_face_with_scale(self, frame, bbox, scale, target_size):
        """
        Crop face region from frame with a specific scale factor.
        Enlarges the bbox by 'scale' factor, then resizes to target_size.
        """
        x1, y1, x2, y2 = bbox
        h, w = frame.shape[:2]
        face_w = x2 - x1
        face_h = y2 - y1

        # Compute center
        cx = (x1 + x2) / 2.0
        cy = (y1 + y2) / 2.0

        # New dimensions with scale
        new_size = max(face_w, face_h) * scale
        half = new_size / 2.0

        # Crop coordinates (clamped)
        crop_x1 = int(max(0, cx - half))
        crop_y1 = int(max(0, cy - half))
        crop_x2 = int(min(w, cx + half))
        crop_y2 = int(min(h, cy + half))

        crop = frame[crop_y1:crop_y2, crop_x1:crop_x2]
        if crop.size == 0:
            return None

        # Resize to model input size
        crop_resized = cv2.resize(crop, target_size, interpolation=cv2.INTER_LINEAR)
        return crop_resized

    def _preprocess(self, crop):
        """Preprocess crop for ONNX model input."""
        # Convert BGR to RGB
        img = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        # Normalize to [0, 1]
        img = img.astype(np.float32) / 255.0
        # Transpose to NCHW
        img = np.transpose(img, (2, 0, 1))
        # Add batch dimension
        img = np.expand_dims(img, axis=0)
        return img

    def check(self, frame, bbox):
        """
        Check liveness for a face in frame.

        Args:
            frame: BGR numpy array (full frame)
            bbox: [x1, y1, x2, y2] face bounding box

        Returns:
            (liveness_score, is_real): tuple
            liveness_score: float 0.0-1.0 (higher = more likely real)
            is_real: bool (True if score >= threshold)
        """
        if not self.is_initialized or not self.sessions:
            # No models loaded, assume real (skip liveness)
            return 1.0, True

        try:
            scores = []
            for session, input_size, scale in self.sessions:
                # Crop face with this model's scale factor
                crop = self._crop_face_with_scale(frame, bbox, scale, input_size)
                if crop is None:
                    continue

                # Preprocess
                input_data = self._preprocess(crop)

                # Get input name
                input_name = session.get_inputs()[0].name

                # Run inference
                outputs = session.run(None, {input_name: input_data})
                output = outputs[0]

                # Parse output: typically shape (1, 3) or (1, 2)
                # For MiniFASNet: output shape is (1, 3) where:
                #   index 0 = fake score
                #   index 1 = real score (for 2-class)
                #   or softmax over 3 classes
                if output.shape[-1] >= 2:
                    # Softmax
                    exp_vals = np.exp(output[0] - np.max(output[0]))
                    probs = exp_vals / np.sum(exp_vals)
                    # Real face probability (index 1 for 2-class, index 1 for 3-class)
                    real_prob = float(probs[1]) if len(probs) >= 2 else float(probs[0])
                    scores.append(real_prob)
                else:
                    scores.append(float(output[0][0]))

            if not scores:
                return 1.0, True

            # Average scores from all models
            avg_score = sum(scores) / len(scores)
            is_real = avg_score >= Config.LIVENESS_THRESHOLD

            return avg_score, is_real

        except Exception as e:
            logger.error(f"Liveness check error: {e}")
            # On error, default to real (don't block attendance)
            return 1.0, True

    def check_batch(self, frame, bboxes):
        """
        Check liveness for multiple faces in a frame.
        Returns list of (score, is_real) tuples.
        """
        results = []
        for bbox in bboxes:
            score, is_real = self.check(frame, bbox)
            results.append((score, is_real))
        return results



# import os
# import cv2
# import torch
# import logging
# import numpy as np
# import torch.nn as nn

# from config import Config

# logger = logging.getLogger(__name__)


# # -----------------------------------
# # SIMPLE MiniFASNet Architecture
# # -----------------------------------

# class SimpleAntiSpoofNet(nn.Module):

#     def __init__(self):

#         super(SimpleAntiSpoofNet, self).__init__()

#         self.features = nn.Sequential(

#             nn.Conv2d(3, 32, 3, padding=1),
#             nn.ReLU(),

#             nn.MaxPool2d(2),

#             nn.Conv2d(32, 64, 3, padding=1),
#             nn.ReLU(),

#             nn.MaxPool2d(2),

#             nn.Conv2d(64, 128, 3, padding=1),
#             nn.ReLU(),

#             nn.AdaptiveAvgPool2d((1, 1))
#         )

#         self.classifier = nn.Sequential(

#             nn.Flatten(),

#             nn.Linear(128, 2)
#         )

#     def forward(self, x):

#         x = self.features(x)

#         x = self.classifier(x)

#         return x


# # -----------------------------------
# # LIVENESS CHECKER
# # -----------------------------------

# class LivenessChecker:

#     def __init__(self):

#         self.models = []

#         self.is_initialized = False

#     def initialize(self):

#         try:

#             model_files = [

#                 "2.7_80x80_MiniFASNetV2.pth",
#                 "4_0_0_80x80_MiniFASNetV1SE.pth"
#             ]

#             for model_file in model_files:

#                 model_path = os.path.join(
#                     Config.ANTI_SPOOF_MODEL_DIR,
#                     model_file
#                 )

#                 if not os.path.exists(model_path):

#                     logger.warning(f"Model not found: {model_path}")

#                     continue

#                 # Create model structure
#                 model = SimpleAntiSpoofNet()

#                 # Load weights
#                 state_dict = torch.load(
#                     model_path,
#                     map_location=torch.device("cpu")
#                 )

#                 # Handle checkpoint formats
#                 if isinstance(state_dict, dict):

#                     if "state_dict" in state_dict:
#                         state_dict = state_dict["state_dict"]

#                 missing, unexpected = model.load_state_dict(
#                     state_dict,
#                     strict=False
#                 )

#                 print("\n====================")
#                 print(model_file)
#                 print("Missing Keys:", len(missing))
#                 print("Unexpected Keys:", len(unexpected))

#                 if len(missing) > 0:
#                     print("Sample Missing:", missing[:5])

#                 if len(unexpected) > 0:
#                     print("Sample Unexpected:", unexpected[:5])

#                 print("====================\n")

#                 model.eval()

#                 self.models.append(model)

#                 logger.info(f"Loaded anti-spoof model: {model_file}")

#             if len(self.models) > 0:

#                 self.is_initialized = True

#                 logger.info("Liveness checker initialized")

#             else:

#                 logger.warning("No anti-spoof models loaded")

#             return self.is_initialized

#         except Exception as e:

#             logger.error(f"Liveness initialization error: {e}")

#             return False

#     def preprocess(self, face):

#         face = cv2.resize(face, (80, 80))

#         face = cv2.cvtColor(face, cv2.COLOR_BGR2RGB)

#         face = face.astype(np.float32) / 255.0

#         face = np.transpose(face, (2, 0, 1))

#         face = np.expand_dims(face, axis=0)

#         tensor = torch.tensor(face, dtype=torch.float32)

#         return tensor

#     def check(self, frame, bbox):

#         try:

#             if not self.is_initialized:

#                 return 1.0, True

#             x1, y1, x2, y2 = bbox

#             h, w = frame.shape[:2]

#             x1 = max(0, x1)
#             y1 = max(0, y1)
#             x2 = min(w, x2)
#             y2 = min(h, y2)

#             face = frame[y1:y2, x1:x2]

#             if face.size == 0:

#                 return 0.0, False

#             input_tensor = self.preprocess(face)

#             scores = []

#             for model in self.models:

#                 with torch.no_grad():

#                     output = model(input_tensor)

#                     probabilities = torch.softmax(output, dim=1)

#                     real_score = float(probabilities[0][1])

#                     scores.append(real_score)

#             if len(scores) == 0:

#                 return 0.0, False
#             avg_score = sum(scores) / len(scores)

#             threshold = 0.80
            
#             # print(f"Liveness Score: {avg_score:.4f}")
#             print(
#                 f"Liveness Score: {avg_score:.4f} | "
#                 f"Threshold: 0.80 | "
#                 f"Result: {'REAL' if avg_score >= 0.80 else 'SPOOF'}"
#             )

#             is_real = avg_score >= threshold

#             return avg_score, is_real

#         except Exception as e:

#             logger.error(f"Liveness check error: {e}")

#             return 0.0, False

#     def check_batch(self, frame, bboxes):

#         results = []

#         for bbox in bboxes:

#             score, is_real = self.check(frame, bbox)

#             results.append((score, is_real))

#         return results