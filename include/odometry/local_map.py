from abc import ABC, abstractmethod
from collections import namedtuple
from typing import Optional, Tuple

import numpy as np
import torch
from hydra.core.config_store import ConfigStore
from pykdtree.kdtree import KDTree

# Hydra and OmegaConf imports
from hydra.conf import dataclass, MISSING

# Project Imports
from slam.common.geometry import projection_map_to_points, compute_neighbors, compute_normal_map, mask_not_null
from slam.common.pose import Pose
from slam.common.projection import Projector
from slam.odometry import *
from slam.common.utils import assert_debug, check_tensor, TensorType, remove_nan
from slam.common.utils import ObjectLoaderEnum


# ----------------------------------------------------------------------------------------------------------------------
@dataclass
class LocalMapConfig:
    """An abstract Configuration of the Local Map"""
    pose: str = "euler"
    type: str = MISSING  # Each subclass must define its type


# ----------------------------------------------------------------------------------------------------------------------
class LocalMap(ABC):
    """An abstract Local Map for a Frame-to-Model ICP-based Odometry estimation"""

    @dataclass
    class NeighborhoodResult:
        neighbor_points: Optional[TensorType] = None
        neighbor_normals: Optional[TensorType] = None
        new_target_points: Optional[TensorType] = None

    def __init__(self, config: LocalMapConfig, **kwargs):
        super().__init__()
        self.config = config
        self.pose = Pose(config.pose)

    @abstractmethod
    def init(self):
        """Clears and Initialize the Local Map"""
        raise NotImplementedError("")

    @abstractmethod
    def update(self, new_relative_pose: TensorType,
               new_pc_data: Optional[TensorType] = None,
               new_vertex_map: Optional[TensorType] = None, **kwargs) -> None:
        """
        Updates the Local Map, by incorporating the new frame registered

        Args:
            new_relative_pose (torch.Tensor): The relative pose between the new frame and the current Local Map state
            new_pc_data (torch.Tensor): The Point Cloud tensor of the new frame to insert into the map `(N, 3)`
            new_vertex_map (torch.Tensor): The vertex map (spherical projection) of the Point Cloud Data
                                           to insert into the map `(3, H, W)`
        """
        raise NotImplementedError("")

    @abstractmethod
    def nearest_neighbor_search(self, points: TensorType,
                                with_normals: bool = True,
                                with_new_target_points: bool = True, **kwargs) -> NeighborhoodResult:
        """
        Finds nearest neighbors correspondences in the map for a set of points
        """
        raise NotImplementedError("")

    @abstractmethod
    def get_last_frame(self) -> torch.Tensor:
        """
        Returns the last frame registered in the Local Map
        """
        raise NotImplementedError("")


# ----------------------------------------------------------------------------------------------------------------------
@dataclass
class ProjectiveLocalMapConfig(LocalMapConfig):
    """Configuration for a ProjectiveLocalMap"""
    local_map_size: int = 20
    type: str = "projective_local_map"
    normals_kernel_size: int = 5  # The kernel size for the normal computation


