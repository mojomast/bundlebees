"""Range-capable review server. Bind only to the interface explicitly selected."""
from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler
from pathlib import Path
import argparse,re
p=argparse.ArgumentParser();p.add_argument('--bind',required=True);p.add_argument('--port',type=int,required=True);p.add_argument('--directory',type=Path,required=True);a=p.parse_args();public=a.directory.resolve()
class Handler(SimpleHTTPRequestHandler):
 def __init__(self,*args,**kwargs):super().__init__(*args,directory=str(public),**kwargs)
 def list_directory(self,path):self.send_error(403);return None
 def send_head(self):
  self.byte_range=None;path=Path(self.translate_path(self.path))
  if not path.is_file() or not self.headers.get('Range'):return super().send_head()
  size=path.stat().st_size;m=re.fullmatch(r'bytes=(\d+)-(\d*)',self.headers['Range'])
  if not m:self.send_error(416);return None
  start=int(m[1]);end=min(size-1,int(m[2]) if m[2] else size-1)
  if start>end:self.send_error(416);return None
  f=path.open('rb');f.seek(start);self.byte_range=end-start+1;self.send_response(206);self.send_header('Content-Type',self.guess_type(str(path)));self.send_header('Content-Length',str(self.byte_range));self.send_header('Content-Range',f'bytes {start}-{end}/{size}');self.send_header('Accept-Ranges','bytes');self.end_headers();return f
 def copyfile(self,source,output):
  if self.byte_range is None:return super().copyfile(source,output)
  remaining=self.byte_range
  while remaining:
   data=source.read(min(65536,remaining))
   if not data:break
   output.write(data);remaining-=len(data)
print(f'bundlebees review server, port {a.port}',flush=True)
ThreadingHTTPServer((a.bind,a.port),Handler).serve_forever()
