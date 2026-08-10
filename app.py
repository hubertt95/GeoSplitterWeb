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
        'public_road_idx': None, 'inner_road_idx': None,
        'sub_parcels': [], 'remainder_parcel': None, 'road_polygon': None,
        'cut_lines': [], 'centroid_wgs84': [52.0, 19.0], 'zoom_start': 6,
        'picking_mode': 'Oczekiwanie'
    })

# ==========================================
# 1. FUNKCJE POMOCNICZE I MATEMATYCZNE
# ==========================================
def _extend_line(line, dist=2000):
    c = list(line.coords)
    dx1, dy1 = c[0][0]-c[1][0], c[0][1]-c[1][1]
    l1 = math.hypot(dx1, dy1)
    p_start = (c[0][0] + dx1/l1*dist, c[0][1] + dy1/l1*dist) if l1 else c[0]
    dx2, dy2 = c[-1][0]-c[-2][0], c[-1][1]-c[-2][1]
    l2 = math.hypot(dx2, dy2)
    p_end = (c[-1][0] + dx2/l2*dist, c[-1][1] + dy2/l2*dist) if l2 else c[-1]
    return LineString([p_start] + c + [p_end])

def _cut_parcel(poly, v_cut, v_sweep, target_area, cut_from_back=False):
    cx, cy, sx, sy = v_cut[0], v_cut[1], v_sweep[0], v_sweep[1]
    c_point = poly.centroid
    coords = list(poly.exterior.coords)
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
        st.error("Nie znaleziono działek. Sprawdź TERYT.")
        return

    # EPSG:2180 to EPSG:2000 & WGS84
    temp_union = unary_union([g for _, g in raw_polygons])
    trans_to_wgs = Transformer.from_crs("EPSG:2180", "EPSG:4326", always_xy=True)
    lon, lat = trans_to_wgs.transform(temp_union.centroid.x, temp_union.centroid.y)
    
    st.session_state.centroid_wgs84 = [lat, lon]
    st.session_state.zoom_start = 18
    
    epsg_2000 = "EPSG:2176" if lon < 16.5 else "EPSG:2177" if lon < 19.5 else "EPSG:2178" if lon < 22.5 else "EPSG:2179"
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
    st.session_state.inner_road_idx = None
    st.session_state.sub_parcels = []
    st.session_state.picking_mode = 'Oczekiwanie'
    st.success(f"Pobrano {len(original_2000)} działek. Gotowe do wyboru krawędzi.")

