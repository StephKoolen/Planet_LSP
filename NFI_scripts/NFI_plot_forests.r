# plot the various different definitions of forest to see how they differ 
# what happens to the number of forests above 50 and 100ha if we add in a bufferof 5 meter 

# load libraries
library(sf)
library(tidyverse)
library(ggplot2)
library(cowplot)

# Load the data - initally at 50ha
nfi50 <- read_csv("data/NFI/NFI_2024_50ha.csv")

nfi_spatial <- st_read("data/NFI/NFI_GB_IFT_Data_20250826.shp")
nfi_joined_spatial <- st_read("data/NFI/joined_buffer_removed.gpkg")


# Create data at 50ha by filtering the datastes
nfi50_spatial <- nfi_spatial %>%
  filter(Area_Ha > 50) 

nfi50_joined <- nfi_joined_spatial %>%
    filter(area_ha > 50)

# create data at 100ha by filtering the dataset
nfi100_spatial <- nfi_spatial %>%
  filter(Area_Ha > 100) 

nfi100_joined <- nfi_joined_spatial %>%
    filter(area_ha > 100)

# Basic summary of the data
summary(nfi50_spatial)
summary(nfi50_joined)


# counts for titles
n_forests_50_spatial <- nrow(nfi50_spatial)
n_forests_50_joined <- nrow(nfi50_joined)
n_forests_100_spatial <- nrow(nfi100_spatial)
n_forests_100_joined <- nrow(nfi100_joined)

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
                        include.lowest = TRUE)) %>%
  mutate(Area_bin = factor(Area_bin, levels = area_labels))


# Map forest distribution across the UK (50ha)
forest_map <- ggplot(nfi50_spatial) +
  geom_sf(aes(fill = Area_bin, color = Area_bin), size = 0.2) +
  labs(
    title = paste("Spatial Distribution of Broadleaved Forests across the UK\n(Area > 50 ha, n =", n_forests_50_spatial, ")"),
    fill = "Area (ha)",
    color = "Area (ha)"
  ) +
  scale_fill_viridis_d(option = "turbo", direction = -1, limits = area_labels) +
  scale_color_viridis_d(option = "turbo", direction = -1, limits = area_labels, guide = "none") +
  guides(fill = guide_legend(override.aes = list(color = "grey40", size = 0.5))) +
  theme_minimal()

save_plot(forest_map, filename = ("figures/maps/forest_50ha.png"), base_width = 6, base_height = 8)

# ---- Joined 50 ha map ----
# ensure joined dataset has the same factor levels
nfi50_joined <- nfi50_joined %>%
  mutate(Area_bin = cut(area_ha,
                        breaks = area_bins,
                        labels = area_labels,
                        right = FALSE,
                        include.lowest = TRUE)) %>%
  mutate(Area_bin = factor(Area_bin, levels = area_labels))

forest_map_50_joined <- ggplot(nfi50_joined) +
  geom_sf(aes(fill = Area_bin, color = Area_bin), size = 0.2) +
  labs(title = paste("Spatial Distribution (joined) of Broadleaved Forests across the UK\n(Area > 50 ha, n =", n_forests_50_joined, ")"),
       fill = "Area (ha)", color = "Area (ha)") +
  scale_fill_viridis_d(option = "turbo", direction = -1, limits = area_labels) +
  scale_color_viridis_d(option = "turbo", direction = -1, limits = area_labels, guide = "none") +
  guides(fill = guide_legend(override.aes = list(color = "grey40", size = 0.5))) +
  theme_minimal()

save_plot(forest_map_50_joined, filename = ("figures/maps/forest_joined_50ha.png"), base_width = 6, base_height = 8)

# ---- Counts per Area_bin for >50 ha (original and joined) ----
# compute counts, ensuring all bins are present
counts_50_spatial <- nfi50_spatial %>%
  st_drop_geometry() %>%
  count(Area_bin) %>%
  complete(Area_bin = area_labels, fill = list(n = 0)) %>%
  mutate(Area_bin = factor(Area_bin, levels = area_labels))

counts_50_joined <- nfi50_joined %>%
  st_drop_geometry() %>%
  count(Area_bin) %>%
  complete(Area_bin = area_labels, fill = list(n = 0)) %>%
  mutate(Area_bin = factor(Area_bin, levels = area_labels))

# determine y-axis max to leave space for labels
max_count <- max(c(counts_50_spatial$n, counts_50_joined$n), na.rm = TRUE)
ylimit <- max_count * 1.15

# bar plot for original >50ha with labels above bars
bar_50_spatial <- ggplot(counts_50_spatial, aes(x = Area_bin, y = n, fill = Area_bin)) +
  geom_col(color = "grey30") +
  geom_text(aes(label = n), vjust = -0.5, size = 3) +
  scale_fill_viridis_d(option = "turbo", direction = -1, limits = area_labels) +
  labs(title = paste("Number of forests per size bin — Original (Area > 50 ha, n =", n_forests_50_spatial, ")"),
       x = "Area bin (ha)", y = "Number of forests") +
  theme_minimal() +
  theme(axis.text.x = element_text(angle = 45, hjust = 1)) +
  expand_limits(y = ylimit)

