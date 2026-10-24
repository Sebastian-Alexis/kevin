"""Hand-label the gold eval set from the REAL capture path (Pi HDMI capture of the demo machine).

This is the one dataset we can't generate: it measures the actor on exactly what it will see in the demo.
It is eval-only (source "gold"); build_dataset.py never trains on it.

  python datagen/gold_label.py --pi http://ai-kvm.local:8080       # grab frames from the Pi's /screenshot
  python datagen/gold_label.py --local                              # dev: grab this Mac's screen
  python datagen/gold_label.py --import-dir ~/Desktop/captures      # label existing images
  open http://127.0.0.1:8770

In the page: G grabs a new frame, drag a box over the target, type the instruction a planner would give, Enter saves.
Label several targets per frame. Aim for ~200: ~100 in the demo apps, ~100 in apps we hold out of training.
"""
import argparse
import io
import json
import subprocess
import sys
import tempfile
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Shard  # noqa: E402

PAGE = r"""<!doctype html><meta charset=utf-8><title>gold labeler</title>
<style>
body{font:14px system-ui;margin:0;background:#15161a;color:#e8e8ea;display:grid;grid-template-columns:1fr 340px;height:100vh}
#wrap{position:relative;overflow:auto;padding:12px}#cv{max-width:100%;cursor:crosshair;display:block}
aside{padding:14px;border-left:1px solid #333;display:flex;flex-direction:column;gap:10px;overflow:auto}
input,select,button{font:inherit;padding:7px;border-radius:6px;border:1px solid #444;background:#222;color:#eee}
button{cursor:pointer;background:#2d5bd7;border:0}button.alt{background:#333}
.row{display:flex;gap:6px}.row>*{flex:1}small{color:#999}#log{font:12px ui-monospace;white-space:pre-wrap;color:#9c9}
kbd{background:#333;border-radius:3px;padding:0 4px}
</style>
<div id=wrap><canvas id=cv></canvas></div>
<aside>
  <div class=row><button id=grab>Grab frame <kbd>G</kbd></button><button class=alt id=skip>Next import</button></div>
  <small id=info>no frame yet</small>
  <label>Action <select id=act>
    <option value=click>click (drag a box)</option><option value=right>right click</option><option value=double>double click</option>
    <option value=type>type</option><option value=key>key combo</option><option value=scroll>scroll</option></select></label>
  <input id=intent placeholder="Instruction, e.g. Click the Downloads folder in the sidebar" autocomplete=off>
  <input id=arg placeholder="text to type / combo like cmd+c / ticks like -5" style=display:none autocomplete=off>
  <input id=app placeholder="App name (for holdout split), e.g. Calendar" autocomplete=off>
  <button id=save>Save <kbd>Enter</kbd></button>
  <small>Drag a tight box around the target. Several labels per frame is fine. App name sticks between saves.</small>
  <div id=stats></div><div id=log></div>
</aside>
<script>
const cv=document.getElementById('cv'),cx=cv.getContext('2d');let img=new Image(),frame=null,box=null,drag=null,done=[];
const $=id=>document.getElementById(id);
function draw(){if(!frame)return;cx.drawImage(img,0,0);cx.lineWidth=2;
 for(const b of done){cx.strokeStyle='#3c6';cx.strokeRect(b[0],b[1],b[2]-b[0],b[3]-b[1]);}
 if(box){cx.strokeStyle='#f24';cx.strokeRect(box[0],box[1],box[2]-box[0],box[3]-box[1]);}}
function pos(e){const r=cv.getBoundingClientRect();return[(e.clientX-r.left)*cv.width/r.width,(e.clientY-r.top)*cv.height/r.height];}
cv.onmousedown=e=>{drag=pos(e);box=[...drag,...drag];};
cv.onmousemove=e=>{if(!drag)return;const p=pos(e);box=[Math.min(drag[0],p[0]),Math.min(drag[1],p[1]),Math.max(drag[0],p[0]),Math.max(drag[1],p[1])];draw();};
cv.onmouseup=()=>{drag=null;$('intent').focus();};
async function load(r){const j=await r.json();if(j.error){alert(j.error);return;}frame=j.frame;done=[];box=null;
 img=new Image();img.onload=()=>{cv.width=img.width;cv.height=img.height;draw();};img.src='/frames/'+j.frame+'?t='+Date.now();
 $('info').textContent=j.frame+'  '+(j.note||'');stats();}
$('grab').onclick=()=>load(fetch('/api/grab',{method:'POST'}));
$('skip').onclick=()=>load(fetch('/api/next',{method:'POST'}));
$('act').onchange=()=>{$('arg').style.display=['type','key','scroll'].includes($('act').value)?'':'none';};
async function save(){if(!frame)return;const a=$('act').value,intent=$('intent').value.trim();if(!intent)return alert('instruction?');
 const W=cv.width,H=cv.height,k=v=>Math.round(v*1000);let action,bbox=null;
 if(['click','right','double'].includes(a)){if(!box||box[2]-box[0]<3)return alert('drag a box first');
   bbox=[k(box[0]/W),k(box[1]/H),k(box[2]/W),k(box[3]/H)];
   action={action:'click',x:Math.round((bbox[0]+bbox[2])/2),y:Math.round((bbox[1]+bbox[3])/2)};
   if(a==='right')action.button='right';if(a==='double')action.count=2;}
 else if(a==='type')action={action:'type',text:$('arg').value};
 else if(a==='key')action={action:'key',combo:$('arg').value};
 else{const c=box?[k((box[0]+box[2])/2/W),k((box[1]+box[3])/2/H)]:[500,500];action={action:'scroll',x:c[0],y:c[1],ticks:parseInt($('arg').value||'-5')};}
 const r=await(await fetch('/api/save',{method:'POST',body:JSON.stringify({frame,intent,action,bbox,app:$('app').value.trim()})})).json();
 if(r.error)return alert(r.error);if(box)done.push(box);box=null;$('intent').value='';$('arg').value='';draw();
 $('log').textContent=intent+'\n'+JSON.stringify(action)+'\n\n'+$('log').textContent;stats();}
$('save').onclick=save;
document.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();save();}
 else if(e.key.toLowerCase()==='g'&&document.activeElement.tagName!=='INPUT'){$('grab').click();}};
async function stats(){const s=await(await fetch('/api/stats')).json();$('stats').textContent=`${s.steps} labels on ${s.frames} frames  (${s.dir})`;}
stats();
</script>"""