# ==========================================
# 3. GENEROWANIE KONCEPCJI
# ==========================================
def run_design(road_width, road_pos, road_end, turnaround, t_size, last_area, cut_angle, target_area, exact_count, remainder_mode):
    if not st.session_state.main_polygon or st.session_state.public_road_idx is None or st.session_state.inner_road_idx is None:
        st.warning("Najpierw wskaż na mapie Drogę Gminną (kliknij '1') i Wewnętrzną (kliknij '2').")
        return

    main_poly = st.session_state.main_polygon
    edges = st.session_state.edges
    rw = road_width
    is_middle = road_pos == "Środek Działki"
    stop_at_last = road_end == "Zatrzymaj przed ostatnią działką"
    
    st.session_state.sub_parcels, st.session_state.remainder_parcel, st.session_state.cut_lines = [], None, []

    inner_path = edges[st.session_state.inner_road_idx]
    ext_path = _extend_line(inner_path, 2000)
    c = list(ext_path.coords)
    dx, dy = c[-1][0]-c[0][0], c[-1][1]-c[0][1]
    l_len = math.hypot(dx, dy)
    nx, ny = -dy/l_len, dx/l_len 
    
    cx, cy = main_poly.centroid.x - c[0][0], main_poly.centroid.y - c[0][1]
    if cx*nx + cy*ny < 0: nx, ny = -nx, -ny

    if is_middle:
        projections = [(pt[0]-c[0][0])*nx + (pt[1]-c[0][1])*ny for pt in main_poly.exterior.coords]
        offset = max(projections) / 2
        road_centerline = affinity.translate(ext_path, xoff=nx*offset, yoff=ny*offset)
        
        p1, p2 = road_centerline.coords[0], road_centerline.coords[-1]
        r1, r2 = (p1[0] - nx*(rw/2), p1[1] - ny*(rw/2)), (p2[0] - nx*(rw/2), p2[1] - ny*(rw/2))
        r3, r4 = (p2[0] + nx*(rw/2), p2[1] + ny*(rw/2)), (p1[0] + nx*(rw/2), p1[1] + ny*(rw/2))
        full_road_base = Polygon([r1, r2, r3, r4])
    else:
        p1, p2 = ext_path.coords[0], ext_path.coords[-1]
        p3, p4 = (p2[0] + nx*rw, p2[1] + ny*rw), (p1[0] + nx*rw, p1[1] + ny*rw)
        full_road_base = Polygon([p1, p2, p3, p4])

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
            c_line = road_centerline if is_middle else affinity.translate(ext_path, xoff=nx*(rw/2), yoff=ny*(rw/2))
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
                
                if is_middle:
                    C1, C2 = (end_pt[0] + nx*(t_size/2), end_pt[1] + ny*(t_size/2)), (end_pt[0] - nx*(t_size/2), end_pt[1] - ny*(t_size/2))
                else:
                    C1 = (end_pt[0] - nx*(rw/2), end_pt[1] - ny*(rw/2))
                    C2 = (C1[0] + nx*t_size, C1[1] + ny*t_size)
                    
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
        if remainder_mode == "Rozrzuć po równo na wszystkie" and not exact_count:
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
                if remainder_mode == "Dołącz do ostatniej działki" and st.session_state.sub_parcels:
                    st.session_state.sub_parcels[-1] = unary_union([st.session_state.sub_parcels[-1], rem_poly])
                elif remainder_mode == "Rozrzuć po równo na wszystkie":
                    st.session_state.sub_parcels.append(rem_poly)
                else:
                    st.session_state.remainder_parcel = rem_poly

    if back_parcel: st.session_state.sub_parcels.append(back_parcel)

# ==========================================
# 4. INTERFEJS STREAMLIT (UI)
# ==========================================
st.title("🗺️ AI GeoSplitter - Generative Design")

# PANEL GŁÓWNY (Lewa: Ustawienia, Prawa: Mapa)
col1, col2 = st.columns([1, 2])

with col1:
    with st.expander("1. GUGiK Data (EPSG:2000)", expanded=True):
        ids_input = st.text_input("ID Działki (TERYT):", "143411_4.0001.172")
        if st.button("Pobierz Geometrię"): fetch_data(ids_input)

    with st.expander("2. Interaktywny Kontekst z Mapy", expanded=True):
        st.write("Wybierz na mapie numery linii dla odniesienia:")
        c_mode1, c_mode2 = st.columns(2)
        with c_mode1:
            if st.button("🔴 Wybierz Gminną"): st.session_state.picking_mode = 'public'
        with c_mode2:
            if st.button("🟠 Wybierz Wewnętrzną"): st.session_state.picking_mode = 'inner'
            
        st.info(f"Obecnie klikasz na mapie by przypisać: **{st.session_state.picking_mode}**")
        if st.button("Przelicz i Rysuj Podział", type="primary"):
            # Parametry pobrane z UI
            pass # Wykonanie poniżej w bloku UI
            
    with st.expander("3. Architektura Drogi", expanded=False):
        road_width = st.slider("Szerokość drogi (m):", 5.0, 15.0, 8.0)
        road_pos = st.selectbox("Pozycja:", ["Przy krawędzi (Bok)", "Środek Działki"])
        road_end = st.selectbox("Zakończenie:", ["Do samego końca działki", "Zatrzymaj przed ostatnią działką"])
        last_area = st.number_input("Pow. ost. działki (m²):", value=1500)
        turnaround = st.checkbox("Dodaj plac do zawracania")
        t_size = st.number_input("Wymiar placu (m):", value=12.5)

    with st.expander("4. Parametry Cięcia", expanded=False):
        cut_angle = st.selectbox("Kierunek linii:", ["Prostopadle do drogi wewn.", "Prostopadle do drogi gminnej", "Równolegle do drogi gminnej"])
        div_type = st.radio("Metoda:", ["Docelowa pow. (m²)", "Liczba równych działek"])
        target_area = st.number_input("Docelowa pow.:", value=1000) if div_type == "Docelowa pow. (m²)" else 1000
        exact_count = st.number_input("Liczba działek:", value=5, min_value=1) if div_type != "Docelowa pow. (m²)" else None
        remainder_mode = st.selectbox("Resztówka:", ["Rozrzuć po równo na wszystkie", "Wydziel osobną resztówkę", "Dołącz do ostatniej działki"])

    if st.button("🚀 Wygeneruj Projekt Podziału", use_container_width=True, type="primary"):
        run_design(road_width, road_pos, road_end, turnaround, t_size, last_area, cut_angle, target_area, exact_count, remainder_mode)

    # EKSPORT DXF (Przycisk pobierania Streamlit)
    if st.session_state.sub_parcels:
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
                msp.add_lwpolyline(list(st.session_state.road_polygon.exterior.coords), close=True, dxfattribs={'layer': 'DROGA_PROJ'})
            for i, p in enumerate(st.session_state.sub_parcels):
                c = list(p.exterior.coords)
                msp.add_lwpolyline(c, close=True, dxfattribs={'layer': 'GRANICE_PROJ'})
                msp.add_text(f"Dz.{i+1} pow. {p.area:.0f} m2", dxfattribs={'layer': 'OPISY', 'height': 2.0}).set_placement((p.centroid.x, p.centroid.y))
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
            st.download_button("💾 Pobierz DXF (EPSG:2000)", data=buffer.getvalue(), file_name="geo_podzial.dxf", mime="application/dxf")
        except Exception as e: st.error(f"Błąd DXF: {e}")

