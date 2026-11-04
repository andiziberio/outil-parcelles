#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Application Streamlit: KMZ -> parcelles cadastrales belges -> Excel.
Installation:
  pip install streamlit geopandas shapely pyproj requests pandas openpyxl pyogrio
Lancement:
  streamlit run outil_parcelles_kmz.py
"""
from __future__ import annotations
import io, re, tempfile, zipfile
from pathlib import Path
import xml.etree.ElementTree as ET
import geopandas as gpd
import pandas as pd
import requests
import streamlit as st
from shapely.geometry import LineString, Point, Polygon

WFS_URL = "https://ccff02.minfin.fgov.be/geoservices/arcgis/rest/services/INSPIRE/CP/MapServer/exts/InspireFeatureDownload/service"
DEFAULT_TYPENAME = "CP:CadastralParcel"
WORK_CRS = "EPSG:31370"
NS = {"k": "http://www.opengis.net/kml/2.2"}


def parse_coordinates(text):
    result=[]
    for token in (text or "").strip().split():
        values=token.split(",")
        if len(values)>=2:
            try: result.append((float(values[0]),float(values[1])))
            except ValueError: pass
    return result


def read_kmz(uploaded):
    data=uploaded.getvalue() if hasattr(uploaded,"getvalue") else Path(uploaded).read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names=[x for x in archive.namelist() if x.lower().endswith(".kml")]
        if not names: raise ValueError("Aucun fichier KML trouve dans le KMZ.")
        root=ET.fromstring(archive.read(names[0]))
    parent={child:node for node in root.iter() for child in node}
    rows=[]
    for pm in root.findall(".//k:Placemark",NS):
        name=(pm.findtext("k:name",default="",namespaces=NS) or "").strip()
        folders=[]; node=pm
        while node in parent:
            node=parent[node]
            if node.tag.endswith("Folder"):
                folder=(node.findtext("k:name",default="",namespaces=NS) or "").strip()
                if folder: folders.append(folder)
        folder=" / ".join(reversed(folders))
        label=name or folder or "Sans nom"
        for item in pm.findall(".//k:Point",NS):
            c=parse_coordinates(item.findtext("k:coordinates",default="",namespaces=NS))
            if c: rows.append((label,folder,Point(c[0])))
        for item in pm.findall(".//k:LineString",NS):
            c=parse_coordinates(item.findtext("k:coordinates",default="",namespaces=NS))
            if len(c)>=2: rows.append((label,folder,LineString(c)))
        for item in pm.findall(".//k:Polygon",NS):
            outer=item.findtext(".//k:outerBoundaryIs/k:LinearRing/k:coordinates",default="",namespaces=NS)
            shell=parse_coordinates(outer); holes=[]
            for inner in item.findall(".//k:innerBoundaryIs/k:LinearRing/k:coordinates",NS):
                ring=parse_coordinates(inner.text)
                if len(ring)>=4: holes.append(ring)
            if len(shell)>=4:
                geom=Polygon(shell,holes)
                if not geom.is_valid: geom=geom.buffer(0)
                if not geom.is_empty: rows.append((label,folder,geom))
    if not rows: raise ValueError("Aucune geometrie exploitable dans le KMZ.")
    return gpd.GeoDataFrame({"ELEMENT_KMZ":[r[0] for r in rows],"DOSSIER_KMZ":[r[1] for r in rows]},geometry=[r[2] for r in rows],crs="EPSG:4326")


def classify(name,folder,geom_type):
    value=f"{folder} {name}".casefold()
    if re.search(r"acc[eè]s|access|chemin|route|track|tole",value): return "ACCES"
    if re.search(r"tireuse|freineuse|tirage|freinage",value): return "TIREUSE_FREINEUSE"
    if re.search(r"zone.*travaux|travaux|work.?zone|plateforme|stockage",value): return "ZONE_TRAVAUX"
    if re.search(r"surplomb|overhead|ligne|line|port[eé]e|span",value): return "SURPLOMB"
    return "SURPLOMB" if geom_type in ("LineString","MultiLineString") else "AUTRE"


def prepare_impact(raw,access_width,overhead_width,point_radius,include_other):
    gdf=raw.to_crs(WORK_CRS).copy()
    gdf["TYPE_IMPACT"]=[classify(n,f,t) for n,f,t in zip(gdf.ELEMENT_KMZ,gdf.DOSSIER_KMZ,gdf.geom_type)]
    if not include_other: gdf=gdf[gdf.TYPE_IMPACT!="AUTRE"].copy()
    if gdf.empty: raise ValueError("Aucun objet utile reconnu. Activez 'Inclure les objets non classes'.")
    geometries=[]
    for _,row in gdf.iterrows():
        geom=row.geometry
        if geom.geom_type in ("Point","MultiPoint"): geom=geom.buffer(point_radius)
        elif geom.geom_type in ("LineString","MultiLineString"):
            half=(access_width if row.TYPE_IMPACT=="ACCES" else overhead_width)/2
            if half>0: geom=geom.buffer(half,cap_style=2,join_style=2)
        if not geom.is_valid: geom=geom.buffer(0)
        geometries.append(geom)
    gdf.geometry=geometries
    return gdf[~gdf.geometry.is_empty].copy()


def feature_types(url):
    r=requests.get(url,params={"service":"WFS","request":"GetCapabilities","version":"2.0.0"},timeout=90)
    r.raise_for_status(); root=ET.fromstring(r.content); names=[]
    for ft in root.iter():
        if ft.tag.endswith("FeatureType"):
            name=next((x.text.strip() for x in list(ft) if x.tag.endswith("Name") and x.text),None)
            if name: names.append(name)
    return names


def read_response(content,content_type):
    suffix=".geojson" if "json" in content_type.lower() else ".gml"
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/("response"+suffix); path.write_bytes(content)
        return gpd.read_file(path)


def fetch_parcels(impact,url,typename,page_size=5000):
    minx,miny,maxx,maxy=impact.to_crs("EPSG:4326").total_bounds
    # WFS 2.0 with EPSG:4326 uses latitude,longitude axis order.
    bbox=f"{miny},{minx},{maxy},{maxx},urn:ogc:def:crs:EPSG::4326"
    pages=[]; start=0
    while True:
        params={"service":"WFS","version":"2.0.0","request":"GetFeature","typeNames":typename,
                "srsName":"EPSG:4326","bbox":bbox,"count":page_size,"startIndex":start,
                "outputFormat":"application/json"}
        r=requests.get(url,params=params,timeout=180); r.raise_for_status()
        try: page=read_response(r.content,r.headers.get("content-type",""))
        except Exception as exc: raise RuntimeError("Reponse WFS illisible: "+r.text[:400]) from exc
        if page.empty: break
        pages.append(page)
        if len(page)<page_size: break
        start+=page_size
        if start>200000: raise RuntimeError("Emprise trop grande: plus de 200 000 objets.")
    if not pages: raise RuntimeError("Aucune parcelle renvoyee par le WFS.")
    result=gpd.GeoDataFrame(pd.concat(pages,ignore_index=True),crs=pages[0].crs)
    return result[result.geometry.notna() & ~result.geometry.is_empty].to_crs(WORK_CRS)


def find_column(columns,candidates):
    normalized={re.sub(r"[^a-z0-9]","",c.casefold()):c for c in columns}
    for candidate in candidates:
        key=re.sub(r"[^a-z0-9]","",candidate.casefold())
        if key in normalized: return normalized[key]
    for candidate in candidates:
        key=re.sub(r"[^a-z0-9]","",candidate.casefold())
        for norm,original in normalized.items():
            if key in norm: return original
    return None


def analyze(impact,parcels):
    joined=gpd.sjoin(parcels,impact[["ELEMENT_KMZ","DOSSIER_KMZ","TYPE_IMPACT","geometry"]],how="inner",predicate="intersects")
    if joined.empty: return pd.DataFrame()
    joined["SURFACE_INTERSECTION_M2"]=[geom.intersection(impact.geometry.loc[i]).area for geom,i in zip(joined.geometry,joined.index_right)]
    cols=joined.columns
    cap=find_column(cols,["nationalCadastralReference","capakey","parcelKey","localId"])
    num=find_column(cols,["label","parcelNumber","numeroParcelle","perceelnummer"])
    com=find_column(cols,["municipality","commune","gemeente","administrativeUnit"])
    sec=find_column(cols,["section"]); div=find_column(cols,["division","cadastralDivision"])
    def s(column): return joined[column].astype(str) if column else pd.Series("",index=joined.index)
    result=pd.DataFrame({"TYPE_IMPACT":joined.TYPE_IMPACT,"ELEMENT_KMZ":joined.ELEMENT_KMZ,
        "DOSSIER_KMZ":joined.DOSSIER_KMZ,"CAPAKEY":s(cap),"NUMERO_PARCELLE":s(num),
        "COMMUNE":s(com),"DIVISION":s(div),"SECTION":s(sec),
        "SURFACE_INTERSECTION_M2":joined.SURFACE_INTERSECTION_M2.round(2)})
    return result.replace({"nan":"","None":""}).drop_duplicates().sort_values(["TYPE_IMPACT","ELEMENT_KMZ","CAPAKEY"]).reset_index(drop=True)


def workbook(result,parameters):
    output=io.BytesIO()
    summary=result.groupby("TYPE_IMPACT",dropna=False).size().reset_index(name="NOMBRE_INTERSECTIONS")
    with pd.ExcelWriter(output,engine="openpyxl") as writer:
        result.to_excel(writer,index=False,sheet_name="Parcelles")
        summary.to_excel(writer,index=False,sheet_name="Synthese")
        pd.DataFrame(parameters.items(),columns=["PARAMETRE","VALEUR"]).to_excel(writer,index=False,sheet_name="Parametres")
        for sheet in writer.book.worksheets:
            sheet.freeze_panes="A2"; sheet.auto_filter.ref=sheet.dimensions
            for column in sheet.columns:
                sheet.column_dimensions[column[0].column_letter].width=min(max(len(str(c.value or "")) for c in column)+2,55)
    return output.getvalue()


def run(uploaded,access_width,overhead_width,point_radius,url,typename,include_other):
    impact=prepare_impact(read_kmz(uploaded),access_width,overhead_width,point_radius,include_other)
    parcels=fetch_parcels(impact,url,typename)
    result=analyze(impact,parcels)
    params={"WFS":url,"Couche WFS":typename,"CRS calcul":WORK_CRS,"Largeur acces (m)":access_width,
            "Largeur surplomb (m)":overhead_width,"Rayon points (m)":point_radius,
            "Objets KMZ retenus":len(impact),"Parcelles WFS chargees":len(parcels)}
    return result,workbook(result,params)


st.set_page_config(page_title="Parcelles permis KMZ",layout="wide")
st.title("Parcelles cadastrales pour permis de ligne aerienne")
st.write("Chargez un KMZ, interrogez le parcellaire cadastral et exportez les intersections dans Excel.")
uploaded=st.file_uploader("Fichier KMZ",type=["kmz"])
c1,c2,c3=st.columns(3)
access_width=c1.number_input("Largeur totale des acces (m)",0.0,100.0,5.0,0.5)
overhead_width=c2.number_input("Largeur totale du corridor de surplomb (m)",0.0,200.0,0.0,1.0)
point_radius=c3.number_input("Rayon autour des points (m)",0.0,100.0,5.0,1.0)
with st.expander("Parametres avances"):
    url=st.text_input("URL WFS",WFS_URL)
    if st.button("Detecter les couches WFS"):
        try: st.session_state["types"]=feature_types(url); st.success(f"{len(st.session_state['types'])} couche(s) detectee(s).")
        except Exception as exc: st.error(str(exc))
    options=st.session_state.get("types",[])
    typename=st.selectbox("Couche WFS",options,index=0) if options else st.text_input("Couche WFS",DEFAULT_TYPENAME)
    include_other=st.checkbox("Inclure les objets KMZ non classes",False)
if st.button("Analyser les parcelles",type="primary",disabled=uploaded is None):
    try:
        with st.spinner("Lecture du KMZ, telechargement cadastral et intersections..."):
            result,xlsx=run(uploaded,access_width,overhead_width,point_radius,url,typename,include_other)
        if result.empty: st.warning("Aucune intersection trouvee.")
        else:
            distinct=result.CAPAKEY[result.CAPAKEY!=""].nunique()
            st.success(f"{len(result)} intersections trouvees; {distinct} CAPAKEY distinctes.")
            st.dataframe(result,use_container_width=True,hide_index=True)
            st.download_button("Telecharger Parcelles_permis.xlsx",xlsx,"Parcelles_permis.xlsx","application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception as exc: st.exception(exc)
