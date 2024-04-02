"Model that includes semantic support as well as depth supervision"
from dataclasses import dataclass, field
from typing import Dict, List, Literal, Tuple, Type

import numpy as np
import torch

from nerfstudio.model_components.losses import DepthLossType, depth_loss, depth_ranking_loss
from nerfstudio.models.nerfacto import NerfactoModelConfig
from nerfstudio.cameras.rays import RayBundle
from nerfstudio.utils import colormaps
from nerfstudio.model_components.renderers import SemanticRenderer

from semantic_nerfacto.semantic_nerfacto import SemanticNerfactoModel

@dataclass
class SemanticDepthNerfactoModelConfig(NerfactoModelConfig):
    # Combine configurations of both models
    _target: Type = field(default_factory=lambda: SemanticDepthNerfactoModel)
    depth_loss_mult: float = 1e-3
    is_euclidean_depth: bool = False
    depth_sigma: float = 0.01
    should_decay_sigma: bool = False
    starting_depth_sigma: float = 0.2
    sigma_decay_rate: float = 0.99985
    depth_loss_type: DepthLossType = DepthLossType.DS_NERF
    use_appearance_embedding: bool = True
    """Whether to use appearance embeddings. Throws error if not included"""
    average_init_density: float = 1.0
    semantic_loss_weight: float = 1.0
    pass_semantic_gradients: bool = False

class SemanticDepthNerfactoModel(SemanticNerfactoModel):
    config: SemanticDepthNerfactoModelConfig

    def __init__(self, config: SemanticDepthNerfactoModelConfig, metadata: Dict, **kwargs) -> None:
        super().__init__(config, metadata, **kwargs)
        if self.config.should_decay_sigma:
            self.depth_sigma = torch.tensor([self.config.starting_depth_sigma])
        else:
            self.depth_sigma = torch.tensor([self.config.depth_sigma])


    def get_outputs(self, ray_bundle: RayBundle):
        outputs = super().get_outputs(ray_bundle)  # Get semantic outputs
        assert "semantics" in outputs and "semantics_colormap" in outputs, "No semantics in superclass output!"
        # If depth supervision is applicable, add depth-related outputs
        if ray_bundle.metadata is not None and "directions_norm" in ray_bundle.metadata:
            outputs["directions_norm"] = ray_bundle.metadata["directions_norm"]

        return outputs

    def get_metrics_dict(self, outputs, batch):
        metrics_dict = super().get_metrics_dict(outputs, batch)  # Get semantic metrics
        
        # Add depth-related metrics if depth images are in the batch
        if self.training and "depth_image" in batch:
            depth_image = batch["depth_image"].to(self.device)
            if self.config.depth_loss_type in (DepthLossType.DS_NERF, DepthLossType.URF):
                sigma = self._get_sigma().to(self.device)
                depth_loss_value = 0
                for i in range(len(outputs["weights_list"])):
                    depth_loss_value += depth_loss(
                        weights=outputs["weights_list"][i],
                        ray_samples=outputs["ray_samples_list"][i],
                        termination_depth=depth_image,
                        predicted_depth=outputs["depth"],
                        sigma=sigma,
                        directions_norm=outputs.get("directions_norm"),
                        is_euclidean=self.config.is_euclidean_depth,
                        depth_loss_type=self.config.depth_loss_type,
                    )
                metrics_dict["depth_loss"] = depth_loss_value / len(outputs["weights_list"])
            elif self.config.depth_loss_type == DepthLossType.SPARSENERF_RANKING:
                metrics_dict["depth_ranking"] = depth_ranking_loss(
                    outputs["expected_depth"], depth_image
                )

        return metrics_dict

    def get_loss_dict(self, outputs, batch, metrics_dict=None):
        loss_dict = super().get_loss_dict(outputs, batch, metrics_dict)  # Get semantic losses
        assert "semantics_loss" in loss_dict, "No semantic loss in loss_dict!"
        # Add depth-related losses if depth images are in the batch
        if self.training and "depth_image" in batch:
            assert metrics_dict is not None and ("depth_loss" in metrics_dict or "depth_ranking" in metrics_dict)
            if "depth_loss" in metrics_dict:
                loss_dict["depth_loss"] = self.config.depth_loss_mult * metrics_dict["depth_loss"]
            if "depth_ranking" in metrics_dict:
                loss_dict["depth_ranking"] = (
                    self.config.depth_loss_mult
                    * metrics_dict["depth_ranking"]
                    * np.interp(self.step, [0, 2000], [0, 0.2])
                )
        return loss_dict
    
    def get_image_metrics_and_images(
        self, outputs: Dict[str, torch.Tensor], batch: Dict[str, torch.Tensor]
    ) -> Tuple[Dict[str, float], Dict[str, torch.Tensor]]:
        
        metrics, images = super().get_image_metrics_and_images(outputs, batch)
        assert "semantics_colormap" in images, "No semantics_colormap in images dict!"
        
        """Appends ground truth depth to the depth image."""
        ground_truth_depth = batch["depth_image"].to(self.device)
        if not self.config.is_euclidean_depth:
            ground_truth_depth = ground_truth_depth * outputs["directions_norm"]

        ground_truth_depth_colormap = colormaps.apply_depth_colormap(ground_truth_depth)
        predicted_depth_colormap = colormaps.apply_depth_colormap(
            outputs["depth"],
            accumulation=outputs["accumulation"],
            near_plane=float(torch.min(ground_truth_depth).cpu()),
            far_plane=float(torch.max(ground_truth_depth).cpu()),
        )
        images["depth"] = torch.cat([ground_truth_depth_colormap, predicted_depth_colormap], dim=1)
        depth_mask = ground_truth_depth > 0
        metrics["depth_mse"] = float(
            torch.nn.functional.mse_loss(outputs["depth"][depth_mask], ground_truth_depth[depth_mask]).cpu()
        )
        return metrics, images

    def _get_sigma(self):
        if not self.config.should_decay_sigma:
            return self.depth_sigma
        self.depth_sigma = torch.maximum(
            self.config.sigma_decay_rate * self.depth_sigma, torch.tensor([self.config.depth_sigma])
        )
        return self.depth_sigma