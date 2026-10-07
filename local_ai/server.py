"""Loopback-only inference service. Run with the separate model environment."""
import base64
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
import json
from PIL import Image
from runtime import Runtime

runtime = Runtime()


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path != '/health':
            return self.reply(404, {'error': 'Not found'})
        self.reply(200, {'status': 'ready', 'loaded_model': runtime.loaded})

    def do_POST(self):
        if self.path != '/generate':
            return self.reply(404, {'error': 'Not found'})
        # Browsers cannot call this endpoint cross-origin. Django calls it server-side.
        if self.headers.get('Origin'):
            return self.reply(403, {'error': 'Browser requests are not supported'})
        try:
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length <= 24*1024*1024:
                return self.reply(413, {'error': 'Invalid request size'})
            request = json.loads(self.rfile.read(length))
            prompt = request['prompt']
            if not isinstance(prompt, str) or len(prompt) > 80000:
                raise ValueError('Invalid prompt')
            tokens = int(request.get('max_tokens', 1600))
            if not 1 <= tokens <= 4096:
                raise ValueError('Invalid token limit')
            image = None
            if request.get('image'):
                image = Image.open(BytesIO(base64.b64decode(request['image'], validate=True)))
                if image.width*image.height > 12000000:
                    raise ValueError('Image too large')
                image.load()
            result = runtime.generate(request.get('model', 'qwen'), prompt, image, tokens)
            self.reply(200, result)
        except Exception as exc:
            # Do not expose document data or prompts in logs or error messages.
            print('Inference failed:', type(exc).__name__, flush=True)
            self.reply(503, {'error': 'Local inference failed. Check model installation and GPU memory.'})

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    print('Local AI listening on 127.0.0.1:8011', flush=True)
    HTTPServer(('127.0.0.1', 8011), Handler).serve_forever()
