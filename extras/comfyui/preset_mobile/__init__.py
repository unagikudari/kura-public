"""preset_mobile: a one-column phone page for the character-select nodes (owner 2026-11-16).

Open  http(s)://<this ComfyUI>/preset_mobile . The page is served by this ComfyUI and talks only to it: it reads the
PresetPick_<family> node definitions (/object_info), queues the same node an editor tab would (/prompt), shows the
result (/history, /view) and can hand a picture to kura through the kura_send extension. No second copy of the presets
or of the scene controls lives here: what the node offers is what the page shows.
"""
from pathlib import Path

from aiohttp import web
from server import PromptServer

NODE_CLASS_MAPPINGS = {}
PAGE = Path(__file__).resolve().parent / 'mobile.html'


@PromptServer.instance.routes.get('/preset_mobile')
async def page(request):
    return web.Response(text=PAGE.read_text(encoding='utf-8'), content_type='text/html', headers={'Cache-Control': 'no-store'})
