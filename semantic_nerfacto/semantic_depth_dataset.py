from typing import Dict, Union
import json
import numpy as np
import torch
from PIL import Image
from pathlib import Path
from transformers import AutoImageProcessor, AutoModelForDepthEstimation
from rich.progress import track

from nerfstudio.data.dataparsers.base_dataparser import DataparserOutputs, Semantics
from nerfstudio.data.datasets.base_dataset import InputDataset
from nerfstudio.data.utils.data_utils import get_semantics_and_mask_tensors_from_path, get_depth_image_from_path
from nerfstudio.model_components import losses
from nerfstudio.utils.misc import torch_compile
from nerfstudio.utils.rich_utils import CONSOLE


class SemanticDepthDataset(InputDataset):
    exclude_batch_keys_from_device = InputDataset.exclude_batch_keys_from_device + ["mask", "semantics", "depth_image"]

    def __init__(self, dataparser_outputs: DataparserOutputs, scale_factor: float = 1.0):
        super().__init__(dataparser_outputs, scale_factor)
        assert "semantics" in dataparser_outputs.metadata.keys() and isinstance(self.metadata["semantics"], Semantics)
        self.semantics = self.metadata["semantics"]
        self.mask_indices = torch.tensor(
            [self.semantics.classes.index(mask_class) for mask_class in self.semantics.mask_classes]
        ).view(1, 1, -1)

        # Depth image handling
        # TODO if depth images already exist from LiDAR, extend them with pretrained model
        self.depth_filenames = self.metadata.get("depth_filenames")
        self.depth_unit_scale_factor = self.metadata.get("depth_unit_scale_factor", 1.0)
        # if not self.depth_filenames:
        # Currently always generate depth as LiDAR depth is sparse
        self._generate_depth_images(dataparser_outputs)

    def _generate_depth_images(self, dataparser_outputs: DataparserOutputs):
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        cache = dataparser_outputs.image_filenames[0].parent / "depths.npy"
        if cache.exists():
            CONSOLE.print("[bold yellow] Loading pseudodata depth from cache!")
            self.depths = torch.from_numpy(np.load(cache)).to(device)
        else:
            CONSOLE.print("[bold yellow] No depth data found! Generating pseudodepth...")
            losses.FORCE_PSEUDODEPTH_LOSS = True
            depth_tensors = []
            repo = "LiheYoung/depth-anything-small-hf"
            image_processor = AutoImageProcessor.from_pretrained(repo)
            model = AutoModelForDepthEstimation.from_pretrained(repo)
            for image_filename in track(dataparser_outputs.image_filenames, description="Generating depth images"):
                pil_image = Image.open(image_filename)
                inputs = image_processor(images=pil_image, return_tensors="pt")
                with torch.no_grad():
                    outputs = model(**inputs)
                    predicted_depth = outputs.predicted_depth
                depth_tensors.append(predicted_depth)
            self.depths = torch.stack(depth_tensors)
            np.save(cache, self.depths.cpu().numpy())
            self.depth_filenames = None

    def get_metadata(self, data: Dict) -> Dict:
        
        # handle mask
        filepath = self.semantics.filenames[data["image_idx"]]
        semantic_label, mask = get_semantics_and_mask_tensors_from_path(
            filepath=filepath, mask_indices=self.mask_indices, scale_factor=self.scale_factor
        )
        if "mask" in data.keys():
            mask = mask & data["mask"]
        
        # Handle depth stuff
        image_idx = data["image_idx"]
        if self.depth_filenames is None:
            depth_image = self.depths[data[image_idx]]
        else:
            filepath = self.depth_filenames[data[image_idx]]
            height = int(self._dataparser_outputs.cameras.height[data[image_idx]])
            width = int(self._dataparser_outputs.cameras.width[data[image_idx]])
            scale_factor = self.depth_unit_scale_factor * self.scale_factor
            depth_image = get_depth_image_from_path(
                filepath=filepath, height=height, width=width, scale_factor=scale_factor
            )
        return {"mask": mask, "semantics": semantic_label, "depth_image": depth_image}
    
    def _find_transform(self, image_path: Path) -> Union[Path, None]:
        while image_path.parent != image_path:
            transform_path = image_path.parent / "transforms.json"
            if transform_path.exists():
                return transform_path
            image_path = image_path.parent
        return None