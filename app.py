import streamlit as st
import math
import requests
import io
import ezdxf
from ezdxf.enums import TextEntityAlignment
from pyproj import Transformer
import folium
from streamlit_folium import st_folium
from shapely.geometry import Polygon, Point, LineString
from shapely.ops import unary_union, split
import shapely.wkt
import shapely.affinity as affinity

st.set_page_config(layout="wide", page_title="AI GeoSplitter Web", page_icon="🗺️")

# --- STAN APLIKACJI (SESSION STATE) ---
if 'original_parcels' not in st.session_state:
    st.session_state.update({
        'original_parcels': [], 'main_polygon': None, 'edges': [],
        'public_road_idx': None, 'inner_road_indices': [],
        'sub_parcels': [], 'remainder_parcel': None, 'road_polygon': None,
        'cut_lines': [], 'centroid_wgs84': [52.0, 19.0], 'zoom_start': 6,
        'picking_mode': 'Oczekiwanie', 'epsg_code': 'EPSG:2178'
    })

# ==========================================
# 1. FUNKCJE POMOCNICZE I MATEMATYCZNE
# ==========================================
def _extend_polyline(line, dist=2000):
    if line.geom_type == 'MultiLineString':
        try: line = shapely.ops.linemerge(line)
        except: line = line.geoms[0]
    c = list(line.coords)
    if len(c) < 2: return line
    dx1, dy1 = c[0][0]-c[1][0], c[0][1]-c[1][1]
    l1 = math.hypot(dx1, dy1)
    p_start = (c[0][0] + dx1/l1*dist, c[0][1] + dy1/l1*dist) if l1 else c[0]
    dx2, dy2 = c[-1][0]-c[-2][0], c[-1][1]-c[-2][1]
    l2 = math.hypot(dx2, dy2)
    p_end = (c[-1][0] + dx2/l2*dist, c[-1][1] + dy2/l2*dist) if l2 else c[-1]
    return LineString([p_start] + c[1:-1] + [p_end])

def _cut_parcel(poly, v_cut, v_sweep, target_area, cut_from_back=False):
    cx, cy, sx, sy = v_cut[0], v_cut[1], v_sweep[0], v_sweep[1]
    c_point = poly.centroid
    coords = []
    if poly.geom_type == 'Polygon': coords = list(poly.exterior.coords)
    elif hasattr(poly, 'geoms'):
        for g in poly.geoms:
            if g.geom_type == 'Polygon': coords.extend(list(g.exterior.coords))
    if not coords: return None, None, None

    projections = [(x - c_point.x)*sx + (y - c_point.y)*sy for x, y in coords]
    t_low, t_high = min(projections), max(projections)
    best_cut, best_rem, best_line, best_diff = None, None, None, float('inf')

    for _ in range(45):
        t_mid = (t_low + t_high) / 2
        px, py = c_point.x + t_mid * sx, c_point.y + t_mid * sy
        line = LineString([(px - cx*10000, py - cy*10000), (px + cx*10000, py + cy*10000)])
        try: split_res = split(poly, line)
        except: continue

        if len(split_res.geoms) >= 2:
            g_front, g_back = [], []
            for g in split_res.geoms:
                proj = (g.centroid.x - c_point.x)*sx + (g.centroid.y - c_point.y)*sy
                if proj < t_mid: g_front.append(g)
                else: g_back.append(g)
                
            part_front = unary_union(g_front) if g_front else None
            part_back = unary_union(g_back) if g_back else None

            if part_front and part_back:
                test_part = part_back if cut_from_back else part_front
                diff = abs(test_part.area - target_area)
                if diff < best_diff:
                    best_diff, best_line = diff, line
                    best_cut = part_back if cut_from_back else part_front
                    best_rem = part_front if cut_from_back else part_back
                if diff < 0.5: break
                elif test_part.area < target_area: 
                    if cut_from_back: t_high = t_mid
                    else: t_low = t_mid
                else: 
                    if cut_from_back: t_low = t_mid
                    else: t_high = t_mid
    return best_cut, best_rem, best_line

