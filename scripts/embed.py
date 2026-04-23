"""Shared embedding model — lazy-loaded singleton for bge-small-zh."""
_model = None

def get_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        _model = TextEmbedding("BAAI/bge-small-zh-v1.5")
    return _model
