from typing import Union, Optional, Type

import torch
from torchvision.transforms.functional import to_tensor
import collections
from torch.utils.data.dataloader import default_collate

import numpy as np

from slam.common.utils import assert_debug, check_tensor


def custom_to_tensor(data: Union[torch.Tensor, np.ndarray, dict],
                     device: Union[str, torch.device] = "cuda",
                     torchviz_conversion: bool = True,
                     batch_dim: bool = False) -> Union[torch.Tensor, dict]:
    """
    Converts data to a Tensor for compatible data types

    Parameters
    ----------
    data : An data to convert to torch
         The data can be a map, numpy ndarray or other.
         All tensor like data (numpy ndarray for instance) are converted to tensor
         All the values of containers are transformed to tensors
    device : The device to send the tensors to
    torchviz_conversion : Whether to use torchviz conversion
        Default torch to_tensor method simply converts a numpy tensor to a torch Tensor
        torchvision to_tensor also changes the layout of the image-like tensors,
        A H, W, D numpy image becomes a D, H, W tensor.
        One must therefore be careful that this is what is intended
    batch_dim : bool
        Whether to add a dimension (in first position)
        A [N1, N2, ..., NK] tensor will be transformed to [1, N1, N2, ..., NK] tensor
    """
    if isinstance(data, collections.Mapping):
        return {key: custom_to_tensor(data[key],
                                      device=device,
                                      torchviz_conversion=torchviz_conversion,
                                      batch_dim=batch_dim) for key in data}
    if isinstance(data, np.ndarray):
        if torchviz_conversion:
            tensor = to_tensor(data).to(device=device)
        else:
            tensor = torch.from_numpy(data).to(device=device)
        if batch_dim:
            tensor = tensor.unsqueeze(0)
        return tensor

    if isinstance(data, torch.Tensor):
        tensor = data.to(device=device)
        if batch_dim:
            tensor = tensor.unsqueeze(0)
        return tensor

    return data


def send_to_device(data: Union[dict, torch.Tensor, np.ndarray],
                   device: torch.device,
                   convert_numpy: bool = True,
                   torchviz_conversion: bool = True) -> object:
    """
    Sends data to the device if it can

    torch.Tensor are sent to the device,
    containers send all the torch.Tensor to the devices
    Other data types are left unchanged
    """
    if isinstance(data, torch.Tensor):
        data = data.to(device=device)

    if isinstance(data, collections.Mapping):
        data = {key: send_to_device(data[key], device) for key in data}

    if isinstance(data, np.ndarray) and convert_numpy:
        data = custom_to_tensor(data,
                                device=device,
                                torchviz_conversion=torchviz_conversion)

    return data


def convert_pose_transform(pose: Union[torch.Tensor, np.ndarray],
                           dest: type = torch.Tensor,
                           device: Optional[torch.device] = None,
                           dtype: Optional[Union[torch.dtype, np.number, Type]] = None):
    """Converts a [4, 4] pose tensor to the desired type

    Returns a tensor (either a numpy.ndarray or torch.Tensor depending on dest type)
    >>> check_tensor(convert_pose_transform(torch.eye(4).reshape(4, 4), np.ndarray), [4, 4])
    >>> check_tensor(convert_pose_transform(torch.eye(4).reshape(1, 4, 4), np.ndarray, dtype=np.float32), [4, 4])
    >>> check_tensor(convert_pose_transform(torch.eye(4).reshape(1, 4, 4), torch.Tensor, dtype=torch.float32), [1, 4, 4])
    >>> check_tensor(convert_pose_transform(torch.eye(4).reshape(4, 4), torch.Tensor, dtype=torch.float32), [4, 4])
    >>> check_tensor(convert_pose_transform(np.eye(4).reshape(4, 4), torch.Tensor, dtype=torch.float32), [4, 4])
    >>> check_tensor(convert_pose_transform(np.eye(4).reshape(4, 4), np.ndarray, dtype=np.float32), [4, 4])
    >>> check_tensor(convert_pose_transform(np.eye(4).reshape(4, 4), np.ndarray), [4, 4])
    """
    # Check size
    if isinstance(pose, torch.Tensor):
        assert_debug(list(pose.shape) == [1, 4, 4] or list(pose.shape) == [4, 4],
                     f"Wrong tensor shape, expected [(1), 4, 4], got {pose.shape}")
        if dest == torch.Tensor:
            assert_debug(isinstance(dtype, torch.dtype), f"The dtype {dtype} is not a torch.dtype")
            return pose.to(device=device if device is not None else pose.device,
                           dtype=dtype if dtype is not None else pose.dtype)
        else:
            assert_debug(dest == np.ndarray, "Only numpy.ndarray and torch.Tensor are supported as destination tensor")
            np_array = pose.detach().cpu().numpy()
            if dtype is not None:
                assert_debug(issubclass(dtype, np.number), f"Expected a numpy.dtype, got {dtype}")
                np_array = np_array.astype(dtype)
            return np_array.reshape(4, 4)
    else:
        assert_debug(isinstance(pose, np.ndarray), f"Only numpy.ndarray and torch.Tensor are supported. Got {pose}.")
        check_tensor(pose, [4, 4])
        if dest == torch.Tensor:
            tensor = torch.from_numpy(pose).to(dtype=dtype, device=device)
            return tensor
        if dtype is not None:
            assert_debug(issubclass(dtype, np.number), f"Expected numpy.dtype, got {dtype}")
            new_pose = pose.astype(dtype)
            return new_pose
        return pose


# ----------------------------------------------------------------------------------------------------------------------
# Collate Function
from slam.common.modules import _with_ct_icp

if _with_ct_icp:
    import pyct_icp as pct


def collate_fun(batch) -> object:
    """
    Overrides pytorch default collate function, to keep numpy arrays in dictionaries

    If `batch` is a dictionary, every key containing the key `numpy` will not be converted to a tensor
    And a suffix "_<batch_idx>" will be appended to the key, to identify arrays by their batch index

    The original key will map only to the first element of the batch
    """
    elem = batch[0]
    if isinstance(elem, list):
        return batch
    elif isinstance(elem, collections.Mapping):

        result = dict()
        for key in elem:
            cumulate_key = "numpy" in key
            if _with_ct_icp:
                if isinstance(elem[key], pct.LiDARFrame):
                    cumulate_key = True
            if cumulate_key:
                for idx, d in enumerate(batch):
                    if idx == 0:
                        result[f"{key}"] = d[key]

                    result[f"{key}_{idx}"] = d[key]

            else:
                result[key] = collate_fun([d[key] for d in batch])
        return result
    else:
        return default_collate(batch)
