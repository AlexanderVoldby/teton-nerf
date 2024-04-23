# Setup detectron2 logger
from detectron2.utils.logger import setup_logger
setup_logger()

# import some common detectron2 utilities
from detectron2 import model_zoo
from detectron2.engine import DefaultPredictor
from detectron2.config import get_cfg
from detectron2.utils.visualizer import Visualizer
from detectron2.data import MetadataCatalog

import os
import json
import cv2
import glob
import click
from pathlib import Path


class SemanticSegmentor():
    
    def __init__(self):
    
        self.cfg = get_cfg()
        self.cfg.merge_from_file(model_zoo.get_config_file("COCO-PanopticSegmentation/panoptic_fpn_R_101_3x.yaml"))
        self.cfg.MODEL.WEIGHTS = model_zoo.get_checkpoint_url("COCO-PanopticSegmentation/panoptic_fpn_R_101_3x.yaml")
        
        # Reduce the number of classes the model is using
        expected_things = ["chair", "couch", "bed", "dining table", "toilet", "tv"]
        
        expected_stuff = ["blanket","curtain", "door-stuff", "floor-wood",
                          "mirror-stuff", "pillow", "shelf", "stairs",
                          "wall-brick", "table", "window", "ceiling", "floor", "wall", "rug"]
        
        self.metadata = MetadataCatalog.get(self.cfg.DATASETS.TRAIN[0])
        self.predictor = DefaultPredictor(self.cfg)
        self.num_things = len(self.metadata.thing_classes)
        self.num_stuff = len(self.metadata.stuff_classes)
        
    def predict(self, image):
        panoptic_seg, segments_info = self.predictor(image)["panoptic_seg"]
        # We are doing semantic segmentation so we simply want to map the panoptic ID to a class ID:
        panoptic_seg = panoptic_seg.cpu().numpy()
        semantic_seg = panoptic_seg.copy()
        for info in segments_info:
            try:
                if info["isthing"]:
                    semantic_seg[semantic_seg == info["id"]] = info["category_id"]
                else:
                    semantic_seg[semantic_seg == info["id"]] = info["category_id"] + self.num_things
            except Exception as e:
                print(f"Error: {e}")
        return semantic_seg, panoptic_seg, segments_info
    
    def visualize(self, image, panoptic_segmentation, segments_info, filename):
        v = Visualizer(image[:, :, ::-1], MetadataCatalog.get(self.cfg.DATASETS.TRAIN[0]), scale=1.2)
        out = v.draw_panoptic_seg_predictions(panoptic_segmentation.to("cpu"), segments_info)
        cv2.imwrite(f"{filename.strip('.png')}_segmentation.png", out.get_image()[:, :, ::-1])
        # cv2.imshow(out.get_image()[:, :, ::-1])
        
    def save_metadata(self, data):
        metadict = {
            "thing_classes": self.metadata.thing_classes,
            "stuff_classes": self.metadata.stuff_classes,
            "thing_colors": self.metadata.thing_colors,
            "stuff_colors": self.metadata.stuff_colors,
            "thing_dataset_id_to_contiguous_id": self.metadata.thing_dataset_id_to_contiguous_id,
            "stuff_dataset_id_to_contiguous_id": self.metadata.stuff_dataset_id_to_contiguous_id
        }
        
        json_file_path = f"{data}/panoptic_classes.json"

        # Save the metadata to a JSON file
        with open(json_file_path, 'w') as f:
            json.dump(metadict, f, indent=4)

        
    def add_segmentation(self, data):
        print("Generating semantics")
        # Find a way to get these from some metadata in the dataset
        image_folder_suffixes = ['', '_2', '_4', '_8']

        for suffix in image_folder_suffixes:
            # Construct the path to the current image folder
            image_folder_path = os.path.join(data, f'images{suffix}')

            # Create a new folder for the panoptic segmentations
            segmentation_folder_path = os.path.join(data, f'segmentations{suffix}')
            if not os.path.exists(segmentation_folder_path):
                os.makedirs(segmentation_folder_path)
            
            visualization_folder_path = os.path.join(data, "semantic_visualizations")
            if not os.path.exists(visualization_folder_path):
                os.makedirs(visualization_folder_path)

            # Find all image files in the current image folder and make segmentation
            image_files = glob.glob(os.path.join(image_folder_path, '*.*'))
            segmentation_filenames = []
            for image_file in image_files:
                image = cv2.imread(image_file)
                semantic_segmentation, panoptic_segmentation, segments_info = self.predict(image)
                base_name = os.path.basename(image_file).replace(".jpg", ".png")
                visualization_file_path = os.path.join(visualization_folder_path, base_name)
                segmentation_file_path = Path(segmentation_folder_path / base_name)
                segmentation_filenames.append(segmentation_file_path)
                cv2.imwrite(segmentation_file_path, semantic_segmentation)
                self.visualize(image, panoptic_segmentation, segments_info, visualization_file_path)

        # Add panoptic_classes.json to dataset
        self.save_metadata(data)
        
        return segmentation_filenames


@click.command()
@click.option("--data", help="Path to dataset")
def main(data):
    SS = SemanticSegmentor()
    SS.add_segmentation(data)
    

if __name__ == "__main__":
    main()
    