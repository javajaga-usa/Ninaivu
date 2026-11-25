"""Finding and recognising faces: the model plugin (P16).

``engine`` is the detector and recogniser (YuNet and SFace through OpenCV) with
the quality gate and the crops; ``arcface`` is the second pair (SCRFD and
ArcFace on ONNX Runtime) chosen with the ``face_model`` setting. Who a face
*is* stays in the core (``ninaivu/media/facematch.py``, ``faceindex.py``),
which has no model import.
"""
