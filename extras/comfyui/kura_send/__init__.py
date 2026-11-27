"""kura_send: a ComfyUI page extension that lets a HUMAN send a chosen image to kura (owner 2026-11-16).

The 「kura に送る」 button on Save Image / Preview Image (and the same entry in the node's right-click menu) sends the
picture being shown. There is deliberately no "send everything" switch: it would also send the failures (owner
2026-11-16). The server side only copies the chosen file into
`output/kura-send/`; ComfyUI holds no kura credential. The host's timer (kura: scripts/comfy_output_intake.py) uploads
what lands there. Nothing is sent that a person did not pick: agents' test generations stay where they are.
"""
import os
import shutil

import folder_paths
from aiohttp import web
from server import PromptServer

WEB_DIRECTORY = './web'
NODE_CLASS_MAPPINGS = {}
INBOX = 'kura-send'


@PromptServer.instance.routes.post('/kura_send/send')
async def send(request):
    body = await request.json()
    sent, refused = [], []
    inbox = os.path.join(folder_paths.get_output_directory(), INBOX)
    os.makedirs(inbox, exist_ok=True)
    for image in body.get('images') or []:
        name, subfolder, kind = str(image.get('filename') or ''), str(image.get('subfolder') or ''), str(image.get('type') or 'output')
        base = folder_paths.get_directory_by_type(kind)
        source = os.path.realpath(os.path.join(base or '', subfolder, name))
        if not base or not name or os.path.commonpath([os.path.realpath(base), source]) != os.path.realpath(base) or not os.path.isfile(source):
            refused.append(name); continue
        target = os.path.join(inbox, os.path.basename(name))
        if os.path.exists(target) and not os.path.samefile(source, target) and os.path.getsize(target) != os.path.getsize(source):
            stem, extension = os.path.splitext(os.path.basename(name))
            target = os.path.join(inbox, f'{stem}-{int(os.path.getmtime(source))}{extension}')
        if not os.path.exists(target):
            shutil.copy2(source, target)
        sent.append(os.path.basename(target))
    return web.json_response({'sent': sent, 'refused': refused})
