from pathlib import Path
import re

import peft
import torch
from torch import nn
import safetensors.torch
import torchvision
from PIL import Image, ImageOps
from torchvision import transforms
import imageio

from utils.common import is_main_process, VIDEO_EXTENSIONS, round_to_nearest_multiple, round_down_to_multiple


def make_contiguous(*tensors):
    return tuple(x.contiguous() for x in tensors)


def extract_clips(video, target_frames, video_clip_mode):
    # video is (channels, num_frames, height, width)
    frames = video.shape[1]
    if frames < target_frames:
        # TODO: think about how to handle this case. Maybe the video should have already been thrown out?
        print(f'video with shape {video.shape} is being skipped because it has less than the target_frames')
        return []

    if video_clip_mode == 'single_beginning':
        return [video[:, :target_frames, ...]]
    elif video_clip_mode == 'single_middle':
        start = int((frames - target_frames) / 2)
        assert frames-start >= target_frames
        return [video[:, start:start+target_frames, ...]]
    elif video_clip_mode == 'multiple_overlapping':
        # Extract multiple clips so we use the whole video for training.
        # The clips might overlap a little bit. We never cut anything off the end of the video.
        num_clips = ((frames - 1) // target_frames) + 1
        start_indices = torch.linspace(0, frames-target_frames, num_clips).int()
        return [video[:, i:i+target_frames, ...] for i in start_indices]
    else:
        raise NotImplementedError(f'video_clip_mode={video_clip_mode} is not recognized')


def convert_crop_and_resize(pil_img, width_and_height):
    if pil_img.mode not in ['RGB', 'RGBA'] and 'transparency' in pil_img.info:
        pil_img = pil_img.convert('RGBA')

    # add white background for transparent images
    if pil_img.mode == 'RGBA':
        canvas = Image.new('RGBA', pil_img.size, (255, 255, 255))
        canvas.alpha_composite(pil_img)
        pil_img = canvas.convert('RGB')
    else:
        pil_img = pil_img.convert('RGB')

    return ImageOps.fit(pil_img, width_and_height)


class PreprocessMediaFile:
    def __init__(self, config, support_video=False, framerate=None, round_height=1, round_width=1, round_frames=1):
        self.config = config
        self.video_clip_mode = config.get('video_clip_mode', 'single_middle')
        print(f'using video_clip_mode={self.video_clip_mode}')
        self.pil_to_tensor = transforms.Compose([transforms.ToTensor(), transforms.Normalize([0.5], [0.5])])
        self.support_video = support_video
        self.framerate = framerate
        self.round_height = round_height
        self.round_width = round_width
        self.round_frames = round_frames
        if self.support_video:
            assert self.framerate

    def __call__(self, filepath, size_bucket):
        width, height, frames = size_bucket
        height_rounded = round_to_nearest_multiple(height, self.round_height)
        width_rounded = round_to_nearest_multiple(width, self.round_width)
        frames_rounded = round_down_to_multiple(frames - 1, self.round_frames) + 1

        is_video = (Path(filepath).suffix in VIDEO_EXTENSIONS)
        if is_video:
            assert self.support_video
            num_frames = 0
            for frame in imageio.v3.imiter(filepath, fps=self.framerate):
                num_frames += 1
            frames = imageio.v3.imiter(filepath, fps=self.framerate)
        else:
            num_frames = 1
            pil_img = Image.open(filepath)
            frames = [pil_img]

        video = torch.empty((num_frames, 3, height_rounded, width_rounded))
        for i, frame in enumerate(frames):
            if not isinstance(frame, Image.Image):
                frame = torchvision.transforms.functional.to_pil_image(frame)
            cropped_image = convert_crop_and_resize(frame, (width_rounded, height_rounded))
            video[i, ...] = self.pil_to_tensor(cropped_image)

        if not self.support_video:
            return [video.squeeze(0)]

        # (num_frames, channels, height, width) -> (channels, num_frames, height, width)
        video = torch.permute(video, (1, 0, 2, 3))
        if not is_video:
            return [video]
        else:
            return extract_clips(video, frames_rounded, self.video_clip_mode)


class BasePipeline:
    framerate = None

    def load_diffusion_model(self):
        pass

    def get_vae(self):
        raise NotImplementedError()

    def get_text_encoders(self):
        raise NotImplementedError()

    def configure_adapter(self, adapter_config):
        target_linear_modules = []
    
        # Get the flag from adapter config with False as default
        only_double_blocks = adapter_config.get('only_double_blocks', False)
    
        # Iterate through named modules to get full path names
        for name, module in self.transformer.named_modules():
            if module.__class__.__name__ not in self.adapter_target_modules:
                continue
            
            # Skip single blocks when only_double_blocks is True    
            if only_double_blocks and "single_blocks" in name:
                continue
            
            # Get all linear layers within the module
            for n, submodule in module.named_modules():
                if isinstance(submodule, nn.Linear):
                    print(f"Adding {name}.{n}")
                    target_linear_modules.append(f"{name}.{n}")

        adapter_type = adapter_config['type']
        if adapter_type == 'lora':
            peft_config = peft.LoraConfig(
                r=adapter_config['rank'],
                lora_alpha=adapter_config['alpha'],
                lora_dropout=adapter_config['dropout'],
                bias='none',
                target_modules=target_linear_modules
            )
        else:
            raise NotImplementedError(f'Adapter type {adapter_type} is not implemented')
        self.peft_config = peft_config
        self.lora_model = peft.get_peft_model(self.transformer, peft_config)
        #self.transformer.add_adapter(peft_config)
        if is_main_process():
            self.lora_model.print_trainable_parameters()
        for name, p in self.transformer.named_parameters():
            p.original_name = name
            if p.requires_grad:
                p.data = p.data.to(adapter_config['dtype'])
        return peft_config

    def save_adapter(self, save_dir, peft_state_dict):
        raise NotImplementedError()

    def load_adapter_weights(self, adapter_path):
        if is_main_process():
            print(f'Loading adapter weights from path {adapter_path}')
        adapter_state_dict = safetensors.torch.load_file(Path(adapter_path) / 'adapter_model.safetensors')
        modified_state_dict = {}
        model_parameters = set(name for name, p in self.transformer.named_parameters())
        for k, v in adapter_state_dict.items():
            # Replace Diffusers or ComfyUI prefix
            k = re.sub(r'^(transformer|diffusion_model)\.', '', k)
            # Replace weight at end for LoRA format
            k = re.sub(r'\.weight$', '.default.weight', k)
            if k not in model_parameters:
                raise RuntimeError(f'modified_state_dict key {k} is not in the model parameters')
            modified_state_dict[k] = v
        self.transformer.load_state_dict(modified_state_dict, strict=False)

    def save_model(self, save_dir, diffusers_sd):
        raise NotImplementedError()

    def get_preprocess_media_file_fn(self):
        return PreprocessMediaFile(self.config, support_video=False)

    def get_call_vae_fn(self, vae):
        raise NotImplementedError()

    def get_call_text_encoder_fn(self, text_encoder):
        raise NotImplementedError()

    def prepare_inputs(self, inputs, timestep_quantile=None):
        raise NotImplementedError()

    def to_layers(self):
        raise NotImplementedError()
