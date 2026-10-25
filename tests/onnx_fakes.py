"""Tiny stand-ins for the on-device engine's image models (tests/test_ai_images.py).

Each is a real ONNX file with the inputs and outputs of the model it stands
in for (Immich's SigLIP 2 export, transformers.js's Florence-2 export), and a
few kilobytes of arithmetic inside, so ONNX Runtime loads and runs them
exactly as it would the real ones. What they compute is chosen to be easy to
check: the image half averages the colours, the words half counts tokens,
and the stand-in decoder writes a fixed sentence one token at a time, reading
its position from the length of its own key-value cache.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

onnx = None


def _onnx():
    global onnx
    import onnx as module                                       # noqa: PLC0415
    onnx = module
    return module


def _save(graph, path: Path) -> None:
    helper = _onnx().helper
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 18)],
                              ir_version=8)
    path.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(model, str(path))


def _tensor(name, array):
    return _onnx().numpy_helper.from_array(np.asarray(array), name)


# ---------------------------------------------------------------------------
# SigLIP 2, as Immich exports it
# ---------------------------------------------------------------------------

DIM = 8


def siglip(folder: Path, *, words: list[str]) -> Path:
    """visual/model.onnx, textual/model.onnx and textual/tokenizer.json."""
    h = _onnx().helper
    T = onnx.TensorProto
    rng = np.random.default_rng(7)
    # Pictures: the mean of each channel, through a fixed projection.
    w_image = rng.normal(size=(3, DIM)).astype(np.float32)
    graph = h.make_graph(
        [h.make_node("ReduceMean", ["image", "axes"], ["mean"], keepdims=0),
         h.make_node("MatMul", ["mean", "w"], ["embedding"])],
        "visual",
        [h.make_tensor_value_info("image", T.FLOAT, ["batch", 3, 224, 224])],
        [h.make_tensor_value_info("embedding", T.FLOAT, ["batch", DIM])],
        [_tensor("axes", np.array([2, 3], dtype=np.int64)), _tensor("w", w_image)])
    _save(graph, folder / "visual" / "model.onnx")
    # Words: the token ids as numbers, through a fixed projection.
    w_text = rng.normal(size=(64, DIM)).astype(np.float32)
    graph = h.make_graph(
        [h.make_node("Cast", ["text"], ["ids"], to=T.FLOAT),
         h.make_node("MatMul", ["ids", "w"], ["embedding"])],
        "textual",
        [h.make_tensor_value_info("text", T.INT32, ["batch", 64])],
        [h.make_tensor_value_info("embedding", T.FLOAT, ["batch", DIM])],
        [_tensor("w", w_text)])
    _save(graph, folder / "textual" / "model.onnx")
    tokenizer(folder / "textual" / "tokenizer.json",
              ["<pad>", "<eos>", "<bos>", "<unk>", *words])
    return folder


def tokenizer(path: Path, vocab: list[str]) -> Path:
    from tokenizers import Tokenizer, models, pre_tokenizers  # noqa: PLC0415
    made = Tokenizer(models.WordLevel({w: i for i, w in enumerate(vocab)}, unk_token="<unk>"))
    made.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    path.parent.mkdir(parents=True, exist_ok=True)
    made.save(str(path))
    return path


# ---------------------------------------------------------------------------
# Florence-2, as transformers.js exports it
# ---------------------------------------------------------------------------

HIDDEN = 8
HEADS, HEAD_DIM = 2, 4
VOCAB = ["<s>", "<pad>", "</s>", "<unk>", "a", "dog", "on", "the", "beach"]
#: What the stand-in decoder writes after its forced first token.
SENTENCE = ["a", "dog", "on", "the", "beach"]


def florence(folder: Path, *, sentence: list[str] = SENTENCE) -> Path:
    h = _onnx().helper
    T = onnx.TensorProto
    onnx_dir = folder / "onnx"
    rng = np.random.default_rng(11)
    # The image encoder: 4 "patches" out of the channel means.
    graph = h.make_graph(
        [h.make_node("ReduceMean", ["pixel_values", "axes"], ["mean"], keepdims=0),
         h.make_node("MatMul", ["mean", "w"], ["flat"]),
         h.make_node("Reshape", ["flat", "shape"], ["image_features"])],
        "vision",
        [h.make_tensor_value_info("pixel_values", T.FLOAT, ["batch", 3, "h", "w"])],
        [h.make_tensor_value_info("image_features", T.FLOAT, ["batch", 4, HIDDEN])],
        [_tensor("axes", np.array([2, 3], dtype=np.int64)),
         _tensor("w", rng.normal(size=(3, 4 * HIDDEN)).astype(np.float32)),
         _tensor("shape", np.array([-1, 4, HIDDEN], dtype=np.int64))])
    _save(graph, onnx_dir / "vision_encoder.onnx")
    # The word table.
    graph = h.make_graph(
        [h.make_node("Gather", ["table", "input_ids"], ["inputs_embeds"])],
        "embed",
        [h.make_tensor_value_info("input_ids", T.INT64, ["batch", "length"])],
        [h.make_tensor_value_info("inputs_embeds", T.FLOAT, ["batch", "length", HIDDEN])],
        [_tensor("table", rng.normal(size=(len(VOCAB), HIDDEN)).astype(np.float32))])
    _save(graph, onnx_dir / "embed_tokens_int8.onnx")
    # The text encoder: hands its input back, masked.
    graph = h.make_graph(
        [h.make_node("Cast", ["attention_mask"], ["maskf"], to=T.FLOAT),
         h.make_node("Unsqueeze", ["maskf", "last"], ["mask3"]),
         h.make_node("Mul", ["inputs_embeds", "mask3"], ["last_hidden_state"])],
        "encoder",
        [h.make_tensor_value_info("inputs_embeds", T.FLOAT, ["batch", "length", HIDDEN]),
         h.make_tensor_value_info("attention_mask", T.INT64, ["batch", "length"])],
        [h.make_tensor_value_info("last_hidden_state", T.FLOAT, ["batch", "length", HIDDEN])],
        [_tensor("last", np.array([2], dtype=np.int64))])
    _save(graph, onnx_dir / "encoder_model_int8.onnx")
    _save(_decoder(sentence), onnx_dir / "decoder_model_merged_int8.onnx")
    tokenizer(folder / "tokenizer.json", VOCAB)
    return folder


def _decoder(sentence: list[str]):
    """The merged decoder: logits for "the next token", and the cache grown
    by one. Its position is the length of the decoder cache it is given;
    position 0 (the forced beginning-of-text) says nothing useful, then the
    sentence, then end-of-text."""
    h = _onnx().helper
    T = onnx.TensorProto
    script = [VOCAB.index(w) for w in sentence] + [VOCAB.index("</s>")]
    table = np.full((len(script) + 2, len(VOCAB)), -5.0, dtype=np.float32)
    table[0, VOCAB.index("<unk>")] = 5.0
    for at, token in enumerate(script, start=1):
        table[at, token] = 5.0
    table[len(script) + 1, VOCAB.index("</s>")] = 5.0
    cache = ["batch", HEADS, "past", HEAD_DIM]
    nodes = [
        # Where it is: how many tokens the cache already holds.
        h.make_node("Shape", ["past_key_values.0.decoder.key"], ["shape"]),
        h.make_node("Gather", ["shape", "two"], ["position"], axis=0),
        h.make_node("Gather", ["table", "position"], ["row"], axis=0),
        h.make_node("Reshape", ["row", "logits_shape"], ["logits_one"]),
        # The same for every photograph in the batch.
        h.make_node("Shape", ["inputs_embeds"], ["embeds_shape"]),
        h.make_node("Slice", ["embeds_shape", "zero1", "one"], ["batch"]),
        h.make_node("Concat", ["batch", "one_vocab"], ["batch_logits_shape"], axis=0),
        h.make_node("Expand", ["logits_one", "batch_logits_shape"], ["logits"]),
        # The new token's key and value, from its vector.
        h.make_node("Reshape", ["inputs_embeds", "kv_shape"], ["kv"]),
        h.make_node("Concat", ["past_key_values.0.decoder.key", "kv"],
                    ["present.0.decoder.key"], axis=2),
        h.make_node("Concat", ["past_key_values.0.decoder.value", "kv"],
                    ["present.0.decoder.value"], axis=2),
        # The encoder's half: worked out from its output at the first token;
        # handed back unchanged after that, as the real cache branch does.
        h.make_node("ReduceMean", ["encoder_hidden_states", "one"], ["enc_mean"], keepdims=1),
        h.make_node("Reshape", ["enc_mean", "kv_shape"], ["enc_kv"]),
        h.make_node("If", ["use_cache_branch"], ["present.0.encoder.key"],
                    then_branch=_identity("past_key_values.0.encoder.key", "k_then"),
                    else_branch=_identity("enc_kv", "k_else")),
        h.make_node("If", ["use_cache_branch"], ["present.0.encoder.value"],
                    then_branch=_identity("past_key_values.0.encoder.value", "v_then"),
                    else_branch=_identity("enc_kv", "v_else")),
    ]
    inputs = [
        h.make_tensor_value_info("encoder_attention_mask", T.INT64, ["batch", "enc"]),
        h.make_tensor_value_info("encoder_hidden_states", T.FLOAT, ["batch", "enc", HIDDEN]),
        h.make_tensor_value_info("inputs_embeds", T.FLOAT, ["batch", 1, HIDDEN]),
        h.make_tensor_value_info("past_key_values.0.decoder.key", T.FLOAT, cache),
        h.make_tensor_value_info("past_key_values.0.decoder.value", T.FLOAT, cache),
        h.make_tensor_value_info("past_key_values.0.encoder.key", T.FLOAT,
                                 ["batch", HEADS, "enc_past", HEAD_DIM]),
        h.make_tensor_value_info("past_key_values.0.encoder.value", T.FLOAT,
                                 ["batch", HEADS, "enc_past", HEAD_DIM]),
        h.make_tensor_value_info("use_cache_branch", T.BOOL, [1]),
    ]
    outputs = [
        h.make_tensor_value_info("logits", T.FLOAT, ["batch", 1, len(VOCAB)]),
        h.make_tensor_value_info("present.0.decoder.key", T.FLOAT, cache),
        h.make_tensor_value_info("present.0.decoder.value", T.FLOAT, cache),
        h.make_tensor_value_info("present.0.encoder.key", T.FLOAT, None),
        h.make_tensor_value_info("present.0.encoder.value", T.FLOAT, None),
    ]
    initialisers = [
        _tensor("two", np.array(2, dtype=np.int64)),
        _tensor("one", np.array([1], dtype=np.int64)),
        _tensor("table", table),
        _tensor("logits_shape", np.array([1, 1, len(VOCAB)], dtype=np.int64)),
        _tensor("kv_shape", np.array([-1, HEADS, 1, HEAD_DIM], dtype=np.int64)),
        _tensor("zero1", np.array([0], dtype=np.int64)),
        _tensor("one_vocab", np.array([1, len(VOCAB)], dtype=np.int64)),
    ]
    return h.make_graph(nodes, "decoder", inputs, outputs, initialisers)


def _identity(source: str, name: str):
    h = onnx.helper
    return h.make_graph([h.make_node("Identity", [source], [name])], f"g_{name}", [],
                        [h.make_tensor_value_info(name, onnx.TensorProto.FLOAT, None)])
