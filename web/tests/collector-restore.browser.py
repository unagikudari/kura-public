"""Browser restore controls on built app; all collector requests are local fixtures."""
import base64,hashlib,hmac,json,os,socket,subprocess,tempfile,threading,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.request import urlopen
from urllib.parse import urlparse
from pathlib import Path
from playwright.sync_api import expect,sync_playwright


def exercise(context,base,width,evidence):
    page=context.new_page();errors=[];writes=[];held=[];restored=False
    page.on('pageerror',lambda e:errors.append(str(e)))
    def route(req):
        nonlocal restored
        path=req.request.url.split('/m/collector-next/')[1]
        if req.request.method=='POST':
            assert path=='exchange/restore'
            writes.append(req.request.post_data_buffer);held.append(req);return
        if path=='status':value={'safe_mode':False}
        elif path=='collections':value=[{'id':7,'title':'復元した作品'}] if restored else []
        else:value={'items':[],'next_cursor':None}
        req.fulfill(json=value)
    page.route('**/m/collector-next/**',route)
    page.goto(base+'/lab/collector');expect(page.get_by_role('status').first).to_contain_text('まだメディアがありません')
    page.get_by_text('作品ZIPから復元',exact=True).click()
    component=page.locator('details.restore')
    component.locator('input').set_input_files({'name':'collection.zip','mimeType':'application/zip','buffer':b'local-fixture-zip'})
    component.get_by_role('button',name='このZIPを復元').click()
    expect(component.get_by_role('button',name='復元中…')).to_be_disabled()
    expect(component.get_by_role('status')).to_contain_text('送信・検証')
    page.wait_for_timeout(50);assert len(held)==1
    held.pop().fulfill(status=409,json={'detail':{'error':'exchange_target_not_empty','fix':'復元には空の新ライブラリが必要です。'}})
    expect(component.get_by_role('alert')).to_contain_text('空の新ライブラリ')
    component.get_by_role('button',name='このZIPを復元').click()
    page.wait_for_timeout(50);assert len(held)==1
    restored=True
    held.pop().fulfill(json={'collection_id':7,'entries':3})
    expect(component.get_by_role('status')).to_have_text('3ページを復元しました。')
    expect(page.get_by_role('combobox').first).to_have_value('7')
    assert writes==[b'local-fixture-zip']*2 and not errors,errors
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    if evidence:page.screenshot(path=str(evidence/f'collector-restore-{width}.png'),full_page=True)
    page.close();return {'width':width,'progress':True,'errors':True,'success':True,'no_overflow':True}


def main():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_GET(self):
            value=[] if self.path.startswith('/api/tag-categories') else {}
            body=json.dumps(value).encode();self.send_response(200);self.send_header('content-type','application/json');self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    base=f'http://127.0.0.1:{port}';api=f'http://127.0.0.1:{server.server_port}'
    secret='synthetic-collector-restore-only'
    env={**os.environ,'HOST':'127.0.0.1','PORT':str(port),'ORIGIN':base,'INTERNAL_API_URL':api,'PUBLIC_API_URL':api,'KURA_SESSION_SECRET':secret,'KURA_API_KEY':'synthetic-only'}
    enc=lambda b:base64.urlsafe_b64encode(b).rstrip(b'=').decode()
    payload=enc(json.dumps({'u':'synthetic','t':'synthetic','e':'','exp':int(time.time())+3600}).encode())
    cookie=payload+'.'+enc(hmac.new(secret.encode(),payload.encode(),hashlib.sha256).digest())
    evidence=Path(os.environ.get('EVIDENCE_DIR','/tmp/kura-restore-ui-shots'));evidence.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryFile() as log:
        process=subprocess.Popen(['node','tests/serve-fixture.mjs'],cwd=Path(__file__).resolve().parents[1],env=env,stdout=log,stderr=log)
        try:
            for _ in range(100):
                try:urlopen(base+'/login',timeout=.3).close();break
                except Exception:time.sleep(.1)
            else:raise RuntimeError('Fixture server not ready')
            with sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True,executable_path=os.environ['CHROMIUM'])
                results=[]
                for width in (320,390,844,1280):
                    context=browser.new_context(viewport={'width':width,'height':900},has_touch=width!=1280,is_mobile=width!=1280,reduced_motion='reduce')
                    context.add_cookies([{'name':'kura_session','value':cookie,'url':base}])
                    def fence(route):
                        parsed=urlparse(route.request.url)
                        if parsed.hostname!='127.0.0.1' or parsed.port not in (port,server.server_port):raise AssertionError('External fixture request')
                        route.continue_()
                    context.route('**/*',fence)
                    results.append(exercise(context,base,width,evidence));context.close()
                browser.close();print(json.dumps({'status':'PASS','cases':results}))
        except Exception:
            log.seek(0);print(log.read().decode(errors='replace')[-2000:]);raise
        finally:
            process.terminate();process.wait(timeout=10);server.shutdown();server.server_close();thread.join()

if __name__=='__main__':main()