save_plot(bar_50_spatial, filename = ("figures/maps/counts_50_original.png"), base_width = 8, base_height = 4)

# bar plot for joined >50ha with labels above bars
bar_50_joined <- ggplot(counts_50_joined, aes(x = Area_bin, y = n, fill = Area_bin)) +
  geom_col(color = "grey30") +
  geom_text(aes(label = n), vjust = -0.5, size = 3) +
  scale_fill_viridis_d(option = "turbo", direction = -1, limits = area_labels) +
  labs(title = paste("Number of forests per size bin — Joined (Area > 50 ha, n =", n_forests_50_joined, ")"),
       x = "Area bin (ha)", y = "Number of forests") +
  theme_minimal() +
  theme(axis.text.x = element_text(angle = 45, hjust = 1)) +
  expand_limits(y = ylimit)

save_plot(bar_50_joined, filename = ("figures/maps/counts_50_joined.png"), base_width = 8, base_height = 4)

# ---- Change per Area_bin (joined - original) for >50 ha ----
counts_50_change <- counts_50_joined %>%
  rename(n_joined = n) %>%
  left_join(counts_50_spatial %>% rename(n_spatial = n), by = "Area_bin") %>%
  replace_na(list(n_joined = 0, n_spatial = 0)) %>%
  mutate(delta = n_joined - n_spatial,
         percent = ifelse(n_spatial == 0, NA_real_, (delta / n_spatial) * 100),
         Area_bin = factor(Area_bin, levels = area_labels),
         label_text = ifelse(n_spatial == 0 & n_joined > 0,
                             paste0("+", delta, " (new)"),
                             ifelse(delta >= 0, paste0("+", delta), as.character(delta))),
         vjust = ifelse(is.na(percent), -0.5, ifelse(percent >= 0, -0.5, 1.2)))

# determine percent y limits (allow space for labels)
max_percent <- max(abs(counts_50_change$percent), na.rm = TRUE)
y_limit_percent <- ifelse(is.finite(max_percent), max_percent * 1.3, 10)

## Absolute change plot (joined - original) — keeps signed bars
abs_change_plot_50 <- ggplot(counts_50_change, aes(x = Area_bin, y = delta, fill = Area_bin)) +
  geom_col(color = "grey30") +
  geom_text(aes(label = label_text, vjust = ifelse(delta >= 0, -0.5, 1.2)), size = 3) +
  scale_fill_viridis_d(option = "turbo", direction = -1, limits = area_labels) +
  labs(title = paste("Absolute change in number of forests per size bin (Joined - Original) — Area > 50 ha"),
       x = "Area bin (ha)", y = "Change in number of forests") +
  theme_minimal() +
  theme(axis.text.x = element_text(angle = 45, hjust = 1))

save_plot(abs_change_plot_50, filename = ("figures/maps/counts_50_delta.png"), base_width = 8, base_height = 4)

## Percent change plot (non-negative y-axis) — show percent increases only; labels show signed counts
counts_50_change <- counts_50_change %>%
  mutate(percent_pos = ifelse(is.na(percent), 0, pmax(percent, 0)))

max_percent_pos <- max(counts_50_change$percent_pos, na.rm = TRUE)
y_limit_percent_pos <- ifelse(is.finite(max_percent_pos) && max_percent_pos > 0, max_percent_pos * 1.3, 10)

change_plot_50 <- ggplot(counts_50_change, aes(x = Area_bin, y = percent_pos, fill = Area_bin)) +
  geom_col(color = "grey30") +
  geom_text(aes(label = label_text), vjust = -0.5, size = 3) +
  scale_fill_viridis_d(option = "turbo", direction = -1, limits = area_labels) +
  labs(title = paste("Percent increase in number of forests per size bin (Joined vs Original) — Area > 50 ha"),
       x = "Area bin (ha)", y = "Percent change (%)") +
  theme_minimal() +
  theme(axis.text.x = element_text(angle = 45, hjust = 1)) +
  expand_limits(y = c(0, y_limit_percent_pos))

save_plot(change_plot_50, filename = ("figures/maps/counts_50_change.png"), base_width = 8, base_height = 4)
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
  labs(title = paste("Spatial Distribution of Broadleaved Forests across the UK\n(Area > 100 ha, n =", n_forests_100_spatial, ")"),
    fill = "Area (ha)",
    color = "Area (ha)"
  ) +
  scale_fill_viridis_d(option = "turbo", direction = -1, limits = area_labels) +
  scale_color_viridis_d(option = "turbo", direction = -1, limits = area_labels, guide = "none") +
  guides(fill = guide_legend(override.aes = list(color = "grey40", size = 0.5))) +
  theme_minimal()

