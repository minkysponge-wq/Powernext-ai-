"""Local-only model runtime. No reference answers or application database access."""
import gc
import json
import os
from pathlib import Path
import time

os.environ.setdefault('HF_HOME', str(Path(__file__).resolve().parent / 'cache'))
MODELS = {'qwen': 'Qwen/Qwen3-VL-2B-Instruct', 'paddle': 'PaddlePaddle/PaddleOCR-VL-1.6'}


class Runtime:
    def __init__(self):
        self.model = self.processor = self.loaded = None

    def load(self, name):
        import torch
        from transformers import AutoProcessor, AutoModelForImageTextToText
        if name not in MODELS:
            raise ValueError('Unsupported local model')
        if self.loaded == name:
            return
        self.loaded = None
        self.model = self.processor = None
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        manifest = json.loads(Path(__file__).with_name('models.json').read_text())
        revision = manifest[name]['revision']
        self.processor = AutoProcessor.from_pretrained(MODELS[name], revision=revision, local_files_only=True)
        self.model = AutoModelForImageTextToText.from_pretrained(
            MODELS[name], revision=revision, local_files_only=True,
            dtype=torch.bfloat16 if torch.cuda.is_available() else torch.float32,
            attn_implementation='sdpa').to('cuda' if torch.cuda.is_available() else 'cpu').eval()
        self.loaded = name
        self.revision = revision

    def generate(self, name, prompt, image=None, max_tokens=1600):
        import torch
        self.load(name)
        content = []
        if image is not None:
            content.append({'type': 'image', 'image': image.convert('RGB')})
        content.append({'type': 'text', 'text': prompt})
        kwargs = {}
        if image is not None:
            if name != 'paddle':
                kwargs['images_kwargs'] = {'max_pixels': 786432}
        inputs = self.processor.apply_chat_template([{'role': 'user', 'content': content}],
            tokenize=True, add_generation_prompt=True, return_dict=True, return_tensors='pt', **kwargs).to(self.model.device)
        started = time.perf_counter()
        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
        with torch.inference_mode():
            outputs = self.model.generate(**inputs, max_new_tokens=max_tokens, do_sample=False)
        generated = outputs[0][inputs['input_ids'].shape[-1]:]
        text = self.processor.decode(generated, skip_special_tokens=True)
        return {'text': text, 'model': MODELS[name], 'revision': self.revision,
                'seconds': time.perf_counter() - started, 'tokens': len(generated),
                'truncated': len(generated) >= max_tokens,
                'peak_gpu_mb': torch.cuda.max_memory_allocated()/1024**2 if torch.cuda.is_available() else 0}
