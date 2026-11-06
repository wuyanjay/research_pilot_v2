"""Generic metric display/PNG packaging. No retrieval or simulation algorithm."""
import base64
import math
import struct
import zlib
from font import FONT


def render(payload):
    series=payload['series'];labels=payload['protocol']['plot_labels']
    if not isinstance(series,list) or not 1 <= len(series) <= 2:
        raise ValueError('Plot requires one or two measured series')
    for row in series:
        if (not isinstance(row,dict) or not isinstance(row.get('name'),str)
                or not isinstance(row.get('values'),list) or len(row['values']) != len(labels)
                or any(type(v) not in (int,float) or not math.isfinite(v) for v in row['values'])):
            raise ValueError('Plot values must be finite and match plot_labels')
    w,h=1000,620
    pixels=bytearray([255])*(w*h*3)
    colors=[(35,95,185),(225,120,30)]
    def rectangle(x,y,width,height,color):
        width=max(0,min(width,w-x))
        for row in range(y,min(h,y+height)):
            start=(row*w+x)*3
            pixels[start:start+width*3]=bytes(color)*width
    def text(x,y,value):
        for char in value.upper():
            for i,bit in enumerate(FONT.get(char,'0'*35)):
                if bit=='1': rectangle(x+(i%5)*2,y+(i//5)*2,2,2,(0,0,0))
            x+=12
    text(24,18,payload['case_id']+' CPU OFFLINE SEED '+str(payload['protocol']['seed']))
    text(24,48,'BLUE = '+payload['series'][0]['name'])
    if len(payload['series'])>1: text(24,78,'ORANGE = '+payload['series'][1]['name'])
    for i,label in enumerate(payload['protocol']['plot_labels']):
        y=120+i*152
        scale=max([s['values'][i] for s in payload['series']]+[1e-12])
        text(24,y,label)
        text(24,y+100,'X: 0 TO '+format(scale,'.4f')+'; Y: SERIES')
        for k,series in enumerate(payload['series']):
            val=series['values'][i]
            rectangle(24,y+27+k*32,max(1,int(650*max(0,val)/scale)),22,colors[k])
            text(710,y+30+k*32,format(val,'.6f'))
    text(24,590,'EXPERIMENT RESULTS')
    def chunk(kind,data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    raw=b''.join(b'\0'+pixels[y*w*3:(y+1)*w*3] for y in range(h))
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',w,h,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(raw))+chunk(b'IEND',b'')


def package(payload):
    case=payload['case_id'];protocol=payload['protocol']
    status=payload.get('status');values=payload['values']
    if status not in ('completed','failed','materials_missing'):
        raise ValueError('Explicit experiment status is required; packaging does not prove completion')
    if len(protocol['metrics']) != 3 or not isinstance(values,list) or len(values) != 3:
        raise ValueError('Exactly three metric definitions and observed values are required')
    if status == 'completed':
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in values):
            raise ValueError('Completed metrics must be finite measured values')
    elif any(v is not None for v in values):
        raise ValueError('Incomplete experiments must preserve metric objects with null observed values')
    metrics=[dict(name=m['name'],unit=m['unit'],reference=None,observed=v,relative_error=None)
             for m,v in zip(protocol['metrics'],payload['values'])]
    labels='; '.join(m['name']+' ('+m['unit']+')' for m in protocol['metrics'])
    legend='; '.join(c+' = '+s['name'] for c,s in zip(['BLUE','ORANGE'],payload['series']))
    submission={'case_id':case,'status':status,'metrics':metrics,'figures':[{
        'path':'data/figure/'+payload['team']+'/'+case+'-result.png',
        'caption':protocol['figure_caption'],'x_label':labels,'y_label':'实验策略','legend':legend}]}
    return {'submission':submission,'details':payload['details'],
            'png_base64':base64.b64encode(render(payload)).decode()}
