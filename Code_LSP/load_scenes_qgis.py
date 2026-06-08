from pathlib import Path

from qgis.core import (
    QgsProject,
    QgsRasterLayer,
    QgsMultiBandColorRenderer,
    QgsContrastEnhancement,
    QgsRasterBandStats,
)

# Change this to your folder
folder = r"C:\Users\reub0539\work\Planet_LSP\data\wytham_test\wytham"

for tif_path in Path(folder).rglob("a*.tif"):
    layer = QgsRasterLayer(str(tif_path), tif_path.stem)

    if not layer.isValid():
        print(f"Invalid raster: {tif_path}")
        continue

    provider = layer.dataProvider()

    # True colour: R=3, G=2, B=1
    renderer = QgsMultiBandColorRenderer(provider, 3, 2, 1)

    # Stretch each band for display
    for band, setter in [
        (3, renderer.setRedContrastEnhancement),
        (2, renderer.setGreenContrastEnhancement),
        (1, renderer.setBlueContrastEnhancement),
    ]:
        stats = provider.bandStatistics(band, QgsRasterBandStats.All, layer.extent(), 0)
        ce = QgsContrastEnhancement(provider.dataType(band))
        ce.setContrastEnhancementAlgorithm(QgsContrastEnhancement.StretchToMinimumMaximum)
        ce.setMinimumValue(stats.minimumValue)
        ce.setMaximumValue(stats.maximumValue)
        setter(ce)

    layer.setRenderer(renderer)
    layer.triggerRepaint()
    QgsProject.instance().addMapLayer(layer)

    print(f"Loaded: {tif_path.name}")

print("Finished.")