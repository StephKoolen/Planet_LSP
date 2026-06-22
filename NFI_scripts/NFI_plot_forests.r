# plot the various different definitions of forest to see how they differ 
# what happens to the number of forests above 50 and 100ha if we add in a bufferof 5 meter 

# Load the data - initally at 50ha
nfi50 <- read_csv("data/NFI/NFI_2024_50ha.csv")
nfi50_spatial <- st_read("data/NFI/NFI_2024_50ha.shp")


# Create data at 100ha by filtering the 50ha data
nfi100_spatial <- nfi50_spatial %>%
  filter(Area_Ha > 100) 

nfi100 <- nfi50 %>%
    filter(Area_Ha > 100)


# create folder to save the visualisations
if(!dir.exists(here("NFI_50ha","visualisations"))){
  dir.create(here("NFI_50ha","visualisations"))
}

# Basic summary of the data
summary(nfi50)
n_forests_50 <- nrow(nfi50)
n_forests_100 <- nrow(nfi100)

# ---- UK broadleaved forest map ----
# plot forest shapefiles, to visualise the spatial distribution of broadleaved forests across the UK. Colour the forests based on size (area in ha) to see if there are any spatial patterns in the distribution of larger vs smaller forests across the UK. Start with the map for forests >50ha.

# create new colour bins for the map. 50,75,100,150,200,250,300,400,500,750,1000,>1000. This will help to visualise the spatial distribution of forests of different sizes across the UK.
area_bins <- c(50, 75, 100, 150, 200, 250, 300, 400, 500, 750, 1000, Inf)
area_labels <- c("50-75", "75-100", "100-150", "150-200", "200-250", "250-300", "300-400", "400-500", "500-750", "750-1000", ">1000")

## 50 ha map
# Assign the same factor levels to both spatial datasets so the discrete scale is identical
nfi50_spatial <- nfi50_spatial %>%
  mutate(Area_bin = cut(Area_Ha,
                        breaks = area_bins,
                        labels = area_labels,
                        right = FALSE,
                        include.lowest = TRUE))


# Map forest distribution across the UK (50ha)
forest_map <- ggplot(nfi50_spatial) +
  geom_sf(aes(fill = Area_bin, color = Area_bin), size = 0.2) +
  labs(
    title = paste("Spatial Distribution of Broadleaved Forests across the UK\n(Area > 50 ha, n =", n_forests_50, ")"),
    fill = "Area (ha)",
    color = "Area (ha)"
  ) +
  scale_fill_viridis_d(option = "turbo", direction = -1) +
  scale_color_viridis_d(option = "turbo", direction = -1, guide = "none") +
  guides(fill = guide_legend(override.aes = list(color = "grey40", size = 0.5))) +
  theme_minimal()

save_plot(forest_map, filename = here("NFI_50ha","visualisations","forest_spatial_distribution.png"), base_width = 6, base_height = 8)

## 100 ha map
# assign the same factor levels to both spatial datasets so the discrete scale is identical
nfi100_spatial <- nfi100_spatial %>%
    mutate(Area_bin = cut(Area_Ha,
                            breaks = area_bins,
                            labels = area_labels,
                            right = FALSE,
                            include.lowest = TRUE)) %>%
    mutate(Area_bin = factor(Area_bin, levels = area_labels))

# create a similar map but for forests >100ha to see if there are any differences in the spatial distribution of larger forests across the UK
forest_map_100 <- ggplot(nfi100_spatial) +
  geom_sf(aes(fill = Area_bin, color = Area_bin), size = 0.2) +
  labs(title = paste("Spatial Distribution of Broadleaved Forests across the UK\n(Area > 100 ha, n =", n_forests_100, ")"),
    fill = "Area (ha)",
    color = "Area (ha)"
  ) +
  scale_fill_viridis_d(option = "turbo", direction = -1) +
  scale_color_viridis_d(option = "turbo", direction = -1, guide = "none") +
  guides(fill = guide_legend(override.aes = list(color = "grey40", size = 0.5))) +
  theme_minimal()

save_plot(forest_map_100, filename = here("NFI_50ha","visualisations","forest_spatial_distribution_100ha.png"), base_width = 8, base_height = 10)