# --- MAPA FOLIUM (PRAWA STRONA) ---
with col2:
    m = folium.Map(location=st.session_state.centroid_wgs84, zoom_start=st.session_state.zoom_start, tiles="CartoDB positron")
    
    # Rysowanie bazowej działki jako obrysu
    if st.session_state.main_polygon:
        trans_to_wgs = Transformer.from_crs("EPSG:2000", "EPSG:4326", always_xy=True)
        # Hack by wykryć z której strefy jesteśmy (uproszczony dla wizu)
        epsg_detect = "EPSG:2177" # Można dynamicznie poprawić
        trans = Transformer.from_crs(epsg_detect, "EPSG:4326", always_xy=True)
        
        # Aby ułatwić klikanie w sieci - każda linia granicy jest osobną, grubą, klikalną linią w Folium z tooltipem!
        for idx, edge in enumerate(st.session_state.edges):
            c_wgs = [trans.transform(x, y)[::-1] for x, y in edge.coords] # Folium to [lat, lon]
            
            color = 'black'
            if idx == st.session_state.public_road_idx: color = 'red'
            elif idx == st.session_state.inner_road_idx: color = 'orange'
            
            folium.PolyLine(locations=c_wgs, color=color, weight=6, tooltip=f"Krawędź nr: {idx}").add_to(m)

        # Rysowanie zaprojektowanych działek (Przybliżona konwersja do WGS dla wizu)
        if st.session_state.sub_parcels:
            for i, p in enumerate(st.session_state.sub_parcels):
                c_wgs = [trans.transform(x, y)[::-1] for x, y in p.exterior.coords]
                folium.Polygon(locations=c_wgs, color='blue', fill=True, fill_opacity=0.3, tooltip=f"Dz. {i+1} ({p.area:.0f}m2)").add_to(m)
        if st.session_state.road_polygon:
            c_wgs = [trans.transform(x, y)[::-1] for x, y in st.session_state.road_polygon.exterior.coords]
            folium.Polygon(locations=c_wgs, color='yellow', fill=True, fill_opacity=0.6).add_to(m)

    st_data = st_folium(m, width=900, height=700)
    
    # Obsługa kliknięć "z mapy" (Folium zwraca nam tekst z Tooltipa klikniętego obiektu!)
    if st_data['last_object_clicked_tooltip']:
        try:
            clicked_idx = int(st_data['last_object_clicked_tooltip'].replace("Krawędź nr: ", ""))
            if st.session_state.picking_mode == 'public':
                st.session_state.public_road_idx = clicked_idx
                st.session_state.picking_mode = 'Oczekiwanie'
                st.rerun()
            elif st.session_state.picking_mode == 'inner':
                st.session_state.inner_road_idx = clicked_idx
                st.session_state.picking_mode = 'Oczekiwanie'
                st.rerun()
        except: pass