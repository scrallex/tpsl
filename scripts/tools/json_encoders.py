import json
from typing import Any
import numpy as np
import torch


class NumpyEncoder(json.JSONEncoder):
    """JSON encoder that handles NumPy arrays and scalars."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, np.integer):
            return int(obj)
        if hasattr(obj, "item") and callable(getattr(obj, "item")):
            return obj.item()
        return super().default(obj)


class TorchEncoder(json.JSONEncoder):
    """JSON encoder that handles PyTorch tensors."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, torch.Tensor):
            return obj.cpu().detach().numpy().tolist()
        return super().default(obj)
