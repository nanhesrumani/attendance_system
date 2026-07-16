import os
import logging

import cv2
from googleapiclient import model
import numpy as np
import torch

from config import Config
from recognition.anti_spoof.MiniFASNet import (
    MiniFASNetV2,
    MiniFASNetV1SE,
)

logger = logging.getLogger(__name__)


class LivenessChecker:

    MODEL_CONFIGS = [
        (
            "2.7_80x80_MiniFASNetV2.pth",
            MiniFASNetV2,
            (5, 5)
        ),
        (
            "4_0_0_80x80_MiniFASNetV1SE.pth",
            MiniFASNetV1SE,
            (5, 5)
        ),
    ]

    def __init__(self):
        self.models = []
        self.device = torch.device("cpu")
        self.is_initialized = False

    def initialize(self):

        try:

            loaded = 0

            for model_file, model_builder, conv6_kernel in self.MODEL_CONFIGS:

                model_path = os.path.join(
                    Config.ANTI_SPOOF_MODEL_DIR,
                    model_file
                )

                if not os.path.isfile(model_path):
                    logger.warning(
                        f"Anti-spoof model not found: {model_path}"
                    )
                    continue

                model = model_builder(
                    conv6_kernel=conv6_kernel
                )            

                checkpoint = torch.load(
                    model_path,
                    map_location=self.device
                )

                if isinstance(checkpoint, dict):

                    if "state_dict" in checkpoint:
                        checkpoint = checkpoint["state_dict"]

                cleaned = {}

                for k, v in checkpoint.items():

                    if k.startswith("module."):
                        k = k[7:]

                    cleaned[k] = v

                missing, unexpected = model.load_state_dict(
                    cleaned,
                    strict=True
                )

                logger.info(
                    f"{model_file} "
                    f"(missing={len(missing)}, "
                    f"unexpected={len(unexpected)})"
                )

                model.eval()
                model.to(self.device)

                self.models.append(model)

                loaded += 1

            if loaded > 0:

                self.is_initialized = True

                logger.info(
                    f"LivenessChecker ready: {loaded} model(s) loaded"
                )

            else:

                logger.warning(
                    "No anti-spoof models loaded."
                )

            return self.is_initialized

        except Exception as e:

            logger.exception(
                f"Liveness initialization failed: {e}"
            )

            return False

    def _preprocess(self, face):

        face = cv2.resize(face, (80, 80))

        face = cv2.cvtColor(
            face,
            cv2.COLOR_BGR2RGB
        )

        face = face.astype(np.float32) / 255.0

        face = np.transpose(face, (2, 0, 1))

        face = np.expand_dims(face, axis=0)

        return torch.tensor(
            face,
            dtype=torch.float32,
            device=self.device
        )

    def check(self, frame, bbox):

        if not self.is_initialized:
            return 1.0, True

        try:

            x1, y1, x2, y2 = bbox

            h, w = frame.shape[:2]

            x1 = max(0, int(x1))
            y1 = max(0, int(y1))
            x2 = min(w, int(x2))
            y2 = min(h, int(y2))

            bw = x2 - x1
            bh = y2 - y1

            pad_x = int(bw * 0.8)
            pad_y = int(bh * 0.8)

            nx1 = max(0, x1 - pad_x)
            ny1 = max(0, y1 - pad_y)
            nx2 = min(w, x2 + pad_x)
            ny2 = min(h, y2 + pad_y)

            face = frame[ny1:ny2, nx1:nx2]
            cv2.imwrite("debug_crop.jpg", face)
            if face.size == 0:
                return 0.0, False

            input_tensor = self._preprocess(face)

            scores = []

            with torch.no_grad():

                for model in self.models:

                    output = model(input_tensor)

                    print("RAW OUTPUT =", output.cpu().numpy())

                    probs = torch.softmax(output, dim=1)

                    print(
                        "C0=", float(probs[0][0]),
                        "C1=", float(probs[0][1]),
                        "C2=", float(probs[0][2])
                    )

                    print("ARGMAX =", torch.argmax(probs, dim=1).item())
                    real_score = float(probs[0][2])
                    scores.append(real_score)

            if not scores:
                return 0.0, False

            avg_score = sum(scores) / len(scores)

            print(
                f"Liveness Score: {avg_score:.4f} | "
                f"Threshold: {Config.LIVENESS_THRESHOLD:.2f}"
            )

            is_real = (
                avg_score >= Config.LIVENESS_THRESHOLD
            )

            return avg_score, is_real

        except Exception as e:

            logger.exception(
                f"Liveness check failed: {e}"
            )

            return 1.0, True

    def check_batch(self, frame, bboxes):

        results = []

        for bbox in bboxes:

            score, is_real = self.check(
                frame,
                bbox
            )

            results.append(
                (score, is_real)
            )

        return results