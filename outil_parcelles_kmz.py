#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Option A: Streamlit KMZ + fichier cadastral local -> Excel, sans WFS."""
import io, re, tempfile, zipfile
from pathlib import Path
import xml.etree.ElementTree as ET
import geopandas as gpd
import pandas as pd
import streamlit as st
from shapely.geometry import LineString, Point, Polygon

WORK_CRS='EPSG:3812'
NS={'k':'http://www.opengis.net/kml/2.2'}

def coords(text):
    out=[]
    for token in (text or '').strip().split():
        p=token.split(',')
        if len(p)>=2:
            try: out.append((float(p[0]),float(p[1])))
            except ValueError: pass
    return out

def read_kmz(upload):
    raw=upload.getvalue()
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names=[n for n in z.namelist() if n.lower().endswith('.kml')]
        if not names: raise ValueError('Aucun KML dans le KMZ.')
        root=ET.fromstring(z.read(names[0]))
    parent={c:n for n in root.iter() for c in n}
    rows=[]
    for pm in root.findall('.//k:Placemark',NS):
        name=(pm.findtext('k:name',default='',namespaces=NS) or '').strip()
        folders=[]; n=pm
        while n in parent:
            n=parent[n]
            if n.tag.endswith('Folder'):
                f=(n.findtext('k:name',default='',namespaces=NS) or '').strip()
                if f: folders.append(f)
        folder=' / '.join(reversed(folders)); label=name or folder or 'Sans nom'
        for x in pm.findall('.//k:Point',NS):
            c=coords(x.findtext('k:coordinates',default='',namespaces=NS))
            if c: rows.append((label,folder,Point(c[0])))
        for x in pm.findall('.//k:LineString',NS):
            c=coords(x.findtext('k:coordinates',default='',namespaces=NS))
            if len(c)>=2: rows.append((label,folder,LineString(c)))
        for x in pm.findall('.//k:Polygon',NS):
            c=coords(x.findtext('.//k:outerBoundaryIs/k:LinearRing/k:coordinates',default='',namespaces=NS))
            if len(c)>=4:
                g=Polygon(c)
                if not g.is_valid:g=g.buffer(0)
                if not g.is_empty: rows.append((label,folder,g))
    if not rows: raise ValueError('Aucune geometrie exploitable dans le KMZ.')
    return gpd.GeoDataFrame({'ELEMENT_KMZ':[r[0] for r in rows],'DOSSIER_KMZ':[r[1] for r in rows]},geometry=[r[2] for r in rows],crs='EPSG:4326')

def classify(name,folder,typ):
    s=f'{folder} {name}'.casefold()
    if re.search(r'acc[eè]s|access|chemin|route|track|tole',s): return 'ACCES'
    if re.search(r'tireuse|freineuse|tirage|freinage',s): return 'TIREUSE_FREINEUSE'
    if re.search(r'zone.*travaux|travaux|work.?zone|plateforme|stockage',s): return 'ZONE_TRAVAUX'
    if re.search(r'surplomb|overhead|ligne|line|port[eé]e|span',s): return 'SURPLOMB'
    return 'SURPLOMB' if 'LineString' in typ else 'AUTRE'

def prepare(raw,access_width,overhead_width,point_radius,include_other):
    g=raw.to_crs(WORK_CRS).copy()
    g['TYPE_IMPACT']=[classify(n,f,t) for n,f,t in zip(g.ELEMENT_KMZ,g.DOSSIER_KMZ,g.geom_type)]
    if not include_other:g=g[g.TYPE_IMPACT!='AUTRE'].copy()
    if g.empty:raise ValueError('Aucun objet utile reconnu dans le KMZ.')
    new=[]
    for _,r in g.iterrows():
        x=r.geometry
        if x.geom_type in ('Point','MultiPoint'):x=x.buffer(point_radius)
        elif x.geom_type in ('LineString','MultiLineString'):
            w=access_width if r.TYPE_IMPACT=='ACCES' else overhead_width
            if w>0:x=x.buffer(w/2,cap_style=2,join_style=2)
        if not x.is_valid:x=x.buffer(0)
        new.append(x)
    g.geometry=new
    return g[~g.geometry.is_empty].copy()

def read_cadastre(upload):
    name=upload.name.lower(); raw=upload.getvalue()
    with tempfile.TemporaryDirectory() as td:
        root=Path(td)
        if name.endswith('.zip'):
            zpath=root/'cadastre.zip';zpath.write_bytes(raw)
            with zipfile.ZipFile(zpath) as z:z.extractall(root/'unzipped')
            candidates=[p for p in (root/'unzipped').rglob('*') if p.suffix.lower() in ('.gpkg','.gml','.shp','.geojson','.json')]
            if not candidates:raise ValueError('ZIP: aucun GPKG, GML, SHP ou GeoJSON trouve.')
            path=next((p for p in candidates if p.suffix.lower()=='.gpkg'),candidates[0])
        else:
            suffix=Path(upload.name).suffix or '.gml';path=root/('cadastre'+suffix);path.write_bytes(raw)
        try:g=gpd.read_file(path)
        except Exception as e:raise RuntimeError(f'Impossible de lire le fichier cadastral {upload.name}: {e}') from e
    if g.empty:raise ValueError('Le fichier cadastral est vide.')
    if g.crs is None:raise ValueError('Le fichier cadastral ne contient pas de CRS/projection.')
    g=g[g.geometry.notna() & ~g.geometry.is_empty].copy()
    return g.to_crs(WORK_CRS)

