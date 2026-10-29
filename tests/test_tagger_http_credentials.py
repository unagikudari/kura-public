"""Real HTTP accepts encoded credentials for both release parts and reports."""
import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
import requests
from tagger.credentials import token_headers
from tagger.release_client import make_http_callbacks


def test_release_and_state_report_share_valid_wire_credentials():
    seen=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            header=self.headers.get('Authorization','')
            decoded=base64.b64decode(header.removeprefix('Token '),validate=True).decode()
            if decoded!='worker:synthetic-raw-token':self.send_error(401);return
            seen.append(self.path)
            body=json.dumps({'release_id':7}).encode() if self.path.endswith('/active') else b'model-bytes'
            self.send_response(200);self.end_headers();self.wfile.write(body)
        def do_POST(self):
            self.do_GET()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        origin=f'http://127.0.0.1:{server.server_port}'
        headers=token_headers('worker','synthetic-raw-token')
        meta,data=make_http_callbacks(origin,headers)
        assert meta()=={'release_id':7}
        assert data(7,'model')==b'model-bytes'
        assert requests.post(origin+'/api/tagger/node-state',headers=headers,timeout=2).status_code==200
        assert len(seen)==3
    finally:server.shutdown();server.server_close();thread.join()


@pytest.mark.parametrize('user,token',[('', 'token'),('worker','')])
def test_missing_credentials_fail_before_network(user,token):
    with pytest.raises(ValueError,match='KURA_USER and KURA_TOKEN'):token_headers(user,token)
