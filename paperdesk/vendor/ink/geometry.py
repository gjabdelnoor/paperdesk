"""Local, bounded raster/SVG geometry to experimental pen ink. No cloud calls."""
import math
import os
import re
import select
import struct
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps

MAX_RECORDS = 1200
MAX_STROKES = 350


def bounds(text):
    values = tuple(int(v.strip()) for v in text.split(','))
    if len(values) != 4:
        raise ValueError('Enter left,top,right,bottom in 0..10000 pen-axis units')
    left, top, right, bottom = values
    if not (0 <= left < right <= 10000 and 0 <= top < bottom <= 10000):
        raise ValueError('Invalid placement rectangle')
    return values


def _numbers(text):
    tokens = re.findall(r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?', text)
    if re.sub(r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?|[\s,]', '', text):
        raise ValueError('Only unitless finite SVG numbers are supported')
    values = [float(t) for t in tokens]
    if any(not math.isfinite(v) or abs(v) > 1000000 for v in values):
        raise ValueError('SVG numeric range exceeded')
    return values


def svg_geometry(source):
    with open(source, 'rb') as f:
        raw = f.read(131073)
    if len(raw) > 131072 or b'\x00' in raw or b'<!' in raw or b'<?' in raw:
        raise ValueError('SVG exceeds 128 KiB or contains declarations/entities/instructions')
    root = ET.fromstring(raw.decode('utf-8', errors='strict'))
    ns = '{http://www.w3.org/2000/svg}'
    allowed = {'svg': {'viewBox', 'width', 'height'}, 'g': set(),
               'line': {'x1', 'y1', 'x2', 'y2'},
               'polyline': {'points'}, 'polygon': {'points'},
               'rect': {'x', 'y', 'width', 'height'},
               'circle': {'cx', 'cy', 'r'}, 'ellipse': {'cx', 'cy', 'rx', 'ry'},
               'path': {'d'}}
    styles = {'fill', 'stroke', 'stroke-width'}
    nodes = list(root.iter())
    if len(nodes) > 400:
        raise ValueError('SVG node limit exceeded')
    strokes = []
    for index, node in enumerate(nodes):
        tag = node.tag.removeprefix(ns)
        if tag not in allowed or (index == 0) != (tag == 'svg'):
            raise ValueError('Unsupported SVG element; only simple geometry is accepted')
        if node.text and node.text.strip() or node.tail and node.tail.strip():
            raise ValueError('SVG text must be converted to explicit outlines locally')
        if set(node.attrib) - allowed[tag] - styles:
            raise ValueError('Unsupported SVG attribute (transforms/styles/references are forbidden)')
        if tag not in ('svg', 'g') and len(node):
            raise ValueError('Geometry cannot contain child elements')
        for key in ('fill', 'stroke'):
            if key in node.attrib and node.attrib[key] not in ('none', 'black', '#000', '#000000'):
                raise ValueError('SVG accepts only black/none paint')
        if 'fill' in node.attrib and node.attrib['fill'] != 'none':
            raise ValueError('Filled SVG is unsupported; export explicit outline geometry')
        if 'stroke-width' in node.attrib:
            values = _numbers(node.attrib['stroke-width'])
            if len(values) != 1 or values[0] <= 0:
                raise ValueError('Invalid stroke width')
        def number(key, default='0'):
            values = _numbers(node.attrib.get(key, default))
            if len(values) != 1:
                raise ValueError('Expected one SVG number')
            return values[0]
        if node.attrib.get('stroke') == 'none':
            raise ValueError('Invisible geometry is unsupported')
        if tag in ('svg', 'g'):
            continue
        if tag == 'line':
            stroke = [(number('x1'), number('y1')), (number('x2'), number('y2'))]
        elif tag in ('polyline', 'polygon'):
            values = _numbers(node.attrib.get('points', ''))
            if len(values) < 4 or len(values) % 2:
                raise ValueError('Invalid SVG points')
            stroke = list(zip(values[::2], values[1::2]))
            if tag == 'polygon':
                stroke.append(stroke[0])
        elif tag == 'rect':
            x,y,w,h = [number(k) for k in ('x','y','width','height')]
            if w <= 0 or h <= 0:
                raise ValueError('Invalid rectangle')
            stroke = [(x,y),(x+w,y),(x+w,y+h),(x,y+h),(x,y)]
        elif tag in ('circle', 'ellipse'):
            x,y = number('cx'),number('cy')
            rx,ry = (number('r'),number('r')) if tag == 'circle' else (number('rx'),number('ry'))
            if min(rx,ry) <= 0:
                raise ValueError('Invalid ellipse')
            stroke = [(x+rx*math.cos(i*math.tau/48),y+ry*math.sin(i*math.tau/48)) for i in range(49)]
        else:
            data = node.attrib.get('d', '')
            tokens = re.findall(r'[MLHVZ]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?', data)
            if re.sub(r'[MLHVZ]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?|[\s,]', '', data):
                raise ValueError('SVG paths support explicit absolute M L H V Z only; flatten curves first')
            stroke=[]; i=0
            while i < len(tokens):
                command=tokens[i]; i+=1
                count={'M':2,'L':2,'H':1,'V':1,'Z':0}.get(command)
                if count is None or i+count>len(tokens):
                    raise ValueError('Explicit SVG path command required')
                values=_numbers(' '.join(tokens[i:i+count])); i+=count
                if len(values)!=count or (command!='M' and not stroke):
                    raise ValueError('Malformed SVG path')
                if command=='M':
                    if stroke:
                        strokes.append(stroke)
                    stroke=[tuple(values)]
                elif command=='Z': stroke.append(stroke[0])
                elif command=='L': stroke.append(tuple(values))
                elif command=='H': stroke.append((values[0],stroke[-1][1]))
                else: stroke.append((stroke[-1][0],values[0]))
        strokes.append(stroke)
    view = _numbers(root.attrib.get('viewBox', ''))
    if len(view)!=4 or view[2]<=0 or view[3]<=0:
        raise ValueError('SVG requires a positive viewBox')
    if not strokes or sum(map(len,strokes))>MAX_RECORDS:
        raise ValueError('Empty or excessive SVG geometry')
    x,y,w,h=view
    if any(not (x<=px<=x+w and y<=py<=y+h) for s in strokes for px,py in s):
        raise ValueError('SVG geometry falls outside viewBox')
    return strokes, tuple(view)


def _contours(mask):
    pixels=mask.load(); w,h=mask.size; edges={}
    def dark(x,y): return 0<=x<w and 0<=y<h and pixels[x,y]==0
    def edge(a,b): edges.setdefault(a,[]).append(b)
    for y in range(h):
        for x in range(w):
            if not dark(x,y): continue
            if not dark(x,y-1): edge((x,y),(x+1,y))
            if not dark(x+1,y): edge((x+1,y),(x+1,y+1))
            if not dark(x,y+1): edge((x+1,y+1),(x,y+1))
            if not dark(x-1,y): edge((x,y+1),(x,y))
    strokes=[]
    while edges:
        first=next(iter(edges)); point=first; stroke=[first]
        while point in edges:
            nxt=edges[point].pop()
            if not edges[point]: del edges[point]
            if len(stroke)>=2 and ((stroke[-1][0]-stroke[-2][0])*(nxt[1]-stroke[-1][1]) ==
                                   (stroke[-1][1]-stroke[-2][1])*(nxt[0]-stroke[-1][0])):
                stroke[-1]=nxt
            else: stroke.append(nxt)
            point=nxt
            if point==first: break
        if len(stroke)>=2: strokes.append(stroke)
    return strokes


def convert(source, preview, placement, threshold=160, size=160, mode='diagram', axis_ratio=11180/15340):
    if not 1<=threshold<=254 or not 16<=size<=256 or mode not in ('diagram','photo','hatch'):
        raise ValueError('Threshold 1..254, resolution 16..256; mode diagram/photo/hatch')
    placement=bounds(','.join(map(str,placement)))
    if not math.isfinite(axis_ratio) or not .1<=axis_ratio<=10:
        raise ValueError('Invalid physical pen aspect ratio')
    if Path(source).suffix.lower()=='.svg':
        raw,view=svg_geometry(source)
    else:
        with Image.open(source) as image:
            if image.width*image.height>20000000:
                raise ValueError('Image exceeds 20 million pixels; crop locally first')
            image=ImageOps.exif_transpose(image).convert('RGBA')
            white=Image.new('RGBA',image.size,'white'); white.alpha_composite(image)
            gray=white.convert('L')
        gray.thumbnail((size,size),Image.Resampling.LANCZOS)
        if mode=='photo':
            gray=ImageOps.invert(gray.filter(ImageFilter.FIND_EDGES))
            ImageDraw.Draw(gray).rectangle((0,0,gray.width-1,gray.height-1),outline='white')
        mask=gray.point(lambda v:0 if v<threshold else 255)
        if mode=='hatch':
            raw=[]
            for y in range(0,mask.height,3):
                x=0
                while x<mask.width:
                    if mask.getpixel((x,y)): x+=1; continue
                    start=x
                    while x+1<mask.width and mask.getpixel((x+1,y))==0: x+=1
                    raw.append([(start,y),(x+1,y)]); x+=1
        else: raw=_contours(mask)
        view=(0,0,mask.width,mask.height)
    left,top,right,bottom=placement; vx,vy,w,h=view
    scale=min((right-left)*axis_ratio/w,(bottom-top)/h)
    offset_x=left+((right-left)-w*scale/axis_ratio)/2
    offset_y=top+((bottom-top)-h*scale)/2
    strokes=[[(round(offset_x+(x-vx)*scale/axis_ratio),round(offset_y+(y-vy)*scale)) for x,y in s] for s in raw]
    records=protocol(strokes)
    canvas=Image.new('RGB',(round(600*axis_ratio),600),'white'); draw=ImageDraw.Draw(canvas)
    def p(point): return (point[0]*(canvas.width-1)/10000,point[1]*599/10000)
    draw.rectangle((*p((left,top)),*p((right,bottom))),outline='red')
    for stroke in strokes: draw.line([p(point) for point in stroke],fill='black',width=1)
    canvas.save(preview)
    return strokes,{'strokes':len(strokes),'points':sum(map(len,strokes)),
                    'records':len(records)-1,'minimum_seconds':(len(records)-1)*.02,'preview':str(preview)}


def protocol(strokes):
    if not strokes or len(strokes)>MAX_STROKES:
        raise ValueError('Empty or excessive stroke list; crop/simplify or reduce resolution')
    result=[]
    for stroke in strokes:
        if not 2<=len(stroke)<=MAX_RECORDS:
            raise ValueError('Each stroke needs at least two points')
        for index,point in enumerate(stroke):
            if len(point)!=2 or any(type(v) is not int or not 0<=v<=10000 for v in point):
                raise ValueError('Coordinates must be integers in 0..10000')
            result.append(struct.pack('<BII',0 if index==0 else 1,*point))
            if len(result)>MAX_RECORDS: raise ValueError('Too many records; crop/simplify or reduce resolution')
        result.append(struct.pack('<BII',2,*stroke[-1]))
    if len(result)>MAX_RECORDS: raise ValueError('Too many records')
    return result+[struct.pack('<BII',3,0,0)]


def stream(command, strokes, cancelled=lambda:False, progress=lambda n,total:None, mode=b'D'):
    records=protocol(strokes)
    if mode not in (b'D',b'V'): raise ValueError('Invalid stream mode')
    process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,bufsize=0)
    os.set_blocking(process.stdin.fileno(),False)
    deadline=time.monotonic()+15
    def wait(fd,write=False):
        while time.monotonic()<deadline:
            if cancelled(): raise InterruptedError('Ink cancelled; partial marks may remain; no replay')
            ready=select.select([] if write else [fd],[fd] if write else [],[],.02)
            if ready[1 if write else 0]: return
        raise TimeoutError('Ink deadline exceeded; partial marks may remain; no replay')
    def send(data):
        while data:
            wait(process.stdin,True)
            try: n=os.write(process.stdin.fileno(),data)
            except BlockingIOError: continue
            if n<=0: raise OSError('Ink write failed; no replay')
            data=data[n:]
    def ack(expected):
        wait(process.stdout)
        if os.read(process.stdout.fileno(),1)!=expected:
            raise OSError('Stylus protocol/link failed; partial marks may remain; no replay')
    try:
        send(mode); ack(b'R')
        deadline=time.monotonic()+28
        for index,record in enumerate(records):
            send(record); ack(b'E' if record[0]==3 else b'A'); progress(index+1,len(records))
        process.stdin.close()
        if process.wait(timeout=2): raise OSError('Stylus helper failed; inspect tablet')
    finally:
        if not process.stdin.closed:
            try: process.stdin.close()
            except OSError: pass
        try: process.wait(timeout=2.5)
        except subprocess.TimeoutExpired: process.kill(); process.wait()
        process.stdout.close()