def grab_frame(args):
    if args.pi:
        with urllib.request.urlopen(args.pi.rstrip("/") + "/screenshot", timeout=5) as r:
            return Image.open(io.BytesIO(r.read())), "pi"
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        subprocess.run(["screencapture", "-x", "-m", "-t", "png", f.name], check=True)
        return Image.open(f.name).copy(), "local"


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--pi", help="Pi control API base, e.g. http://ai-kvm.local:8080")
    g.add_argument("--local", action="store_true")
    g.add_argument("--import-dir")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--shard", default=None, help="reuse a shard id to keep appending")
    args = ap.parse_args()

    shard = Shard("gold", shard_id=args.shard, capture="pi" if args.pi else "local" if args.local else "import")
    queue = sorted(p for p in Path(args.import_dir).expanduser().iterdir()
                   if p.suffix.lower() in (".png", ".jpg", ".jpeg")) if args.import_dir else []
    if args.shard:  # appending: continue frame numbering
        shard.n_frames = len(list((shard.dir / "frames").glob("*.jpg")))
        shard.n_steps = sum(1 for _ in open(shard.dir / "steps.jsonl"))

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path == "/":
                b = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b)
            elif self.path.startswith("/frames/"):
                p = shard.dir / self.path.split("?")[0][1:]
                if not p.resolve().is_relative_to(shard.dir.resolve()) or not p.exists():
                    return self._json({"error": "not found"}, 404)
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.end_headers()
                self.wfile.write(p.read_bytes())
            elif self.path == "/api/stats":
                self._json({"steps": shard.n_steps, "frames": shard.n_frames, "dir": str(shard.dir)})
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0)
            try:
                if self.path == "/api/grab":
                    img, how = grab_frame(args)
                    return self._json({"frame": shard.add_frame(img), "note": f"{how} {img.size[0]}x{img.size[1]}"})
                if self.path == "/api/next":
                    if not queue:
                        return self._json({"error": "import queue empty"})
                    p = queue.pop(0)
                    return self._json({"frame": shard.add_frame(Image.open(p)), "note": f"{p.name}, {len(queue)} left"})
                if self.path == "/api/save":
                    j = json.loads(body)
                    extra = {"app": j["app"]} if j.get("app") else {}
                    ok = shard.add_step(j["frame"], j["intent"], j["action"], bbox=j.get("bbox"), os="target", **extra)
                    return self._json({"ok": ok} if ok else {"error": "invalid action (check text/combo)"})
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"})
            self._json({"error": "not found"}, 404)

    print(f"labeling into {shard.dir}\nopen http://127.0.0.1:{args.port}")
    try:
        ThreadingHTTPServer(("127.0.0.1", args.port), H).serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shard.close()
        print(f"saved {shard.n_steps} labels")


if __name__ == "__main__":
    main()