# ==========================================
# 2. POBIERANIE Z GUGIK I TRANSFORMACJE
# ==========================================
def fetch_data(ids_str):
    ids = [i.strip() for i in ids_str.split(',') if i.strip()]
    if not ids: return
    raw_polygons = []
    for teryt_id in ids:
        url = f"https://uldk.gugik.gov.pl/?request=GetParcelById&id={teryt_id}&result=geom_wkt"
        try:
            res = requests.get(url, timeout=10)
            if res.status_code == 200 and 'POLYGON' in res.text:
                wkt_str = res.text.strip().split('\n')[-1].replace('SRID=2180;', '')
                raw_geom = shapely.wkt.loads(wkt_str)
                raw_polygons.append((teryt_id, raw_geom))
        except: pass

    if not raw_polygons:
        st.error("Nie znaleziono działek. Sprawdź numery TERYT.")
        return

    temp_union = unary_union([g for _, g in raw_polygons])
    trans_to_wgs = Transformer.from_crs("EPSG:2180", "EPSG:4326", always_xy=True)
    lon, lat = trans_to_wgs.transform(temp_union.centroid.x, temp_union.centroid.y)
    
    st.session_state.centroid_wgs84 = [lat, lon]
    st.session_state.zoom_start = 18
    
    epsg_2000 = "EPSG:2176" if lon < 16.5 else "EPSG:2177" if lon < 19.5 else "EPSG:2178" if lon < 22.5 else "EPSG:2179"
    st.session_state.epsg_code = epsg_2000 
    
    trans_to_2000 = Transformer.from_crs("EPSG:2180", epsg_2000, always_xy=True)

    original_2000 = []
    for tid, geom in raw_polygons:
        if geom.geom_type == 'Polygon':
            ext = [trans_to_2000.transform(x, y) for x, y in geom.exterior.coords]
            geom_2000 = Polygon(ext)
            original_2000.append({'id': tid, 'geom': geom_2000, 'area': geom_2000.area})

    st.session_state.original_parcels = original_2000
    main_poly = unary_union([p['geom'] for p in original_2000])
    st.session_state.main_polygon = main_poly
    
    edges = []
    if main_poly.geom_type == 'Polygon':
        c = list(main_poly.exterior.coords)
        for i in range(len(c) - 1): edges.append(LineString([c[i], c[i+1]]))
    st.session_state.edges = edges
    
    st.session_state.public_road_idx = None
    st.session_state.inner_road_indices = []
    st.session_state.sub_parcels = []
    st.session_state.road_polygon = None
    st.session_state.remainder_parcel = None
    st.session_state.picking_mode = 'Oczekiwanie'
    st.success(f"Pobrano {len(original_2000)} działek. Gotowe do wyboru krawędzi.")