def col(columns,names):
    norm={re.sub('[^a-z0-9]','',c.casefold()):c for c in columns}
    for n in names:
        k=re.sub('[^a-z0-9]','',n.casefold())
        if k in norm:return norm[k]
    for n in names:
        k=re.sub('[^a-z0-9]','',n.casefold())
        for a,b in norm.items():
            if k in a:return b
    return None

def analyze(impact,parcels):
    minx,miny,maxx,maxy=impact.total_bounds
    p=parcels.cx[minx:maxx,miny:maxy].copy()
    if p.empty:return pd.DataFrame(),0
    j=gpd.sjoin(p,impact[['ELEMENT_KMZ','DOSSIER_KMZ','TYPE_IMPACT','geometry']],how='inner',predicate='intersects')
    if j.empty:return pd.DataFrame(),len(p)
    j['SURFACE_INTERSECTION_M2']=[geom.intersection(impact.geometry.loc[i]).area for geom,i in zip(j.geometry,j.index_right)]
    cap=col(j.columns,['nationalCadastralReference','capakey','parcelKey','localId'])
    num=col(j.columns,['label','parcelNumber','numeroParcelle','perceelnummer'])
    com=col(j.columns,['municipality','commune','gemeente','administrativeUnit'])
    sec=col(j.columns,['section']);div=col(j.columns,['division','cadastralDivision'])
    def s(c):return j[c].astype(str) if c else pd.Series('',index=j.index)
    out=pd.DataFrame({'TYPE_IMPACT':j.TYPE_IMPACT,'ELEMENT_KMZ':j.ELEMENT_KMZ,'DOSSIER_KMZ':j.DOSSIER_KMZ,
      'CAPAKEY':s(cap),'NUMERO_PARCELLE':s(num),'COMMUNE':s(com),'DIVISION':s(div),'SECTION':s(sec),
      'SURFACE_INTERSECTION_M2':j.SURFACE_INTERSECTION_M2.round(2)})
    return out.replace({'nan':'','None':''}).drop_duplicates().sort_values(['TYPE_IMPACT','ELEMENT_KMZ','CAPAKEY']).reset_index(drop=True),len(p)

def excel(out,params):
    b=io.BytesIO()
    summary=out.groupby('TYPE_IMPACT',dropna=False).size().reset_index(name='NOMBRE_INTERSECTIONS')
    with pd.ExcelWriter(b,engine='openpyxl') as w:
        out.to_excel(w,index=False,sheet_name='Parcelles');summary.to_excel(w,index=False,sheet_name='Synthese')
        pd.DataFrame(params.items(),columns=['PARAMETRE','VALEUR']).to_excel(w,index=False,sheet_name='Parametres')
        for sh in w.book.worksheets:
            sh.freeze_panes='A2';sh.auto_filter.ref=sh.dimensions
            for c in sh.columns:sh.column_dimensions[c[0].column_letter].width=min(max(len(str(x.value or '')) for x in c)+2,55)
    return b.getvalue()

st.set_page_config(page_title='Parcelles permis KMZ - Option A',layout='wide')
st.title('Parcelles cadastrales pour permis - Option A')
st.caption('Traitement 100 % a partir de fichiers charges: aucun appel WFS.')
kmz=st.file_uploader('1. Charger le KMZ du projet',type=['kmz'])
cad=st.file_uploader('2. Charger le parcellaire cadastral',type=['gpkg','gml','geojson','json','zip'])
c1,c2,c3=st.columns(3)
aw=c1.number_input('Largeur totale des acces (m)',0.0,100.0,5.0,0.5)
ow=c2.number_input('Largeur totale du corridor de surplomb (m)',0.0,200.0,0.0,1.0)
pr=c3.number_input('Rayon autour des points (m)',0.0,100.0,5.0,1.0)
other=st.checkbox('Inclure les objets KMZ non classes',False)
if st.button('Analyser les parcelles',type='primary',disabled=kmz is None or cad is None):
    try:
        with st.spinner('Lecture des fichiers et calcul des intersections...'):
            impact=prepare(read_kmz(kmz),aw,ow,pr,other);parcels=read_cadastre(cad);result,nlocal=analyze(impact,parcels)
        if result.empty:st.warning('Aucune intersection trouvee dans la zone du projet.')
        else:
            distinct=result.CAPAKEY[result.CAPAKEY!=''].nunique()
            st.success(f'{len(result)} intersections; {distinct} CAPAKEY distinctes; {nlocal} parcelles chargees dans l emprise.')
            st.dataframe(result,use_container_width=True,hide_index=True)
            data=excel(result,{'CRS calcul':WORK_CRS,'Fichier KMZ':kmz.name,'Fichier cadastral':cad.name,'Largeur acces (m)':aw,'Largeur surplomb (m)':ow,'Rayon points (m)':pr})
            st.download_button('Telecharger Parcelles_permis.xlsx',data,'Parcelles_permis.xlsx','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    except Exception as e:st.exception(e)
