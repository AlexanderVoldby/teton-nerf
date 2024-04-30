"""Script that is meant to help us load frames and calculate pointclouds from them"""
from __future__ import annotations
import sys
from typing import Tuple, cast, Optional, TYPE_CHECKING

import numpy as np
import torch
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeRemainingColumn

from nerfstudio.cameras.rays import RayBundle
from nerfstudio.data.scene_box import OrientedBox
from nerfstudio.pipelines.base_pipeline import Pipeline
from nerfstudio.utils.rich_utils import CONSOLE


import numpy as np
import numpy.typing as npt
import skimage.transform
import torch

if TYPE_CHECKING:
    import open3d as o3d

def generate_pointcloud(dataset, transform, scale, downsample_factor=1):
    """
    Generate a 3D point cloud from RGB and depth images using intrinsic and extrinsic camera parameters.
    
    Args:
        rgb (numpy.ndarray): The RGB image array of shape (H, W, 3).
        depth (numpy.ndarray): The depth image array of shape (H, W).
        K (numpy.ndarray): The 3x3 camera intrinsic matrix.
        T_world_camera (numpy.ndarray): The 4x4 camera extrinsic matrix.
        mask (numpy.ndarray): A boolean mask of shape (H, W) to select relevant pixels.
        downsample_factor (int): Factor by which to downsample the pixel grid.
    
    Returns:
        numpy.ndarray: 3D points in world coordinates.
        numpy.ndarray: Corresponding colors from the RGB image.
    """
    
    points_and_colors = []
    
    for idx in range(len(dataset)):
        image_tensor = dataset[idx]["image"]  # Assuming this retrieves an RGB image tensor
        camera = dataset.cameras[idx]
        depth = dataset.depths[idx]  # Assuming a method that handles loading and any necessary scaling
        
        depth_np = depth.cpu().numpy().astype('float32') # * 1e3 # Scale to return to original NS scale
        image_np = image_tensor.cpu().numpy().astype(np.uint8)

        mask = np.zeros_like(depth_np, dtype=bool)
        mask[::10, ::10] = True

        rgb = image_tensor[::downsample_factor, ::downsample_factor]
        depth = skimage.transform.resize(depth_np, rgb.shape[:2], order=0)
        mask = cast(
            npt.NDArray[np.bool_],
            skimage.transform.resize(mask, rgb.shape[:2], order=0),
        )
        assert depth.shape == rgb.shape[:2]
        
        K = np.array([
            [camera.fx.item(), 0, camera.cx.item()],
            [0, camera.fy.item(), camera.cy.item()],
            [0, 0, 1]
        ])
        print(f"K: {K}")
        c2w_train = camera.camera_to_worlds.cpu().numpy()
        print(c2w_train)
    
        # Camera transforms are transformed for Nerfstudio optimization so we need to get them back
        # Transformatins are applied to go from training data to camera world frame so we invert the tranformation

        # c2w_train[:3, 3] = c2w_train[:3, 3] / scale
        # print(c2w_train)
        # Rotation part is the identity so let's just apply scaling for now
        # c2w = np.linalg.inv(transform) @ c2w_train
        
        # Get image dimensions and adjust by the downsample factor
        img_wh = image_np.shape[:2][::-1]
        img_wh = tuple(dim // downsample_factor for dim in img_wh)
        
        grid = (
            np.stack(np.meshgrid(np.arange(img_wh[0]), np.arange(img_wh[1])), 2) + 0.5
        )
        grid = grid * downsample_factor

        homo_grid = np.pad(grid[mask], np.array([[0, 0], [0, 1]]), constant_values=1)
        local_dirs = np.einsum("ij,bj->bi", np.linalg.inv(K), homo_grid)
        dirs = np.einsum("ij,bj->bi", c2w_train[:3, :3], local_dirs)
        points = (c2w_train[:, -1] + dirs * depth[mask, None]).astype(np.float32)
        point_colors = rgb[mask]
        
        points_and_colors.append((points, point_colors))

    return points_and_colors

def generate_pointcloud_advanced(dataset, downsample_factor=1):
    """
    Generate a 3D point cloud from RGB and depth images using intrinsic and extrinsic camera parameters.
    
    Args:
        rgb (numpy.ndarray): The RGB image array of shape (H, W, 3).
        depth (numpy.ndarray): The depth image array of shape (H, W).
        K (numpy.ndarray): The 3x3 camera intrinsic matrix.
        T_world_camera (numpy.ndarray): The 4x4 camera extrinsic matrix.
        mask (numpy.ndarray): A boolean mask of shape (H, W) to select relevant pixels.
        downsample_factor (int): Factor by which to downsample the pixel grid.
    
    Returns:
        numpy.ndarray: 3D points in world coordinates.
        numpy.ndarray: Corresponding colors from the RGB image.
    """
    
    points_and_colors = []
    
    for idx in range(len(dataset)):
        image_tensor = dataset[idx]["image"]  # Assuming this retrieves an RGB image tensor
        camera = dataset.cameras[idx]
        depth = dataset.depths[idx]  # Assuming a method that handles loading and any necessary scaling
        
        depth_np = depth.cpu().numpy().astype('float32') # * 1e3 # Scale to return to original NS scale
        image_np = image_tensor.cpu().numpy().astype(np.uint8) # .transpose(1, 2, 0)  # Correctly orient the image

        mask = np.zeros_like(depth_np, dtype=bool)
        mask[::10, ::10] = True
        print(f"Shape of depth: {depth_np.shape}, Shape of mask: {mask.shape}")
        
        K = np.array([
            [camera.fx.item(), 0, camera.cx.item()],
            [0, camera.fy.item(), camera.cy.item()],
            [0, 0, 1]
        ])
        T_world_camera = camera.camera_to_worlds.cpu().numpy()
    
        # Get image dimensions and adjust by the downsample factor
        img_wh = image_np.shape[:2][::-1]
        img_wh = tuple(dim // downsample_factor for dim in img_wh)
        
        # Generate a grid of (x, y) pixel coordinates
        grid = np.stack(np.meshgrid(np.arange(img_wh[0]), np.arange(img_wh[1]), indexing='xy'), axis=-1) + 0.5
        grid *= downsample_factor

        masked_grid = grid[mask]
        homo_grid = np.hstack([masked_grid, np.ones((masked_grid.shape[0], 1))])

        local_dirs = np.einsum('ij,nj->ni', np.linalg.inv(K), homo_grid)
        points_camera = local_dirs * depth_np[mask].reshape(-1, 1)

        points_world = np.einsum('ij,nj->ni', T_world_camera[:3, :3], points_camera) + T_world_camera[:3, -1]
        point_colors = image_np[mask]
        
        points_and_colors.append((points_world, point_colors))

    return points_and_colors

def generate_point_cloud(
    pipeline: Pipeline,
    num_points: int = 1000000,
    remove_outliers: bool = True,
    estimate_normals: bool = False,
    reorient_normals: bool = False,
    rgb_output_name: str = "rgb",
    depth_output_name: str = "depth",
    normal_output_name: Optional[str] = None,
    use_bounding_box: bool = True,
    bounding_box_min: Optional[Tuple[float, float, float]] = None,
    bounding_box_max: Optional[Tuple[float, float, float]] = None,
    crop_obb: Optional[OrientedBox] = None,
    std_ratio: float = 10.0,
) -> o3d.geometry.PointCloud:
    """Generate a point cloud from a nerf.

    Args:
        pipeline: Pipeline to evaluate with.
        num_points: Number of points to generate. May result in less if outlier removal is used.
        remove_outliers: Whether to remove outliers.
        reorient_normals: Whether to re-orient the normals based on the view direction.
        estimate_normals: Whether to estimate normals.
        rgb_output_name: Name of the RGB output.
        depth_output_name: Name of the depth output.
        normal_output_name: Name of the normal output.
        use_bounding_box: Whether to use a bounding box to sample points.
        bounding_box_min: Minimum of the bounding box.
        bounding_box_max: Maximum of the bounding box.
        std_ratio: Threshold based on STD of the average distances across the point cloud to remove outliers.

    Returns:
        Point cloud.
    """

    progress = Progress(
        TextColumn(":cloud: Computing Point Cloud :cloud:"),
        BarColumn(),
        TaskProgressColumn(show_speed=True),
        TimeRemainingColumn(elapsed_when_finished=True, compact=True),
        console=CONSOLE,
    )
    points = []
    rgbs = []
    normals = []
    view_directions = []
    if use_bounding_box and (crop_obb is not None and bounding_box_max is not None):
        CONSOLE.print("Provided aabb and crop_obb at the same time, using only the obb", style="bold yellow")
    with progress as progress_bar:
        task = progress_bar.add_task("Generating Point Cloud", total=num_points)
        while not progress_bar.finished:
            normal = None

            with torch.no_grad():
                ray_bundle, _ = pipeline.datamanager.next_train(0)
                assert isinstance(ray_bundle, RayBundle)
                outputs = pipeline.model(ray_bundle)
            if rgb_output_name not in outputs:
                CONSOLE.rule("Error", style="red")
                CONSOLE.print(f"Could not find {rgb_output_name} in the model outputs", justify="center")
                CONSOLE.print(f"Please set --rgb_output_name to one of: {outputs.keys()}", justify="center")
                sys.exit(1)
            if depth_output_name not in outputs:
                CONSOLE.rule("Error", style="red")
                CONSOLE.print(f"Could not find {depth_output_name} in the model outputs", justify="center")
                CONSOLE.print(f"Please set --depth_output_name to one of: {outputs.keys()}", justify="center")
                sys.exit(1)
            rgba = pipeline.model.get_rgba_image(outputs, rgb_output_name)
            depth = outputs[depth_output_name]
            print(f"Median depth value used for scaling pointcloud {torch.median(depth)}")
            if normal_output_name is not None:
                if normal_output_name not in outputs:
                    CONSOLE.rule("Error", style="red")
                    CONSOLE.print(f"Could not find {normal_output_name} in the model outputs", justify="center")
                    CONSOLE.print(f"Please set --normal_output_name to one of: {outputs.keys()}", justify="center")
                    sys.exit(1)
                normal = outputs[normal_output_name]
                assert (
                    torch.min(normal) >= 0.0 and torch.max(normal) <= 1.0
                ), "Normal values from method output must be in [0, 1]"
                normal = (normal * 2.0) - 1.0
            point = ray_bundle.origins + ray_bundle.directions * depth
            view_direction = ray_bundle.directions

            # Filter points with opacity lower than 0.5
            mask = rgba[..., -1] > 0.5
            point = point[mask]
            view_direction = view_direction[mask]
            rgb = rgba[mask][..., :3]
            if normal is not None:
                normal = normal[mask]

            if use_bounding_box:
                if crop_obb is None:
                    comp_l = torch.tensor(bounding_box_min, device=point.device)
                    comp_m = torch.tensor(bounding_box_max, device=point.device)
                    assert torch.all(
                        comp_l < comp_m
                    ), f"Bounding box min {bounding_box_min} must be smaller than max {bounding_box_max}"
                    mask = torch.all(torch.concat([point > comp_l, point < comp_m], dim=-1), dim=-1)
                else:
                    mask = crop_obb.within(point)
                point = point[mask]
                rgb = rgb[mask]
                view_direction = view_direction[mask]
                if normal is not None:
                    normal = normal[mask]

            points.append(point)
            rgbs.append(rgb)
            view_directions.append(view_direction)
            if normal is not None:
                normals.append(normal)
            progress.advance(task, point.shape[0])
    points = torch.cat(points, dim=0)
    rgbs = torch.cat(rgbs, dim=0)
    view_directions = torch.cat(view_directions, dim=0).cpu()

    import open3d as o3d

    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(points.double().cpu().numpy())
    pcd.colors = o3d.utility.Vector3dVector(rgbs.double().cpu().numpy())

    ind = None
    if remove_outliers:
        CONSOLE.print("Cleaning Point Cloud")
        pcd, ind = pcd.remove_statistical_outlier(nb_neighbors=20, std_ratio=std_ratio)
        print("\033[A\033[A")
        CONSOLE.print("[bold green]:white_check_mark: Cleaning Point Cloud")
        if ind is not None:
            view_directions = view_directions[ind]

    # either estimate_normals or normal_output_name, not both
    if estimate_normals:
        if normal_output_name is not None:
            CONSOLE.rule("Error", style="red")
            CONSOLE.print("Cannot estimate normals and use normal_output_name at the same time", justify="center")
            sys.exit(1)
        CONSOLE.print("Estimating Point Cloud Normals")
        pcd.estimate_normals()
        print("\033[A\033[A")
        CONSOLE.print("[bold green]:white_check_mark: Estimating Point Cloud Normals")
    elif normal_output_name is not None:
        normals = torch.cat(normals, dim=0)
        if ind is not None:
            # mask out normals for points that were removed with remove_outliers
            normals = normals[ind]
        pcd.normals = o3d.utility.Vector3dVector(normals.double().cpu().numpy())

    # re-orient the normals
    if reorient_normals:
        normals = torch.from_numpy(np.array(pcd.normals)).float()
        mask = torch.sum(view_directions * normals, dim=-1) > 0
        normals[mask] *= -1
        pcd.normals = o3d.utility.Vector3dVector(normals.double().cpu().numpy())

    return pcd