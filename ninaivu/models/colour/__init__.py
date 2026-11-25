"""Colour for black-and-white photographs: the model plugin (P16).

DDColor, pinned on the AI models page as ``colour`` (media/model_catalog.py).
``files`` says where its file is looked for, ``lab`` is the arithmetic that
lays its colour under a photograph's own lightness, and ``runner`` is the
capability the model host loads. The feature that asks for it, and the table
of colour copies, are ``ninaivu/features/colour_copies``.
"""
MODEL = "colour"
SWITCH = "colour_photos"
PACKAGES = ("onnxruntime",)