class ProjectiveLocalMap(LocalMap):
    """
    A Local Map which computes neighborhood by projective association
    """

    def __init__(self, config: ProjectiveLocalMapConfig, projector: Projector, **kwargs):
        super().__init__(config)
        assert OmegaConf.get_type(config) == ProjectiveLocalMapConfig
        self.local_map_size = config.local_map_size
        self.projector = projector

        # ---------------------------------
        # Local map tensors saved
        self._local_map_num_elements = []
        self._local_map: torch.Tensor = None  # The point cloud [K, N, 3]
        self._local_map_normals: torch.Tensor = None  # The point cloud normals [K, N, 3]
        self._local_map_mask: torch.Tensor = None  # The point cloud mask [K, N, 1]
        self._local_map_poses: torch.Tensor = None
        self._default_mask: torch.Tensor = None

        # -------------------------------------------------------------
        # Aggregated Vertex Map (model for nearest neighbor computation)
        self._model_vmap: torch.Tensor = None  # [K, 3, H, W]
        self._model_nmap: torch.Tensor = None  # [K, 3, H, W]

    # ------------------------------------------------------------------------------------------------------------------
    def init(self):
        """Initialize the Local Map by releasing all persisted tensors"""
        self._local_map: torch.Tensor = None  # The point cloud [K, N, 3]
        self._local_map_normals: torch.Tensor = None  # The point cloud normals [K, N, 3]
        self._local_map_mask: torch.Tensor = None  # The point cloud mask [K, N, 1]
        self._local_map_poses: torch.Tensor = None
        self._default_mask: torch.Tensor = None

    # ------------------------------------------------------------------------------------------------------------------
    def update(self,
               relative_pose: torch.Tensor,
               new_vertex_map: Optional[torch.Tensor] = None,
               new_normal_map: Optional[torch.Tensor] = None,
               mask: Optional[torch.Tensor] = None,
               **kwargs):
        """
        Updates the local map and (registers a new frame into it)
        """
        check_tensor(relative_pose, [1, 4, 4], torch.Tensor)
        if new_vertex_map is not None:
            check_tensor(new_vertex_map, [1, 3, -1, -1])
            _, _, h, w = new_vertex_map.shape
            if new_normal_map is None:
                normal_map = compute_normal_map(new_vertex_map, kernel_size=self.config.normals_kernel_size)
            check_tensor(normal_map, [1, 3, h, w])
            if mask is None:
                mask = mask_not_null(new_vertex_map)
            check_tensor(mask, [1, 1, h, w])
        if self._local_map is None:
            # Initialize the Map
            self._local_map = new_vertex_map
            self._local_map_normals = normal_map
            self._local_map_mask = mask
            self._local_map_poses = relative_pose
        else:
            old_poses = relative_pose.inverse() @ self._local_map_poses

            if new_vertex_map is not None:
                # Updates the local map poses
                self._local_map_poses = torch.cat([old_poses, torch.eye(4, device=old_poses.device,
                                                                        dtype=old_poses.dtype).unsqueeze(0)], dim=0)
                self._local_map = torch.cat([self._local_map, new_vertex_map], dim=0)
                self._local_map_normals = torch.cat([self._local_map_normals, normal_map], dim=0)
                if mask is None and self._local_map_mask is not None:
                    raise RuntimeError("[ERROR] A Mask is expected but was not given.")
                elif mask is not None:
                    self._local_map_mask = torch.cat([self._local_map_mask, mask], dim=0)
            else:
                self._local_map_poses = old_poses

            if self._local_map_poses.size(0) > self.local_map_size:
                # Suppress a pointcloud if it
                self._local_map = self._local_map[1:]
                self._local_map_normals = self._local_map_normals[1:]
                self._local_map_poses = self._local_map_poses[1:]
                self._local_map_mask = self._local_map_mask[1:]

        self.build_model()

    # ------------------------------------------------------------------------------------------------------------------
    def build_model(self) -> (torch.Tensor, torch.Tensor):
        """Builds the model which allows to construct the nearest neighbor computation"""

        _, _, h, w = self._local_map.shape

        # Build the local map poses
        model_points = self.pose.apply_transformation(projection_map_to_points(self._local_map),
                                                      self._local_map_poses)

        if self._local_map_normals is not None:
            # Build the local map normals
            model_normals = self.pose.apply_rotation(projection_map_to_points(self._local_map_normals),
                                                     self._local_map_poses)
            model_points = torch.cat([model_points, model_normals], dim=2)

        if self._local_map_mask is not None:
            model_points *= projection_map_to_points(self._local_map_mask, num_channels=1)

        local_nmaps_vmaps = self.projector.build_projection_map(model_points[:, :, :3],
                                                                height=h,
                                                                width=w,
                                                                transform=lambda x: model_points)
        self._model_vmap = local_nmaps_vmaps[:, :3]

        if self._local_map_normals is not None:
            self._model_nmap = local_nmaps_vmaps[:, 3:6]

    # ------------------------------------------------------------------------------------------------------------------
    def nearest_neighbor_search(self, target_points: TensorType,
                                with_normals: bool = True,
                                with_new_target_points: bool = True, **kwargs):
        """
        Returns the nearest neighbors by projective data association

        Projects the points in the image plane
        """
        check_tensor(target_points, [-1, 3], torch.Tensor)

        new_target_points = target_points
        new_target_vmap = self.projector.build_projection_map(new_target_points.unsqueeze(0))

        neighbor_vmap, neighbor_nmap = compute_neighbors(new_target_vmap,
                                                         self._model_vmap,
                                                         reference_fields=self._model_nmap)
        neighbor_points = projection_map_to_points(neighbor_vmap).reshape(1, -1, 3)

        new_points = projection_map_to_points(new_target_vmap).reshape(1, -1, 3)
        mask = mask_not_null(new_points, dim=-1) * mask_not_null(neighbor_points, dim=-1)
        mask = mask[:, :, 0]

        results = self.NeighborhoodResult()
        results.neighbor_points = neighbor_points[mask].unsqueeze(0)
        if with_normals:
            neighbor_normals = projection_map_to_points(neighbor_nmap).reshape(1, -1, 3)
            results.neighbor_normals = neighbor_normals[mask].unsqueeze(0)
        if with_new_target_points:
            results.new_target_points = new_points[mask].unsqueeze(0)

        return results

    # ------------------------------------------------------------------------------------------------------------------
    def get_last_frame(self) -> torch.Tensor:
        """Returns the last pointcloud registered to the local map"""
        return projection_map_to_points(self._local_map[-1], dim=0)


