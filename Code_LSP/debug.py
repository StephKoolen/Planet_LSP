import geopandas as gpd
from shapely.geometry import mapping

gdf = gpd.read_file(r"C:/Users/reub0539/OneDrive - Nexus365/Dphil/Projects/Project1/old_python/FullWytham.shp").to_crs(epsg=4326)
row = gdf.iloc[0]
coords = mapping(row.geometry)["coordinates"]

print("Geometry type:", row.geometry.geom_type)
print("Coord nesting depth:", len(coords))
print("First coordinate pair:", coords[0][0])  # should be [lon, lat]
print("Lon range (should be -180 to 180):", coords[0][0][0])
print("Lat range (should be -90 to 90):", coords[0][0][1])