# ==========================================
# 3. GENEROWANIE KONCEPCJI (NA ŻĄDANIE)
# ==========================================
def run_design():
    if not st.session_state.main_polygon or st.session_state.public_road_idx is None or not st.session_state.inner_road_indices:
        st.warning("Przed przeliczeniem wskaż na mapie Drogę Gminną (czerwona) i Wewnętrzną (pomarańczowa).")
        return

    # Pobieranie parametrów z bezpieczeństwem braku wartości (get)
    rw = st.session_state.get('road_width', 8.0)
    is_middle = st.session_state.get('road_pos', "Przy krawędzi (Bok)") == "Środek Działki"
    stop_at_last = st.session_state.get('road_end', "Do samego końca działki") == "Zatrzymaj przed ostatnią działką"
    last_area = st.session_state.get('last_area', 1500.0)
    turnaround = st.session_state.get('turnaround', False)
    t_size = st.session_state.get('t_size', 12.5)
    cut_angle = st.session_state.get('cut_angle', "Prostopadle do drogi wewn.")
    div_type = st.session_state.get('div_type', "Docelowa pow. (m²)")
    target_area = st.session_state.get('target_area', 1000.0)
    exact_count = st.session_state.get('exact_count', 5) if div_type != "Docelowa pow. (m²)" else None
    rem_mode = st.session_state.get('remainder_mode', "Wydziel osobną resztówkę")

    main_poly = st.session_state.main_polygon
    edges = st.session_state.edges
    
    st.session_state.sub_parcels, st.session_state.remainder_parcel, st.session_state.cut_lines = [], None, []

    inner_lines = [edges[i] for i in sorted(st.session_state.inner_road_indices)]
    inner_path = unary_union(inner_lines)
    if inner_path.geom_type == 'MultiLineString':
        try: inner_path = shapely.ops.linemerge(inner_path)
        except: inner_path = inner_lines[0]
        
    ext_path = _extend_polyline(inner_path, 2000)
    c_overall = list(ext_path.coords)
    dx_ov, dy_ov = c_overall[-1][0]-c_overall[0][0], c_overall[-1][1]-c_overall[0][1]
    l_len = math.hypot(dx_ov, dy_ov)
    nx, ny = -dy_ov/l_len, dx_ov/l_len 
    
    cx, cy = main_poly.centroid.x - c_overall[0][0], main_poly.centroid.y - c_overall[0][1]
    if cx*nx + cy*ny < 0: nx, ny = -nx, -ny

    if is_middle:
        projections = [(pt[0]-c_overall[0][0])*nx + (pt[1]-c_overall[0][1])*ny for pt in main_poly.exterior.coords]
        offset = max(projections) / 2
        road_centerline = affinity.translate(ext_path, xoff=nx*offset, yoff=ny*offset)
        full_road_base = road_centerline.buffer(rw/2, cap_style=2)
    else:
        full_road_base = ext_path.buffer(rw, cap_style=2)

    pub_edge = edges[st.session_state.public_road_idx]
    px1, px2 = pub_edge.coords[0], pub_edge.coords[1]
    pdx, pdy = px2[0]-px1[0], px2[1]-px1[1]
    plen = math.hypot(pdx, pdy)
    
    if cut_angle == "Prostopadle do drogi wewn.": v_cut = (nx, ny)
    elif cut_angle == "Prostopadle do drogi gminnej": v_cut = (-pdy/plen, pdx/plen)
    else: v_cut = (pdx/plen, pdy/plen)

    v_sweep = (-v_cut[1], v_cut[0])
    if (main_poly.centroid.x - pub_edge.centroid.x)*v_sweep[0] + (main_poly.centroid.y - pub_edge.centroid.y)*v_sweep[1] < 0:
        v_sweep = (-v_sweep[0], -v_sweep[1])

    back_parcel, working_polygon = None, main_poly
    if stop_at_last:
        back_parcel, working_polygon, _ = _cut_parcel(main_poly, v_cut, v_sweep, last_area, cut_from_back=True)
        if back_parcel is None: working_polygon = main_poly

    road_poly = full_road_base.intersection(working_polygon)

    if turnaround and road_poly.geom_type == 'Polygon':
        try:
            if is_middle: c_line = road_centerline
            else: c_line = affinity.translate(ext_path, xoff=nx*(rw/2), yoff=ny*(rw/2))
                
            clipped_c = c_line.intersection(working_polygon)
            if clipped_c.geom_type == 'MultiLineString':
                lines = list(clipped_c.geoms)
                lines.sort(key=lambda l: pub_edge.distance(Point(l.coords[-1])))
                clipped_c = lines[-1]
                
            if clipped_c.geom_type == 'LineString':
                coords = list(clipped_c.coords)
                end_pt, prev_pt = (coords[-1], coords[-2]) if pub_edge.distance(Point(coords[-1])) > pub_edge.distance(Point(coords[0])) else (coords[0], coords[1])
                rx, ry = prev_pt[0] - end_pt[0], prev_pt[1] - end_pt[1]
                l = math.hypot(rx, ry)
                rx, ry = (rx/l, ry/l) if l > 0 else (0, 0)
                
                lnx, lny = -ry, rx
                if lnx*nx + lny*ny < 0: lnx, lny = -lnx, -lny
                
                if is_middle:
                    C1, C2 = (end_pt[0] + lnx*(t_size/2), end_pt[1] + lny*(t_size/2)), (end_pt[0] - lnx*(t_size/2), end_pt[1] - lny*(t_size/2))
                else:
                    P_edge = (end_pt[0] - lnx*(rw/2), end_pt[1] - lny*(rw/2))
                    C1 = P_edge
                    C2 = (C1[0] + lnx*t_size, C1[1] + lny*t_size)
                    
                C3, C4 = (C2[0] + rx*t_size, C2[1] + ry*t_size), (C1[0] + rx*t_size, C1[1] + ry*t_size)
                
                turnaround_poly = Polygon([C1, C2, C3, C4])
                road_poly = unary_union([road_poly, turnaround_poly]).intersection(working_polygon)
                if back_parcel: back_parcel = back_parcel.difference(turnaround_poly)
        except: pass

    st.session_state.road_polygon = road_poly
    net_poly = working_polygon.difference(road_poly)

    geoms = [net_poly] if net_poly.geom_type == 'Polygon' else net_poly.geoms
    
    for geom in geoms:
        rem_poly = geom
        cuts_needed = exact_count - 1 if exact_count else 999
        cuts_made = 0
        
        side_target = target_area
        if rem_mode == "Rozrzuć po równo na wszystkie" and not exact_count:
            num_fit = int(rem_poly.area // target_area)
            if num_fit > 0: side_target = rem_poly.area / num_fit
        
        while rem_poly.area > 5.0:
            if exact_count:
                if cuts_made >= cuts_needed: break
                current_target = rem_poly.area / (exact_count - cuts_made)
            else:
                if rem_poly.area <= side_target * 1.05: break
                current_target = side_target

            cut_p, rem_poly, line = _cut_parcel(rem_poly, v_cut, v_sweep, current_target)
            if cut_p:
                st.session_state.sub_parcels.append(cut_p)
                st.session_state.cut_lines.append(line)
                cuts_made += 1
            else: break

        if rem_poly and rem_poly.area > 5.0:
            if exact_count: st.session_state.sub_parcels.append(rem_poly)
            else:
                if rem_mode == "Dołącz do ostatniej działki" and st.session_state.sub_parcels:
                    st.session_state.sub_parcels[-1] = unary_union([st.session_state.sub_parcels[-1], rem_poly])
                elif rem_mode == "Rozrzuć po równo na wszystkie":
                    st.session_state.sub_parcels.append(rem_poly)
                else:
                    st.session_state.remainder_parcel = rem_poly

    if back_parcel: st.session_state.sub_parcels.append(back_parcel)


# ==========================================
# 4. INTERFEJS STREAMLIT (UI)
# ==========================================
st.title("🗺️ AI GeoSplitter - Generative Design")

# === PODZIAŁ NA 3 KOLUMNY ===
col_left, col_mid, col_right = st.columns([1.2, 2.5, 1.2])

# --- LEWA KOLUMNA: Ustawienia (Brak wywołań 'on_change') ---
with col_left:
    # GŁÓWNY PRZYCISK NA SAMEJ GÓRZE
    if st.button("🚀 Przelicz Projekt", type="primary", use_container_width=True):
        run_design()

    st.markdown("<br>", unsafe_allow_html=True) # Odstęp

    with st.expander("1. GUGiK Data (EPSG:2000)", expanded=True):
        ids_input = st.text_input("ID Działki (TERYT):", "143411_4.0001.172")
        if st.button("Pobierz Geometrię"): fetch_data(ids_input)

    with st.expander("2. Krawędzie (Map Picking)", expanded=True):
        c_m1, c_m2 = st.columns(2)
        with c_m1:
            if st.button("🔴 Gminna (1x)"): st.session_state.picking_mode = 'public'
        with c_m2:
            if st.button("🟠 Wewn. (Wiele)"): st.session_state.picking_mode = 'inner'
        st.markdown(f"Tryb wyboru: **{st.session_state.picking_mode}**")
            
    with st.expander("3. Architektura Drogi", expanded=True):
        st.slider("Szerokość drogi (m):", 5.0, 15.0, 8.0, key='road_width')
        st.selectbox("Pozycja:", ["Przy krawędzi (Bok)", "Środek Działki"], key='road_pos')
        road_end = st.selectbox("Zakończenie:", ["Do samego końca działki", "Zatrzymaj przed ostatnią działką"], key='road_end')
        
        if road_end == "Zatrzymaj przed ostatnią działką":
            st.number_input("Pow. ost. działki (m²):", min_value=1.0, step=50.0, value=1500.0, key='last_area')
            
        turnaround = st.checkbox("Dodaj plac do zawracania", key='turnaround')
        if turnaround:
            st.number_input("Wymiar placu (m):", min_value=1.0, step=0.5, value=12.5, key='t_size')

    with st.expander("4. Parametry Cięcia", expanded=True):
        st.selectbox("Kierunek linii:", ["Prostopadle do drogi wewn.", "Prostopadle do drogi gminnej", "Równolegle do drogi gminnej"], key='cut_angle')
        div_type = st.radio("Metoda podziału:", ["Docelowa pow. (m²)", "Liczba równych działek"], key='div_type')
        
        if div_type == "Docelowa pow. (m²)":
            st.number_input("Docelowa pow.:", min_value=1.0, step=50.0, value=1000.0, key='target_area')
            st.selectbox("Resztówka:", ["Wydziel osobną resztówkę", "Rozrzuć po równo na wszystkie", "Dołącz do ostatniej działki"], key='remainder_mode')
        else:
            st.number_input("Liczba działek:", min_value=1, step=1, value=5, key='exact_count')


# --- ŚRODKOWA KOLUMNA: Mapa Folium ---
with col_mid:
    m = folium.Map(location=st.session_state.centroid_wgs84, zoom_start=st.session_state.zoom_start, tiles="CartoDB positron")
    
    if st.session_state.main_polygon:
        epsg = st.session_state.get('epsg_code', 'EPSG:2178')
        trans = Transformer.from_crs(epsg, "EPSG:4326", always_xy=True)
        all_wgs_coords = []
        
        for idx, edge in enumerate(st.session_state.edges):
            c_wgs = [trans.transform(x, y)[::-1] for x, y in edge.coords] 
            all_wgs_coords.extend(c_wgs)
            color, weight = '#333333', 3
            if idx == st.session_state.public_road_idx: color, weight = 'red', 5
            elif idx in st.session_state.inner_road_indices: color, weight = 'orange', 5
            folium.PolyLine(locations=c_wgs, color=color, weight=weight, tooltip=f"Krawędź nr: {idx}").add_to(m)

        if st.session_state.sub_parcels:
            for i, p in enumerate(st.session_state.sub_parcels):
                p_geoms = p.geoms if hasattr(p, 'geoms') else [p]
                for g in p_geoms:
                    if g.geom_type == 'Polygon':
                        c_wgs = [trans.transform(x, y)[::-1] for x, y in g.exterior.coords]
                        folium.Polygon(locations=c_wgs, color='blue', weight=2, fill=False).add_to(m)
                        wgs_cent = trans.transform(g.centroid.x, g.centroid.y)[::-1]
                        label_html = f"<div style='font-family: Arial; font-size: 11px; font-weight: bold; color: black; text-align: center; text-shadow: 1px 1px 2px white, -1px -1px 2px white, 1px -1px 2px white, -1px 1px 2px white;'>Dz. {i+1}<br>{p.area:.0f}</div>"
                        folium.Marker(location=wgs_cent, icon=folium.DivIcon(html=label_html, icon_size=(100, 30), icon_anchor=(50, 15))).add_to(m)
        
        if st.session_state.road_polygon:
            r_geoms = st.session_state.road_polygon.geoms if hasattr(st.session_state.road_polygon, 'geoms') else [st.session_state.road_polygon]
            for g in r_geoms:
                if g.geom_type == 'Polygon':
                    c_wgs = [trans.transform(x, y)[::-1] for x, y in g.exterior.coords]
                    folium.Polygon(locations=c_wgs, color='#ff8800', weight=2, fill=False).add_to(m)
                    
        if st.session_state.remainder_parcel:
            rem_geoms = st.session_state.remainder_parcel.geoms if hasattr(st.session_state.remainder_parcel, 'geoms') else [st.session_state.remainder_parcel]
            for g in rem_geoms:
                if g.geom_type == 'Polygon':
                    c_wgs = [trans.transform(x, y)[::-1] for x, y in g.exterior.coords]
                    folium.Polygon(locations=c_wgs, color='red', weight=2, dash_array='5, 5', fill=False).add_to(m)
                    wgs_cent = trans.transform(g.centroid.x, g.centroid.y)[::-1]
                    label_html = f"<div style='font-family: Arial; font-size: 10px; font-weight: bold; color: red; text-align: center; text-shadow: 1px 1px 1px white;'>Reszta<br>{g.area:.0f}</div>"
                    folium.Marker(location=wgs_cent, icon=folium.DivIcon(html=label_html, icon_size=(80, 30), icon_anchor=(40, 15))).add_to(m)
            
        if all_wgs_coords: m.fit_bounds(all_wgs_coords)

    st_data = st_folium(m, width=700, height=800)
    
    # Przechwytywanie kliknięć na krawędzie
    if st_data['last_object_clicked_tooltip']:
        try:
            clicked_idx = int(st_data['last_object_clicked_tooltip'].replace("Krawędź nr: ", ""))
            if st.session_state.picking_mode == 'public':
                st.session_state.public_road_idx = clicked_idx
                st.session_state.picking_mode = 'Oczekiwanie'
                st.rerun()
            elif st.session_state.picking_mode == 'inner':
                if clicked_idx in st.session_state.inner_road_indices:
                    st.session_state.inner_road_indices.remove(clicked_idx)
                else:
                    st.session_state.inner_road_indices.append(clicked_idx)
                st.rerun()
        except: pass


# --- PRAWA KOLUMNA: Raport ---
with col_right:
    st.markdown("### 📊 Raport Koncepcji")
    
    if st.session_state.main_polygon and st.session_state.sub_parcels:
        tot_area = st.session_state.main_polygon.area
        road_area = st.session_state.road_polygon.area if st.session_state.road_polygon else 0
        net_area = tot_area - road_area
        
        st.info(f"**Łączna pow. działek:** {tot_area:.0f} m²\n\n"
                f"**Powierzchnia drogi:** {road_area:.0f} m²\n\n"
                f"**Pow. na działki budowlane:** {net_area:.0f} m²")
                
        if st.session_state.remainder_parcel:
            st.error(f"**Resztówka:** {st.session_state.remainder_parcel.area:.0f} m²")
            
        st.markdown("---")
        st.markdown("#### Wykaz Działek:")
        
        for i, p in enumerate(st.session_state.sub_parcels):
            mbr = p.minimum_rotated_rectangle
            c = list(mbr.exterior.coords)
            s1 = math.hypot(c[1][0]-c[0][0], c[1][1]-c[0][1])
            s2 = math.hypot(c[2][0]-c[1][0], c[2][1]-c[1][1])
            w, l = min(s1, s2), max(s1, s2)
            
            with st.expander(f"Dz. {i+1} | {p.area:.0f} m²", expanded=False):
                st.write(f"**Wymiary:** ~{w:.1f} m x {l:.1f} m")
                st.write("**Skład ewidencji:**")
                for orig in st.session_state.original_parcels:
                    try:
                        inter = p.intersection(orig['geom'])
                        if inter.area > 1.0:
                            st.caption(f"- z działki {orig['id'].split('.')[-1]}: {inter.area:.0f} m²")
                    except: pass

        st.markdown("---")
        # Przycisk Eksportu DXF
        try:
            doc = ezdxf.new('R2010')
            msp = doc.modelspace()
            doc.layers.add(name='GRANICE_PROJ', color=3)
            doc.layers.add(name='DROGA_PROJ', color=2)
            doc.layers.add(name='WYMIARY', color=1)
            doc.layers.add(name='OPISY', color=7)
            doc.layers.add(name='EWIDENCJA', color=8).off()

            for orig in st.session_state.original_parcels:
                if orig['geom'].geom_type == 'Polygon': msp.add_lwpolyline(list(orig['geom'].exterior.coords), close=True, dxfattribs={'layer': 'EWIDENCJA'})
            
            if st.session_state.road_polygon:
                r_geoms = st.session_state.road_polygon.geoms if hasattr(st.session_state.road_polygon, 'geoms') else [st.session_state.road_polygon]
                for g in r_geoms:
                    if g.geom_type == 'Polygon': msp.add_lwpolyline(list(g.exterior.coords), close=True, dxfattribs={'layer': 'DROGA_PROJ'})
            
            for i, p in enumerate(st.session_state.sub_parcels):
                p_geoms = p.geoms if hasattr(p, 'geoms') else [p]
                for poly in p_geoms:
                    if poly.geom_type != 'Polygon': continue
                    c = list(poly.exterior.coords)
                    msp.add_lwpolyline(c, close=True, dxfattribs={'layer': 'GRANICE_PROJ'})
                    msp.add_text(f"Dz.{i+1} pow. {poly.area:.0f} m2", dxfattribs={'layer': 'OPISY', 'height': 2.0}).set_placement((poly.centroid.x, poly.centroid.y))
                    for j in range(len(c)-1):
                        dx, dy = c[j+1][0]-c[j][0], c[j+1][1]-c[j][1]
                        dist = math.hypot(dx, dy)
                        if dist > 2.0:
                            adeg = math.degrees(math.atan2(dy, dx))
                            if adeg > 90 or adeg <= -90: adeg += 180
                            dt = msp.add_text(f"-{dist:.2f}-", dxfattribs={'layer': 'WYMIARY', 'height': 1.0, 'rotation': adeg})
                            dt.set_placement(((c[j][0]+c[j+1][0])/2, (c[j][1]+c[j+1][1])/2), align=TextEntityAlignment.MIDDLE_CENTER)

            buffer = io.StringIO()
            doc.write(buffer)
            st.download_button("💾 Eksportuj do DXF", data=buffer.getvalue(), file_name="geo_podzial.dxf", mime="application/dxf", type="primary", use_container_width=True)
        except Exception as e: st.error(f"Błąd przygotowania DXF: {e}")
        
    else:
        st.write("Wskaż krawędzie i kliknij 'Przelicz Projekt' po lewej stronie.")