# ----------------------------------------------------------------------------------------------------------------------
@dataclass
class KdTreeLocalMapConfig(LocalMapConfig):
    """
    A KdTree-based Local Map
    """
    local_map_size: int = 20
    num_neighbors_normals: int = 10  # The number of neighbors for the normal computation
    type: str = "kdtree_local_map"


class KdTreeLocalMap(LocalMap):
    """
    A Local Map which computes neighborhood by projective association

    The local map keeps in memory the last N processed Frames
    """

    def __init__(self, config: LocalMapConfig, **kwargs):
        super().__init__(config)

        # ---------------------------------
        # Local map tensors saved
        self._local_map: np.ndarray = None
        self._local_map_num_elements: list = []

        self._model_points: np.ndarray = None
        self._model_kdtree: KDTree = None
        self._model_normals: np.ndarray = None
        self._old_normals = None
        self._old_model = None
        self.__k_normals = self.config.num_neighbors_normals

    # ------------------------------------------------------------------------------------------------------------------
    def init(self):
        self._local_map = None
        self._local_map_num_elements = []

        self._model_points = None
        self._model_normals = None
        self._model_kdtree = None

        self._old_normals = None
        self._old_model = None

    # ------------------------------------------------------------------------------------------------------------------
    def set_map_pointcloud(self, pointcloud: np.ndarray, normals: Optional[np.ndarray] = None):
        """
        Builds a new map from the aggregated pointcloud `pointcloud`
        """
        check_tensor(pointcloud, [-1, 3], np.ndarray)
        self.init()
        self._local_map = pointcloud
        self.build_model()
        if normals is not None:
            check_tensor(normals, [*pointcloud.shape], np.ndarray)
            self._model_normals = normals

    # ------------------------------------------------------------------------------------------------------------------
    def update(self,
               relative_pose: TensorType,
               new_pc_data: Optional[TensorType] = None,
               new_vertex_map: Optional[torch.Tensor] = None,
               **kwargs):
        """
        Updates the local map and (registers a new frame into it)
        """
        numpy_pc = None
        num_elements = 0

        if new_pc_data is not None:
            if isinstance(new_pc_data, torch.Tensor):
                numpy_pc = new_pc_data.reshape(-1, 3).cpu().numpy()
            elif isinstance(new_pc_data, np.ndarray):
                numpy_pc = new_pc_data.reshape(-1, 3)
            else:
                raise ValueError("Expected a numpy.ndarray or a torch.Tensor")
        elif new_vertex_map is not None:
            check_tensor(new_vertex_map, [1, 3, -1, -1])
            _, _, h, w = new_vertex_map.shape
            torch_pc = new_vertex_map[0].permute(1, 2, 0).view(1, -1, 3)
            numpy_pc = torch_pc[torch_pc.norm(dim=-1) > 0.01].cpu().numpy()

        if numpy_pc is not None:
            numpy_pc, _filter = remove_nan(numpy_pc)
            num_elements = numpy_pc.shape[0]

        if isinstance(relative_pose, torch.Tensor):
            check_tensor(relative_pose, [1, 4, 4])
            relative_pose = relative_pose[0].cpu().numpy()
        check_tensor(relative_pose, [4, 4], np.ndarray)

        if self._local_map is None:
            # Initialize the Map
            self._local_map = numpy_pc
            self._local_map_num_elements.append(num_elements)
        else:
            self._old_model = self._model_points
            self._old_normals = self._model_normals
            self._model_points = None
            self._model_normals = None

            # Shift local map to the last entry CS
            relative_pose_i = np.linalg.inv(relative_pose)
            transformed_map = np.einsum("ij,nj->ni",
                                        relative_pose_i[:3, :3], self._local_map) + relative_pose_i[:3, 3].reshape(1, 3)

            if numpy_pc is not None:
                self._local_map = np.concatenate([transformed_map, numpy_pc], axis=0)
                self._local_map_num_elements.append(num_elements)
            else:
                self._local_map = transformed_map

            if len(self._local_map_num_elements) > self.config.local_map_size:
                size_first_cloud = self._local_map_num_elements.pop(0)

                # Suppress a pointcloud if it
                self._local_map = self._local_map[size_first_cloud:]

        self.build_model()

    # ------------------------------------------------------------------------------------------------------------------
    def build_model(self):
        """Builds the KdTree and initialize the computation of the normals"""
        self._model_points = self._local_map
        self._model_normals = np.zeros((self._model_points.shape[0], 4), dtype=np.float32)
        self._model_kdtree = KDTree(self._model_points)

    # ------------------------------------------------------------------------------------------------------------------
    def nearest_neighbor_search(self, target_points: TensorType,
                                with_normals: bool = True,
                                with_new_target_points: bool = True, **kwargs) -> LocalMap.NeighborhoodResult:
        """
        Returns the nearest neighbors, and associated normals computed with a KdTree
        """
        is_torch = isinstance(target_points, torch.Tensor)
        if is_torch:
            numpy_points = target_points.cpu().numpy()
        else:
            numpy_points = target_points
        check_tensor(numpy_points, [-1, 3], np.ndarray)

        distances, indices = self._model_kdtree.query(numpy_points)
        neighbors = self._model_points[indices[:]]

        result = self.NeighborhoodResult()
        result.neighbor_points = torch.from_numpy(neighbors).unsqueeze(0) if is_torch else neighbors
        if with_normals:
            normals = self.__get_normals(indices[:])
            result.neighbor_normals = torch.from_numpy(normals).unsqueeze(0) if is_torch else normals
        if with_new_target_points:
            result.new_target_points = target_points.reshape(1, neighbors.shape[0], 3) if is_torch else target_points
        return result

    def __get_normals(self, indices):
        # Compute normals for points whose normals is not already computed
        normals_to_compute = self._model_normals[indices, 3] == 0.0
        to_compute_indices = indices[normals_to_compute]
        if to_compute_indices.shape[0] > 0:
            map_points = self._model_points[to_compute_indices]
            num_points = map_points.shape[0]
            # Compute their neighborhood
            _, map_neighbors_indices = self._model_kdtree.query(map_points, k=self.__k_normals + 1)

            map_neighbors_indices = map_neighbors_indices[:, 1:]
            map_neighbors = self._model_points[map_neighbors_indices.flatten()].reshape(num_points,
                                                                                        self.__k_normals, 3)

            centered = (map_neighbors - map_points.reshape(num_points, 1, 3))
            covs = (centered.reshape(num_points, self.__k_normals, 3, 1) * \
                    centered.reshape(num_points, self.__k_normals, 1, 3)).mean(axis=1)
            u, s, vh = np.linalg.svd(covs)
            # The normal is the direction vector of the least significant value
            normals = vh[:, 2, :3]

            self._model_normals[to_compute_indices, :3] = normals
            self._model_normals[to_compute_indices, 3] = 1.0

        normals = self._model_normals[indices, :3]
        return normals

    # ------------------------------------------------------------------------------------------------------------------
    def get_last_frame(self) -> torch.Tensor:
        """Returns the last pointcloud registered to the local map"""
        return torch.from_numpy(self._local_map[-self._local_map_num_elements[-1]:])


# ----------------------------------------------------------------------------------------------------------------------
# Hydra Group odometry/local_map definition
cs = ConfigStore.instance()
cs.store(group="slam/odometry/local_map", name="projective", node=ProjectiveLocalMapConfig)
cs.store(group="slam/odometry/local_map", name="kdtree", node=KdTreeLocalMapConfig)


class LOCAL_MAP(ObjectLoaderEnum, Enum):
    """Convenient Enum to load LocalMap from configuration"""

    projective_local_map = (ProjectiveLocalMap, ProjectiveLocalMapConfig)
    kdtree_local_map = (KdTreeLocalMap, KdTreeLocalMapConfig)

    @classmethod
    def type_name(cls):
        return "type"