save_plot(forest_map_100, filename = ("figures/maps/forest_100ha.png"),  base_width = 6, base_height = 8)

# ---- Joined 100 ha map ----
nfi100_joined <- nfi100_joined %>%
  mutate(Area_bin = cut(area_ha,
                        breaks = area_bins,
                        labels = area_labels,
                        right = FALSE,
                        include.lowest = TRUE)) %>%
  mutate(Area_bin = factor(Area_bin, levels = area_labels))

forest_map_100_joined <- ggplot(nfi100_joined) +
  geom_sf(aes(fill = Area_bin, color = Area_bin), size = 0.2) +
  labs(title = paste("Spatial Distribution (joined) of Broadleaved Forests across the UK\n(Area > 100 ha, n =", n_forests_100_joined, ")"),
       fill = "Area (ha)", color = "Area (ha)") +
  scale_fill_viridis_d(option = "turbo", direction = -1, limits = area_labels) +
  scale_color_viridis_d(option = "turbo", direction = -1, limits = area_labels, guide = "none") +
  guides(fill = guide_legend(override.aes = list(color = "grey40", size = 0.5))) +
  theme_minimal()

save_plot(forest_map_100_joined, filename = ("figures/maps/forest_joined_100ha.png"),  base_width = 6, base_height = 8)

# ---- Overlay maps to show added areas (joined vs spatial) ----
# 50 ha overlay: plot joined first, then spatial on top
extra_50 <- n_forests_50_joined - n_forests_50_spatial
pct_increase_50 <- round((extra_50 / n_forests_50_spatial) * 100, 1)
overlay_50 <- ggplot() +
  geom_sf(data = nfi50_joined,  fill = "#dd0a0a", color = "#dd0a0a") +
  geom_sf(data = nfi50_spatial, fill = "#103d17", color = "#103d17") +
  labs(title = paste("Joined (red, n =",n_forests_50_joined,") vs Original (green, n =",n_forests_50_spatial,") >50 ha\n(+", extra_50, " forests, +", pct_increase_50, "%)")) +
  theme_minimal()

save_plot(overlay_50, filename = ("figures/maps/overlay_forest_50ha.png"), base_width = 6, base_height = 8)

# 100 ha overlay
extra_100 <- n_forests_100_joined - n_forests_100_spatial
pct_increase_100 <- round((extra_100 / n_forests_100_spatial) * 100, 1)
overlay_100 <- ggplot() +
  geom_sf(data = nfi100_joined, fill = "#dd0a0a", color = "#dd0a0a") +
  geom_sf(data = nfi100_spatial, fill = "#103d17", color = "#103d17") +
  labs(title = paste("Joined (red, n =",n_forests_100_joined, ") vs Original (green, n =",n_forests_100_spatial, ") >100 ha\n(+",extra_100, " forests, +", pct_increase_100, "%)")) +
  theme_minimal()

save_plot(overlay_100, filename =("figures/maps/overlay_forest_100ha.png"),  base_width = 6, base_height = 8)

# ---- Difference maps: explicit new areas in joined data ----
try({
  # 50 ha: union and difference
  joined_union_50 <- st_union(st_make_valid(nfi50_joined))
  spatial_union_50 <- st_union(st_make_valid(nfi50_spatial))
  new_areas_50 <- st_difference(joined_union_50, spatial_union_50)
  if(!st_is_empty(new_areas_50)){
    new_areas_50_sf <- st_sf(geometry = new_areas_50)
    diff_map_50 <- ggplot() +
      geom_sf(data = new_areas_50_sf, fill = "#e41a1c", color = NA, alpha = 0.8) +
      labs(title = "Areas present in joined data but not in original spatial (>50 ha)") +
      theme_minimal()
    save_plot(diff_map_50, filename = ("figures/maps/new_forests_50ha.png"), base_width = 6, base_height = 8)
  }

  # 100 ha: union and difference
  joined_union_100 <- st_union(st_make_valid(nfi100_joined))
  spatial_union_100 <- st_union(st_make_valid(nfi100_spatial))
  new_areas_100 <- st_difference(joined_union_100, spatial_union_100)
  if(!st_is_empty(new_areas_100)){
    new_areas_100_sf <- st_sf(geometry = new_areas_100)
    diff_map_100 <- ggplot() +
      geom_sf(data = new_areas_100_sf, fill = "#e41a1c", color = NA, alpha = 0.8) +
      labs(title = "Areas present in joined data but not in original spatial (>100 ha)") +
      theme_minimal()
    save_plot(diff_map_100, filename = ("figures/maps/new_forests_100ha.png"), base_width = 6, base_height = 8)
  }
}, silent = FALSE)