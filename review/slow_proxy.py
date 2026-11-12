"""Real HTTP forwarding with file-body pacing, independent of browser download ownership."""
import threading,time,urllib.request,urllib.error
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer


class SlowProxy:
    def __init__(self, backend, rate=1024*1024):
        self.events=[]
        parent=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def forward(self):
                body=self.rfile.read(int(self.headers.get('Content-Length',0))) if self.command in ('POST','PUT','PATCH') else None
                headers={k:v for k,v in self.headers.items() if k.lower() not in ('host','connection','accept-encoding')}
                request=urllib.request.Request(backend+self.path,data=body,headers=headers,method=self.command)
                try:
                    try:response=urllib.request.urlopen(request,timeout=40)
                    except urllib.error.HTTPError as e:response=e
                    with response:
                        length=int(response.headers.get('Content-Length',0));kind='native' if self.path.startswith('/v1/downloads/') else 'storage' if self.path.startswith('/v1/storage/') else 'other'
                        parent.events.append({'method':self.command,'kind':kind,'bytes':length,'status':response.status})
                        self.send_response(response.status)
                        for key,value in response.headers.items():
                            if key.lower() not in ('connection','transfer-encoding'):self.send_header(key,value)
                        self.send_header('Connection','close');self.end_headers()
                        start=time.monotonic();sent=0
                        while True:
                            chunk=response.read(65536)
                            if not chunk:break
                            self.wfile.write(chunk);self.wfile.flush();sent+=len(chunk)
                            if kind in ('native','storage') and length>1024*1024:
                                delay=sent/rate-(time.monotonic()-start)
                                if delay>0:time.sleep(delay)
                except (BrokenPipeError,ConnectionResetError):pass
                self.close_connection=True
            do_GET=do_HEAD=do_POST=do_PUT=do_DELETE=do_PATCH=forward
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.url='http://127.0.0.1:'+str(self.server.server_port)
    def start(self):self.thread.start();return self
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join()
